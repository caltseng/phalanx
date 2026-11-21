"""Transactions for durable intake and versioned lifecycle events."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
import json
from time import time
from uuid import uuid4

from pydantic import TypeAdapter

from .finalization_schema import enqueue
from .store import connect
from .transitions import (
    Event, Evidence, Execution, Outcome, Stage, Status, Target, TERMINAL, Wait, decide, start,
)


STATE = TypeAdapter(Execution)
PRIORITY = {"low": 0, "normal": 1, "urgent": 2}


class Missing(ValueError):
    pass


class Conflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def timestamp():
    return datetime.now(UTC).isoformat(timespec="microseconds")


@contextmanager
def read_transaction(path):
    with connect(path, mode="ro") as db:
        db.execute("BEGIN")
        yield db
        db.execute("COMMIT")


def replayable(db, scope, key, payload, perform, *, should_record=None):
    if key is None:
        return perform()
    fingerprint = digest(encode(payload))
    receipt = db.execute(
        "SELECT request_digest, response_json FROM request_receipts WHERE scope=? AND idempotency_key=?",
        (scope, key),
    ).fetchone()
    if receipt:
        if receipt["request_digest"] != fingerprint:
            raise Conflict("Idempotency key was already used with a different request")
        return json.loads(receipt["response_json"])
    result = perform()
    if should_record is None or should_record(result):
        db.execute("INSERT INTO request_receipts VALUES (?, ?, ?, ?)",
                   (scope, key, fingerprint, encode(result)))
    return result


def register(db, repository_id, remote):
    db.execute(
        "INSERT INTO repositories VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (repository_id, remote, timestamp()),
    )
    repository = db.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone()
    if repository is None or repository["remote"] != remote:
        raise Conflict("Repository ID or remote is already registered")
    return dict(repository)


def repositories(db):
    return [dict(row) for row in db.execute("SELECT * FROM repositories ORDER BY id")]


def item_row(db, item_id):
    item = db.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
    if item is None:
        raise Missing("Work item not found")
    return item


def current_execution(db, item_id):
    row = db.execute(
        """SELECT * FROM executions AS current WHERE item_id=?
           AND NOT EXISTS (SELECT 1 FROM executions WHERE previous_id=current.id)""",
        (item_id,),
    ).fetchone()
    if row is None:
        raise Missing("Work item not found")
    return row


def execution_view(row):
    return {
        "id": row["id"], "previous_id": row["previous_id"], "version": row["version"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "state": json.loads(row["state_json"]),
        "ready_since_ms": row["ready_since"], "retry_at_ms": row["retry_at"],
    }


def inspect(db, item_id):
    item = item_row(db, item_id)
    return {
        "id": item["id"], "created_at": item["created_at"],
        **json.loads(item["request_json"]),
        "execution": execution_view(current_execution(db, item_id)),
    }


def history(db, item_id):
    item_row(db, item_id)
    return [execution_view(row) for row in db.execute(
        "SELECT * FROM executions WHERE item_id=? ORDER BY created_at, id", (item_id,),
    )]


def execution_row(db, item_id, execution_id):
    row = db.execute("SELECT * FROM executions WHERE item_id=? AND id=?",
                     (item_id, execution_id)).fetchone()
    if row is None:
        raise Missing("Execution not found")
    return row


def timeline(db, item_id, execution_id):
    execution_row(db, item_id, execution_id)
    return [{
        "version": row["version"], "event": row["event"], "attempt_id": row["attempt_id"],
        "created_at": row["created_at"], "details": json.loads(row["details_json"]),
        "state": json.loads(row["state_json"]),
    } for row in db.execute(
        "SELECT * FROM transition_history WHERE execution_id=? ORDER BY version", (execution_id,),
    )]


def record(db, execution_id, version, event, details, state_json, now, attempt_id=None):
    db.execute("INSERT INTO transition_history VALUES (?, ?, ?, ?, ?, ?, ?)",
               (execution_id, version, attempt_id, event, encode(details), state_json, now))


def new_execution(db, item_id, target, *, previous_id=None):
    execution_id = uuid4().hex
    clock = datetime.now(UTC)
    now = clock.isoformat(timespec="microseconds")
    state_json = encode(asdict(start(target)))
    item = item_row(db, item_id)
    request = json.loads(item["request_json"])
    db.execute(
        """INSERT INTO executions (id, item_id, previous_id, version, state_json,
               created_at, updated_at, queue_repository, queue_affinity, queue_priority, ready_since)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
        (execution_id, item_id, previous_id, state_json, now, now, item["repository_id"],
         request.get("runner_affinity") or "", PRIORITY[request.get("priority", "normal")],
         int(clock.timestamp() * 1000)),
    )
    record(db, execution_id, 1, "rerun" if previous_id else "intake", {}, state_json, now)
    return execution_view(execution_row(db, item_id, execution_id))


