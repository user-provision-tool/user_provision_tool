"""T2 — import the live YAML registry into the ``registry`` schema.

Run inside the -api container (the same absolute ``PROVISION_DIR`` paths are
mounted there) or on the host for a rehearsal:

    python -m migrations.import_registry \
        --yaml  /srv/provision_subnet_acl/generated/user_registry.yml \
        --tasks /srv/provision_subnet_acl/generated/task_logs/task_registry.json \
        --sqlite /srv/provision_subnet_acl/gateway_data/gateway.db \
        --report /srv/provision_subnet_acl/generated/import_report.json

Guarantees:
  * only the live paths are read; a missing file is reported, never invented;
  * every insert is idempotent (ON CONFLICT / delete-then-insert for a service's
    normalized rows), so a retried step cannot double-insert;
  * credentials never land in plaintext: ``passwd_plain`` is encrypted with
    keyring v1, a bcrypt-only service becomes ``reset_required``;
  * a case-folded owner collision, an unresolvable tuple, or an ambiguous
    recipe stops with a report instead of merging/guessing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from psycopg.types.json import Jsonb

from lib import db, keys

SECRET_KEYS = {
    "passwd", "passwd_plain", "privkey", "password", "api_key", "token",
    "byok_api_key", "resume_values",
}


def _utc(epoch: float | int | None) -> datetime:
    if not epoch:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if k not in SECRET_KEYS}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _parse_sub_service(served_name: str) -> str | None:
    """``<stem>--<sub>.<domain>`` -> ``sub``; None when there is no separator."""
    if "--" not in served_name:
        return None
    left, right = served_name.split("--", 1)
    return right.split(".", 1)[0] or None


def _stream_port(entry: dict[str, Any]) -> int | None:
    """Parse the stream row's target port from its own rendered conf."""
    by_cat = entry.get("nginx_conf_paths_by_category") or {}
    candidates: list[str] = []
    if isinstance(by_cat, dict) and by_cat.get("stream"):
        candidates.append(by_cat["stream"])
    for p in entry.get("nginx_conf_paths") or []:
        if isinstance(p, str) and p.endswith(".stream.conf"):
            candidates.append(p)
    for path in candidates:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Rendered stream confs use a bare upstream (`proxy_pass host:7687;`),
        # not a scheme; also accept an upstream block's `server host:port;`.
        m = re.search(r"proxy_pass\s+(?:https?://)?[^:;\s]+:(\d+)", text)
        if not m:
            m = re.search(r"(?:server|socket)\s+\S+:(\d+)\s*;", text)
        if m:
            return int(m.group(1))
    return None


