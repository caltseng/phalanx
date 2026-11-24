"""Writer registration: one native worker session per open execution, fenced by a writer token.

A writer claims a named execution, never the queue head. All launches enforce the persisted
writer cap; dispatch pause also refuses new writers (D6). The writer keeps its execution across
stages: each accepted stage result opens the next stage's attempt under the same writer.
"""

from datetime import UTC, datetime
from time import time
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StringConstraints

from . import captains, dispatch, intake, intake_api
from .auth import authenticate, require_coordinator, require_writer
from .flags import all_flags
from .intake_models import Identifier, Payload, Text, Versioned
from .pr_models import SHA
from .transitions import TERMINAL, Event, Stage, Status


# Writer attempts have no timed expiry. The writer token is the fence.
WRITER_RUNNER_ID = "@writer"
NO_EXPIRY = 253402300799

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Nonce = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,100}$")]
TokenDigest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Url = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9+.-]{0,31}://\S{1,2000}$")]

router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])


class Registration(Versioned):
    captain_id: Identifier
    runtime: Short
    nonce: Nonce
    token_digest: TokenDigest
    # The open writer this registration replaces. Its Captain attests the old session is stopped.
    replaces: Identifier | None = None
    replaced_stopped: bool = False


class Update(Payload):
    captain_id: Identifier
    writer_id: Identifier
    session_id: Short | None = None
    session_url: Url | None = None
    state: Literal["busy", "idle", "stopped", "error"] | None = None
    # The owning Captain hands its unconfirmed intent to any live Captain.
    abandon: bool = False


class Withdrawal(Payload):
    captain_id: Identifier
    writer_id: Identifier
    reason: Text
    # Required once the writer was confirmed: its session is stopped.
    stopped: bool = False


class Progress(Payload):
    note: Text | None = None


class Capacity(Payload):
    phase: Literal["building", "delivering"]
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    handoff_head: SHA | None = None


def writer_row(db, writer_id):
    row = db.execute("SELECT * FROM writers WHERE id=?", (writer_id,)).fetchone()
    if row is None:
        raise intake.Missing("Writer not found")
    return row


def open_writer(db, execution_id):
    return db.execute("SELECT * FROM writers WHERE execution_id=? AND ended_at IS NULL",
                      (execution_id,)).fetchone()


def binding(db, writer):
    execution = intake.execution_row(db, writer["item_id"], writer["execution_id"])
    return {
        **intake.execution_view(execution),
        "item_id": writer["item_id"],
        "last_reply": intake.latest_reply(db, execution["id"]),
        "writer": {
            "id": writer["id"], "captain_id": writer["captain_id"], "runtime": writer["runtime"],
            "nonce": writer["nonce"], "state": "ended" if writer["ended_at"] else writer["state"],
            "attempt_id": writer["attempt_id"], "replaced_id": writer["replaced_id"],
            "session_id": writer["session_id"], "session_url": writer["session_url"],
            "created_at": writer["created_at"], "confirmed_at": writer["confirmed_at"],
            "abandoned_at": writer["abandoned_at"], "state_reported_at": writer["state_reported_at"],
            "last_progress_at": writer["last_progress_at"], "progress_note": writer["progress_note"],
            "ended_at": writer["ended_at"], "end_reason": writer["end_reason"],
            "building": bool(writer["building"]),
            "building_requested_at": writer["building_requested_at"],
            "capacity_revision": writer["capacity_revision"],
            "handoff_head": execution["handoff_head"],
            "capacity_wait": (dispatch.building_wait(db, writer["id"]) or
                              "Building capacity available; retry acquisition"
                              if writer["building_requested_at"] and not writer["building"] else None),
        },
    }


def live_captain(db, captain_id):
    captain = captains.captain_row(db, captain_id)
    if captain["state"] == "ended":
        raise intake.Conflict("An ended Captain cannot act on writers")
    return captain


def claim_next(db, writer, *, continued_from=None):
    """Open the current stage's attempt when the execution is queued and ready, else bind none."""
    row = intake.execution_row(db, writer["item_id"], writer["execution_id"])
    state = intake.STATE.validate_json(row["state_json"])
    now = time()
    claim = None
    if (state.status == Status.QUEUED and state.stopping is None and state.pending_output is None
            and row["ready_since"] is not None and row["ready_since"] <= int(now * 1000)
            and not db.execute("SELECT paused FROM dispatch_settings WHERE id=1").fetchone()[0]):
        if not dispatch.needs_building(state.stage) or acquire_building(db, writer):
            claim = intake.claim_attempt(
                db, row, state, runner_id=WRITER_RUNNER_ID, lease_digest=writer["token_digest"],
                lease_expires_at=NO_EXPIRY, now=now, continued_from=continued_from,
            )
    db.execute("UPDATE writers SET attempt_id=? WHERE id=?",
               (claim["attempt_id"] if claim else None, writer["id"]))
    return claim


