"""Server-owned direct dispatch settings."""

from time import time


def needs_building(stage):
    return stage not in ("publication", "delivery")


def active_writers(db):
    return db.execute(
        """SELECT w.id, w.item_id, w.session_id, w.building FROM writers w
           WHERE w.ended_at IS NULL
             AND EXISTS (SELECT 1 FROM executions e WHERE e.id=w.execution_id
                         AND e.status NOT IN ('completed', 'failed', 'cancelled'))
           ORDER BY w.created_at, w.id"""
    ).fetchall()


def settings(db):
    row = db.execute("SELECT * FROM dispatch_settings WHERE id=1").fetchone()
    active = active_writers(db)
    building = sum(writer["building"] for writer in active)
    return {"paused": bool(row["paused"]), "aging_seconds": row["aging_seconds"],
            "max_writers": row["max_writers"], "active_writers": len(active),
            "max_building_writers": row["max_building_writers"], "building_writers": building,
            "delivering_writers": len(active) - building}


def first_rework(db):
    """Only runnable registered PR fixes get preference; blocked requests cannot starve intake."""
    return db.execute(
        """SELECT w.id FROM writers w JOIN executions e ON e.id=w.execution_id
           WHERE w.ended_at IS NULL AND w.building=0 AND w.building_requested_at IS NOT NULL
             AND json_extract(e.state_json, '$.primary_pr') IS NOT NULL
             AND e.status IN ('queued', 'running', 'waiting_for_external')
             AND json_extract(e.state_json, '$.stopping') IS NULL
             AND json_extract(e.state_json, '$.pending_output') IS NULL
             AND e.dependency_wait_json IS NULL AND (e.retry_at IS NULL OR e.retry_at <= ?)
           ORDER BY w.building_requested_at, w.id LIMIT 1""", (int(time() * 1000),)
    ).fetchone()


def building_wait(db, writer_id=None):
    capacity = settings(db)
    if capacity["paused"]:
        return "Dispatch is paused"
    if capacity["building_writers"] >= capacity["max_building_writers"]:
        return f"Building capacity exhausted: {capacity['building_writers']}/{capacity['max_building_writers']}"
    first = first_rework(db)
    if first is not None and first["id"] != writer_id:
        return f"Building capacity reserved for waiting PR rework writer {first['id']}"
    return None


def admission_wait(db, stage):
    capacity = settings(db)
    if capacity["paused"]:
        return "Dispatch is paused"
    if capacity["active_writers"] >= capacity["max_writers"]:
        return f"Total capacity exhausted: max_writers={capacity['max_writers']}"
    return building_wait(db) if needs_building(stage) else None


def set_dispatch(db, payload):
    from . import intake

    changes = payload.model_dump(exclude_unset=True)
    current = settings(db)
    building = changes.get("max_building_writers", current["max_building_writers"])
    total = changes.get("max_writers", current["max_writers"])
    if building > total:
        raise intake.Conflict("Building writers must be less than or equal to Total writers; "
                              "update both limits together")
    if changes:
        columns = ", ".join(f"{name}=?" for name in changes)
        db.execute(f"UPDATE dispatch_settings SET {columns} WHERE id=1", tuple(changes.values()))
    return settings(db)
