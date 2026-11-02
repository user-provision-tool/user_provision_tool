"""Test helpers that create/remove registry rows through the PostgreSQL API.

The pre-cutover suite manipulated a YAML list directly (``registry.add_user`` /
``remove_user_service``). The registry is now the ``registry`` schema; these
helpers express the same setup in terms of the repository functions the
application itself uses, so the tests exercise the real write path.
"""

from __future__ import annotations

import uuid
from typing import Any

from lib import db, registry


def add_user(entry: dict[str, Any], *, status: str | None = None,
             procedure_id: uuid.UUID | None = None) -> tuple[str, str]:
    """Insert one owner + service + instance from a legacy-style entry dict.

    Returns ``(service_id, procedure_id)``. Fields the instance projection
    carries (hostname, subnet, paths, ...) are copied when present.
    """
    proc = procedure_id or uuid.uuid4()
    fields = {
        key: entry[key]
        for key in registry._INSTANCE_FIELDS
        if key in entry and entry[key] is not None
    }
    fields.pop("status", None)
    fields.pop("current_procedure_id", None)
    with db.connection() as conn:
        owner_id, _created = registry.ensure_owner(conn, entry["user_name"])
        service_id = uuid.uuid4()
        registry.insert_service(
            conn,
            service_id=service_id,
            owner_id=owner_id,
            service_name=entry["service_name"],
            label=str(entry.get("label", "0")),
            recipe_path=entry.get("recipe_path", "."),
        )
        registry.insert_instance(
            conn,
            service_id,
            procedure_id=proc,
            status=status or entry.get("status") or "running",
            **fields,
        )
        if entry.get("task_id"):
            registry.insert_task(
                conn,
                task_id=str(entry["task_id"]),
                procedure_id=proc,
                service_id=service_id,
                kind="deploy",
                operation="register",
            )
    return str(service_id), str(proc)


def remove_user_service(user_name: str, service_name: str, label: str) -> bool:
    """Full delete (``scope=service``) by the active tuple; ``False`` if absent."""
    service_id = registry.resolve_service_id(
        user_name=user_name, service_name=service_name, label=str(label)
    )
    if service_id is None:
        return False
    with db.connection() as conn:
        registry.delete_instance(conn, uuid.UUID(service_id))
        conn.execute(
            "DELETE FROM registry.service_endpoints WHERE service_id = %s",
            (service_id,),
        )
        conn.execute(
            "DELETE FROM registry.service_basic WHERE service_id = %s", (service_id,)
        )
        registry.tombstone(conn, uuid.UUID(service_id))
    return True
