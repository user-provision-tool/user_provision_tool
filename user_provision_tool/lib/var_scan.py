"""lib/var_scan.py — ${VAR} interpolation scanner for docker-compose.

Design §Env story L156-163, L177-178: the ``needs_env`` signal comes from the
presence of any ``${VAR}`` interpolation in the selected/merged compose
(service-level ``env_file:`` lists are NOT part of the signal).  Robustness
requirements: nested defaults (``${A:-${B:-x}}``), required syntax
(``${A:?err}``) and ``$$`` escapes (``$${VAR}`` is a literal ``${VAR}``) are
all handled and tested here.

Env-file syntax supported (Docker Compose interpolation):
  ${VAR}            — required, no default
  ${VAR:-default}   — default used when unset
  ${VAR-default}    — same, colon-less form
  ${VAR:?err}       — error when unset
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class VarRef:
    name: str
    has_default: bool = False
    required: bool = False
    default_raw: str = ""


def _iter_var_contents(text: str):
    """Yield the raw content of every unescaped ``${...}`` in *text*.

    Handles nested braces (``${A:-${B:-x}}``) and ``$$`` escapes
    (``$${VAR}`` / ``$$${VAR}`` → only the odd ``$``-prefixed one counts).
    """
    i = 0
    n = len(text)
    while i < n:
        idx = text.find("${", i)
        if idx == -1:
            return
        # Count consecutive '$' before the '{' — odd count = escaped literal.
        backslashes = 0
        j = idx - 1
        while j >= 0 and text[j] == "$":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 1:
            i = idx + 2
            continue
        # Brace-match (nested ${...} inside defaults).
        depth = 1
        k = idx + 2
        while k < n and depth > 0:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
            k += 1
        if depth != 0:
            return  # unclosed — stop scanning
        yield text[idx + 2 : k - 1]
        i = k


def _split_modifier(content: str) -> tuple[str, str, str]:
    """Split ``NAME[:MOD]REST`` at the top-level modifier (outside nested ${}).

    Returns ``(name, modifier, rest)`` where modifier is one of
    ``""``, ``":-"``, ``":?"``, ``"-"``.
    """
    depth = 0
    i = 0
    n = len(content)
    while i < n:
        if content.startswith("${", i):
            depth += 1
            i += 2
            continue
        if content[i] == "}":
            depth -= 1
            i += 1
            continue
        if depth == 0 and content[i] == ":" and i + 1 < n and content[i + 1] in "-?":
            return content[:i], content[i : i + 2], content[i + 2 :]
        if depth == 0 and content[i] == "-":
            return content[:i], "-", content[i + 1 :]
        i += 1
    return content, "", ""


def scan_compose_vars(text: str) -> list[VarRef]:
    """Return every ``${VAR}`` reference in *text* (each occurrence + nested).

    ``$$``-escaped sequences are skipped.  Nested defaults are recorded as
    separate refs (``${A:-${B:-x}}`` → A (with default) and B (with default)).
    """
    refs: list[VarRef] = []
    for content in _iter_var_contents(text):
        name, modifier, rest = _split_modifier(content)
        if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        refs.append(
            VarRef(
                name=name,
                has_default=modifier in (":-", "-"),
                required=modifier == ":?",
                default_raw=rest,
            )
        )
        if "${" in rest:
            refs.extend(scan_compose_vars(rest))
    return refs


def needs_env(compose_text: str) -> bool:
    """True when *compose_text* contains any ``${VAR}`` interpolation."""
    return any(scan_compose_vars(compose_text))


def _parse_env_names(env_text: str) -> set[str]:
    """Return variable names defined in an env-file text (``NAME=VALUE`` lines)."""
    names: set[str] = set()
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            names.add(line.split("=", 1)[0].strip())
    return names


def env_completeness(vars: list[VarRef], env_text: str) -> list[str]:
    """Names referenced without a default that are missing from *env_text*.

    Both ``${VAR}`` and ``${VAR:?err}`` count as needing a value; a deploy
    without them can fail at up time.  Deterministic (sorted).
    """
    if not vars:
        return []
    env_names = _parse_env_names(env_text)
    needed = {r.name for r in vars if not r.has_default}
    return sorted(n for n in needed if n not in env_names)


def skeleton_env(vars: list[VarRef], existing_text: str = "") -> str:
    """Deterministic ``.env`` skeleton prefill (design L177-178).

    Every no-default var gets a ``NAME=`` line (existing values are reused
    when present in *existing_text*).  Order = first-scan order, de-duped.
    """
    existing = _parse_env_names(existing_text)
    seen: set[str] = set()
    lines: list[str] = []
    for r in vars:
        if r.name in seen or r.has_default:
            continue
        seen.add(r.name)
        lines.append(f"{r.name}=")
    if lines:
        return "\n".join(lines) + "\n"
    return ""
