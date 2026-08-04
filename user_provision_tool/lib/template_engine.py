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


class _PathPlaceholderUndefined(Undefined):
    """Renders as a valid absolute path placeholder so YAML stays parseable."""
    def __str__(self) -> str:
        return "/tmp/__placeholder__"

    def __getitem__(self, key: object) -> "_PathPlaceholderUndefined":
        return _PathPlaceholderUndefined()

    def __iter__(self):
        return iter([])


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


def render_compose(
    template_path: str,
    output_path: str,
    user_name: str,
    service_name: str,
    label: str,
    volumes: dict[str, str],
    env_file: str | None = None,
) -> str | None:
    """Render a docker-compose template and write the output file.

    If *env_file* is given, it is copied next to the generated compose file
    with a per-user unique name (``.env.{user_name}.{label}``) so that
    multiple users in the same project directory don't collide.  The copied
    file path is returned so ``docker compose --env-file`` can reference it.

    Additionally, any ``env_file: .env`` directives in service definitions
    (both string and list forms) are replaced with the per-user env file name,
    so containers load environment variables from the correct file.

    Two distinct placeholder types are handled by different engines:
      - ``{{ var }}``   — Jinja2; resolved here at render time.
      - ``${ENV_VAR}``  — Docker Compose variable substitution; left as-is
                          in the rendered YAML and resolved by docker at
                          ``compose up`` time via the env_file.
    """
    env, tpl_name = _make_env(template_path)
    prefix = container_prefix(service_name, user_name, label)
    ctx: dict[str, Any] = {
        "user_name": user_name,
        "service_name": service_name,
        "label": label,
        "container_prefix": prefix,
        "network_name": user_network_name(service_name, user_name, label),
        "volumes": volumes,
    }
    rendered = env.get_template(tpl_name).render(**ctx)

    # --- Handle env_file: copy with per-user name + rewrite .env refs ---
    copied_env: str | None = None
    if env_file and Path(env_file).is_file():
        # Per-user unique env file name to avoid collisions between users
        # sharing the same project directory.
        per_user_env_name = f".env.{user_name}.{label}"
        dest = Path(output_path).parent / per_user_env_name
        if Path(env_file).resolve() != dest.resolve():
            shutil.copy2(env_file, dest)
        copied_env = str(dest)

        # Replace env_file: .env references in the rendered compose so
        # containers load env vars from the correct per-user file.
        # Walk the parsed dict to replace .env before dumping — avoids
        # indentation-dependent regex matching on flattened YAML text.
        rendered = _rewrite_env_file_in_dict(rendered, per_user_env_name)

    with open(output_path, "w") as f:
        f.write(rendered)
    # Create .generated marker so the gateway UI shows it under "Generated Files"
    Path(str(output_path) + ".generated").write_text("")
    if copied_env:
        Path(copied_env + ".generated").write_text("")

    return copied_env


def _rewrite_env_file_in_dict(yaml_text: str, per_user_env_name: str) -> str:
    """Replace ``.env`` references in ``env_file:`` directives with *per_user_env_name*.

    Parses the YAML text into a dict, walks the service definitions to find
    and replace ``env_file`` values pointing to ``.env``, then re-serialises.
    This is immune to indentation variations that break regex-based approaches.

    Handles both forms:
      - String:  ``env_file: .env``
      - List:    ``env_file:\\n  - .env``
    """
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # Fall back to the legacy regex approach if YAML is unparseable
        return _rewrite_env_file_refs_legacy(yaml_text, per_user_env_name)

    if not isinstance(data, dict):
        return yaml_text

    services = data.get("services")
    if isinstance(services, dict):
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            env_val = svc.get("env_file")
            if env_val == ".env":
                svc["env_file"] = per_user_env_name
            elif isinstance(env_val, list):
                svc["env_file"] = [
                    per_user_env_name if v == ".env" else v for v in env_val
                ]

    return yaml.dump(
        data,
        Dumper=IndentedDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        indent=2,
    )


def _rewrite_env_file_refs_legacy(yaml_text: str, per_user_env_name: str) -> str:
    """Legacy regex-based fallback for unparseable YAML.

    Handles both forms:
      - String:  ``env_file: .env``
      - List:    ``env_file:\\n  - .env``
    """
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
                if m2.group(2) == ".env":
                    line = f"{m2.group(1)}- {per_user_env_name}"
                result.append(line)
                continue
            else:
                in_env_file = False

        # Handle string form: env_file: .env  (on a single line)
        line = re.sub(
            r"^(\s*env_file:\s+)\.env(\s*)$",
            rf"\1{per_user_env_name}\2",
            line,
        )
        result.append(line)

    return "\n".join(result)


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
    """Render a nginx conf template and write the output file.

    When *https* is True, the template can reference:
      - ``{{ https }}``                  — boolean True
      - ``{{ ssl_certificate_path }}``   — absolute path to fullchain.pem
      - ``{{ ssl_certificate_key_path }}`` — absolute path to privkey.pem
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
        "hostname": f"{service_name}-{user_name}-{label}.{domain_name}",
        "htpasswd_path": htpasswd_path,
        "https": https,
        "ssl_certificate_path": ssl_certificate_path,
        "ssl_certificate_key_path": ssl_certificate_key_path,
    }
    rendered = env.get_template(tpl_name).render(**ctx)
    if not htpasswd_path:
        # Strip auth_basic directives — no password was set for this user
        rendered = re.sub(r'[ \t]*auth_basic[^\n]*\n', '', rendered)

    # ------------------------------------------------------------------
    # Rewrite static proxy_pass → variable-based for per-request DNS
    # resolution.  Without this, nginx resolves the upstream hostname
    # ONCE at startup and caches the IP forever.  If the container
    # restarts or is missing at reload time, nginx hangs.  With
    # variables, resolution is deferred to request time and nginx
    # starts/reloads cleanly regardless of upstream state.
    #
    #  Before:  proxy_pass http://myapp-user_alice-0-web:80;
    #  After:   set $upstream_abc12 myapp-user_alice-0-web:80;
    #           proxy_pass http://$upstream_abc12;
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
    # Create .generated marker so the gateway UI shows it under "Generated Files"
    Path(str(output_path) + ".generated").write_text("")