def _recipe_path(entry: dict[str, Any], source_projects_dir: Path) -> tuple[str | None, str | None]:
    """Return ``(recipe_path, error)`` for one entry.

    Derived only from paths recorded INSIDE the service's project directory:
    rendered nginx confs live under ``generated/`` and say nothing about the
    recipe. More than one distinct recipe directory is ambiguous and stops the
    entry (422-equivalent), never a guess.
    """
    service_name = str(entry.get("service_name") or "")
    project = (source_projects_dir / service_name).resolve()
    found: set[str] = set()
    candidates: list[str] = []
    for key in ("compose_template_path", "compose_file_path", "env_file_path"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            candidates.append(v)
    for v in entry.get("compose_sources") or []:
        if isinstance(v, str) and v:
            candidates.append(v)
    for c in candidates:
        try:
            rel = Path(c).resolve().parent.relative_to(project)
        except (ValueError, OSError):
            continue
        found.add("" if str(rel) == "." else rel.as_posix())
    if not found:
        return None, f"no recorded path resolves inside {project}"
    if len(found) > 1:
        return None, f"ambiguous recipe path: {sorted(found)}"
    return next(iter(found)), None


def _status_map(entry: dict[str, Any]) -> tuple[str, dict | None, str | None]:
    raw = str(entry.get("status") or "").strip().lower()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if raw in ("live", "healthy", "running"):
        return "running", None, None
    if raw in ("failed", "error"):
        reason = "legacy error status" if raw == "error" else "legacy failed status"
        return "failed", {"stage": "import", "message": reason, "reason": "import", "at": at}, reason
    if raw == "unknown":
        return "unknown", None, "preserved unknown status"
    # building/deleting/missing/anything else has no runner across the freeze.
    return "unknown", None, f"legacy status {raw or 'missing'} imported as unknown"


def _ensure_owner(conn, username: str) -> uuid.UUID:
    row = conn.execute(
        "SELECT id FROM registry.api_users WHERE username = %s", (username,)
    ).fetchone()
    if row:
        return row["id"]
    oid = uuid.uuid4()
    conn.execute(
        "INSERT INTO registry.api_users (id, username, state, created_at) "
        "VALUES (%s, %s, 'live', %s) ON CONFLICT (username) DO NOTHING",
        (oid, username, datetime.now(timezone.utc)),
    )
    row = conn.execute(
        "SELECT id FROM registry.api_users WHERE username = %s", (username,)
    ).fetchone()
    return row["id"]


def _upsert_service(conn, entry: dict, owner_id: uuid.UUID, recipe: str,
                    service_id: uuid.UUID) -> uuid.UUID:
    active = conn.execute(
        "SELECT id FROM registry.services WHERE owner_api_user_id=%s "
        "AND service_name=%s AND label=%s AND deleted_at IS NULL",
        (owner_id, entry["service_name"], str(entry.get("label", "0"))),
    ).fetchone()
    if active:
        return active["id"]
    tomb = conn.execute(
        "SELECT id FROM registry.services WHERE owner_api_user_id=%s "
        "AND service_name=%s AND label=%s AND deleted_at IS NOT NULL",
        (owner_id, entry["service_name"], str(entry.get("label", "0"))),
    ).fetchone()
    if tomb:
        raise ValueError("tuple matches a tombstone; re-creation needs a new label")
    conn.execute(
        "INSERT INTO registry.services (id, owner_api_user_id, service_name, label, "
        "recipe_path, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (service_id, owner_id, entry["service_name"], str(entry.get("label", "0")),
         recipe, datetime.now(timezone.utc)),
    )
    return service_id


