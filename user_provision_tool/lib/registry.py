"""ID-based registry repository (T3).

``registry.services.id`` (UUID) is the identity. ``(owner_api_user_id,
service_name, label)`` is an active unique attribute, never a foreign key.

The rest of the -api still consumes the flat *entry* dict it always has; this
module assembles that dict from the normalized tables so no caller keeps a
second copy of the schema. Writes are targeted by ``service_id`` — there is no
"load everything and save everything" path.

One-way dependency: this module never reads or references a gateway table.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from . import db

_log = logging.getLogger("provision-api.registry")


class RegistryError(ValueError):
    """A refused registry operation (409 at the HTTP edge)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

_INSTANCE_FIELDS = (
    "status", "current_procedure_id", "failure", "previous_status", "previous_failure",
    "hostname", "subnet", "gateway", "network_name", "compose_template_path",
    "compose_file_path", "nginx_conf_template_path", "nginx_conf_path",
    "nginx_conf_paths", "nginx_conf_paths_by_category", "htpasswd_path",
    "ssl_certificate_path", "ssl_certificate_key_path", "env_files",
    "env_file_path", "container_names", "profiles", "volumes", "build_args", "tls",
)

_SELECT = """
SELECT s.id                AS service_id,
       s.owner_api_user_id,
       a.username          AS user_name,
       s.service_name,
       s.label,
       s.recipe_path,
       s.created_at,
       s.deleted_at,
       i.status,
       i.current_procedure_id,
       i.failure,
       i.previous_status,
       i.previous_failure,
       i.hostname,
       i.subnet,
       i.gateway,
       i.network_name,
       i.compose_template_path,
       i.compose_file_path,
       i.nginx_conf_template_path,
       i.nginx_conf_path,
       i.nginx_conf_paths,
       i.nginx_conf_paths_by_category,
       i.htpasswd_path,
       i.ssl_certificate_path,
       i.ssl_certificate_key_path,
       i.env_files,
       i.env_file_path,
       i.container_names,
       i.profiles,
       i.volumes,
       i.build_args,
       i.tls,
       (SELECT t.task_id FROM registry.tasks t
         WHERE t.procedure_id = i.current_procedure_id
         ORDER BY t.created_at DESC, t.task_id DESC LIMIT 1) AS task_id
  FROM registry.services s
  JOIN registry.api_users a ON a.id = s.owner_api_user_id
  LEFT JOIN registry.service_instances i ON i.service_id = s.id
"""


def _endpoint_map(service_ids: Iterable[str] | None = None) -> dict[str, list[dict]]:
    sql = ("SELECT service_id, served_name, kind, sub_service, port "
           "FROM registry.service_endpoints")
    params: tuple | None = None
    if service_ids is not None:
        ids = list(service_ids)
        if not ids:
            return {}
        sql += " WHERE service_id = ANY(%s)"
        params = (ids,)
    sql += " ORDER BY id"
    out: dict[str, list[dict]] = {}
    for row in db.fetch_all(sql, params):
        out.setdefault(str(row["service_id"]), []).append(row)
    return out


def _basic_map(service_ids: Iterable[str] | None = None) -> dict[str, dict]:
    sql = "SELECT service_id, basic_username, key_version, basic_version, reset_required FROM registry.service_basic"
    params: tuple | None = None
    if service_ids is not None:
        ids = list(service_ids)
        if not ids:
            return {}
        sql += " WHERE service_id = ANY(%s)"
        params = (ids,)
    return {str(r["service_id"]): r for r in db.fetch_all(sql, params)}


def _entry(row: dict, endpoints: list[dict], basic: dict | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "service_id": str(row["service_id"]),
        "owner_api_user_id": str(row["owner_api_user_id"]),
        "user_name": row["user_name"],
        "service_name": row["service_name"],
        "label": str(row["label"]),
        "recipe_path": row.get("recipe_path") or "",
        "deleted_at": row.get("deleted_at"),
        "task_id": row.get("task_id") or "",
        "http_endpoints": [e["served_name"] for e in endpoints if e["kind"] == "http"],
        "stream_sub_services": [e["served_name"] for e in endpoints if e["kind"] == "stream"],
    }
    for key in _INSTANCE_FIELDS:
        if key in row:
            entry[key] = row[key]
    # Absent allocation is NULL in the database (the subnet unique index is
    # partial on NOT NULL); the flat entry keeps the historical empty-string
    # shape its consumers read.
    entry["subnet"] = entry.get("subnet") or ""
    entry["gateway"] = entry.get("gateway") or ""
    # Compatibility keys the card/CLI still read.
    entry["nginx_conf_template_paths"] = list(
        (entry.get("nginx_conf_paths_by_category") or {}).values()
    ) if isinstance(entry.get("nginx_conf_paths_by_category"), dict) else []
    if not entry.get("nginx_conf_paths"):
        entry["nginx_conf_paths"] = [
            v for v in (entry.get("nginx_conf_paths_by_category") or {}).values()
        ] if isinstance(entry.get("nginx_conf_paths_by_category"), dict) else []
    if basic:
        entry["basic_username"] = basic["basic_username"]
        entry["basic_version"] = basic["basic_version"]
        entry["reset_required"] = bool(basic["reset_required"])
        entry["has_basic"] = basic.get("key_version") is not None
    return entry


