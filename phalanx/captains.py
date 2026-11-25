"""Captain registration, heartbeats, liveness, handoff and timeline actions."""

from datetime import UTC, datetime, timedelta
import json
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, StringConstraints

from . import intake
from .auth import authenticate_board_read, require_coordinator
from .intake_api import Key, run
from .intake_models import Identifier, Payload, Text
from .session_links import session_id as native_session_id, session_key


# A Captain whose last heartbeat is older than this is stale (D5).
STALE = timedelta(minutes=15)
# A Captain still ending this long after it started its handoff is treated as dead.
ENDING_LIMIT = timedelta(minutes=30)
IDLE_WITH_WORK = timedelta(minutes=30)
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CaptainState = Literal["active", "ending", "ended"]
# A Captain moves only forward: active, then ending, then ended.
ORDER = {"active": 0, "ending": 1, "ended": 2}

router = APIRouter(prefix="/v1")


class Pickup(Payload):
    repositories: dict[Identifier, Text | None] = Field(default_factory=dict, max_length=100)
    runner_id: Identifier | None = None


class CaptainRegistration(Payload):
    session_ref: Text
    charter_revision: Short
    parent_id: Identifier | None = None
    pickup: Pickup = Field(default_factory=Pickup)


class Session(Payload):
    """A native session the Captain can see, by its runtime ID and display name."""
    id: Short
    name: Short
    writer_nonce: Short | None = None
    availability: Literal["present", "missing", "deleted"] | None = None
    runtime: Short | None = None


class Heartbeat(Payload):
    charter_revision: Short
    wake_count: Annotated[int, Field(strict=True, ge=0)]
    compaction_count: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)] | None = None
    state: CaptainState
    focus: list[Identifier] = Field(default_factory=list, max_length=100)
    sessions: list[Session] = Field(default_factory=list, max_length=500)
    handoff_note: Text | None = None
    pickup: Pickup | None = None


class Action(Payload):
    captain_id: Identifier
    action: Short
    reason: Text


class SessionArchive(Payload):
    session_id: Short
    outcome: Literal["archived", "blocked", "refused"]
    reason: Text
    reported_by: Short


def archive_status(db, session_id):
    row = db.execute("SELECT * FROM session_archives WHERE session_id=?", (session_id,)).fetchone()
    writers = [dict(writer) for writer in db.execute(
        "SELECT id, item_id, execution_id, ended_at FROM writers WHERE session_id=?", (session_id,))]
    captains = [row for row in db.execute("SELECT * FROM captains")
                if native_session_id(row["session_ref"]) == session_id]
    return {
        "record": dict(row) if row else None,
        "writers": writers,
        "active": any(db.execute(
                      "SELECT 1 FROM writers WHERE item_id=? AND ended_at IS NULL LIMIT 1",
                      (writer["item_id"],)).fetchone() for writer in writers)
                  or any(captain["state"] != "ended" or open_intents(db, captain["id"])
                         or cleanup_pending(db, captain["id"])
                         or not ((captain["handoff_note"] or "").startswith("Stop condition met:")
                                 or db.execute("SELECT 1 FROM captains WHERE parent_id=? LIMIT 1",
                                               (captain["id"],)).fetchone())
                         for captain in captains),
    }


def cleanup_pending(db, captain_id):
    return db.execute(
        """SELECT count(*) FROM writers w LEFT JOIN session_archives a ON a.session_id=w.session_id
           WHERE w.captain_id=? AND w.session_id IS NOT NULL
             AND (w.ended_at IS NULL OR a.outcome IS NULL OR a.outcome='blocked')""",
        (captain_id,)).fetchone()[0]


def record_archive(db, payload):
    current = archive_status(db, payload.session_id)
    if current["active"] and payload.outcome == "archived":
        raise intake.Conflict("The session still owns a writer or an unfinished Captain handoff")
    previous = current["record"]
    if previous and previous["outcome"] == "archived":
        return previous
    if previous and previous["outcome"] == "refused" and payload.outcome != "archived":
        return previous
    db.execute(
        """INSERT INTO session_archives VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (session_id) DO UPDATE SET outcome=excluded.outcome, reason=excluded.reason,
               reported_by=excluded.reported_by, updated_at=excluded.updated_at""",
        (payload.session_id, payload.outcome, payload.reason, payload.reported_by, intake.timestamp()),
    )
    return archive_status(db, payload.session_id)["record"]


def captain_row(db, captain_id):
    row = db.execute("SELECT * FROM captains WHERE id=?", (captain_id,)).fetchone()
    if row is None:
        raise intake.Missing("Captain not found")
    return row


def gone_at(row):
    """When a Captain stops counting as live: a missed heartbeat, or a handoff that ran too long."""
    if row["state"] == "ended":
        return datetime.fromisoformat(row["heartbeat_at"])
    gone = datetime.fromisoformat(row["heartbeat_at"]) + STALE
    if row["ending_at"] is not None:
        gone = min(gone, datetime.fromisoformat(row["ending_at"]) + ENDING_LIMIT)
    return gone