def _import_instance_and_rows(conn, service_id: uuid.UUID, entry: dict, report: dict) -> None:
    status, failure, note = _status_map(entry)
    if note:
        report["status_notes"].append({"service_id": str(service_id), "note": note})
    env_files = entry.get("env_files")
    conn.execute(
        """
        INSERT INTO registry.service_instances (
            service_id, status, failure, hostname, subnet, gateway, network_name,
            compose_template_path, compose_file_path, nginx_conf_paths,
            nginx_conf_template_path, nginx_conf_path, nginx_conf_paths_by_category,
            htpasswd_path, ssl_certificate_path, ssl_certificate_key_path,
            env_files, env_file_path, container_names, profiles, volumes, build_args,
            tls, created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (service_id) DO UPDATE SET
            status=EXCLUDED.status, failure=EXCLUDED.failure, hostname=EXCLUDED.hostname,
            subnet=EXCLUDED.subnet, gateway=EXCLUDED.gateway, network_name=EXCLUDED.network_name,
            compose_template_path=EXCLUDED.compose_template_path,
            compose_file_path=EXCLUDED.compose_file_path,
            nginx_conf_paths=EXCLUDED.nginx_conf_paths,
            nginx_conf_template_path=EXCLUDED.nginx_conf_template_path,
            nginx_conf_path=EXCLUDED.nginx_conf_path,
            nginx_conf_paths_by_category=EXCLUDED.nginx_conf_paths_by_category,
            htpasswd_path=EXCLUDED.htpasswd_path,
            ssl_certificate_path=EXCLUDED.ssl_certificate_path,
            ssl_certificate_key_path=EXCLUDED.ssl_certificate_key_path,
            env_files=EXCLUDED.env_files, env_file_path=EXCLUDED.env_file_path,
            container_names=EXCLUDED.container_names, profiles=EXCLUDED.profiles,
            volumes=EXCLUDED.volumes, build_args=EXCLUDED.build_args, tls=EXCLUDED.tls,
            updated_at=EXCLUDED.updated_at
        """,
        (
            service_id, status, Jsonb(failure) if failure else None,
            entry.get("hostname") or "", entry.get("subnet") or "",
            entry.get("gateway") or "", entry.get("network_name") or "",
            entry.get("compose_template_path") or "", entry.get("compose_file_path") or "",
            Jsonb(entry.get("nginx_conf_paths") or []),
            entry.get("nginx_conf_template_path") or "",
            entry.get("nginx_conf_path") or "",
            Jsonb(entry.get("nginx_conf_paths_by_category") or {}),
            entry.get("htpasswd_path") or "",
            entry.get("ssl_certificate_path") or "",
            entry.get("ssl_certificate_key_path") or "",
            Jsonb(env_files or []), entry.get("env_file_path") or "",
            list(entry.get("container_names") or []), list(entry.get("profiles") or []),
            Jsonb(entry.get("volumes") or {}), Jsonb(entry.get("build_args") or {}),
            bool(entry.get("tls") if "tls" in entry else entry.get("https")),
            datetime.now(timezone.utc), datetime.now(timezone.utc),
        ),
    )

    # Normalized endpoints: one row per (served_name, kind), so a name that is
    # both an http and a stream endpoint becomes two rows.
    conn.execute("DELETE FROM registry.service_endpoints WHERE service_id=%s", (service_id,))
    rows: list[tuple[Any, ...]] = []
    for name in entry.get("http_endpoints") or []:
        if isinstance(name, str) and name.strip():
            rows.append((name.strip().lower(), "http", _parse_sub_service(name), None))
    stream_port = _stream_port(entry)
    for name in entry.get("stream_sub_services") or []:
        if not isinstance(name, str) or not name.strip():
            continue
        if stream_port is None:
            report["endpoint_notes"].append(
                {"service_id": str(service_id), "served_name": name,
                 "note": "stream port unparsable; imported NULL"}
            )
        rows.append((name.strip().lower(), "stream", _parse_sub_service(name), stream_port))
    for served, kind, sub, port in rows:
        try:
            conn.execute(
                "INSERT INTO registry.service_endpoints "
                "(service_id, served_name, kind, sub_service, port) VALUES (%s,%s,%s,%s,%s)",
                (service_id, served, kind, sub, port),
            )
        except Exception as exc:  # noqa: BLE001 — duplicate after case-fold
            raise ValueError(f"duplicate {kind} endpoint {served!r} after case-folding: {exc}")

    # Basic credential. plaintext is encrypted with the keyring; a bcrypt-only
    # row has no plaintext and is marked reset_required, never invented.
    plain = entry.get("passwd_plain")
    bcrypt = entry.get("passwd")
    basic_username = str(entry.get("user_name") or "")
    if plain:
        envelope = keys.encrypt(str(plain), version="v1").encode("utf-8")
        conn.execute(
            "INSERT INTO registry.service_basic (service_id, basic_username, secret_enc, "
            "key_version, basic_version, reset_required) VALUES (%s,%s,%s,1,1,false) "
            "ON CONFLICT (service_id) DO UPDATE SET basic_username=EXCLUDED.basic_username, "
            "secret_enc=EXCLUDED.secret_enc, key_version=EXCLUDED.key_version, "
            "reset_required=false",
            (service_id, basic_username, envelope),
        )
    elif bcrypt:
        conn.execute(
            "INSERT INTO registry.service_basic (service_id, basic_username, secret_enc, "
            "key_version, basic_version, reset_required) VALUES (%s,%s,NULL,NULL,1,true) "
            "ON CONFLICT (service_id) DO UPDATE SET basic_username=EXCLUDED.basic_username, "
            "reset_required=true",
            (service_id, basic_username),
        )
        report["reset_required"].append(str(service_id))
    else:
        conn.execute(
            "INSERT INTO registry.service_basic (service_id, basic_username, secret_enc, "
            "key_version, basic_version, reset_required) VALUES (%s,%s,NULL,NULL,1,false) "
            "ON CONFLICT (service_id) DO UPDATE SET basic_username=EXCLUDED.basic_username",
            (service_id, basic_username),
        )


