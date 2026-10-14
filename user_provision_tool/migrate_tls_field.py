#!/usr/bin/env python3
"""One-off migration: rename the registry entry's TLS flag `https` -> `tls`.

Ruled 2026-10-10. The deploy request, the CLI and the template variable were
already renamed to `tls` (with `https` kept as a documented alias for one round);
the STORED field was left behind because six call sites and nine live entries read
it. The record is now renamed too, so the platform has one vocabulary for the
switch — and this script moves the entries that predate it.

What it sweeps, per registry entry:

    {"https": true, ...}   ->  {"tls": true, ...}     (the old key is REMOVED)

It is **idempotent**: an entry that already carries `tls` is left alone, so a
second run reports nothing to do. The flag is never invented — an entry with
neither key stays without one, and `registry.entry_tls` reads both for one round,
so nothing depends on this having run.

A backup of the registry file is written beside it before anything changes, and
every write goes through `registry.transaction()`, so the running -api's own
writes are serialised against it (the file has a flock for exactly this).

Usage:
    python migrate_tls_field.py --registry <path> [--dry-run]

**Always name the registry explicitly.** The module's default is a repo-local
file, and the -api's own container gets its path from `REGISTRY_FILE`; a bare run
therefore lands on a stale local artifact rather than the stack you meant. The
resolved path is printed before anything is read or written, so a wrong target is
visible rather than silent.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import registry  # noqa: E402


def _migrate_entry(entry: dict) -> bool:
    """Rename the flag on one entry. Returns True when it changed."""
    if not isinstance(entry, dict) or "https" not in entry:
        return False
    # The new key's value comes from the old one — never a default, or an entry
    # that was explicitly False would silently become True.
    if "tls" not in entry:
        entry["tls"] = bool(entry.get("https"))
    del entry["https"]
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry", required=True,
                    help="path to the registry YAML to migrate (name it explicitly)")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    path = Path(args.registry)
    registry.REGISTRY_FILE = path          # the module reads this everywhere
    print(f"registry: {path}")
    if not path.is_file():
        print(f"registry not found: {path}")
        return 1
    print(f"entries: {len(registry._load())}")

    with registry.transaction():
        users = registry._load()
        changed = []
        for entry in users:
            before = dict(entry)
            if _migrate_entry(entry):
                changed.append(
                    (before.get("user_name", ""), before.get("service_name", ""),
                     str(before.get("label", "")), before.get("https"))
                )

        if not changed:
            print(f"nothing to do — {len(users)} entries already carry `tls`")
            return 0

        print(f"{len(changed)} of {len(users)} entries to rename:")
        for user, svc, label, was in changed:
            print(f"  {user}/{svc}/{label}: https={was} -> tls={bool(was)}")

        if args.dry_run:
            print("dry run — nothing written")
            return 0

        backup = path.with_name(path.name + ".bak-tls")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
        registry._save(users)
        print(f"wrote {len(users)} entries with the `tls` key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