def is_live(row, now):
    return row["state"] != "ended" and now < gone_at(row)


def active_holds(db, now):
    """Current-version holds remain advisory and expire with their Captain or after 30 minutes."""
    live = {row["id"] for row in db.execute("SELECT * FROM captains WHERE state != 'ended'")
            if is_live(row, now)}
    if not live:
        return {}
    return {
        row["execution_id"]: row["reason"]
        for row in db.execute(
            """SELECT action.* FROM captain_actions AS action
               JOIN executions AS execution ON execution.id = action.execution_id
               WHERE action.action = 'held' AND action.created_at > ?
                 AND execution.status = 'queued' AND action.version = execution.version
                 AND NOT EXISTS (
                     SELECT 1 FROM captain_actions AS latest
                     WHERE latest.execution_id = action.execution_id AND latest.version = action.version
                       AND (latest.created_at, latest.rowid) > (action.created_at, action.rowid))""",
            ((now - IDLE_WITH_WORK).isoformat(timespec="microseconds"),))
        if row["captain_id"] in live
    }


def open_intents(db, captain_id):
    """Writer intents this Captain launched and has neither confirmed, abandoned nor ended."""
    return [dict(row) for row in db.execute(
        """SELECT id AS writer_id, item_id, execution_id, nonce, created_at FROM writers
           WHERE captain_id=? AND confirmed_at IS NULL AND abandoned_at IS NULL AND ended_at IS NULL
           ORDER BY created_at, id""", (captain_id,))]


def availability_reports(db):
    """Latest provider-scoped native observations, including other Captains' sessions."""
    return {
        session_key(row["session_id"], row["runtime"] or session_key(row["session_ref"])[0]):
            row["availability"]
        for row in db.execute(
            """SELECT json_extract(session.value, '$.id') AS session_id,
                      json_extract(session.value, '$.availability') AS availability,
                      json_extract(session.value, '$.runtime') AS runtime, captains.session_ref
               FROM captains, json_each(captains.sessions_json) AS session
               WHERE json_extract(session.value, '$.availability') IN ('present', 'missing', 'deleted')
               ORDER BY captains.heartbeat_at, captains.rowid""")
    }


def view(db, row, now=None, session_reports=None):
    now = now or datetime.now(UTC)
    return {
        "id": row["id"], "session_ref": row["session_ref"], "charter_revision": row["charter_revision"],
        "pickup": json.loads(row["pickup_json"]),
        "session_availability": (availability_reports(db) if session_reports is None else session_reports).get(
            session_key(row["session_ref"]), "unverified"),
        "parent_id": row["parent_id"], "state": row["state"], "wake_count": row["wake_count"],
        "compaction_count": row["compaction_count"],
        "focus": json.loads(row["focus_json"]), "sessions": json.loads(row["sessions_json"]),
        "handoff_note": row["handoff_note"], "created_at": row["created_at"],
        "heartbeat_at": row["heartbeat_at"], "ending_at": row["ending_at"], "live": is_live(row, now),
        "stale": row["state"] != "ended" and not is_live(row, now),
        "open_intents": open_intents(db, row["id"]),
        "successors": [successor["id"] for successor in db.execute(
            "SELECT id FROM captains WHERE parent_id=? ORDER BY created_at, id", (row["id"],))],
        "successor_name": successor_name(row["id"]),
        "cleanup_pending": cleanup_pending(db, row["id"]),
        "ended_writers": [dict(writer) for writer in db.execute(
            """SELECT w.id, w.item_id, w.execution_id, w.session_id, w.ended_at, w.end_reason,
                      a.outcome AS archive_outcome, a.reason AS archive_reason
               FROM writers w LEFT JOIN session_archives a ON a.session_id=w.session_id
               WHERE w.captain_id=? AND w.ended_at IS NOT NULL AND w.session_id IS NOT NULL
                 AND (a.outcome IS NULL OR a.outcome='blocked')
               ORDER BY a.updated_at, w.ended_at, w.id LIMIT 100""", (row["id"],))],
    }


def successor_name(captain_id):
    """The session name for this Captain's successor. It is the launch nonce, so a rerun finds the same chat."""
    return f"phx captain {captain_id[:8]}"


def register(db, payload):
    if payload.parent_id is not None:
        captain_row(db, payload.parent_id)
    captain_id = uuid4().hex
    now = intake.timestamp()
    db.execute(
        """INSERT INTO captains (id, session_ref, charter_revision, parent_id, state, created_at, heartbeat_at,
                                 pickup_json)
           VALUES (?, ?, ?, ?, 'active', ?, ?, ?)""",
        (captain_id, payload.session_ref, payload.charter_revision, payload.parent_id, now, now,
         intake.encode(payload.pickup.model_dump())),
    )
    return view(db, captain_row(db, captain_id))


