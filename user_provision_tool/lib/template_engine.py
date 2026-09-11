"""Jinja2-based template rendering for compose and nginx files.

Template variables available in both compose and nginx templates:
  {{ user_name }}
  {{ service_name }}
  {{ label }}
  {{ domain_name }}
  {{ container_prefix }}   ->  {service_name}-user_{user_name}-{label}-
  {{ htpasswd_path }}      ->  absolute path to the generated .htpasswd file (nginx only)
  {{ volumes }}            ->  dict of host_path -> container_path mappings

For compose templates, each service name and container_name referencing another service
should use the container_prefix so inter-service communication works by generated name.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined

from .yaml_utils import IndentedDumper


def _make_env(template_path: str) -> tuple[Environment, str]:
    tpl = Path(template_path).resolve()
    env = Environment(
        loader=FileSystemLoader(str(tpl.parent)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    return env, tpl.name


def container_prefix(service_name: str, user_name: str, label: str) -> str:
    return f"{service_name}-user_{user_name}-{label}-"


def user_network_name(service_name: str, user_name: str, label: str) -> str:
    """Return the isolated Docker network name for a user service instance."""
    return f"{service_name}-user_{user_name}-{label}"


def service_hostname(service_name: str, user_name: str, label: str, domain: str) -> str:
    """The nginx ``server_name`` / externally-served hostname for one instance.

    Single source of truth for the service URL host — used both at deploy time
    (nginx conf) and by the pre-deploy url-base endpoint, so the pre-deploy and
    deploy URLs are consistent by construction (design: serving-URL contract).
    """
    return f"{service_name}-{user_name}-{label}.{domain}"


class _PathPlaceholderUndefined(Undefined):
    """Renders as a valid absolute path placeholder so YAML stays parseable."""
    def __str__(self) -> str:
        return "/tmp/__placeholder__"

    def __getitem__(self, key: object) -> "_PathPlaceholderUndefined":
        return _PathPlaceholderUndefined()

    def __iter__(self):
        return iter([])


_BIND_COMMENT_RE = re.compile(
    r"#\s+(volumes|env_files)\['([^']+)'\]\s*←\s*original path:\s*(.+?)\s*$",
    flags=re.MULTILINE,
)


def extract_template_bind_sources(compose_template_path: str, kind: str) -> dict[str, str]:
    """Return ``{key: declared_src}`` parsed from the converter header comments.

    The converter writes one comment line per bind mount / env file:
      ``#     volumes['KEY']  ← original path: ./nginx/conf.d``
      ``#     env_files['KEY']  ← original path: ./config/app.env``

    These are the declared source paths (relative to the template dir) that
    ``_auto_volumes`` / ``render_compose`` use for the copy-if-empty bootstrap.
    """
    with open(compose_template_path) as f:
        content = f.read()
    result: dict[str, str] = {}
    for m in _BIND_COMMENT_RE.finditer(content):
        if m.group(1) == kind:
            result[m.group(2)] = m.group(3).strip()
    return result


def extract_template_volume_sources(compose_template_path: str) -> dict[str, str]:
    """Return ``{volume_key: declared_src}`` from converter header comments."""
    return extract_template_bind_sources(compose_template_path, "volumes")


def extract_template_env_file_sources(compose_template_path: str) -> dict[str, str]:
    """Return ``{env_key: declared_src}`` from converter header comments."""
    return extract_template_bind_sources(compose_template_path, "env_files")


def extract_template_volumes(compose_template_path: str) -> list[str]:
    """Return the list of volume keys referenced in a compose template.

    Three sources are scanned and merged (in order, de-duplicated):
    1. ``{{ volumes['key'] }}`` / ``{{ volumes["key"] }}`` Jinja2 expressions
       — covers bind-mount sources substituted at render time.
    2. Top-level named ``volumes:`` block — covers named Docker volumes.
    3. Plain (non-Jinja2) bind-mount source paths in service volumes lists.
    """
    with open(compose_template_path) as f:
        content = f.read()

    # --- 1. Jinja2 dict-access patterns: {{ volumes['key'] }} or {{ volumes["key"] }} ---
    _JINJA_VOL_RE = re.compile(r'\{\{-?\s*volumes\[[\'"]([^\'"]+)[\'"]\]\s*-?\}\}')
    jinja_keys: list[str] = []
    for m in _JINJA_VOL_RE.finditer(content):
        key = m.group(1)
        if key not in jinja_keys:
            jinja_keys.append(key)

    # --- 2 & 3. Render with placeholder values and parse the YAML ---
    try:
        env = Environment(undefined=_PathPlaceholderUndefined)
        rendered = env.from_string(content).render()
    except Exception:
        rendered = content
    try:
        data = yaml.safe_load(rendered)
    except yaml.YAMLError:
        data = {}
    top_volumes = list((data or {}).get("volumes", {}).keys()) if isinstance(
        (data or {}).get("volumes"), dict
    ) else []
    # Also collect bind-mount sources from services
    service_volumes: list[str] = []
    for svc in ((data or {}).get("services", {}) or {}).values():
        for v in (svc or {}).get("volumes", []):
            if isinstance(v, str) and ":" in v:
                src = v.split(":")[0]
                if not src.startswith("/") and src not in top_volumes:
                    service_volumes.append(src)
            elif isinstance(v, dict):
                src = v.get("source", "")
                if src and not src.startswith("/") and src not in top_volumes:
                    service_volumes.append(src)

    # Merge all three sources, preserving order and de-duplicating
    seen: set[str] = set()
    result: list[str] = []
    for key in jinja_keys + top_volumes + service_volumes:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def canonical_env_file_name(index: int, user_name: str, label: str) -> str:
    """Positional canonical name for an interpolation env file (design §3).

    The INCOMING name is ignored entirely; the index is the position in the
    `env_files` list (0-based → 1-based canonical suffix). This replaces the
    old source-derived convention (which produced pathological duplicates
    like ``.env.specialsync2.specialsync2.0.0`` from already-per-user-named
    inputs).
    """
    return f".env.{index + 1}.{user_name}.{label}"


def render_compose(
    template_path: str,
    output_path: str,
    user_name: str,
    service_name: str,
    label: str,
    volumes: dict[str, str],
    env_file: str | None = None,
    env_files: dict[str, str] | None = None,
    env_files_all: list[str] | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> str | None:
    """Render a docker-compose template and write the output file.

    If *env_file* is given, it is copied next to the generated compose file
    with a per-user unique name (``.env.{user_name}.{label}``) so that
    multiple users in the same project directory don't collide.  The copied
    file path is returned so ``docker compose --env-file`` can reference it.

    If *subnet* and *gateway* are given, they are passed to the template
    as Jinja2 variables.  The per-user compose template should guard the
    IPAM block with ``{% if subnet %}`` for backward compatibility when
    subnet management is disabled.

    Service-level env files (design §Env story L180-184): declared
    ``env_file:`` paths in the template reference ``{{ env_files['KEY'] }}``
    tokens.  *env_files* maps KEY → operator-supplied host path override.
    Each declared file that EXISTS in the recipe dir is copied into the
    per-user-per-recipe area (next to the rendered compose) and the token
    resolves to the copy; missing files stay missing (the token resolves to
    the original declared path, so compose warns / required:true fails up).

    Additionally, any ``env_file: .env`` directives in service definitions
    (both string and list forms) from pre-tokenisation templates are replaced
    with the per-user env file name.

    Two distinct placeholder types are handled by different engines:
      - ``{{ var }}``   — Jinja2; resolved here at render time.
      - ``${ENV_VAR}``  — Docker Compose variable substitution; left as-is
                          in the rendered YAML and resolved by docker at
                          ``compose up`` time via the env_file.
    """
    env, tpl_name = _make_env(template_path)
    prefix = container_prefix(service_name, user_name, label)

    # --- Resolve declared service env files to per-user copies ---
    # The PROJECT interpolation env (declared as ``.env`` / ``./.env`` in a
    # service's env_file) is NOT a service file to copy from the recipe — it
    # must resolve to the canonical per-user interpolation env (design §3), the
    # copy of the FIRST passed interpolation env file (or the empty fallback).
    primary_env_name = canonical_env_file_name(0, user_name, label)
    all_env: list[str] = list(env_files_all or [])
    if env_file and env_file not in all_env:
        all_env.insert(0, env_file)
    has_interp_env = any(Path(ef).is_file() for ef in all_env)

    declared_env = extract_template_env_file_sources(template_path)
    resolved_env_files: dict[str, str] = {}
    copied_names: set[str] = set()
    for key, declared_src in declared_env.items():
        override = (env_files or {}).get(key)
        if override:
            # Operator-supplied host path override — used as-is, never seeded.
            resolved_env_files[key] = override
            continue
        # Project interpolation .env → point at the canonical per-user env.
        if Path(declared_src).name in (".env", ".env.example"):
            resolved_env_files[key] = primary_env_name if has_interp_env else declared_src
            continue
        src = Path(template_path).parent / declared_src
        if src.is_file():
            dest = Path(output_path).parent / f"{Path(declared_src).name}.{user_name}.{label}"
            if dest.name not in copied_names:
                shutil.copy2(src, dest)
                copied_names.add(dest.name)
                Path(str(dest) + ".generated").write_text("")
            resolved_env_files[key] = dest.name
        else:
            # Missing file stays missing — compose warns for required:false,
            # blocks up for required:true.
            resolved_env_files[key] = declared_src

    ctx: dict[str, Any] = {
        "user_name": user_name,
        "service_name": service_name,
        "label": label,
        "container_prefix": prefix,
        "network_name": user_network_name(service_name, user_name, label),
        "volumes": volumes,
        "env_files": resolved_env_files,
        "subnet": subnet or "",
        "gateway": gateway or "",
    }
    rendered = env.get_template(tpl_name).render(**ctx)

    # --- Handle interpolation env files: copy with per-user names + rewrite .env refs ---
    # Repeatable --env-file (design §Env story L153-155): order is preserved,
    # later files win at up time (compose semantics). The PRIMARY is the copy of
    # the FIRST interpolation env file → canonical .env.1.{user}.{label} (or the
    # empty fallback when none is passed); services' env_file refs to the
    # project ``.env``/``./.env`` are rewritten to this primary.
    copied_env: str | None = None
    for idx, ef in enumerate(all_env):
        if not Path(ef).is_file():
            continue
        # Positional canonicalization (design §3): incoming names ignored.
        dest = Path(output_path).parent / canonical_env_file_name(idx, user_name, label)
        if dest.name not in copied_names and Path(ef).resolve() != dest.resolve():
            shutil.copy2(ef, dest)
            copied_names.add(dest.name)
            Path(str(dest) + ".generated").write_text("")
        if idx == 0:
            copied_env = str(dest)

    if not copied_env:
        # Empty-pass invariant (design G17): create the empty canonical primary
        # so `--env-file` blocks compose's auto-read of the recipe .env, and the
        # .env refs point at it (uniform naming — no legacy `.env.{u}.{l}`).
        empty_primary = Path(output_path).parent / primary_env_name
        if not empty_primary.exists():
            empty_primary.write_text("")
            Path(str(empty_primary) + ".generated").write_text("")
        copied_env = str(empty_primary)

    # Runtime view of the interpolation env = the ORDERED canonical copies of
    # every passed interpolation file (decision 3: expand, later-wins == the
    # merge `--env-file` gives interpolation). N==1 collapses to the single
    # `.env.1…`.
    env_ref_targets = [
        canonical_env_file_name(idx, user_name, label)
        for idx, ef in enumerate(all_env)
        if Path(ef).is_file()
    ]
    if not env_ref_targets:
        env_ref_targets = [primary_env_name]

    if has_interp_env or not all_env:
        # Rewrite env_file refs that RESOLVE to the project `.env` (path
        # identity — `.env`, `./.env`, `././.env`, `sub/../.env` all match) to
        # the canonical list above, so containers load the same per-user env
        # compose interpolates from.
        rendered = _rewrite_env_file_in_dict(
            rendered, env_ref_targets, Path(template_path).parent
        )

    with open(output_path, "w") as f:
        f.write(rendered)
    # Mark rendered compose as generated
    Path(str(output_path) + ".generated").write_text("")

    return copied_env


def _rewrite_env_file_in_dict(
    yaml_text: str,
    env_ref_targets: list[str],
    base_dir: Path,
) -> str:
    """Rewrite env_file refs that RESOLVE to the project interpolation ``.env``.

    Matching is by PATH IDENTITY against ``base_dir/.env`` (any spelling —
    ``.env``, ``./.env``, ``././.env``, ``sub/../.env`` — resolves to the same
    file and matches). A matched ref is replaced by ``env_ref_targets``, the
    ordered canonical interpolation files (decision 3: expansion; N==1 collapses
    to the single name).

    Handles string form, list items, and ``{path, required}`` object entries.
    """
    project_env = (base_dir / ".env").resolve()

    def _is_project_env(v: Any) -> bool:
        if not isinstance(v, str) or not v:
            return False
        try:
            return (base_dir / v).resolve() == project_env
        except Exception:
            return False

    def _targets() -> list[str]:
        return env_ref_targets if len(env_ref_targets) > 1 else env_ref_targets

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # Fall back to the legacy regex approach if YAML is unparseable
        return _rewrite_env_file_refs_legacy(yaml_text, env_ref_targets, base_dir)

    if not isinstance(data, dict):
        return yaml_text

    services = data.get("services")
    if isinstance(services, dict):
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            env_val = svc.get("env_file")
            if isinstance(env_val, str):
                if _is_project_env(env_val):
                    svc["env_file"] = _targets()
            elif isinstance(env_val, list):
                new_items: list[Any] = []
                for v in env_val:
                    if isinstance(v, dict):
                        # {path, required: …} object entry
                        if _is_project_env(v.get("path")):
                            new_items.extend(_targets())
                        else:
                            new_items.append(v)
                    elif _is_project_env(v):
                        new_items.extend(_targets())
                    else:
                        new_items.append(v)
                svc["env_file"] = new_items

    return yaml.dump(
        data,
        Dumper=IndentedDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        indent=2,
    )


def _rewrite_env_file_refs_legacy(
    yaml_text: str, env_ref_targets: list[str], base_dir: Path
) -> str:
    """Legacy regex-based fallback for unparseable YAML.

    Best-effort path-identity match (resolution against ``base_dir/.env``);
    a matched ref is replaced by the canonical targets (decision 3).
    """
    targets = env_ref_targets if len(env_ref_targets) > 1 else env_ref_targets
    project_env = (base_dir / ".env").resolve()

    def _proj(v: str) -> bool:
        try:
            return bool(v) and (base_dir / v).resolve() == project_env
        except Exception:
            return False

    lines = yaml_text.split("\n")
    result: list[str] = []
    in_env_file = False
    env_file_indent = 0

    for line in lines:
        # Detect start of a list-form env_file: key (line ends with just "env_file:")
        m = re.match(r"^(\s*)env_file:\s*$", line)
        if m:
            in_env_file = True
            env_file_indent = len(m.group(1))
            result.append(line)
            continue

        if in_env_file:
            # List item under env_file:
            m2 = re.match(r"^(\s+)-\s+(.*)$", line)
            if m2 and len(m2.group(1)) >= env_file_indent:
                if _proj(m2.group(2)):
                    for t in targets:
                        result.append(f"{m2.group(1)}- {t}")
                else:
                    result.append(line)
                continue
            else:
                in_env_file = False

        # Handle string form: env_file: .env (single line)
        m3 = re.match(r"^(\s*env_file:\s+)(.*?)(\s*)$", line)
        if m3 and _proj(m3.group(2)):
            line = m3.group(1) + (targets[0] if len(targets) == 1 else ", ".join(targets)) + m3.group(3)
        result.append(line)

    return "\n".join(result)


# ---------------------------------------------------------------------------
# v5 simple per-service conf (acl-enforcement-design-v5 §5, decisions 6/8)
# ---------------------------------------------------------------------------
# The per-service conf is the simple, ACL-free form — byte-identical between
# minimal and fullset, HTTP/HTTPS (F8):
#   server_name + auth_basic + auth_basic_user_file + location / proxy_pass
# The ACL gate lives ENTIRELY at the edge (-nginx-acl). No auth_request,
# /_auth_jwt, /_set_token, /__basic__/, @auth_401/@auth_403, error_page,
# env.d include, or $auth_mode ever appear in an internal conf (v5 §5).
# ---------------------------------------------------------------------------


def strip_v4_scaffold(content: str) -> str:
    """Remove stale v2/v3/v4 ACL scaffolding from a rendered per-service conf.

    Used by the v5 migration (:func:`migrate_v5`) and startup recovery for
    confs that have no registry template to re-render from. Brace-balanced so
    ``@auth_401``/``@auth_403`` blocks with nested ``if`` are fully removed.
    The variable proxy_pass form (``set $upstream_0000 …``, decision 8) is
    preserved — only the scaffold's bare ``set $upstream <target>;`` is dropped.
    """
    # auth_request / auth_request_set / error_page lines.
    content = re.sub(r"[ \t]*auth_request[ \t]+[^\n;]*;[ \t]*\r?\n", "", content)
    content = re.sub(r"[ \t]*auth_request_set[ \t]+\$[^\n;]*;[ \t]*\r?\n", "", content)
    content = re.sub(r"[ \t]*error_page[ \t]+[^\n;]*;[ \t]*\r?\n", "", content)
    # mode-switch rewrite (rewrite phase).
    content = re.sub(
        r'[ \t]*if \(\$auth_mode != "acl"\)[ \t]*\{.*?\}[ \t]*\r?\n',
        "", content, flags=re.DOTALL,
    )
    # scaffold `set $auth_mode / $portal_scheme / $dashboard_host` lines.
    content = re.sub(
        r"[ \t]*set \$(auth_mode|portal_scheme|dashboard_host)\b[^\n;]*;[ \t]*\r?\n",
        "", content,
    )
    # the v4 scaffold's `set $upstream <target>;` (bare name `upstream`, NOT
    # the variable proxy_pass `upstream_0000` form).
    content = re.sub(r"[ \t]*set \$upstream\s+[^\n;]*;[ \t]*\r?\n", "", content)
    # env.d include + the edge-only credential-injection header.
    content = re.sub(r"[ \t]*include /etc/nginx/env\.d/\*\.env;[ \t]*\r?\n", "", content)
    content = re.sub(
        r'[ \t]*proxy_set_header Authorization "Basic \$service_basic";[ \t]*\r?\n',
        "", content,
    )
    # Brace-balanced removal of scaffold location blocks.
    for prefix in (
        "location = /_set_token",
        "location = /_auth_jwt",
        "location /__bypass__/",
        "location /__basic__/",
        "location @auth_401",
        "location @auth_403",
    ):
        content = _strip_location_blocks(content, prefix)
    return content


def _strip_location_blocks(content: str, prefix: str) -> str:
    """Remove ``location … { … }`` blocks (brace-balanced) starting with *prefix*."""
    pattern = re.compile(r"^([ \t]*)" + re.escape(prefix), re.MULTILINE)
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if pattern.match(line):
            depth = line.count("{") - line.count("}")
            i += 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count("{") - lines[i].count("}")
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def render_nginx_conf(
    template_path: str,
    output_path: str,
    user_name: str,
    service_name: str,
    label: str,
    domain_name: str,
    htpasswd_path: str,
    https: bool = False,
    ssl_certificate_path: str = "",
    ssl_certificate_key_path: str = "",
) -> None:
    """Render a nginx conf template and write the output file (v5 simple form).

    When *https* is True, the template can reference:
      - ``{{ https }}``                  — boolean True
      - ``{{ ssl_certificate_path }}``   — absolute path to fullchain.pem
      - ``{{ ssl_certificate_key_path }}`` — absolute path to privkey.pem

    The rendered conf is the v5 simple, ACL-free form (decision 6 / F8): the
    template's ``server_name + auth_basic + auth_basic_user_file + location /
    proxy_pass`` is kept verbatim at server level. When *htpasswd_path* is
    empty the ``auth_basic`` directives are dropped (open service). Stale
    v2/v3/v4 ACL scaffolding in the template is stripped (idempotent
    regeneration) and static ``proxy_pass`` is rewritten to the variable form
    (decision 8) so nginx defers upstream resolution to request time. NO ACL
    scaffolding is ever injected (the gate lives at the edge). Independent of
    ``ENABLE_ACL`` / ``PORTAL_MODE``.
    """
    env, tpl_name = _make_env(template_path)
    prefix = container_prefix(service_name, user_name, label)
    ctx: dict[str, Any] = {
        "user_name": user_name,
        "service_name": service_name,
        "label": label,
        "domain_name": domain_name,
        "container_prefix": prefix,
        "network_name": user_network_name(service_name, user_name, label),
        # Single source of truth for the served hostname (feature 3): the same
        # builder the pre-deploy /service-url-base endpoint and the registry use.
        "hostname": service_hostname(service_name, user_name, label, domain_name),
        "htpasswd_path": htpasswd_path,
        "https": https,
        "ssl_certificate_path": ssl_certificate_path,
        "ssl_certificate_key_path": ssl_certificate_key_path,
    }
    rendered = env.get_template(tpl_name).render(**ctx)

    # --- auth_basic lives at server level (v5 §5). Empty htpasswd → open. ---
    if not htpasswd_path:
        rendered = re.sub(r'[ \t]*auth_basic[ \t]+"[^"]*"[ \t]*;', "", rendered)
        rendered = re.sub(r"[ \t]*auth_basic_user_file[ \t]*;", "", rendered)
        rendered = re.sub(r"[ \t]*auth_basic_user_file[ \t]+\S+[ \t]*;", "", rendered)

    # --- Strip stale v2/v3/v4 ACL scaffolding (idempotent regeneration) ---
    rendered = strip_v4_scaffold(rendered)

    # ------------------------------------------------------------------
    # Rewrite static proxy_pass → variable-based for per-request DNS
    # resolution (decision 8).  Without this, nginx resolves the upstream
    # hostname ONCE at startup and caches the IP forever.  If the container
    # restarts or is missing at reload time, nginx hangs.  With variables,
    # resolution is deferred to request time and nginx starts/reloads
    # cleanly regardless of upstream state.
    #
    #  Before:  proxy_pass http://myapp-user_alice-0-web:80;
    #  After:   set $upstream_0000 myapp-user_alice-0-web:80;
    #           proxy_pass http://$upstream_0000;
    # ------------------------------------------------------------------
    _upstream_counter = 0

    def _rewrite_proxy_pass(m: re.Match) -> str:
        nonlocal _upstream_counter
        scheme = m.group("scheme")
        host = m.group("host")
        port = m.group("port") or ""
        target = f"{host}{port}"
        var_name = f"upstream_{_upstream_counter:04d}"
        _upstream_counter += 1
        return (
            f"{m.group('indent')}set ${var_name} {target};\n"
            f"{m.group('indent')}proxy_pass {scheme}${var_name};"
        )

    rendered = re.sub(
        r"^(?P<indent>[ \t]*)proxy_pass\s+(?P<scheme>https?://)(?P<host>[^:;\s]+)(?P<port>:\d+)?\s*;",
        _rewrite_proxy_pass,
        rendered,
        flags=re.MULTILINE,
    )

    with open(output_path, "w") as f:
        f.write(rendered)
    # Mark rendered nginx conf as generated
    Path(str(output_path) + ".generated").write_text("")