def create(db, payload):
    if not db.execute("SELECT 1 FROM repositories WHERE id=?", (payload.repository_id,)).fetchone():
        raise Missing("Repository not registered")
    item_id = uuid4().hex
    request = payload.model_dump(mode="json")
    if payload.runner_affinity is None:
        del request["runner_affinity"]
    db.execute("INSERT INTO work_items VALUES (?, ?, ?, ?)",
               (item_id, payload.repository_id, encode(request), timestamp()))
    new_execution(db, item_id, payload.target)
    return inspect(db, item_id)


def check_current(db, item_id, execution_id, version):
    row = current_execution(db, item_id)
    if row["id"] != execution_id or row["version"] != version:
        raise Conflict("Execution or state version has changed")
    return row, STATE.validate_json(row["state_json"])


def rerun(db, item_id, execution_id, payload):
    _, state = check_current(db, item_id, execution_id, payload.expected_version)
    if state.status not in TERMINAL:
        raise Conflict("Only closed executions can be rerun")
    if (state.target == Target.REPORT) != (payload.target == Target.REPORT):
        raise Conflict("A rerun must preserve the requested output type")
    return new_execution(db, item_id, payload.target, previous_id=execution_id)


def apply(db, row, state, event, *, details, attempt_id=None, retry_at=None, **options):
    """Apply the verified event and append history in the caller's write transaction."""
    try:
        next_state = decide(state, event, **options)
    except ValueError as error:
        raise Conflict(str(error)) from error
    version = row["version"] + 1
    clock = datetime.now(UTC)
    now = clock.isoformat(timespec="microseconds")
    state_json = encode(asdict(next_state))
    ready_since = None
    if next_state.status == Status.QUEUED:
        ready_since = retry_at if retry_at is not None else int(clock.timestamp() * 1000)
    retry_count = row["retry_count"] if next_state.stage == state.stage else 0
    if event == Event.RETRY:
        retry_count += 1
    db.execute(
        """UPDATE executions SET version=?, state_json=?, updated_at=?, ready_since=?,
           retry_at=?, retry_count=? WHERE id=?""",
        (version, state_json, now, ready_since, retry_at, retry_count, row["id"]),
    )
    record(db, row["id"], version, event, details, state_json, now, attempt_id)
    if state.pending_output is None and next_state.pending_output is not None:
        enqueue(db, row["id"], version)
    if attempt_id and event != Event.CLAIM:
        db.execute("UPDATE attempts SET ended_at=? WHERE id=?", (now, attempt_id))
    return execution_view(execution_row(db, row["item_id"], row["id"]))