def heartbeat(db, captain_id, payload):
    row = captain_row(db, captain_id)
    if ORDER[payload.state] < ORDER[row["state"]]:
        raise intake.Conflict(f"A Captain that is {row['state']} cannot become {payload.state}. "
                              "Register a new Captain")
    now = intake.timestamp()
    if payload.pickup is not None:
        db.execute("UPDATE captains SET pickup_json=? WHERE id=?",
                   (intake.encode(payload.pickup.model_dump()), captain_id))
    elif payload.charter_revision != row["charter_revision"]:
        db.execute("UPDATE captains SET pickup_json=? WHERE id=?",
                   (intake.encode(Pickup().model_dump()), captain_id))
    # Each session keeps the time it was first reported, so its flags keep their first_seen.
    seen = {session["id"]: session["first_seen"] for session in json.loads(row["sessions_json"])}
    sessions = [{**session.model_dump(exclude_none=True), "first_seen": seen.get(session.id, now)}
                for session in payload.sessions]
    ending_at = row["ending_at"] or (now if payload.state == "ending" else None)
    # Native counters are cumulative for this session; retries and older reports cannot add or undo one.
    compaction_count = row["compaction_count"]
    if payload.compaction_count is not None:
        compaction_count = max(compaction_count or 0, payload.compaction_count)
    db.execute(
        """UPDATE captains SET charter_revision=?, wake_count=?, state=?, focus_json=?, sessions_json=?,
               handoff_note=?, heartbeat_at=?, ending_at=?, compaction_count=? WHERE id=?""",
        (payload.charter_revision, payload.wake_count, payload.state, intake.encode(payload.focus),
         intake.encode(sessions), payload.handoff_note, now, ending_at, compaction_count, captain_id),
    )
    return view(db, captain_row(db, captain_id))


def captains(db, limit):
    now = datetime.now(UTC)
    rows = db.execute(
        "SELECT * FROM captains ORDER BY state = 'ended', heartbeat_at DESC, id LIMIT ?", (limit,),
    ).fetchall()
    reports = availability_reports(db)
    return {"captains": [view(db, row, now, reports) for row in rows]}


def current_captain(db):
    """Resolve routing from all active registrations, never the recent-history page."""
    now = datetime.now(UTC)
    candidates = [
        {"id": row["id"], "session_ref": row["session_ref"]}
        for row in db.execute("SELECT * FROM captains WHERE state='active' ORDER BY created_at, id")
        if is_live(row, now)
    ]
    captain = None
    if not candidates:
        reason = "No live active Captain; ending, ended and stale registrations are not routing destinations"
    elif len(candidates) != 1:
        reason = f"{len(candidates)} live active Captains; no unique routing destination"
    elif not candidates[0]["session_ref"].strip():
        reason = "The live active Captain has no native session binding"
    else:
        captain = candidates[0]
        reason = "Unique live active registration; verify its session_ref and conversation link in the runtime"
    return {"captain": captain, "candidates": candidates, "reason": reason,
            "observed_at": now.isoformat(timespec="microseconds")}


def record_action(db, item_id, payload):
    """Put a Captain's action and its reason on the item's current execution timeline."""
    if captain_row(db, payload.captain_id)["state"] == "ended":
        raise intake.Conflict("An ended Captain cannot act")
    execution = intake.current_execution(db, item_id)
    action_id = uuid4().hex
    db.execute("INSERT INTO captain_actions VALUES (?, ?, ?, ?, ?, ?, ?)",
               (action_id, execution["id"], payload.captain_id, execution["version"], payload.action,
                payload.reason, intake.timestamp()))
    return {"id": action_id, "item_id": item_id, "execution_id": execution["id"],
            "version": execution["version"], **payload.model_dump()}


@router.post("/captains", status_code=201, dependencies=[Depends(require_coordinator)])
def create(request: Request, body: CaptainRegistration, key: Key = None):
    return run(request, register, body, write=True, scope="captain", key=key,
               payload=body.model_dump(mode="json"))


@router.put("/captains/{captain_id}", dependencies=[Depends(require_coordinator)])
def update(request: Request, captain_id: Identifier, body: Heartbeat):
    return run(request, heartbeat, captain_id, body, write=True)


@router.post("/items/{item_id}/captain-actions", status_code=201, dependencies=[Depends(require_coordinator)])
def post_action(request: Request, item_id: Identifier, body: Action, key: Key = None):
    return run(request, record_action, item_id, body, write=True, scope=f"captain-action:{item_id}", key=key,
               payload=body.model_dump(mode="json"))


@router.get("/captains", dependencies=[Depends(authenticate_board_read)])
def list_captains(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 25):
    return run(request, captains, limit)


@router.get("/captains/current", dependencies=[Depends(authenticate_board_read)])
def get_current_captain(request: Request):
    return run(request, current_captain)


@router.get("/captains/{captain_id}", dependencies=[Depends(authenticate_board_read)])
def get_captain(request: Request, captain_id: Identifier):
    return run(request, lambda db: view(db, captain_row(db, captain_id)))


@router.get("/session-archives", dependencies=[Depends(require_coordinator)])
def get_archive(request: Request, session_id: Short):
    return run(request, archive_status, session_id)


@router.put("/session-archives", dependencies=[Depends(require_coordinator)])
def put_archive(request: Request, body: SessionArchive):
    return run(request, record_archive, body, write=True)
