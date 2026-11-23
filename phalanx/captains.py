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


# A Captain whose last heartbeat is older than this is stale (D5).
STALE = timedelta(minutes=15)
# A Captain still ending this long after it started its handoff is treated as dead.
ENDING_LIMIT = timedelta(minutes=30)
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CaptainState = Literal["active", "ending", "ended"]
# A Captain moves only forward: active, then ending, then ended.
ORDER = {"active": 0, "ending": 1, "ended": 2}

router = APIRouter(prefix="/v1")


class CaptainRegistration(Payload):
    session_ref: Text
    charter_revision: Short
    parent_id: Identifier | None = None


class Session(Payload):
    """A native session the Captain can see, by its runtime ID and display name."""
    id: Short
    name: Short


class Heartbeat(Payload):
    charter_revision: Short
    wake_count: Annotated[int, Field(strict=True, ge=0)]
    state: CaptainState
    focus: list[Identifier] = Field(default_factory=list, max_length=100)
    sessions: list[Session] = Field(default_factory=list, max_length=500)
    handoff_note: Text | None = None


class Action(Payload):
    captain_id: Identifier
    action: Short
    reason: Text


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


def open_intents(db, captain_id):
    """Writer intents this Captain launched and has neither confirmed, abandoned nor ended."""
    return [dict(row) for row in db.execute(
        """SELECT id AS writer_id, item_id, execution_id, nonce, created_at FROM writers
           WHERE captain_id=? AND confirmed_at IS NULL AND abandoned_at IS NULL AND ended_at IS NULL
           ORDER BY created_at, id""", (captain_id,))]


def view(db, row, now=None):
    now = now or datetime.now(UTC)
    return {
        "id": row["id"], "session_ref": row["session_ref"], "charter_revision": row["charter_revision"],
        "parent_id": row["parent_id"], "state": row["state"], "wake_count": row["wake_count"],
        "focus": json.loads(row["focus_json"]), "sessions": json.loads(row["sessions_json"]),
        "handoff_note": row["handoff_note"], "created_at": row["created_at"],
        "heartbeat_at": row["heartbeat_at"], "ending_at": row["ending_at"], "live": is_live(row, now),
        "stale": row["state"] != "ended" and not is_live(row, now),
        "open_intents": open_intents(db, row["id"]),
        "successors": [successor["id"] for successor in db.execute(
            "SELECT id FROM captains WHERE parent_id=? ORDER BY created_at, id", (row["id"],))],
        "successor_name": successor_name(row["id"]),
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
        """INSERT INTO captains (id, session_ref, charter_revision, parent_id, state, created_at, heartbeat_at)
           VALUES (?, ?, ?, ?, 'active', ?, ?)""",
        (captain_id, payload.session_ref, payload.charter_revision, payload.parent_id, now, now),
    )
    return view(db, captain_row(db, captain_id))


def heartbeat(db, captain_id, payload):
    row = captain_row(db, captain_id)
    if ORDER[payload.state] < ORDER[row["state"]]:
        raise intake.Conflict(f"A Captain that is {row['state']} cannot become {payload.state}. "
                              "Register a new Captain")
    now = intake.timestamp()
    # Each session keeps the time it was first reported, so its flags keep their first_seen.
    seen = {session["id"]: session["first_seen"] for session in json.loads(row["sessions_json"])}
    sessions = [{**session.model_dump(), "first_seen": seen.get(session.id, now)}
                for session in payload.sessions]
    ending_at = row["ending_at"] or (now if payload.state == "ending" else None)
    db.execute(
        """UPDATE captains SET charter_revision=?, wake_count=?, state=?, focus_json=?, sessions_json=?,
               handoff_note=?, heartbeat_at=?, ending_at=? WHERE id=?""",
        (payload.charter_revision, payload.wake_count, payload.state, intake.encode(payload.focus),
         intake.encode(sessions), payload.handoff_note, now, ending_at, captain_id),
    )
    return view(db, captain_row(db, captain_id))


def captains(db, limit):
    now = datetime.now(UTC)
    rows = db.execute(
        "SELECT * FROM captains ORDER BY state = 'ended', heartbeat_at DESC, id LIMIT ?", (limit,),
    ).fetchall()
    return {"captains": [view(db, row, now) for row in rows]}


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


@router.get("/captains/{captain_id}", dependencies=[Depends(authenticate_board_read)])
def get_captain(request: Request, captain_id: Identifier):
    return run(request, lambda db: view(db, captain_row(db, captain_id)))
