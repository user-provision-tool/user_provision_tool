"""lib/compose_merge.py — multi-file docker-compose ordered merge.

Design §Compose multi-file L68-80: when a deploy payload selects 2+ compose
files they are merged with compose-spec merge semantics via::

    docker compose -f a -f b config --no-interpolate --no-path-resolution

Both flags are mandatory (G5): ``--no-interpolate`` keeps ``${VAR}``
interpolation intact for per-user env resolution at up time;
``--no-path-resolution`` keeps relative paths relative so build contexts
still resolve from the per-user rendered file location.

Top-level ``x-*`` extension blocks are stripped from the merged output before
the result is converted to a template.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import docker_ops


def merge_compose_data(paths: list[str]) -> dict[str, Any]:
    """Merge *paths* (ordered) into a pure dict — no files are written.

    Pure preview variant of :func:`merge_compose_files`: runs ``docker
    compose config`` on the ordered file list, strips top-level ``x-*``
    extension blocks, and returns the parsed merged mapping.  Used by the
    lightweight convert/preview response (design §Implementation notes
    L284-286) so the deploy panel can obtain volume keys from the
    converter's in-call src→key mapping without producing artifacts.

    Parameters
    ----------
    paths:
        Ordered list of compose file paths (at least 2).

    Returns
    -------
    dict
        The merged compose mapping (x-* blocks stripped).

    Raises
    ------
    RuntimeError
        If ``docker compose config`` fails (invalid selected compose —
        the deploy will fail cleanly at config time).
    ValueError
        If fewer than 2 paths are given.
    """
    if len(paths) < 2:
        raise ValueError("merge_compose_data requires at least 2 compose files")

    cmd = ["docker", "compose"]
    for p in paths:
        cmd += ["-f", p]
    cmd += ["config", "--no-interpolate", "--no-path-resolution"]

    result = docker_ops._run(cmd, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"docker compose config failed while merging {paths}:\n{detail}"
        )

    try:
        data = yaml.safe_load(result.stdout)
    except yaml.YAMLError as e:
        raise RuntimeError(f"merged compose output is not valid YAML: {e}")

    if not isinstance(data, dict):
        raise RuntimeError("merged compose output is not a mapping")

    # Strip top-level x-* extension blocks (design §Compose multi-file L76-77).
    return {k: v for k, v in data.items() if not str(k).startswith("x-")}


def merge_compose_files(paths: list[str], out_path: str) -> dict[str, Any]:
    """Merge *paths* (ordered) into *out_path*.

    Parameters
    ----------
    paths:
        Ordered list of compose file paths.  A single path is returned
        unchanged (direct conversion path, F15 — no merge).
    out_path:
        Destination for the merged YAML (``<first>.merged.yml``).

    Returns
    -------
    dict with ``merged`` (bool), ``out_path`` (str).

    Raises
    ------
    RuntimeError
        If ``docker compose config`` fails (invalid selected compose —
        the deploy will fail cleanly at config time).
    ValueError
        If fewer than 2 paths are given.
    """
    if len(paths) < 2:
        raise ValueError("merge_compose_files requires at least 2 compose files")

    data = merge_compose_data(paths)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        yaml.dump(
            data,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        )

    return {"merged": True, "out_path": str(out)}
