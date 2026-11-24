"""Server-owned direct dispatch settings."""


def active_writers(db):
    return db.execute(
        """SELECT w.id, w.item_id, w.session_id FROM writers w
           WHERE w.ended_at IS NULL
             AND EXISTS (SELECT 1 FROM executions e WHERE e.id=w.execution_id
                         AND e.status NOT IN ('completed', 'failed', 'cancelled'))
           ORDER BY w.created_at, w.id"""
    ).fetchall()


def settings(db):
    row = db.execute("SELECT * FROM dispatch_settings WHERE id=1").fetchone()
    return {"paused": bool(row["paused"]), "aging_seconds": row["aging_seconds"],
            "max_writers": row["max_writers"], "active_writers": len(active_writers(db))}


def set_dispatch(db, payload):
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        columns = ", ".join(f"{name}=?" for name in changes)
        db.execute(f"UPDATE dispatch_settings SET {columns} WHERE id=1", tuple(changes.values()))
    return settings(db)