def claim_attempt(db, row, state, *, runner_id, lease_digest, lease_expires_at, now=None):
    """Persist a claim after PH-06 selects and admits it in this same write transaction."""
    if state.status != Status.QUEUED or lease_expires_at <= (time() if now is None else now):
        raise Conflict("Claim requires a queued execution and a live lease")
    attempt_id = uuid4().hex
    db.execute(
        """INSERT INTO attempts
           (id, execution_id, stage, runner_id, lease_digest, lease_expires_at, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (attempt_id, row["id"], state.stage, runner_id, lease_digest, lease_expires_at, timestamp()),
    )
    execution = apply(db, row, state, Event.CLAIM, attempt_id=attempt_id,
                      details={"runner_id": runner_id, "ready_since_ms": row["ready_since"]})
    return {"attempt_id": attempt_id, "execution": execution}


def check_attempt(db, execution_id, state, payload, lease_digest):
    attempt = db.execute(
        """SELECT * FROM attempts WHERE id=? AND execution_id=? AND ended_at IS NULL""",
        (payload.attempt_id, execution_id),
    ).fetchone()
    if (attempt is None or payload.stage != state.stage or attempt["stage"] != state.stage
            or attempt["lease_expires_at"] <= int(time())
            or not compare_digest(attempt["lease_digest"], lease_digest)):
        raise Conflict("Attempt, stage, or lease is no longer current")


def report(db, item_id, execution_id):
    execution = execution_row(db, item_id, execution_id)
    if execution["status"] not in TERMINAL:
        raise Missing("Stored report not found")
    row = db.execute("SELECT markdown FROM reports WHERE execution_id=?", (execution_id,)).fetchone()
    if row is None:
        raise Missing("Stored report not found")
    return row["markdown"]


def cancel(db, item_id, execution_id, payload):
    return request_stop(db, item_id, execution_id, payload, Event.CANCEL)


def close_failure(db, item_id, execution_id, payload):
    return request_stop(db, item_id, execution_id, payload, Event.CLOSE_FAILURE)


def request_stop(db, item_id, execution_id, payload, event):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    details = {"reason": payload.reason}
    updated = apply(db, row, state, event, details=details)
    # A passive report wait with no open attempt has no outstanding effects.
    if state.status == Status.WAITING_FOR_EXTERNAL and state.target == Target.REPORT:
        active = db.execute(
            "SELECT 1 FROM attempts WHERE execution_id=? AND ended_at IS NULL", (execution_id,),
        ).fetchone()
        if active is None:
            row, state = check_current(db, item_id, execution_id, updated["version"])
            return apply(db, row, state, Event.STOP_CONFIRMED, details=details)
    return updated


def stop_confirmed(db, item_id, execution_id, payload, lease_digest):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    check_attempt(db, execution_id, state, payload, lease_digest)
    return confirm_stop(db, row, state, payload.attempt_id, details={})


def confirm_stop(db, row, state, attempt_id, *, details):
    if state.stopping not in {Status.CANCELLED, Status.FAILED}:
        raise Conflict("No stop request is awaiting worker confirmation")
    request = db.execute(
        "SELECT details_json FROM transition_history WHERE execution_id=? AND version=?",
        (row["id"], row["version"]),
    ).fetchone()
    return apply(db, row, state, Event.STOP_CONFIRMED,
                 details={**details, **json.loads(request["details_json"])}, attempt_id=attempt_id)


def result(db, item_id, execution_id, payload, lease_digest):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    check_attempt(db, execution_id, state, payload, lease_digest)
    wait = Wait(**payload.wait.model_dump()) if payload.wait else None
    details = payload.model_dump(mode="json", exclude={"report_markdown", "expected_version"})
    draft_revision = payload.report_revision
    if payload.report_markdown is not None:
        if state.stage != Stage.REPORT_DRAFTING or payload.outcome != Outcome.SUCCEEDED:
            raise Conflict("Markdown is accepted on a successful report drafting result")
        if draft_revision is not None:
            raise Conflict("The server derives the revision from the draft Markdown")
        draft_revision = digest(payload.report_markdown)
        db.execute(
            "INSERT INTO report_drafts VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (execution_id, draft_revision, payload.report_markdown),
        )
        details["report_revision"] = draft_revision
    elif state.stage == Stage.REPORT_DRAFTING and payload.outcome == Outcome.SUCCEEDED:
        raise Conflict("Successful drafting requires the draft Markdown")

    if state.stage == Stage.REPORT_STORAGE and payload.outcome == Outcome.SUCCEEDED:
        if draft_revision is None or draft_revision != state.report_draft:
            raise Conflict("Storage requires the current reviewed draft revision")
        draft = db.execute(
            "SELECT 1 FROM report_drafts WHERE execution_id=? AND revision=?",
            (execution_id, draft_revision),
        ).fetchone()
        if draft is None:
            raise Conflict("Reviewed draft Markdown is unavailable")

    return apply(
        db, row, state, Event.RESULT, details=details, attempt_id=payload.attempt_id,
        outcome=payload.outcome, wait=wait,
        evidence=Evidence(candidate=payload.candidate, reviewed_candidate=payload.reviewed_candidate,
                          prevalidated_candidate=payload.prevalidated_candidate,
                          report_draft=draft_revision, reviewed_report=draft_revision),
    )

def draft(db, item_id, execution_id, revision):
    execution_row(db, item_id, execution_id)
    row = db.execute("SELECT markdown FROM report_drafts WHERE execution_id=? AND revision=?",
                     (execution_id, revision)).fetchone()
    if row is None:
        raise Missing("Report draft not found")
    return row["markdown"]
