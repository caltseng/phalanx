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

from .store import connect
from .transitions import (
    Event, Evidence, Execution, Outcome, Stage, Status, StoredReport, Target, TERMINAL, Wait, decide, start,
)


STATE = TypeAdapter(Execution)


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


def replayable(db, scope, key, payload, perform):
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
    now = timestamp()
    state_json = encode(asdict(start(target)))
    db.execute(
        """INSERT INTO executions (id, item_id, previous_id, version, state_json, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, ?, ?)""",
        (execution_id, item_id, previous_id, state_json, now, now),
    )
    record(db, execution_id, 1, "rerun" if previous_id else "intake", {}, state_json, now)
    return execution_view(execution_row(db, item_id, execution_id))


def create(db, payload):
    if not db.execute("SELECT 1 FROM repositories WHERE id=?", (payload.repository_id,)).fetchone():
        raise Missing("Repository not registered")
    item_id = uuid4().hex
    db.execute("INSERT INTO work_items VALUES (?, ?, ?, ?)",
               (item_id, payload.repository_id, encode(payload.model_dump(mode="json")), timestamp()))
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


def apply(db, row, state, event, *, details, attempt_id=None, **options):
    """Apply the verified event and append history in the caller's write transaction."""
    try:
        next_state = decide(state, event, **options)
    except ValueError as error:
        raise Conflict(str(error)) from error
    version = row["version"] + 1
    now = timestamp()
    state_json = encode(asdict(next_state))
    db.execute("UPDATE executions SET version=?, state_json=?, updated_at=? WHERE id=?",
               (version, state_json, now, row["id"]))
    record(db, row["id"], version, event, details, state_json, now, attempt_id)
    if attempt_id and event != Event.CLAIM:
        db.execute("UPDATE attempts SET ended_at=? WHERE id=?", (now, attempt_id))
    return execution_view(execution_row(db, row["item_id"], row["id"]))


def claim_attempt(db, row, state, *, runner_id, lease_digest, lease_expires_at):
    """Persist a claim after PH-06 selects and admits it in this same write transaction."""
    if state.status != Status.QUEUED or lease_expires_at <= int(time()):
        raise Conflict("Claim requires a queued execution and a live lease")
    attempt_id = uuid4().hex
    db.execute(
        """INSERT INTO attempts
           (id, execution_id, stage, runner_id, lease_digest, lease_expires_at, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (attempt_id, row["id"], state.stage, runner_id, lease_digest, lease_expires_at, timestamp()),
    )
    execution = apply(db, row, state, Event.CLAIM, attempt_id=attempt_id,
                      details={"runner_id": runner_id})
    return {"attempt_id": attempt_id, "execution": execution}


def _check_attempt(db, execution_id, state, payload, lease_digest):
    attempt = db.execute(
        """SELECT * FROM attempts WHERE id=? AND execution_id=? AND ended_at IS NULL""",
        (payload.attempt_id, execution_id),
    ).fetchone()
    if (attempt is None or payload.stage != state.stage or attempt["stage"] != state.stage
            or attempt["lease_expires_at"] <= int(time())
            or not compare_digest(attempt["lease_digest"], lease_digest)):
        raise Conflict("Attempt, stage, or lease is no longer current")


def _store_report(db, item_id, execution_id, markdown, revision=None):
    db.execute("INSERT INTO reports VALUES (?, ?)", (execution_id, markdown))
    return StoredReport(f"/v1/items/{item_id}/executions/{execution_id}/report", revision)


def report(db, item_id, execution_id):
    execution_row(db, item_id, execution_id)
    row = db.execute("SELECT markdown FROM reports WHERE execution_id=?", (execution_id,)).fetchone()
    if row is None:
        raise Missing("Stored report not found")
    return row["markdown"]


def _cancellation_report(db, row, state, reason):
    stages = db.execute(
        "SELECT DISTINCT stage FROM attempts WHERE execution_id=?", (row["id"],),
    )
    attempted = ", ".join(sorted(stage[0] for stage in stages)) or "No stages claimed"
    markdown = (
        f"# Cancellation report\n\n"
        f"Last stage: {state.stage}\n\n"
        f"Reason: {reason}\n\n"
        f"Stages attempted: {attempted}\n\n"
        f"Partial PR: {state.primary_pr or 'None recorded'}\n\n"
        f"Candidate: {state.candidate or 'None recorded'}\n\n"
        "Review existing artifacts before rerunning the item.\n"
    )
    return _store_report(db, row["item_id"], row["id"], markdown)


def cancel(db, item_id, execution_id, payload):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    stored = None
    if state.status in {Status.QUEUED, Status.WAITING_FOR_INPUT}:
        stored = _cancellation_report(db, row, state, payload.reason)
    updated = apply(db, row, state, Event.CANCEL, details={"reason": payload.reason},
                    evidence=Evidence(stored_report=stored))
    # Report storage is local and atomic. A passive report wait with no
    # open attempt has no worker or provider write left to reconcile.
    if state.status == Status.WAITING_FOR_EXTERNAL and state.target == Target.REPORT:
        active = db.execute(
            "SELECT 1 FROM attempts WHERE execution_id=? AND ended_at IS NULL", (execution_id,),
        ).fetchone()
        if active is None:
            row, state = check_current(db, item_id, execution_id, updated["version"])
            stored = _cancellation_report(db, row, state, payload.reason)
            return apply(db, row, state, Event.STOP_CONFIRMED, details={},
                         evidence=Evidence(stored_report=stored))
    return updated


def stop_confirmed(db, item_id, execution_id, payload, lease_digest):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    _check_attempt(db, execution_id, state, payload, lease_digest)
    if state.stopping != Status.CANCELLED:
        raise Conflict("No worker cancellation is awaiting confirmation")
    cancellation = db.execute(
        "SELECT details_json FROM transition_history WHERE execution_id=? AND version=?",
        (execution_id, row["version"]),
    ).fetchone()
    reason = json.loads(cancellation["details_json"])["reason"]
    stored = _cancellation_report(db, row, state, reason)
    return apply(db, row, state, Event.STOP_CONFIRMED, details={},
                 attempt_id=payload.attempt_id, evidence=Evidence(stored_report=stored))


def result(db, item_id, execution_id, payload, lease_digest):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    _check_attempt(db, execution_id, state, payload, lease_digest)
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

    stored = None
    if state.stage == Stage.REPORT_STORAGE and payload.outcome == Outcome.SUCCEEDED:
        if draft_revision is None or draft_revision != state.report_draft:
            raise Conflict("Storage requires the current reviewed draft revision")
        draft = db.execute(
            "SELECT markdown FROM report_drafts WHERE execution_id=? AND revision=?",
            (execution_id, draft_revision),
        ).fetchone()
        if draft is None:
            raise Conflict("Reviewed draft Markdown is unavailable")
        stored = _store_report(db, item_id, execution_id, draft["markdown"], draft_revision)

    return apply(
        db, row, state, Event.RESULT, details=details, attempt_id=payload.attempt_id,
        outcome=payload.outcome, wait=wait,
        evidence=Evidence(candidate=payload.candidate, reviewed_candidate=payload.reviewed_candidate,
                          prevalidated_candidate=payload.prevalidated_candidate,
                          report_draft=draft_revision, reviewed_report=draft_revision, stored_report=stored),
    )

def draft(db, item_id, execution_id, revision):
    execution_row(db, item_id, execution_id)
    row = db.execute("SELECT markdown FROM report_drafts WHERE execution_id=? AND revision=?",
                     (execution_id, revision)).fetchone()
    if row is None:
        raise Missing("Report draft not found")
    return row["markdown"]
