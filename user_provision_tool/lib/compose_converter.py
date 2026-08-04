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

8. Strip `profiles:` from every service.
   Profile selection is a deployment-level concern; every rendered user compose
   must unconditionally start all its services via `docker compose up`.

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
) -> dict:
    container_name_val = tokens.tok(f"{{{{ container_prefix }}}}{svc_key}")

    # Rebuild dict: container_name right after 'image:' for readability;
    # drop 'ports:' entirely (traffic flows through provision-nginx).
    result: dict = {}
    inserted = False
    for key, value in svc.items():
        if key == "container_name":
            continue  # replaced below
        if key == "ports":
            continue  # stripped — use provision-nginx for all ingress
        if key == "profiles":
            continue  # stripped — profile selection is a deployment detail, not per-user
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

    # Replace networks with isolated user network (skip if network_mode is set)
    if "network_mode" not in result:
        result["networks"] = [tokens.tok("{{ network_name }}")]

    return result


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

def convert(data: dict) -> tuple[dict, dict[str, str], _TokenRegistry]:
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

    # 3. Transform each service (skip services locked to a named profile)
    def _is_default_service(svc: dict) -> bool:
        """Return True if the service runs without explicit profile activation.

        Services with no ``profiles`` key are always started.
        Services with ``profiles: [""]`` use an empty-string profile as a
        convention to mark them as the default variant — treated the same way.
        Services with any non-empty profile string are deployment-variant
        services (e.g. ``profiles: ["falkordb"]``) and should not be included
        in a provisioned user compose.
        """
        profiles = svc.get("profiles")
        if not profiles:            # no key, or empty list
            return True
        return all(p == "" for p in profiles)

    result["services"] = {
        svc_key: _transform_service(svc_key, svc, src_to_key, tokens)
        for svc_key, svc in services.items()
        if isinstance(svc, dict) and _is_default_service(svc)
    }

    # 4. Rewrite top-level networks to isolated per-user network
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

    return result, src_to_key, tokens


def make_header(src_to_key: dict[str, str], service_name_hint: str) -> str:
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

    transformed, src_to_key, tokens = convert(data)
    raw_yaml = _dump_yaml(transformed)
    template_body = tokens.detokenize(raw_yaml)
    header = make_header(src_to_key, hint)

    _Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(header + template_body)

    return src_to_key


def get_compose_service_names(compose_path: str) -> list[str]:
    """Return the list of service keys from a compose file or .j2 template.

    Jinja2 tokens (``{{ ... }}``) in templates are replaced with placeholder
    strings before YAML parsing so the file remains parseable.
    """
    import yaml as _yaml

    with open(compose_path) as f:
        raw = f.read()

    # Neutralise Jinja2 expressions so the YAML parser doesn't choke.
    # Replace the entire {{ expr }} token with a plain word so any adjacent
    # text (e.g. "{{ prefix }}web") stays a valid YAML key.
    sanitised = re.sub(r'\{\{.*?\}\}', 'j2placeholder', raw)

    try:
        data = _yaml.safe_load(sanitised)
    except _yaml.YAMLError:
        return []

    if not isinstance(data, dict):
        return []
    services = data.get("services")
    if not isinstance(services, dict):
        return []
    return list(services.keys())
