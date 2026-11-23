"""Deviation flags: stored flags raised by the reconciler or a Captain, plus server-derived ones.

Flags never block work. A stored flag is cleared by whoever raised it. A derived flag is computed
on read, so it clears itself when its condition clears.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import StringConstraints

from . import intake, reconciliation
from .auth import authenticate_board_read, require_coordinator
from .captains import STALE, is_live
from .intake_api import run
from .intake_models import Payload, Text
from .operator_schema import OPEN


Kind = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Subject = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{0,100}$")]
IDLE_WITH_WORK = timedelta(minutes=30)
LAUNCH_UNCONFIRMED = timedelta(minutes=10)
NO_PROGRESS = timedelta(hours=2)
DERIVED_LIMIT = 200

router = APIRouter(prefix="/v1")


class FlagKey(Payload):
    kind: Kind
    item_id: Subject = ""
    execution_id: Subject = ""
    captain_id: Subject = ""


class Flag(FlagKey):
    reason: Text
    recovery: Text
    notify_owner: bool = False


def flag(kind, reason, recovery, first_seen, *, item_id="", execution_id="", captain_id="",
         notify_owner=False):
    return {"kind": kind, "item_id": item_id, "execution_id": execution_id, "captain_id": captain_id,
            "reason": reason, "recovery": recovery, "first_seen": first_seen.isoformat(timespec="microseconds"),
            "notify_owner": notify_owner, "source": "server"}


def at(value):
    return datetime.fromisoformat(value)


def captain_stale(db, now):
    return [
        flag("captain_stale", f"No heartbeat since {row['heartbeat_at']}",
             "Live Captains take over its intents. The successor or keeper archives it.",
             at(row["heartbeat_at"]) + STALE, captain_id=row["id"])
        for row in db.execute(
            "SELECT * FROM captains WHERE state != 'ended' AND heartbeat_at < ? ORDER BY heartbeat_at LIMIT ?",
            ((now - STALE).isoformat(timespec="microseconds"), DERIVED_LIMIT),
        )
    ]


def no_live_captain(db, now):
    oldest = db.execute(f"SELECT min(created_at) FROM executions WHERE {OPEN}").fetchone()[0]
    if oldest is None:
        return []
    rows = db.execute("SELECT * FROM captains WHERE state != 'ended' OR id = "
                      "(SELECT id FROM captains ORDER BY heartbeat_at DESC, id LIMIT 1)").fetchall()
    if any(is_live(row, now) for row in rows):
        return []
    # The last moment any Captain was live, or when open work first existed, whichever is later.
    gone = [at(row["heartbeat_at"]) + (STALE if row["state"] != "ended" else timedelta()) for row in rows]
    first_seen = max([at(oldest), *gone])
    last = max(rows, key=lambda row: (row["heartbeat_at"], row["id"]), default=None)
    reason = "No live Captain while open work exists"
    if last is not None:
        reason += f". Last Captain {last['id']} ({last['session_ref']}) heartbeat {last['heartbeat_at']}"
        if last["handoff_note"]:
            reason += f". Handoff note: {last['handoff_note']}"
    return [flag("no_live_captain", reason[:16000], "The keeper starts a Captain, or the owner does",
                 first_seen, captain_id=last["id"] if last else "", notify_owner=True)]


def captain_idle_with_work(db, now):
    if not any(is_live(row, now) for row in db.execute("SELECT * FROM captains WHERE state != 'ended'")):
        return []
    cutoff = int((now - IDLE_WITH_WORK).timestamp() * 1000)
    return [
        flag("captain_idle_with_work", "Runnable for 30 min with no registered writer",
             "A Captain launches it, or the charter explains why not",
             datetime.fromtimestamp(row["ready_since"] / 1000, UTC) + IDLE_WITH_WORK,
             item_id=row["item_id"], execution_id=row["id"])
        for row in db.execute(
            """SELECT id, item_id, ready_since FROM executions AS execution
               WHERE status = 'queued' AND ready_since <= ? AND dependency_wait_json IS NULL
                 AND NOT EXISTS (SELECT 1 FROM writers
                                 WHERE execution_id = execution.id AND ended_at IS NULL)
               ORDER BY ready_since, id LIMIT ?""",
            (cutoff, DERIVED_LIMIT),
        )
    ]


OPEN_WRITERS = """
    SELECT writer.*, progress.last_advanced_at FROM writers AS writer
    JOIN executions AS execution ON execution.id = writer.execution_id
    JOIN operator_progress AS progress ON progress.execution_id = writer.execution_id
    WHERE writer.ended_at IS NULL AND execution.status NOT IN ('completed', 'failed', 'cancelled')