def acquire_building(db, writer):
    if writer["building"]:
        return True
    if writer["building_requested_at"] is None:
        db.execute("""UPDATE writers SET building_requested_at=?, capacity_revision=capacity_revision+1
                      WHERE id=?""", (intake.timestamp(), writer["id"]))
    if dispatch.building_wait(db, writer["id"]):
        return False
    db.execute("""UPDATE writers SET building=1, building_requested_at=NULL,
                  capacity_revision=capacity_revision+1 WHERE id=?""", (writer["id"],))
    return True


def release_building(db, writer_id):
    db.execute("""UPDATE writers SET building=0, building_requested_at=NULL,
                  capacity_revision=capacity_revision+1
                  WHERE id=? AND (building=1 OR building_requested_at IS NOT NULL)""", (writer_id,))


def capacity(db, writer_id, payload):
    writer = writer_row(db, writer_id)
    if writer["ended_at"] is not None:
        raise intake.Conflict("The writer has ended")
    if writer["capacity_revision"] != payload.expected_revision:
        raise intake.Conflict("Writer capacity changed; read status before retrying")
    execution = intake.execution_row(db, writer["item_id"], writer["execution_id"])
    state = intake.STATE.validate_json(execution["state_json"])
    if state.status in TERMINAL or state.stopping is not None or state.pending_output is not None:
        raise intake.Conflict("The execution is closed or closing")
    if payload.phase == "delivering":
        if dispatch.needs_building(state.stage):
            raise intake.Conflict("Development, self-review and report work must retain Building capacity")
        if writer["building"]:
            if payload.handoff_head is None:
                raise intake.Conflict("Returning to delivery requires the reviewed commit's handoff_head")
            db.execute("UPDATE executions SET handoff_head=? WHERE id=?",
                       (payload.handoff_head, execution["id"]))
        release_building(db, writer_id)
    else:
        if (state.status == Status.WAITING_FOR_INPUT or execution["dependency_wait_json"] is not None
                or execution["retry_at"] is not None and execution["retry_at"] > int(time() * 1000)
                or state.status == Status.QUEUED and (
                    execution["ready_since"] is None or execution["ready_since"] > int(time() * 1000))):
            raise intake.Conflict("Resolve input, dependencies or scheduling wait before starting building work")
        acquire_building(db, writer)
    return binding(db, writer_row(db, writer_id))


def release(db, writer, reason):
    """End a writer whose session the Captain attests is stopped or never launched.

    A pending owner stop is confirmed on the stopped session's behalf. Otherwise the open attempt
    is requeued for the next writer.
    """
    row = intake.execution_row(db, writer["item_id"], writer["execution_id"])
    state = intake.STATE.validate_json(row["state_json"])
    attempt = db.execute("SELECT id FROM attempts WHERE execution_id=? AND ended_at IS NULL",
                         (row["id"],)).fetchone()
    details = {"reason": f"Writer released: {reason}", "writer_id": writer["id"]}
    if attempt is not None and attempt["id"] == writer["attempt_id"]:
        if state.stopping is not None:
            intake.confirm_stop(db, row, state, attempt["id"], details=details)
        else:
            intake.apply(db, row, state, Event.RETRY, attempt_id=attempt["id"], retry_allowed=True,
                         details=details)
    release_building(db, writer["id"])
    db.execute("UPDATE writers SET ended_at=?, end_reason=?, attempt_id=NULL WHERE id=?",
               (intake.timestamp(), reason, writer["id"]))


