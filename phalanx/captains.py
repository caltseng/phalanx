"""Captain registration, heartbeats and liveness."""

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
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CaptainState = Literal["active", "ending", "ended"]

router = APIRouter(prefix="/v1")


class CaptainRegistration(Payload):
    session_ref: Text
    charter_revision: Short
    parent_id: Identifier | None = None


class Heartbeat(Payload):
    charter_revision: Short
    wake_count: Annotated[int, Field(strict=True, ge=0)]
    state: CaptainState
    focus: list[Identifier] = Field(default_factory=list, max_length=100)
    sessions: list[Short] = Field(default_factory=list, max_length=500)
    handoff_note: Text | None = None


def captain_row(db, captain_id):
    row = db.execute("SELECT * FROM captains WHERE id=?", (captain_id,)).fetchone()
    if row is None:
        raise intake.Missing("Captain not found")
    return row


def is_live(row, now):
    return row["state"] != "ended" and now - datetime.fromisoformat(row["heartbeat_at"]) < STALE


def view(row, now=None):
    now = now or datetime.now(UTC)
    return {
        "id": row["id"], "session_ref": row["session_ref"], "charter_revision": row["charter_revision"],
        "parent_id": row["parent_id"], "state": row["state"], "wake_count": row["wake_count"],
        "focus": json.loads(row["focus_json"]), "sessions": json.loads(row["sessions_json"]),
        "handoff_note": row["handoff_note"], "created_at": row["created_at"],
        "heartbeat_at": row["heartbeat_at"], "live": is_live(row, now),
        "stale": row["state"] != "ended" and not is_live(row, now),
    }


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
    return view(captain_row(db, captain_id))


def heartbeat(db, captain_id, payload):
    row = captain_row(db, captain_id)
    if row["state"] == "ended" and payload.state != "ended":
        raise intake.Conflict("An ended Captain cannot resume. Register a new Captain")
    db.execute(
        """UPDATE captains SET charter_revision=?, wake_count=?, state=?, focus_json=?, sessions_json=?,
               handoff_note=?, heartbeat_at=? WHERE id=?""",
        (payload.charter_revision, payload.wake_count, payload.state, intake.encode(payload.focus),
         intake.encode(payload.sessions), payload.handoff_note, intake.timestamp(), captain_id),
    )
    return view(captain_row(db, captain_id))


def captains(db, limit):
    now = datetime.now(UTC)
    rows = db.execute(
        "SELECT * FROM captains ORDER BY state = 'ended', heartbeat_at DESC, id LIMIT ?", (limit,),
    )
    return {"captains": [view(row, now) for row in rows]}


@router.post("/captains", status_code=201, dependencies=[Depends(require_coordinator)])
def create(request: Request, body: CaptainRegistration, key: Key = None):
    return run(request, register, body, write=True, scope="captain", key=key,
               payload=body.model_dump(mode="json"))


@router.put("/captains/{captain_id}", dependencies=[Depends(require_coordinator)])
def update(request: Request, captain_id: Identifier, body: Heartbeat):
    return run(request, heartbeat, captain_id, body, write=True)


@router.get("/captains", dependencies=[Depends(authenticate_board_read)])
def list_captains(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 25):
    return run(request, captains, limit)


@router.get("/captains/{captain_id}", dependencies=[Depends(authenticate_board_read)])
def get_captain(request: Request, captain_id: Identifier):
    return run(request, lambda db: view(captain_row(db, captain_id)))