"""


def writer_flags(db, now):
    flags = []
    for row in db.execute(OPEN_WRITERS + " ORDER BY writer.created_at, writer.id LIMIT ?", (DERIVED_LIMIT,)):
        subject = {"item_id": row["item_id"], "execution_id": row["execution_id"],
                   "captain_id": row["captain_id"]}
        if row["confirmed_at"] is None and now - at(row["created_at"]) >= LAUNCH_UNCONFIRMED:
            flags.append(flag(
                "launch_unconfirmed", f"Writer intent {row['nonce']} unconfirmed since {row['created_at']}",
                "Search native sessions for the nonce, then confirm or withdraw",
                at(row["created_at"]) + LAUNCH_UNCONFIRMED, **subject))
        if row["state"] == "busy":
            moved = max(at(value) for value in (row["last_progress_at"], row["last_advanced_at"],
                                                row["confirmed_at"]) if value)
            if now - moved >= NO_PROGRESS:
                flags.append(flag(
                    "no_progress", f"Busy with no progress or stage advance since {moved.isoformat()}",
                    "Check the session, then nudge or replace it", moved + NO_PROGRESS, **subject))
        if row["state"] in ("stopped", "error"):
            flags.append(flag(
                "writer_stopped", f"Writer reported {row['state']} without a result",
                "Resume the same session or replace the writer",
                at(row["state_reported_at"] or row["created_at"]), **subject))
    return flags


# Each function is fn(db, now) -> list of flags in the GET /v1/flags shape.
DERIVED = (captain_stale, no_live_captain, captain_idle_with_work, writer_flags, *reconciliation.DERIVED_FLAGS)


def raise_flag(db, payload):
    now = intake.timestamp()
    db.execute(
        """INSERT INTO flags VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (kind, item_id, execution_id, captain_id) DO UPDATE SET
               reason=excluded.reason, recovery=excluded.recovery,
               notify_owner=excluded.notify_owner, updated_at=excluded.updated_at""",
        (payload.kind, payload.item_id, payload.execution_id, payload.captain_id, payload.reason,
         payload.recovery, int(payload.notify_owner), now, now),
    )
    row = db.execute("SELECT * FROM flags WHERE kind=? AND item_id=? AND execution_id=? AND captain_id=?",
                     (payload.kind, payload.item_id, payload.execution_id, payload.captain_id)).fetchone()
    return stored(row)


def clear_flag(db, payload):
    cursor = db.execute("DELETE FROM flags WHERE kind=? AND item_id=? AND execution_id=? AND captain_id=?",
                        (payload.kind, payload.item_id, payload.execution_id, payload.captain_id))
    return {"cleared": cursor.rowcount > 0}


def stored(row):
    return {"kind": row["kind"], "item_id": row["item_id"], "execution_id": row["execution_id"],
            "captain_id": row["captain_id"], "reason": row["reason"], "recovery": row["recovery"],
            "first_seen": row["first_seen"], "notify_owner": bool(row["notify_owner"]), "source": "stored"}


def all_flags(db, now=None):
    now = now or datetime.now(UTC)
    flags = [stored(row) for row in db.execute("SELECT * FROM flags")]
    for derive in DERIVED:
        flags.extend(derive(db, now))
    return {"flags": sorted(flags, key=lambda item: (item["first_seen"], item["kind"]))}


@router.put("/flags", dependencies=[Depends(require_coordinator)])
def put_flag(request: Request, body: Flag):
    return run(request, raise_flag, body, write=True)


@router.post("/flags/clear", dependencies=[Depends(require_coordinator)])
def post_clear(request: Request, body: FlagKey):
    return run(request, clear_flag, body, write=True)


@router.get("/flags", dependencies=[Depends(authenticate_board_read)])
def get_flags(request: Request):
    return run(request, all_flags)