def _entries(where: str = "", params: tuple = ()) -> list[dict[str, Any]]:
    rows = db.fetch_all(_SELECT + " WHERE s.deleted_at IS NULL" + where +
                        " ORDER BY a.username, s.service_name, s.label", params)
    if not rows:
        return []
    ids = [row["service_id"] for row in rows]
    emap = _endpoint_map(ids)
    bmap = _basic_map(ids)
    return [_entry(r, emap.get(str(r["service_id"]), []), bmap.get(str(r["service_id"])))
            for r in rows]


def get_all_users() -> list[dict[str, Any]]:
    return _entries()


def get_user(user_name: str) -> list[dict[str, Any]]:
    return _entries(" AND a.username = %s", (user_name,))


def get_user_service(user_name: str, service_name: str, label: str) -> dict[str, Any] | None:
    found = _entries(" AND a.username = %s AND s.service_name = %s AND s.label = %s",
                     (user_name, service_name, str(label)))
    return found[0] if found else None


def get_service_by_id(service_id: str | uuid.UUID) -> dict[str, Any] | None:
    rows = db.fetch_all(_SELECT + " WHERE s.id = %s", (str(service_id),))
    if not rows:
        return None
    row = rows[0]
    emap = _endpoint_map([row["service_id"]])
    bmap = _basic_map([row["service_id"]])
    return _entry(row, emap.get(str(row["service_id"]), []), bmap.get(str(row["service_id"])))


def resolve_service_id(service_id: str | uuid.UUID | None = None, *,
                       user_name: str | None = None, service_name: str | None = None,
                       label: str | None = None) -> str | None:
    """Resolve one active identity. ID wins; names must describe one row."""
    if service_id:
        row = db.fetch_one(
            "SELECT id, deleted_at FROM registry.services WHERE id = %s", (str(service_id),)
        )
        if not row or row["deleted_at"] is not None:
            return None
        return str(row["id"])
    if user_name is not None and service_name is not None and label is not None:
        row = db.fetch_one(
            "SELECT s.id FROM registry.services s JOIN registry.api_users a "
            "ON a.id = s.owner_api_user_id WHERE a.username=%s AND s.service_name=%s "
            "AND s.label=%s AND s.deleted_at IS NULL",
            (user_name, service_name, str(label)),
        )
        return str(row["id"]) if row else None
    return None


def entry_tls(entry: dict[str, Any] | None) -> bool:
    """The record's ``tls`` switch, reading the pre-rename ``https`` for one round.

    Mirrors ``provision-gateway/app/services/registry_entry.py`` exactly: the new
    name wins when both are present; anything unreadable is ``False``
    (fail-closed), so a bad value never becomes an accidental TLS vhost.
    """
    if not isinstance(entry, dict):
        return False
    if "tls" in entry:
        return bool(entry.get("tls"))
    return bool(entry.get("https"))


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------


def get_owner(owner_id: str | uuid.UUID) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT id, username, state, deleted_at FROM registry.api_users WHERE id=%s",
        (str(owner_id),),
    )


def get_owner_by_username(username: str) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT id, username, state, deleted_at FROM registry.api_users WHERE username=%s",
        (username,),
    )


def ensure_owner(conn, username: str, *, mode: str = "owner") -> tuple[uuid.UUID, bool]:
    """Idempotent ensure-by-username under the account advisory lock.

    ``mode='owner'`` (direct/CLI/minimal deploy) inserts a live service-only
    row or promotes a pending row live. ``mode='account'`` inserts pending.
    A tombstoned username is a 409.
    """
    username = username.strip()
    if not username:
        raise RegistryError("owner_username must be non-empty")
    # The -api owns this lock key. The gateway never takes it.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"account:{username.casefold()}",))
    row = conn.execute(
        "SELECT id, state, deleted_at FROM registry.api_users WHERE username=%s",
        (username,),
    ).fetchone()
    now = utcnow()
    if row:
        if row["deleted_at"] is not None:
            raise RegistryError(f"owner {username!r} is tombstoned")
        if mode == "owner" and row["state"] == "pending":
            conn.execute(
                "UPDATE registry.api_users SET state='live', pending_since=%s WHERE id=%s",
                (now, row["id"]),
            )
            row["state"] = "live"
        return row["id"], False
    oid = uuid.uuid4()
    state = "live" if mode == "owner" else "pending"
    conn.execute(
        "INSERT INTO registry.api_users (id, username, state, pending_since, created_at) "
        "VALUES (%s,%s,%s,%s,%s)",
        (oid, username, state, now, now),
    )
    return oid, True