def _legacy_sessions(sqlite_path: str | None) -> list[dict[str, Any]]:
    if not sqlite_path or not Path(sqlite_path).is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT user_name, service_name, label, recipe_path, job_id, task_id, state "
            "FROM pre_deploy_sessions"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _pair_map(sessions: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str], dict[str, str], list[dict[str, str]]]:
    """One procedure per paired (job, failed task); singles get their own.

    Returns (job->procedure, task->procedure, tuple->procedure, entries).
    """
    job_proc: dict[str, str] = {}
    task_proc: dict[str, str] = {}
    tuple_proc: dict[str, str] = {}
    entries: list[dict[str, str]] = []
    for s in sessions:
        job = s.get("job_id")
        task = s.get("task_id")
        if job in (None, "", 0) and task in (None, ""):
            continue
        proc = job_proc.get(str(job)) if job not in (None, "") else None
        if proc is None and task not in (None, ""):
            proc = task_proc.get(str(task))
        if proc is None:
            proc = str(uuid.uuid4())
        if job not in (None, "", 0):
            job_proc[str(job)] = proc
        if task not in (None, ""):
            task_proc[str(task)] = proc
        tuple_proc[f"{s.get('user_name')}|{s.get('service_name')}|{s.get('label')}"] = proc
        entries.append({"job_id": str(job) if job not in (None, "") else "",
                        "task_id": str(task) if task not in (None, "") else "",
                        "procedure_id": proc})
    return job_proc, task_proc, tuple_proc, entries


TASK_KIND = {"register": ("deploy", "register"), "rebuild": ("rebuild", "rebuild"),
             "remove": ("remove", "remove")}


def _task_status(raw: str) -> str:
    raw = str(raw or "").lower()
    if raw == "succeeded":
        return "completed"
    if raw in ("completed", "failed", "cancelled"):
        return raw
    return "unknown"


def _import_tasks(conn, tasks_path: str | None, tuple_ids: dict[str, uuid.UUID],
                  task_proc: dict[str, str], report: dict) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    if not tasks_path or not Path(tasks_path).is_file():
        report["notes"].append(f"task JSON missing: {tasks_path}")
        return latest
    try:
        data = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report["errors"].append(f"task JSON unreadable: {exc}")
        return latest
    for t in data.get("tasks", []):
        typ = str(t.get("type") or "")
        if typ == "custom":
            report["tasks_skipped"].append({"task_id": t.get("task_id"), "reason": "record-only custom task"})
            continue
        kind, operation = TASK_KIND.get(typ, ("deploy", typ or "register"))
        task_id = str(t.get("task_id") or "")
        if not task_id:
            report["tasks_skipped"].append({"task_id": None, "reason": "missing task id"})
            continue
        target = t.get("target") or {}
        # Older task records persisted no `target`; a register/rebuild result
        # embeds the entry it produced, whose identity fields are the recorded
        # target. Recover them before redaction — never invent a service.
        result_raw = t.get("result")
        if not target and isinstance(result_raw, dict):
            entry = result_raw.get("entry") or {}
            target = {k: entry.get(k) for k in ("user_name", "service_name", "label")}
        tuple_key = f"{target.get('user_name')}|{target.get('service_name')}|{target.get('label')}"
        service_id = tuple_ids.get(tuple_key)
        snapshot = None
        if service_id is None and any(target.get(k) for k in ("user_name", "service_name")):
            snapshot = {"service_name": target.get("service_name"),
                        "user_name": target.get("user_name"), "label": target.get("label")}
        proc = task_proc.get(task_id) or str(uuid.uuid4())
        created = _utc(t.get("created_at"))
        updated = _utc(t.get("updated_at"))
        log_file = t.get("log_file") or ""
        result = _redact(t.get("result"))
        conn.execute(
            "INSERT INTO registry.tasks (task_id, procedure_id, service_id, executor, kind, "
            "operation, request, status, phase, progress, error, log_ref, result, "
            "target_snapshot, created_at, updated_at) "
            "VALUES (%s,%s,%s,'api',%s,%s,NULL,%s,NULL,NULL,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (task_id) DO NOTHING",
            (task_id, proc, service_id, kind, operation, _task_status(t.get("status")),
             t.get("error") or None, log_file or None,
             Jsonb(result) if result is not None else None,
             Jsonb(snapshot) if snapshot else None, created, updated),
        )
        if service_id is not None:
            prev = latest.get(str(service_id))
            if prev is None or created > prev:
                latest[str(service_id)] = created
    return latest