def register(db, item_id, execution_id, payload):
    replay = db.execute("SELECT * FROM writers WHERE nonce=?", (payload.nonce,)).fetchone()
    if replay is not None:
        if (replay["item_id"], replay["execution_id"], replay["captain_id"], replay["runtime"],
                replay["token_digest"], replay["replaced_id"]) != (
                item_id, execution_id, payload.captain_id, payload.runtime, payload.token_digest,
                payload.replaces):
            raise intake.Conflict("This nonce was already used for a different registration")
        return binding(db, replay)
    live_captain(db, payload.captain_id)
    if db.execute("SELECT paused FROM dispatch_settings WHERE id=1").fetchone()["paused"]:
        raise intake.Conflict("Dispatch is paused. No new writers are registered until it resumes")
    _, state = intake.check_current(db, item_id, execution_id, payload.expected_version)
    current = open_writer(db, execution_id)
    if state.status in TERMINAL or state.stopping is not None or state.pending_output is not None:
        if current is not None and state.stopping is not None:
            raise intake.Conflict(f"The execution is closing. Withdraw writer {current['id']} with "
                                  "stopped=true to confirm the stop")
        raise intake.Conflict("The execution is closed or closing")
    if current is not None:
        if payload.replaces != current["id"]:
            raise intake.Conflict(f"Execution already has writer {current['id']}. Name it in replaces "
                                  "to replace it")
        if not payload.replaced_stopped:
            raise intake.Conflict("Replacing a writer requires attesting that its session is stopped")
        release(db, current, "replaced")
    elif payload.replaces is not None:
        raise intake.Conflict("The writer to replace is no longer registered")
    active = dispatch.active_writers(db)
    # A stopped replacement transfers its exact allocation, even after limits are lowered.
    waiting = dispatch.admission_wait(db, state.stage) if current is None else None
    if waiting:
        names = ", ".join(f"{writer['id']} (item {writer['item_id']}, "
                          f"session {writer['session_id'] or 'unconfirmed'})" for writer in active)
        raise intake.Conflict(f"Wait: dispatch {waiting}; "
                              f"active writers: {names}. Retry when a writer ends; no escalation needed")
    state = intake.STATE.validate_json(intake.execution_row(db, item_id, execution_id)["state_json"])
    if state.status == Status.RUNNING:
        raise intake.Conflict("A runner attempt holds this execution")
    writer_id = uuid4().hex
    db.execute(
        """INSERT INTO writers (id, execution_id, item_id, captain_id, runtime, nonce, token_digest,
               replaced_id, state, created_at, building, building_requested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'launching', ?, ?, ?)""",
        (writer_id, execution_id, item_id, payload.captain_id, payload.runtime, payload.nonce,
         payload.token_digest, payload.replaces, intake.timestamp(),
         current["building"] if current is not None else int(dispatch.needs_building(state.stage)),
         current["building_requested_at"] if current is not None else None),
    )
    claim_next(db, writer_row(db, writer_id))
    return binding(db, writer_row(db, writer_id))


def bound_writer(db, item_id, execution_id, writer_id):
    writer = writer_row(db, writer_id)
    if writer["item_id"] != item_id or writer["execution_id"] != execution_id:
        raise intake.Missing("Writer not found")
    return writer


def authorize(db, writer, captain_id):
    """Only the owning Captain acts on an unconfirmed intent until it ends, goes stale or abandons it.

    Returns the Captain that owns the writer afterwards.
    """
    live_captain(db, captain_id)
    if writer["captain_id"] == captain_id or writer["confirmed_at"] is not None:
        return writer["captain_id"]
    owner = captains.captain_row(db, writer["captain_id"])
    if writer["abandoned_at"] is None and captains.is_live(owner, datetime.now(UTC)):
        raise intake.Conflict(f"Captain {owner['id']} owns this unconfirmed intent")
    return captain_id


def update(db, item_id, execution_id, payload):
    writer = bound_writer(db, item_id, execution_id, payload.writer_id)
    if writer["ended_at"] is not None:
        raise intake.Conflict("The writer has ended")
    owner = authorize(db, writer, payload.captain_id)
    if payload.abandon and payload.captain_id != writer["captain_id"]:
        raise intake.Conflict("Only the owning Captain abandons its intent")
    if payload.session_id is None and payload.session_url is not None and writer["session_id"] is None:
        raise intake.Conflict("Confirm the native session ID with its link")
    if payload.session_id is not None and writer["session_id"] not in (None, payload.session_id):
        raise intake.Conflict("The writer is already confirmed with another native session")
    now = intake.timestamp()
    confirming = payload.session_id is not None and writer["session_id"] is None
    state = payload.state or ("busy" if confirming else writer["state"])
    db.execute(
        """UPDATE writers SET captain_id=?, session_id=coalesce(?, session_id),
               session_url=coalesce(?, session_url), confirmed_at=coalesce(confirmed_at, ?),
               abandoned_at=CASE WHEN ? THEN coalesce(abandoned_at, ?) ELSE abandoned_at END,
               state=?, state_reported_at=CASE WHEN ? THEN ? ELSE state_reported_at END
           WHERE id=?""",
        (owner, payload.session_id, payload.session_url, now if confirming else None,
         payload.abandon, now, state, payload.state is not None, now, writer["id"]),
    )
    return binding(db, writer_row(db, writer["id"]))