def resolve_owner(conn, owner_api_user_id: str | None = None,
                  owner_username: str | None = None, *, ensure_mode: str = "owner") -> uuid.UUID:
    """Resolve exactly one owner form (R2.2), inside the caller's transaction.

    A direct/CLI/minimal deploy sends ``owner_username`` and this call ensures
    the live service-only row in the same transaction as the ``services`` insert.
    Both forms present must name the same row; otherwise 409.
    """
    by_id = None
    by_name = None
    if owner_api_user_id:
        try:
            oid = uuid.UUID(str(owner_api_user_id))
        except (ValueError, TypeError) as exc:
            raise RegistryError("owner_api_user_id is not a UUID") from exc
        row = conn.execute(
            "SELECT id, state, deleted_at FROM registry.api_users WHERE id=%s", (oid,)
        ).fetchone()
        if not row or row["state"] != "live" or row["deleted_at"] is not None:
            raise RegistryError("owner_api_user_id is not a live owner")
        by_id = row["id"]
    if owner_username:
        entry = conn.execute(
            "SELECT id, state, deleted_at FROM registry.api_users WHERE username=%s",
            (owner_username.strip(),),
        ).fetchone()
        if entry and entry["deleted_at"] is not None:
            raise RegistryError(f"owner {owner_username!r} is tombstoned")
        if entry and entry["state"] == "live":
            by_name = entry["id"]
        else:
            by_name, _created = ensure_owner(conn, owner_username, mode=ensure_mode)
    if by_id is not None and by_name is not None and by_id != by_name:
        raise RegistryError("owner_api_user_id and owner_username name different owners")
    result = by_id or by_name
    if result is None:
        raise RegistryError("one of owner_api_user_id or owner_username is required")
    return result


def owner_services(owner_id: str | uuid.UUID) -> list[dict[str, Any]]:
    return _entries(" AND s.owner_api_user_id = %s", (str(owner_id),))


def label_lock(conn, owner_id: uuid.UUID, service_name: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"label:{owner_id}:{service_name}",))


def next_free_label(user_name: str, service_name: str) -> str:
    """``max(label)+1`` over active AND historical rows (never reuse a label)."""
    row = db.fetch_one(
        "SELECT s.label FROM registry.services s JOIN registry.api_users a "
        "ON a.id = s.owner_api_user_id WHERE a.username=%s AND s.service_name=%s",
        (user_name, service_name),
    )
    labels: list[int] = []
    for r in db.fetch_all(
        "SELECT s.label FROM registry.services s JOIN registry.api_users a "
        "ON a.id = s.owner_api_user_id WHERE a.username=%s AND s.service_name=%s",
        (user_name, service_name),
    ):
        try:
            labels.append(int(str(r["label"])))
        except (TypeError, ValueError):
            continue
    return str(max(labels) + 1) if labels else "0"


# ---------------------------------------------------------------------------
# Writes (all take the caller's transaction connection)
# ---------------------------------------------------------------------------


def insert_task(conn, *, task_id: str, procedure_id: uuid.UUID, service_id: uuid.UUID | None,
                kind: str, operation: str, request: Any = None) -> None:
    conn.execute(
        "INSERT INTO registry.tasks (task_id, procedure_id, service_id, executor, kind, "
        "operation, request, status, created_at, updated_at) "
        "VALUES (%s,%s,%s,'api',%s,%s,%s,'queued',%s,%s)",
        (task_id, procedure_id, service_id, kind, operation,
         _jsonb(request), utcnow(), utcnow()),
    )


def insert_service(conn, *, service_id: uuid.UUID, owner_id: uuid.UUID, service_name: str,
                   label: str, recipe_path: str) -> None:
    conn.execute(
        "INSERT INTO registry.services (id, owner_api_user_id, service_name, label, "
        "recipe_path, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (service_id, owner_id, service_name, str(label), recipe_path, utcnow()),
    )


def insert_instance(conn, service_id: uuid.UUID, *, procedure_id: uuid.UUID,
                    status: str = "building", **fields: Any) -> None:
    cols = ["service_id", "status", "current_procedure_id", "created_at", "updated_at"]
    vals: list[Any] = [service_id, status, procedure_id, utcnow(), utcnow()]
    allowed = {k for k in _INSTANCE_FIELDS if k not in ("status", "current_procedure_id")}
    for key, value in fields.items():
        if key not in allowed:
            continue
        cols.append(key)
        vals.append(_jsonb(value) if key in (
            "failure", "previous_failure", "nginx_conf_paths", "nginx_conf_paths_by_category",
            "env_files", "volumes", "build_args") else value)
    placeholders = ",".join(["%s"] * len(cols))
    conn.execute(
        f"INSERT INTO registry.service_instances ({','.join(cols)}) VALUES ({placeholders})",
        tuple(vals),
    )