def run(yaml_path: str, tasks_path: str | None, sqlite_path: str | None,
        report_path: str | None, source_projects_dir: str | None = None,
        pair_map_path: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "yaml": yaml_path, "tasks": tasks_path, "sqlite": sqlite_path,
        "owners": 0, "services": 0, "instances": 0, "endpoints": 0, "basic": 0,
        "tasks_imported": 0, "tasks_skipped": [], "skipped": [], "errors": [],
        "status_notes": [], "endpoint_notes": [], "reset_required": [],
        "notes": [], "pair_map": pair_map_path,
    }
    if not Path(yaml_path).is_file():
        report["errors"].append(f"live YAML registry not found: {yaml_path}")
        return report
    entries = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or []
    if not isinstance(entries, list):
        report["errors"].append("live YAML registry is not a list")
        return report

    source_projects = Path(
        source_projects_dir
        or os.environ.get("SOURCE_PROJECTS_DIR", "/srv/provision_subnet_acl/source_projects")
    )

    # Owner case-fold collisions stop the import: two source names must never
    # merge into one citext username.
    by_fold: dict[str, set[str]] = {}
    for e in entries:
        name = str(e.get("user_name") or "")
        if name:
            by_fold.setdefault(name.casefold(), set()).add(name)
    collisions = {k: sorted(v) for k, v in by_fold.items() if len(v) > 1}
    if collisions:
        report["errors"].append(f"owner names collide after case folding: {collisions}")
        return report

    # Resolve recipes and tuple IDs BEFORE writing anything.
    prepared: list[tuple[dict, str, uuid.UUID]] = []
    tuple_ids: dict[str, uuid.UUID] = {}
    for entry in entries:
        recipe, error = _recipe_path(entry, source_projects)
        if error:
            report["skipped"].append({"entry": {k: entry.get(k) for k in
                                                ("user_name", "service_name", "label")},
                                      "reason": error})
            continue
        key = f"{entry.get('user_name')}|{entry.get('service_name')}|{entry.get('label')}"
        sid = uuid.uuid4()
        tuple_ids[key] = sid
        prepared.append((entry, recipe, sid))

    # Pre-deploy-only tuples share the one map (T2 step 3).
    sessions = _legacy_sessions(sqlite_path)
    job_proc, task_proc, tuple_proc, pair_entries = _pair_map(sessions)
    for s in sessions:
        key = f"{s.get('user_name')}|{s.get('service_name')}|{s.get('label')}"
        tuple_ids.setdefault(key, uuid.uuid4())

    db.migrate()
    with db.connection() as conn:
        # Refuse to invent a key when ciphertext already exists.
        enc = conn.execute(
            "SELECT count(*) AS n FROM registry.service_basic WHERE secret_enc IS NOT NULL"
        ).fetchone()["n"]
        keys.ensure_for_startup(enc)

        owner_ids: dict[str, uuid.UUID] = {}
        for entry in entries:
            name = str(entry.get("user_name") or "")
            if not name:
                report["skipped"].append({"entry": entry.get("service_name"), "reason": "missing owner"})
                continue
            if name not in owner_ids:
                owner_ids[name] = _ensure_owner(conn, name)
                report["owners"] += 1
            else:
                continue

        for entry, recipe, sid in prepared:
            owner = owner_ids[str(entry["user_name"])]
            try:
                sid = _upsert_service(conn, entry, owner, recipe, sid)
                tuple_ids[f"{entry.get('user_name')}|{entry.get('service_name')}|{entry.get('label')}"] = sid
                report["services"] += 1
                _import_instance_and_rows(conn, sid, entry, report)
                report["instances"] += 1
            except Exception as exc:  # noqa: BLE001 — report the entry, keep the rest
                report["errors"].append(
                    f"{entry.get('user_name')}/{entry.get('service_name')}/"
                    f"{entry.get('label')}: {exc}"
                )

        latest = _import_tasks(conn, tasks_path, tuple_ids, task_proc, report)
        for sid, _created in latest.items():
            conn.execute(
                "UPDATE registry.service_instances SET current_procedure_id=("
                " SELECT procedure_id FROM registry.tasks WHERE service_id=%s "
                " ORDER BY created_at DESC, task_id DESC LIMIT 1) WHERE service_id=%s",
                (sid, sid),
            )

        report["counts"] = {
            "services": conn.execute("SELECT count(*) AS n FROM registry.services").fetchone()["n"],
            "instances": conn.execute("SELECT count(*) AS n FROM registry.service_instances").fetchone()["n"],
            "endpoints": conn.execute("SELECT count(*) AS n FROM registry.service_endpoints").fetchone()["n"],
            "basic": conn.execute("SELECT count(*) AS n FROM registry.service_basic").fetchone()["n"],
            "tasks": conn.execute("SELECT count(*) AS n FROM registry.tasks").fetchone()["n"],
        }
        conn.execute(
            "INSERT INTO registry.import_state (stage, detail) VALUES ('yaml_registry', %s) "
            "ON CONFLICT (stage) DO UPDATE SET completed_at=now(), detail=EXCLUDED.detail",
            (Jsonb({"counts": report["counts"], "skipped": len(report["skipped"])}),),
        )
        if tasks_path:
            conn.execute(
                "INSERT INTO registry.import_state (stage, detail) VALUES ('task_json', %s) "
                "ON CONFLICT (stage) DO UPDATE SET completed_at=now(), detail=EXCLUDED.detail",
                (Jsonb({"tasks": report["counts"]["tasks"]}),),
            )
        report["tasks_imported"] = report["counts"]["tasks"]

    if pair_map_path:
        Path(pair_map_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pair_map_path).write_text(json.dumps({
            "procedures": pair_entries,
            "tuples": {k: str(v) for k, v in tuple_ids.items()},
        }, indent=2), encoding="utf-8")
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import the live YAML registry (T2)")
    ap.add_argument("--yaml", default=os.environ.get("REGISTRY_FILE"))
    ap.add_argument("--tasks", default=str(Path(os.environ.get(
        "TASK_LOG_DIR", "/srv/provision_subnet_acl/generated/task_logs")) / "task_registry.json"))
    ap.add_argument("--sqlite", default="/srv/provision_subnet_acl/gateway_data/gateway.db")
    ap.add_argument("--source-projects", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--pair-map", default=None)
    args = ap.parse_args(argv)
    if not args.yaml:
        ap.error("--yaml or REGISTRY_FILE is required")
    report_path = args.report or str(Path(args.yaml).parent / "registry_import_report.json")
    pair_map = args.pair_map or str(Path(args.yaml).parent / "registry_import_pair_map.json")
    report = run(args.yaml, args.tasks, args.sqlite, report_path,
                 args.source_projects, pair_map)
    print(json.dumps({k: report[k] for k in ("owners", "services", "instances", "errors", "skipped") if k in report}, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