def withdraw(db, item_id, execution_id, payload):
    writer = bound_writer(db, item_id, execution_id, payload.writer_id)
    if writer["ended_at"] is not None:
        return binding(db, writer)
    authorize(db, writer, payload.captain_id)
    if writer["confirmed_at"] is not None and not payload.stopped:
        raise intake.Conflict("Withdrawing a confirmed writer requires attesting that its session is stopped")
    release(db, writer, f"withdrawn by Captain {payload.captain_id}: {payload.reason}")
    return binding(db, writer_row(db, writer["id"]))


def advance(db, writer_id, previous_attempt_id, view):
    """After an accepted writer post, open the next stage's attempt or end the writer."""
    writer = writer_row(db, writer_id)
    if writer["ended_at"] is None:
        if view["state"]["status"] in TERMINAL:
            intake.end_writers(db, writer["execution_id"], view["state"]["status"])
        else:
            previous = db.execute("SELECT stage FROM attempts WHERE id=?", (previous_attempt_id,)).fetchone()
            if (previous is not None and previous["stage"] == Stage.SELF_REVIEW
                    and view["state"]["stage"] == Stage.PUBLICATION):
                release_building(db, writer_id)
                db.execute("UPDATE executions SET handoff_head=NULL WHERE id=?", (writer["execution_id"],))
                writer = writer_row(db, writer_id)
            claim_next(db, writer, continued_from=previous_attempt_id)
    return binding(db, writer_row(db, writer_id))


def next_attempt(db, writer_id):
    writer = writer_row(db, writer_id)
    if writer["ended_at"] is not None:
        raise intake.Conflict("The writer has ended")
    open_attempt = writer["attempt_id"] and db.execute(
        "SELECT 1 FROM attempts WHERE id=? AND ended_at IS NULL", (writer["attempt_id"],)).fetchone()
    if not open_attempt:
        claim_next(db, writer)
    return binding(db, writer_row(db, writer_id))


def progress(db, writer_id, payload):
    db.execute("UPDATE writers SET last_progress_at=?, progress_note=coalesce(?, progress_note) WHERE id=?",
               (intake.timestamp(), payload.note, writer_id))
    return binding(db, writer_row(db, writer_id))


def current(db, writer_id):
    """The writer's binding plus its execution's open flags, so the writer sees what it must fix."""
    view = binding(db, writer_row(db, writer_id))
    view["flags"] = [flag for flag in all_flags(db)["flags"] if flag["execution_id"] == view["id"]]
    return view


EXECUTION = "/items/{item_id}/executions/{execution_id}/writer"


@router.post(EXECUTION, dependencies=[Depends(require_coordinator)])
def post_writer(request: Request, item_id: Identifier, execution_id: Identifier, body: Registration):
    return intake_api.run(request, register, item_id, execution_id, body, write=True)


@router.patch(EXECUTION, dependencies=[Depends(require_coordinator)])
def patch_writer(request: Request, item_id: Identifier, execution_id: Identifier, body: Update):
    return intake_api.run(request, update, item_id, execution_id, body, write=True)


@router.post(EXECUTION + "/withdraw", dependencies=[Depends(require_coordinator)])
def post_withdraw(request: Request, item_id: Identifier, execution_id: Identifier, body: Withdrawal):
    return intake_api.run(request, withdraw, item_id, execution_id, body, write=True)


def own_writer(request):
    writer = getattr(request.state, "writer", None)
    if writer is None:
        raise HTTPException(403, "Writer token required")
    return writer["id"]


@router.get("/writer", dependencies=[Depends(require_writer)])
def get_current(request: Request):
    return intake_api.run(request, current, own_writer(request))


@router.post("/writer/attempt", dependencies=[Depends(require_writer)])
def post_attempt(request: Request):
    return intake_api.run(request, next_attempt, own_writer(request), write=True)


@router.post("/writer/progress", dependencies=[Depends(require_writer)])
def post_progress(request: Request, body: Progress):
    return intake_api.run(request, progress, own_writer(request), body, write=True)


@router.post("/writer/capacity", dependencies=[Depends(require_writer)])
def post_capacity(request: Request, body: Capacity):
    return intake_api.run(request, capacity, own_writer(request), body, write=True)