def update_instance(conn, service_id: str | uuid.UUID, **fields: Any) -> None:
    allowed = {k for k in _INSTANCE_FIELDS}
    sets: list[str] = []
    vals: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=%s")
        vals.append(_jsonb(value) if key in (
            "failure", "previous_failure", "nginx_conf_paths", "nginx_conf_paths_by_category",
            "env_files", "volumes", "build_args") else value)
    if not sets:
        return
    sets.append("updated_at=%s")
    vals.append(utcnow())
    vals.append(str(service_id))
    conn.execute(
        f"UPDATE registry.service_instances SET {','.join(sets)} WHERE service_id=%s",
        tuple(vals),
    )


def set_status(service_id: str | uuid.UUID, status: str,
               failure: dict | None = None, *, clear_failure: bool = False) -> None:
    with db.connection() as conn:
        if clear_failure:
            update_instance(conn, service_id, status=status, failure=None)
        else:
            update_instance(conn, service_id, status=status, failure=failure)


def set_endpoints(conn, service_id: uuid.UUID,
                  http_names: list[str], stream_names: list[str],
                  stream_port: int | None) -> None:
    """One row per (served_name, kind); a name may be both http and stream."""
    conn.execute("DELETE FROM registry.service_endpoints WHERE service_id=%s", (service_id,))
    for name in http_names:
        conn.execute(
            "INSERT INTO registry.service_endpoints (service_id, served_name, kind, sub_service) "
            "VALUES (%s,%s,'http',%s) ON CONFLICT (served_name, kind) DO NOTHING",
            (service_id, str(name).lower(), _sub_service(name)),
        )
    for name in stream_names:
        conn.execute(
            "INSERT INTO registry.service_endpoints (service_id, served_name, kind, sub_service, port) "
            "VALUES (%s,%s,'stream',%s,%s) ON CONFLICT (served_name, kind) DO NOTHING",
            (service_id, str(name).lower(), _sub_service(name), stream_port),
        )


def set_basic(conn, service_id: uuid.UUID, *, basic_username: str,
              secret_enc: bytes | None, key_version: int | None,
              basic_version: int, reset_required: bool) -> None:
    conn.execute(
        "INSERT INTO registry.service_basic (service_id, basic_username, secret_enc, "
        "key_version, basic_version, reset_required) VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (service_id) DO UPDATE SET basic_username=EXCLUDED.basic_username, "
        "secret_enc=EXCLUDED.secret_enc, key_version=EXCLUDED.key_version, "
        "basic_version=EXCLUDED.basic_version, reset_required=EXCLUDED.reset_required",
        (service_id, basic_username, secret_enc, key_version, basic_version, reset_required),
    )


def set_current_procedure(conn, service_id: uuid.UUID, procedure_id: uuid.UUID) -> None:
    conn.execute(
        "UPDATE registry.service_instances SET current_procedure_id=%s, updated_at=%s "
        "WHERE service_id=%s", (procedure_id, utcnow(), service_id),
    )


def delete_instance(conn, service_id: uuid.UUID) -> None:
    """The fix / instance-only scope: endpoints + instance; services/Basic stay."""
    conn.execute("DELETE FROM registry.service_endpoints WHERE service_id=%s", (service_id,))
    conn.execute("DELETE FROM registry.service_instances WHERE service_id=%s", (service_id,))


def tombstone(conn, service_id: uuid.UUID) -> None:
    """The full service scope: endpoints + Basic + instance + `deleted_at`."""
    conn.execute("DELETE FROM registry.service_endpoints WHERE service_id=%s", (service_id,))
    conn.execute("DELETE FROM registry.service_basic WHERE service_id=%s", (service_id,))
    conn.execute("DELETE FROM registry.service_instances WHERE service_id=%s", (service_id,))
    conn.execute("UPDATE registry.services SET deleted_at=%s WHERE id=%s",
                 (utcnow(), service_id))


def encrypted_basic_count() -> int:
    row = db.fetch_one(
        "SELECT count(*) AS n FROM registry.service_basic WHERE secret_enc IS NOT NULL"
    )
    return int(row["n"]) if row else 0


def _sub_service(name: str) -> str | None:
    if "--" not in str(name):
        return None
    right = str(name).split("--", 1)[1]
    return right.split(".", 1)[0] or None


def _jsonb(value: Any):
    if value is None:
        return None
    import json

    from psycopg.types.json import Jsonb
    # `default=str`: task/entry payloads legitimately carry UUID/datetime
    # objects produced by this service; the column is JSON and must not fail
    # a completed deploy on their type.
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=str))
