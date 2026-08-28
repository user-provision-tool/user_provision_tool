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
        "subnet": subnet or "",
        "gateway": gateway or "",
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
    # Mark rendered compose as generated
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
        "hostname": f"{service_name}-{user_name}-{label}.{domain_name}",
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


def write_env_d(
    generated_dir: str,
    enable_acl: bool,
    portal_scheme: str = "http",
    dashboard_host: str = "localhost:8775",
) -> Path:
    """DEPRECATED (v5, decision 1/6) — ``env.d`` disappears from the internal side.

    v5 moves the ACL gate entirely to the edge ``-nginx-acl`` and the internal
    per-service confs are the simple ACL-free form (§5). ``ENABLE_ACL`` touches
    only the gateway and the edge. This writer is retained only so existing
    callers (older ``-api`` versions) fail gracefully; the current ``-api`` no
    longer calls it. Do not use in new code.

    Returns the path of the written ``mode.env`` (v4 behaviour, unchanged).
    """
    env_dir = Path(generated_dir) / "env.d"
    env_dir.mkdir(parents=True, exist_ok=True)
    mode = "acl" if enable_acl else "basic"
    mode_file = env_dir / "mode.env"
    mode_file.write_text(
        f"set $auth_mode {mode};\n"
        f"set $portal_scheme {portal_scheme};\n"
        f"set $dashboard_host {dashboard_host};\n"
    )
    return mode_file


_PORTAL_SERVER_HEADER = """\
    # Deferred DNS (B12): the gateway/dashboard may not be up when nginx
    # starts (minimal, gateway-less deployment). Variables + resolver defer
    # hostname resolution to request time so nginx always starts cleanly.
    resolver 127.0.0.11 valid=30s ipv6=off;
    set $portal_api subnet-acl-gateway:8770;
    set $portal_dash subnet-acl-dashboard:80;
"""


def _portal_locations() -> str:
    """DEPRECATED (v5, decision 5/15) — the portal moves to the edge ``-nginx-acl``."""
    return (
        "    # Internal-only endpoints must not be reachable via the portal (GAP-31).\n"
        "    location = /api/auth/verify { return 404; }\n"
        "    location = /api/auth/exchange { return 404; }\n"
        "\n"
        "    # API routes → provision-gateway\n"
        "    location /api/ {\n"
        "        proxy_pass http://$portal_api;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
        "\n"
        "    # Service redirect (GET /go/{hostname}) → provision-gateway /api/auth/go/{hostname}\n"
        "    location /go/ {\n"
        "        rewrite ^/go/(.*) /api/auth/go/$1 break;\n"
        "        proxy_pass http://$portal_api;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
        "\n"
        "    # Login page / POST → provision-gateway\n"
        "    location /login {\n"
        "        proxy_pass http://$portal_api;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
        "\n"
        "    # Alert pages (token_expired, acl_denied) → provision-dashboard SPA\n"
        "    location /alert {\n"
        "        proxy_pass http://$portal_dash;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
        "\n"
        "    # Dashboard SPA root → provision-dashboard (lowest priority catch-all)\n"
        "    location / {\n"
        "        proxy_pass http://$portal_dash;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
    )


_PORTAL_HTTP = (
    "# Portal management block (PORTAL_MODE=http) — v4 §5.2, §10.3 F6.\n"
    "server {\n"
    "    listen 80;\n"
    "    server_name subnet-acl-gateway.*;\n"
    + _PORTAL_SERVER_HEADER
    + _portal_locations()
    + "}\n"
)


def write_portal_d(
    generated_dir: str,
    portal_mode: str = "http",
    portal_tls_dir: str = "/etc/letsencrypt/live",
    portal_cert_name: str = "subnet-acl-gateway",
) -> Path:
    """DEPRECATED on the internal (v5, decision 5/15) — the portal moves to the edge.

    v5 serves the portal host from the edge ``-nginx-acl`` (§4.3 portal blocks);
    the internal ``-nginx`` is services-only (§5) and no longer includes
    ``portal.d``. This writer is retained for backward compatibility; the
    current ``-api`` no longer calls it. Do not use in new code.

    - ``http``  → single :80 management block (v4 behavior).
    - ``https`` → :443 ssl portal-cert block + a :80 ``301`` HTTPS redirect.

    Returns the path of the written ``portal.conf``.
    """
    portal_dir = Path(generated_dir) / "portal.d"
    portal_dir.mkdir(parents=True, exist_ok=True)
    portal_file = portal_dir / "portal.conf"
    if portal_mode.lower() != "https":
        portal_file.write_text(_PORTAL_HTTP)
        return portal_file

    cert_name = portal_cert_name or "subnet-acl-gateway"
    fullchain = f"{portal_tls_dir}/{cert_name}/fullchain.pem"
    privkey = f"{portal_tls_dir}/{cert_name}/privkey.pem"
    https = (
        "# Portal management block (PORTAL_MODE=https) — v4 §5.2, §10.3 F6.\n"
        "server {\n"
        "    listen 80;\n"
        "    server_name subnet-acl-gateway.*;\n"
        "    return 301 https://$host$request_uri;\n"
        "}\n"
        "\n"
        "server {\n"
        "    listen 443 ssl;\n"
        "    server_name subnet-acl-gateway.*;\n"
        f"    ssl_certificate {fullchain};\n"
        f"    ssl_certificate_key {privkey};\n"
        + _PORTAL_SERVER_HEADER
        + _portal_locations()
        + "}\n"
    )
    portal_file.write_text(https)
    return portal_file
