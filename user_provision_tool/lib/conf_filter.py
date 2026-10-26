"""D6 / G61 — make the rendered nginx confs follow the compose's active services.

The exposed set is the RENDERED nginx confs, nothing else. Before a conf is
written, every top-level ``server{}`` block is resolved to the compose service
it proxies to and dropped when that service cannot run under the deployment's
active profiles. The rule is compose's own, measured with real containers
(2026-10-24): a service with no ``profiles`` key always runs; ``profiles: [""]``
is the DEFAULT-STACK marker (runs only when NO profile is activated, excluded as
soon as one is); a named profile runs only when active.

The filter is applied to BOTH categories — a selected conf and a generated one,
http and stream alike. It deliberately keeps any block whose upstream is not a
compose service (an external target the compose does not own): the filter's
job is to remove blocks that cannot work, not to judge the operator's conf.

Name-agnostic by construction: a block is identified by the container name it
proxies to, after the instance's container prefix is stripped. It never
compares ``server_name`` values.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml

_SERVER_START = re.compile(r"(?m)^[ \t]*server[ \t]*\{")
_PROXY_PASS = re.compile(
    r"proxy_pass[ \t]+(?:https?://)?([A-Za-z0-9_.-]+)(?::\d+)?[ \t]*;"
)
_SET_UPSTREAM = re.compile(
    r"set[ \t]+\$[A-Za-z0-9_]+[ \t]+([A-Za-z0-9_.-]+)(?::\d+)?[ \t]*;"
)


def compose_active_services(
    compose_path: str | Path, active_profiles: Iterable[str] | None
) -> tuple[set[str], set[str]] | None:
    """``(active, all)`` service-name sets for a RENDERED compose, or None.

    ``None`` means the compose could not be read — the caller then skips the
    filter rather than dropping blocks on a guess.
    """
    try:
        data = yaml.safe_load(Path(compose_path).read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — an unreadable compose disables the filter
        return None
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return None
    active_profiles = {str(p) for p in (active_profiles or []) if p}
    active: set[str] = set()
    all_names: set[str] = set()
    for name, svc in services.items():
        if not isinstance(name, str):
            continue
        all_names.add(name)
        profs = (svc or {}).get("profiles") if isinstance(svc, dict) else None
        profs = [str(p) for p in profs] if isinstance(profs, list) else []
        # Compose's REAL rule (measured 2026-10-24, real containers):
        #   * no `profiles:` key (or an empty list) → always runs;
        #   * `profiles: [""]` is the DEFAULT-STACK marker — it runs only when
        #     NO profile is activated, and is EXCLUDED as soon as one is;
        #   * a non-empty profile runs only when it is in the active set.
        if not profs:
            active.add(name)
        elif active_profiles:
            if any(p in active_profiles for p in profs if p):
                active.add(name)
        elif "" in profs:
            active.add(name)
    return active, all_names


def _server_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of the top-level ``server { … }`` blocks."""
    spans: list[tuple[int, int]] = []
    for m in _SERVER_START.finditer(text):
        depth = 0
        i = m.end() - 1  # at the opening brace
        while i < len(text):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    spans.append((m.start(), i + 1))
                    break
            i += 1
    return spans


def _block_upstreams(block: str) -> list[str]:
    hosts: list[str] = []
    for rx in (_PROXY_PASS, _SET_UPSTREAM):
        for m in rx.finditer(block):
            host = m.group(1)
            if host not in hosts:
                hosts.append(host)
    return hosts


def filter_conf_blocks(
    text: str,
    *,
    active_services: set[str],
    all_services: set[str],
    container_prefix: str,
) -> tuple[str, list[str]]:
    """Drop blocks whose compose target is inactive; return ``(text, dropped)``.

    A block naming several compose services is dropped only if ALL of them are
    inactive. A block naming no known service is kept.
    """
    dropped: list[str] = []
    spans = _server_spans(text)
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        block = text[start:end]
        resolved: list[str] = []
        for host in _block_upstreams(block):
            svc = host[len(container_prefix):] if container_prefix and host.startswith(container_prefix) else host
            if svc in all_services:
                resolved.append(svc)
        if resolved and not any(svc in active_services for svc in resolved):
            name = re.search(r"server_name[ \t]+([^;\n]+);", block)
            dropped.append(name.group(1).strip() if name else ",".join(sorted(set(resolved))))
            out.append(text[cursor:start])
            cursor = end
    if not dropped:
        return text, []
    out.append(text[cursor:])
    return "".join(out), dropped
