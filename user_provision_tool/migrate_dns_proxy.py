"""Operator-run per-instance rollout for the DNS proxy (G101 R6.1).

Re-renders every pre-feature instance with the injected ``dns-proxy``, the
reserved tenant-network address and the dynamic ``ip_range``, then recreates
the instance through the operator-lane rebuild.  Idempotent: an instance whose
rendered compose already carries ``dns-proxy`` is skipped; a failure is reported
per instance and leaves the previous compose on disk.

Run inside the -api container (it needs the Docker socket and the same
``USER_DATA_DIR``/``SUBNET_POOLS``)::

    docker exec subnet-acl-provision-api \
      /app/.venv/bin/python migrate_dns_proxy.py [--dry-run] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys

from lib import provisioner, registry


def migrate(*, dry_run: bool = False) -> dict:
    migrated: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for entry in registry.get_all_users():
        sid = str(entry.get("service_id") or "")
        if not sid:
            continue
        # R6.1 — every PRE-FEATURE RUNNING instance; a stopped/failed one is
        # migrated when it is next started/redeployed, not by this rollout.
        if str(entry.get("status") or "") != "running":
            skipped.append({"service_id": sid, "reason": "not running"})
            continue
        try:
            result = provisioner.rerender_instance(service_id=sid, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — report per instance, continue
            failed.append({"service_id": sid, "error": str(exc)})
            continue
        if result.get("skipped"):
            skipped.append({"service_id": sid, "reason": result.get("reason")})
        else:
            migrated.append(result)
    return {"migrated": migrated, "skipped": skipped, "failed": failed,
            "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list the instances that would be re-rendered")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)
    report = migrate(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"dns-proxy rollout: migrated={len(report['migrated'])} "
            f"skipped={len(report['skipped'])} failed={len(report['failed'])}"
            + (" (dry run)" if report["dry_run"] else "")
        )
        for item in report["failed"]:
            print(f"  failed {item['service_id']}: {item['error']}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":  # pragma: no cover - operator entry
    sys.exit(main())
