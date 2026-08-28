#!/usr/bin/env python3
"""v5 one-off migration for deployed per-service nginx confs (decision 16 / §12 Phase 2).

v4 injected the ACL scaffold (`auth_mode`, `/_auth_jwt`, `/_set_token`,
`/__basic__/`, `@auth_401`/`@auth_403`, `env.d`) into EVERY per-service conf.
v5 reverts the internal ``-nginx`` to the simple ACL-free byte-identical form
(§5) — the ACL gate lives entirely at the edge ``-nginx-acl``.

This script sweeps the generated conf directory:

1. **Re-render** every registry entry that has a template via
   ``render_nginx_conf`` → the v5 simple form (idempotent; a clean conf is a
   no-op).
2. **Strip** stale v4/v3 scaffold from any conf that still carries it
   (orphans without a registry template, or entries whose re-render failed)
   via ``template_engine.strip_v4_scaffold``.
3. **Verify**: report the number of confs still containing ``auth_mode``
   (must be 0), and print the standard `grep -l auth_mode …` equivalent.

Run it after rebuilding ``subnet-acl-provision-api`` and before ``nginx -t``
(Phase 2). Idempotent — safe to run repeatedly.

Usage:
    python -m user_provision_tool.migrate_v5            # GENERATED_DIR env / default
    python -m user_provision_tool.migrate_v5 --dry-run  # report only, no writes
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

# `user_provision_tool/` is a flat (namespace) package: it has no __init__.py
# and `lib/` lives beside this file. Resolve `lib` no matter how we are
# invoked so the DOCUMENTED `python -m user_provision_tool.migrate_v5` works
# from the repo root (`_users_provision/`), alongside the other forms:
#   - `python -m user_provision_tool.migrate_v5`  (repo root, documented)
#   - `python -m migrate_v5`                       (from inside the package dir)
#   - `from migrate_v5 import migrate`             (tests: conftest adds the dir)
_PKG_DIR = Path(__file__).resolve().parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

try:
    from .lib import registry
    from .lib import template_engine
except ImportError:  # plain-module invocation (no package parent, e.g. python migrate_v5.py)
    from lib import registry
    from lib import template_engine

# v5 Phase 2 verification: after migration NO conf may still reference the v4
# scaffold's mode variable (or any scaffold token).
_SCAFFOLD_TOKEN_RE = re.compile(
    r"auth_mode|auth_request|/_auth_jwt|/_set_token|/__basic__/|"
    r"@auth_401|@auth_403|/etc/nginx/env\.d"
)


def _generated_dir() -> Path:
    return Path(os.environ.get("GENERATED_DIR", "/srv/provision_subnet_acl/generated"))


def _domain_of(entry: dict[str, Any]) -> str:
    hostname = entry.get("hostname", "")
    if hostname and "." in hostname:
        return hostname.split(".", 1)[1]
    return "localhost"


def migrate(generated_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """Re-render + strip stale v4 scaffold across *generated_dir*.

    Returns a report dict:
      ``regenerated`` — confs re-rendered from a registry template.
      ``stripped``    — confs cleaned by surgical scaffold stripping.
      ``skipped``     — confs left untouched (already clean).
      ``errors``      — per-file errors.
      ``remaining_scaffold`` — confs still containing a scaffold token after
        migration (must be 0 for a clean migration).
      ``dry_run``     — whether writes were suppressed.
    """
    generated_dir = Path(generated_dir)
    all_users = registry.get_all_users()
    regenerated = 0
    stripped = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    remaining: list[str] = []

    # Pass 1: re-render every registry entry that has a template (v5 simple form).
    for entry in all_users:
        nginx_tpl = entry.get("nginx_conf_template_path") or ""
        nginx_out = entry.get("nginx_conf_path") or ""
        if not nginx_tpl or not nginx_out:
            continue
        out = Path(nginx_out)
        if not out.exists():
            continue
        try:
            if not dry_run:
                template_engine.render_nginx_conf(
                    nginx_tpl,
                    nginx_out,
                    entry.get("user_name", ""),
                    entry.get("service_name", ""),
                    str(entry.get("label", "0")),
                    _domain_of(entry),
                    entry.get("htpasswd_path") or "",
                    https=bool(entry.get("https")),
                    ssl_certificate_path=entry.get("ssl_certificate_path") or "",
                    ssl_certificate_key_path=entry.get("ssl_certificate_key_path") or "",
                )
            regenerated += 1
        except Exception as exc:
            errors.append({
                "conf": str(nginx_out),
                "reason": str(exc),
            })

    # Pass 2: surgical strip for confs that still carry scaffold tokens.
    for cf in sorted(generated_dir.glob("*.nginx.conf")):
        try:
            content = cf.read_text()
            if _SCAFFOLD_TOKEN_RE.search(content):
                if not dry_run:
                    cf.write_text(template_engine.strip_v4_scaffold(content))
                stripped += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append({"conf": str(cf), "reason": str(exc)})

    # Verification: any conf left with a scaffold token?
    for cf in sorted(generated_dir.glob("*.nginx.conf")):
        try:
            if _SCAFFOLD_TOKEN_RE.search(cf.read_text()):
                remaining.append(str(cf))
        except Exception:
            pass

    return {
        "regenerated": regenerated,
        "stripped": stripped,
        "skipped": skipped,
        "errors": errors,
        "remaining_scaffold": remaining,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generated-dir",
        default=os.environ.get("GENERATED_DIR", "/srv/provision_subnet_acl/generated"),
        help="generated conf directory (default: $GENERATED_DIR)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = parser.parse_args()

    report = migrate(Path(args.generated_dir), dry_run=args.dry_run)
    if args.dry_run:
        print("[dry-run] no files were written")
    print(f"regenerated: {report['regenerated']}")
    print(f"stripped:    {report['stripped']}")
    print(f"skipped:     {report['skipped']}")
    for err in report["errors"]:
        print(f"ERROR {err['conf']}: {err['reason']}", file=sys.stderr)
    remaining = report["remaining_scaffold"]
    print(f"remaining scaffold confs: {len(remaining)}")
    for cf in remaining:
        print(f"  {cf}")
    if remaining:
        print("VERIFICATION FAILED: `grep -l auth_mode generated/*.conf` is NOT empty", file=sys.stderr)
        return 1
    print("VERIFICATION PASSED: no scaffold tokens remain in generated confs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
