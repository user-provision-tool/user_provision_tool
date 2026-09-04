"""lib/compose_converter.py

Convert a plain docker-compose.yml into a Jinja2 compose template (.yml.j2)
compatible with user_provision_tool.

Public API
----------
convert(data)                        — pure transform: dict → (dict, src_to_key)
compose_file_to_template(src, dst)   — file-level convenience wrapper
get_compose_service_names(path)      — extract service keys from a compose file or template
make_header(src_to_key, hint)        — produce the comment block for the .j2 file

Transformations applied
-----------------------
1. Strip top-level `name:` (Compose project name).
   The user-supplied `service_name` drives all naming via {{ container_prefix }};
   the Compose project name is irrelevant and would cause volume/network name
   collisions across users if left in.

2. Strip `ports:` from every service.
   All traffic is routed through provision-nginx — no service should ever bind
   a host port.  Binding the same port for every user would cause immediate
   conflicts on the host.

3. container_name — set/replace to {{ container_prefix }}<svc_key>.
   Docker container names are globally unique; each user needs a distinct name.

4. Bind-mount host paths → {{ volumes['key'] }}.
   Passed in at registration time so each user gets their own data directory.

5. Networks → {{ network_name }} with an isolated per-user Docker network.
   Network names are global; users must not share a network.

6. Top-level named volumes → add name: {{ container_prefix }}<vol>.
   Without an explicit name Docker prefixes with the project name, which (once
   `name:` is stripped) defaults to the compose filename stem — unique per user
   and therefore correct.  The explicit name makes it deterministic and readable.
   Volumes with `external: true` are left alone (they are shared by design).

7. Docker Compose ${ENV_VAR} substitutions are left intact for runtime
   resolution via --env-file.

8. Ship `profiles:` verbatim on every service (design §Profiles L84-88).
   Profile activation is a deployment-level choice passed via `docker compose
   --profile` at up time; services with no `profiles:` key always run, and
   `[""]` empty-string-profile services run by default (no flag). All services
   stay in the template so e.g. dify's `["", "postgresql"]` db service is
   never dropped (previously the provisioned dify had NO database).

9. Passthrough system sockets — `/var/run/docker.sock` and `/run/docker.sock`
   are NEVER converted to per-user volume variables.  They remain as literal
   host paths so containers can communicate with the host Docker daemon.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .yaml_utils import IndentedDumper


# ─── Token registry ───────────────────────────────────────────────────────────
# We transform the parsed data dict using opaque tokens, then yaml.dump it,
# then text-replace every token with the intended Jinja2 expression.
# This avoids having to fight with PyYAML's quoting rules around `{{ }}`.

class _TokenRegistry:
    def __init__(self) -> None:
        self._counter = 0
        self._map: dict[str, str] = {}

    def tok(self, jinja2_expr: str) -> str:
        """Return a unique YAML-safe ASCII token for a Jinja2 expression."""
        token = f"JINJA2TOK{self._counter:06d}END"
        self._map[token] = jinja2_expr
        self._counter += 1
        return token

    def detokenize(self, text: str) -> str:
        for token, expr in self._map.items():
            text = text.replace(token, expr)
        return text


# ─── Key generation ───────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s.strip("/\\~")).strip("_")
    return s or "vol"


def _unique_key(candidate: str, used: set[str]) -> str:
    base = _slug(candidate)
    if base not in used:
        used.add(base)
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    key = f"{base}_{n}"
    used.add(key)
    return key


# ─── Volume helpers ───────────────────────────────────────────────────────────

# Paths that must NEVER be converted to per-user template variables.
# These are host system sockets / special files that must remain
# as literal host paths in both the template and rendered compose files.
_PASSTHROUGH_PATHS: set[str] = {
    "/var/run/docker.sock",
    "/run/docker.sock",
}


def _is_passthrough(src: str) -> bool:
    """Return True if *src* is a host system path that must stay literal."""
    return src in _PASSTHROUGH_PATHS


def _is_bind_source(src: str) -> bool:
    """Return True if *src* is a host filesystem path (bind mount)."""
    return (
        src.startswith("/")
        or src.startswith("./")
        or src.startswith("../")
        or src.startswith("~/")
        or src == "."
        or src == "~"
    )


def _collect_bind_mounts(services: dict) -> list[str]:
    """Return ordered, de-duplicated bind-mount sources across all services,
    excluding well-known system paths (e.g. Docker socket) that must remain
    literal."""
    seen: list[str] = []
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for entry in svc.get("volumes", []):
            src = _volume_source(entry)
            if src and _is_bind_source(src) and not _is_passthrough(src) and src not in seen:
                seen.append(src)
    return seen


def _volume_source(entry: Any) -> str | None:
    if isinstance(entry, str):
        parts = entry.split(":")
        return parts[0] if len(parts) >= 2 else None
    if isinstance(entry, dict):
        return entry.get("source")
    return None


def _transform_volume_entry(
    entry: Any, src_to_key: dict[str, str], tokens: _TokenRegistry
) -> Any:
    if isinstance(entry, str):
        colon_idx = entry.find(":")
        if colon_idx == -1:
            return entry
        src = entry[:colon_idx]
        rest = entry[colon_idx:]
        if src in src_to_key:
            return tokens.tok(f"{{{{ volumes['{src_to_key[src]}'] }}}}") + rest
        return entry
    if isinstance(entry, dict):
        src = entry.get("source", "")
        if src in src_to_key:
            result = dict(entry)
            result["source"] = tokens.tok(f"{{{{ volumes['{src_to_key[src]}'] }}}}")
            return result
        return entry
    return entry


# ─── Service transformation ───────────────────────────────────────────────────

def _transform_service(
    svc_key: str,
    svc: dict,
    src_to_key: dict[str, str],
    tokens: _TokenRegistry,
    env_to_key: dict[str, str],
) -> dict:
    container_name_val = tokens.tok(f"{{{{ container_prefix }}}}{svc_key}")

    # Rebuild dict: container_name right after 'image:' for readability;
    # drop 'ports:' entirely (traffic flows through provision-nginx).
    # 'profiles:' is KEPT verbatim (design §Profiles) — activation is a
    # deployment choice via docker compose --profile at up time.
    result: dict = {}
    inserted = False
    for key, value in svc.items():
        if key == "container_name":
            continue  # replaced below
        if key == "ports":
            continue  # stripped — use provision-nginx for all ingress
        result[key] = value
        if key == "image" and not inserted:
            result["container_name"] = container_name_val
            inserted = True
    if not inserted:
        result = {"container_name": container_name_val, **result}

    # Replace bind-mount sources
    if "volumes" in result and isinstance(result["volumes"], list):
        result["volumes"] = [
            _transform_volume_entry(v, src_to_key, tokens) for v in result["volumes"]
        ]

    # Replace declared env_file: paths with per-user template tokens
    # (design §Env story L180-184).  Long-form {path, required} object shape
    # is preserved — only the path is substituted.
    if "env_file" in result:
        result["env_file"] = _transform_env_file_entry(result["env_file"], env_to_key, tokens)

    # Replace networks with isolated user network (skip if network_mode is set)
    if "network_mode" not in result:
        result["networks"] = [tokens.tok("{{ network_name }}")]

    return result


def _transform_env_file_entry(entry: Any, env_to_key: dict[str, str], tokens: _TokenRegistry) -> Any:
    """Rewrite one service ``env_file:`` value to a per-user template token.

    - str form:  ``env_file: ./config/app.env`` → ``env_file: {{ env_files['key'] }}``
    - long-form: ``env_file: {path: ./x.env, required: true}`` → path substituted
      only, all other keys preserved.
    """
    if isinstance(entry, str):
        if entry in env_to_key:
            return tokens.tok(f"{{{{ env_files['{env_to_key[entry]}'] }}}}")
        return entry
    if isinstance(entry, dict):
        path = entry.get("path")
        if path in env_to_key:
            result = dict(entry)
            result["path"] = tokens.tok(f"{{{{ env_files['{env_to_key[path]}'] }}}}")
            return result
        return entry
    if isinstance(entry, list):
        return [_transform_env_file_entry(e, env_to_key, tokens) for e in entry]
    return entry


# ─── YAML serialisation ───────────────────────────────────────────────────────

def _represent_none_as_empty(dumper: yaml.Dumper, _data: None) -> yaml.Node:
    """Emit None as '' so named volumes like 'db_socket:' stay clean."""
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


def _dump_yaml(data: Any) -> str:
    class _Dumper(IndentedDumper):
        pass
    _Dumper.add_representer(type(None), _represent_none_as_empty)
    return yaml.dump(
        data,
        Dumper=_Dumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        indent=2,
    )


# ─── Public API ───────────────────────────────────────────────────────────────

def _collect_env_files(services: dict) -> list[str]:
    """Return ordered, de-duplicated declared ``env_file:`` paths across services."""
    seen: list[str] = []
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for entry in _as_env_file_list(svc.get("env_file")):
            if entry and entry not in seen:
                seen.append(entry)
    return seen


def _as_env_file_list(entry: Any) -> list[str]:
    """Normalise a service ``env_file:`` value into a list of path strings.

    Handles str form, list form, and long-form ``{path, required}`` objects.
    """
    if entry is None:
        return []
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        path = entry.get("path")
        return [path] if isinstance(path, str) else []
    if isinstance(entry, list):
        result: list[str] = []
        for e in entry:
            if isinstance(e, str):
                result.append(e)
            elif isinstance(e, dict) and isinstance(e.get("path"), str):
                result.append(e["path"])
        return result
    return []


def convert(data: dict) -> tuple[dict, dict[str, str], _TokenRegistry, dict[str, str]]:
    """Pure transform of a parsed compose dict.

    Returns
    -------
    transformed : dict
        The transformed data ready for yaml.dump.
    src_to_key : dict[str, str]
        Maps original bind-mount source path → template volume key.
    tokens : _TokenRegistry
        Token registry; call tokens.detokenize(yaml_text) to get the final
        Jinja2 template body.
    env_to_key : dict[str, str]
        Maps declared service ``env_file:`` path → template env-file key.
    """
    tokens = _TokenRegistry()
    result = dict(data)

    # 1. Strip Compose project name
    result.pop("name", None)

    services: dict = result.get("services", {})

    # 2. Collect bind mounts and assign volume keys
    bind_srcs = _collect_bind_mounts(services)
    used_keys: set[str] = set()
    src_to_key: dict[str, str] = {}
    for src in bind_srcs:
        key = _unique_key(Path(src).name or src, used_keys)
        src_to_key[src] = key

    # 2b. Collect declared service env_file paths and assign keys
    env_srcs = _collect_env_files(services)
    env_used: set[str] = set()
    env_to_key: dict[str, str] = {}
    for src in env_srcs:
        key = _unique_key(Path(src).name or src, env_used)
        env_to_key[src] = key

    # 3. Transform every service — ALL services ship in the template
    #    (design §Profiles L84-88: profiles pass through verbatim; only
    #    default + activated profiles run at up time via --profile).
    result["services"] = {
        svc_key: _transform_service(svc_key, svc, src_to_key, tokens, env_to_key)
        for svc_key, svc in services.items()
        if isinstance(svc, dict)
    }

    # 4. Rewrite top-level networks to isolated per-user network
    # IPAM config with Jinja2 guards is injected as a post-processing step
    # in the template body, because it contains multi-line {% if %} blocks
    # that can't be represented as YAML values.
    net_tok = tokens.tok("{{ network_name }}")
    result["networks"] = {net_tok: {"name": net_tok}}

    # 5. Add explicit name to top-level named volumes (skip external ones)
    top_volumes = result.get("volumes")
    if isinstance(top_volumes, dict) and top_volumes:
        new_top: dict = {}
        for vol_name, vol_cfg in top_volumes.items():
            cfg = dict(vol_cfg) if isinstance(vol_cfg, dict) else {}
            if not cfg.get("external"):
                cfg["name"] = tokens.tok(f"{{{{ container_prefix }}}}{vol_name}")
            new_top[vol_name] = cfg
        result["volumes"] = new_top

    return result, src_to_key, tokens, env_to_key


def make_header(
    src_to_key: dict[str, str],
    service_name_hint: str,
    env_to_key: dict[str, str] | None = None,
) -> str:
    """Return the comment block written at the top of a generated .j2 file."""
    lines = [
        f"# {service_name_hint}.yml.j2 — generated by gen_compose_template.py",
        "#",
        "# Template variables resolved at registration time by user_provision_tool:",
        "#   {{ container_prefix }}  e.g. myapp-user_alice-0-",
        "#   {{ user_name }}",
        "#   {{ service_name }}",
        "#   {{ label }}",
        "#   {{ network_name }}",
    ]
    if src_to_key:
        lines.append("#   volumes['KEY']  — bind-mount host paths (one entry per bind-mount):")
        for src, key in src_to_key.items():
            lines.append(f"#     volumes['{key}']  ← original path: {src}")
        lines.append("#")
        lines.append(
            "# Provide these keys with  -v KEY=HOST_PATH  when calling register.py:"
        )
        lines.append(
            "#   " + "  ".join(f"-v {k}=/your/path" for k in src_to_key.values())
        )
    if env_to_key:
        lines.append("#   env_files['KEY']  — declared service env_file: paths:")
        for src, key in env_to_key.items():
            lines.append(f"#     env_files['{key}']  ← original path: {src}")
        lines.append(
            "#     Files that exist in the recipe are copied per-user-per-recipe at"
        )
        lines.append("#     render time; missing files stay missing (compose warns).")
    lines += [
        "#",
        "# NOTE: ports: has been stripped — all ingress goes through provision-nginx.",
        "# Runtime variables resolved by docker compose via --env-file:",
        "#   ${ENV_VAR}  — leave these as-is; supply values in your .env file",
        "#",
        "# If an env_file is provided at registration, any ``env_file: .env``",
        "# directive in services is automatically replaced with a per-user file",
        "# (``.env.{user}.{label}``) at render time.",
        "#",
    ]
    return "\n".join(lines) + "\n"


def compose_file_to_template(
    input_path: str,
    output_path: str,
    service_name_hint: str = "",
) -> dict[str, str]:
    """Convert a docker-compose.yml file to a Jinja2 template file.

    Parameters
    ----------
    input_path : str
        Path to the source docker-compose.yml.
    output_path : str
        Destination path for the generated .yml.j2 template.
    service_name_hint : str
        Used only in the header comment (default: stem of input_path).

    Returns
    -------
    src_to_key : dict[str, str]
        Maps original bind-mount source path → template volume key.
        Pass these key names with ``-v KEY=HOST_PATH`` at registration.

    Raises
    ------
    ValueError
        If the file cannot be parsed or has no 'services:' key.
    """
    from pathlib import Path as _Path
    import yaml as _yaml

    with open(input_path) as f:
        data = _yaml.safe_load(f)

    if not isinstance(data, dict) or "services" not in data:
        raise ValueError(
            f"'{input_path}' does not look like a docker-compose file "
            "(missing 'services:' key)"
        )

    hint = service_name_hint or re.sub(
        r"[^a-zA-Z0-9_]", "_", _Path(input_path).stem
    )

    transformed, src_to_key, tokens, env_to_key = convert(data)
    raw_yaml = _dump_yaml(transformed)
    template_body = tokens.detokenize(raw_yaml)

    # ---- Inject IPAM config under the network block ----
    # The network section looks like:
    #   networks:
    #     {{ network_name }}:
    #       name: {{ network_name }}
    # We append an {% if subnet %} ... {% endif %} block after the 'name:' line.
    ipam_block = (
        "{% if subnet %}\n"
        "    ipam:\n"
        "      config:\n"
        "        - subnet: {{ subnet }}\n"
        "          gateway: {{ gateway }}\n"
        "{% endif %}"
    )
    # Find "name: {{ network_name }}" line in the networks section and append IPAM after it
    import re as _re2
    _net_name_line = _re2.compile(
        r'^(\s+name:\s+\{\{\s*network_name\s*\}\})$',
        flags=_re2.MULTILINE,
    )
    template_body = _net_name_line.sub(
        r'\1\n' + ipam_block,
        template_body,
    )

    header = make_header(src_to_key, hint, env_to_key=env_to_key)

    _Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(header + template_body)
    # Mark as generated so gateway UI shows it under "Generated Files"
    _Path(str(output_path) + ".generated").write_text("")

    return src_to_key


def ensure_subnet_ipam_block(template_path: str) -> bool:
    """Inject the ``{% if subnet %}`` ipam block into a compose .j2 template if missing.

    Gap 8 (subnet §13 Phase 4 Option B): when ``SUBNET_POOLS`` is enabled, an
    old template that lacks the subnet block would reserve a subnet in the
    registry but render no ``ipam:`` block — Docker then auto-assigns a subnet,
    leaking the reserved allocation. Auto re-convert the template (backing up
    the original as ``.bak``) so the allocation renders into the compose.

    Returns True if the template was re-converted, False if it already had the
    subnet block (no-op) or could not be safely re-converted (no
    ``name: {{ network_name }}`` anchor line).
    """
    from pathlib import Path as _Path

    path = _Path(template_path)
    if not path.is_file():
        return False
    content = path.read_text()
    if "{% if subnet %}" in content:
        return False

    ipam_block = (
        "{% if subnet %}\n"
        "    ipam:\n"
        "      config:\n"
        "        - subnet: {{ subnet }}\n"
        "          gateway: {{ gateway }}\n"
        "{% endif %}"
    )
    _net_name_line = re.compile(
        r'^(\s+name:\s+\{\{\s*network_name\s*\}\})$',
        flags=re.MULTILINE,
    )
    new_content, nsubs = _net_name_line.subn(
        r'\1\n' + ipam_block,
        content,
    )
    if nsubs == 0:
        # No anchor line to attach the block to — leave unchanged.
        return False

    backup = _Path(str(path) + ".bak")
    if not backup.exists():
        backup.write_text(content)
    path.write_text(new_content)
    return True


def get_compose_service_names(compose_path: str) -> list[str]:
    """Return the list of service keys from a compose file or .j2 template.

    Jinja2 tokens (``{{ ... }}``) in templates are replaced with placeholder
    strings before YAML parsing so the file remains parseable.

    When YAML parsing fails (e.g. due to malformed template content), falls
    back to regex-based extraction from the ``services:`` section.
    """
    import yaml as _yaml

    with open(compose_path) as f:
        raw = f.read()

    # Neutralise Jinja2 expressions so the YAML parser doesn't choke.
    # First: remove control-flow blocks ({% if/for/endif/endfor %}) which are
    # standalone directives that aren't valid YAML in any position.
    # Then: replace {{ expr }} tokens with a plain word so any adjacent
    # text (e.g. "{{ prefix }}web") stays a valid YAML key.
    sanitised = re.sub(r'\{%-?.*?-?%\}', '', raw)
    sanitised = re.sub(r'\{\{.*?\}\}', 'j2placeholder', sanitised)

    try:
        data = _yaml.safe_load(sanitised)
    except _yaml.YAMLError:
        data = None  # trigger regex fallback below

    if isinstance(data, dict):
        services = data.get("services")
        if isinstance(services, dict) and services:
            return list(services.keys())

    # ---- regex fallback ----
    # Look for top-level ``services:`` key and extract direct child keys.
    # This handles edge cases where the template body is not valid YAML
    # (e.g. leftover IPAM content at mis-matched indentation after
    # {% %} tag stripping).
    svc_match = re.search(
        r'(?:^|\n)services:\s*\n((?:\s{2,}\S.*\n?)*)',
        sanitised,
    )
    if not svc_match:
        return []

    services_block = svc_match.group(1)
    keys: list[str] = re.findall(r'^\s{2}(\S+)', services_block, re.MULTILINE)

    # Exclude known keys that look like top-level compose keys inside the
    # services block (e.g. image:, container_name:, volumes:, networks: if
    # they happen to appear at the same indent as service names — unlikely
    # but defensive).
    known_attrs = {
        'image', 'container_name', 'volumes', 'networks',
        'ports', 'depends_on', 'environment', 'env_file',
        'restart', 'profiles', 'command', 'entrypoint',
        'expose', 'healthcheck', 'labels', 'logging',
        'deploy', 'dns', 'dns_search', 'extra_hosts',
        'cap_add', 'cap_drop', 'security_opt', 'tmpfs',
        'ulimits', 'user', 'working_dir', 'stop_signal',
        'stop_grace_period', 'init', 'privileged', 'tty',
        'stdin_open', 'read_only', 'pid', 'domainname',
        'hostname', 'ipc', 'mac_address', 'mem_limit',
        'memswap_limit', 'oom_score_adj', 'cgroup_parent',
        'build', 'external_links', 'links', 'isolation',
        'network_mode', 'secrets', 'configs', 'devices',
        'sysctls', 'userns_mode', 'group_add', 'shm_size',
        'cpus', 'cpu_shares', 'cpu_period', 'cpu_quota',
        'cpuset', 'blkio_config', 'device_cgroup_rules',
        'credential_spec', 'runtime',
    }
    return [k.rstrip(':') for k in keys if k.rstrip(':') not in known_attrs]
