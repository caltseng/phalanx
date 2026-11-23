"""Transactions for durable intake and versioned lifecycle events."""

from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
import json
from time import time
from uuid import uuid4

from pydantic import TypeAdapter

from . import dependencies
from .finalization_schema import enqueue
from .transitions import (
    Bypass, Event, Evidence, Execution, Outcome, Stage, Status, Target, TERMINAL, Wait, decide, start,
)


STATE = TypeAdapter(Execution)
PRIORITY = {"low": 0, "normal": 1, "urgent": 2}
# Stages whose findings send the item back to development or revision. Counting
# pre-PR stages bounds reruns when development keeps the candidate unchanged.
REWORK_STAGES = {Stage.SELF_REVIEW, Stage.PRE_PR_VALIDATION, Stage.PR_PROMOTION,
                 Stage.ADVERSARIAL_REVIEW, Stage.PR_RECONCILIATION, Stage.CHECK_IN}
REWORK_TARGETS = {Stage.DEVELOPMENT, Stage.PR_REVISION}
# Rework rounds allowed between owner replies.
REWORK_LIMIT = 10
# Development rounds allowed on identical findings before they count as no progress.
REPEAT_LIMIT = 2


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
        "retry_count": row["retry_count"],
        "unmet_dependencies": json.loads(row["dependency_wait_json"] or "[]"),
    }


def inspect(db, item_id):
    item = item_row(db, item_id)
    execution = current_execution(db, item_id)
    reply = db.execute(
        """SELECT version, created_at, details_json FROM transition_history
           WHERE execution_id=? AND event=? ORDER BY version DESC LIMIT 1""",
        (execution["id"], Event.REPLY),
    ).fetchone()
    last_reply = None
    if reply is not None:
        details = json.loads(reply["details_json"])
        last_reply = {"version": reply["version"], "created_at": reply["created_at"],
                      "answer": details["answer"],
                      "answered_by": details.get("answered_by", "owner")}
    return {
        "id": item["id"], "created_at": item["created_at"],
        **json.loads(item["request_json"]),
        "dependencies": dependencies.configured_dependencies(db, item_id),
        "execution": execution_view(execution),
        "last_reply": last_reply,
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
    events = [{
        "version": row["version"], "event": row["event"], "attempt_id": row["attempt_id"],
        "created_at": row["created_at"], "details": json.loads(row["details_json"]),
        "state": json.loads(row["state_json"]),
    } for row in db.execute(
        "SELECT * FROM transition_history WHERE execution_id=? ORDER BY version", (execution_id,),
    )]
    states = {event["version"]: event["state"] for event in events}
    events += [{
        "version": row["version"], "event": "recover_claim", "attempt_id": row["attempt_id"],
        "created_at": row["created_at"],
        "details": {"runner_id": row["runner_id"], "reconciliation": row["reconciliation"]},
        "state": states[row["version"]],
    } for row in db.execute(
        "SELECT * FROM attempt_adoptions WHERE execution_id=?", (execution_id,),
    )]
    return sorted(events, key=lambda event: (event["version"], event["created_at"]))


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
    now_ms = int(clock.timestamp() * 1000)
    dependencies.refresh_wait(db, item_id, execution_id, now_ms)
    if previous_id:
        dependencies.refresh_dependents(db, item_id, now_ms)
    return execution_view(execution_row(db, item_id, execution_id))


def create(db, payload):
    if not db.execute("SELECT 1 FROM repositories WHERE id=?", (payload.repository_id,)).fetchone():
        raise Missing("Repository not registered")
    item_id = uuid4().hex
    request = payload.model_dump(mode="json", exclude={"dependencies"})
    if payload.runner_affinity is None:
        del request["runner_affinity"]
    db.execute("INSERT INTO work_items VALUES (?, ?, ?, ?)",
               (item_id, payload.repository_id, encode(request), timestamp()))
    if payload.dependencies:
        dependencies.replace_dependencies(db, item_id, [
            dependencies.Dependency(link.upstream_id, link.milestone) for link in payload.dependencies
        ])
    new_execution(db, item_id, payload.target)
    return inspect(db, item_id)


def check_current(db, item_id, execution_id, version):
    row = current_execution(db, item_id)
    if row["id"] != execution_id or row["version"] != version:
        raise Conflict("Execution or state version has changed")
    return row, STATE.validate_json(row["state_json"])


def steer(db, item_id, execution_id, payload):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    if state.status != Status.QUEUED or state.stopping or state.pending_output:
        raise Conflict("Steering requires queued work. Resolve the active execution or rerun it first.")

    edits = payload.model_dump(mode="json", exclude_unset=True, exclude={"expected_version"})
    if not edits or any(value is None for name, value in edits.items() if name != "runner_affinity"):
        raise Conflict("Steering requires at least one edit. Use an empty list to clear list fields.")
    request = json.loads(item_row(db, item_id)["request_json"])
    previous = {name: request.get(name) for name in edits if name != "dependencies"}
    if "dependencies" in edits:
        previous["dependencies"] = dependencies.configured_dependencies(db, item_id)
        dependencies.replace_dependencies(db, item_id, [
            dependencies.Dependency(link.upstream_id, link.milestone) for link in payload.dependencies
        ])
    request.update({name: value for name, value in edits.items() if name != "dependencies"})
    now = timestamp()
    db.execute("UPDATE work_items SET request_json=? WHERE id=?", (encode(request), item_id))
    db.execute(
        """UPDATE executions SET version=?, updated_at=?, queue_priority=?, queue_affinity=? WHERE id=?""",
        (row["version"] + 1, now, PRIORITY[request.get("priority", "normal")],
         request.get("runner_affinity") or "", execution_id),
    )
    record(db, execution_id, row["version"] + 1, "steer", {"before": previous, "after": edits},
           row["state_json"], now)
    dependencies.refresh_wait(db, item_id, execution_id, int(time() * 1000))
    return inspect(db, item_id)


def rerun(db, item_id, execution_id, payload):
    _, state = check_current(db, item_id, execution_id, payload.expected_version)
    if state.status not in TERMINAL:
        raise Conflict("Only closed executions can be rerun")
    if (state.target == Target.REPORT) != (payload.target == Target.REPORT):
        raise Conflict("A rerun must preserve the requested output type")
    return new_execution(db, item_id, payload.target, previous_id=execution_id)


def _stalled_rework(db, execution_id, next_state):
    """Return an owner question when published-PR rework stops making progress."""
    stages = sorted(REWORK_STAGES)
    rounds = [findings for findings, in db.execute(
        f"""SELECT json_extract(state_json, '$.findings') FROM transition_history
           WHERE execution_id=? AND event=? AND json_extract(state_json, '$.stage') IN (?, ?)
             AND json_extract(details_json, '$.stage') IN ({','.join('?' * len(stages))})
             AND version > COALESCE((SELECT MAX(version) FROM transition_history
                                     WHERE execution_id=? AND event=?), 0)""",
        (execution_id, Event.RESULT, *sorted(REWORK_TARGETS), *stages, execution_id, Event.REPLY),
    )]
    findings = next_state.findings
    if rounds.count(findings) >= REPEAT_LIMIT:
        reason = f"The same findings returned after {REPEAT_LIMIT} revision rounds"
    elif len(rounds) >= REWORK_LIMIT:
        reason = f"The PR needed {REWORK_LIMIT} revision rounds without becoming ready"
    else:
        return None
    return {"prompt": f"{reason}. Resolve the PR or give direction, then reply to retry.",
            "handoff": f"{next_state.primary_pr}, candidate {next_state.candidate}\n\n"
                       f"Latest findings:\n{findings}"[:16000]}


def apply(db, row, state, event, *, details, attempt_id=None, retry_at=None, **options):
    """Apply the verified event and append history in the caller's write transaction."""
    try:
        next_state = decide(state, event, **options)
    except ValueError as error:
        raise Conflict(str(error)) from error
    if (event == Event.RESULT and state.stage in REWORK_STAGES and next_state.stage in REWORK_TARGETS
            and next_state.findings):
        question = _stalled_rework(db, row["id"], next_state)
        if question is not None:
            details = {**details, "outcome": Outcome.NEEDS_INPUT, "question": question}
            next_state = decide(state, event, outcome=Outcome.NEEDS_INPUT,
                                wait=Wait(question["prompt"], "owner reply"))
    if event == Event.RESULT:
        has_question = details.get("question") is not None
        if next_state.status == Status.WAITING_FOR_INPUT:
            if not has_question and options.get("outcome") == Outcome.NEEDS_INPUT:
                raise Conflict("A structured question and handoff are required for an input wait")
        elif has_question:
            raise Conflict("This result does not open an input wait")
    version = row["version"] + 1
    clock = datetime.now(UTC)
    now = clock.isoformat(timespec="microseconds")
    state_json = encode(asdict(next_state))
    ready_since = None
    if next_state.status == Status.QUEUED:
        ready_since = retry_at if retry_at is not None else int(clock.timestamp() * 1000)
    retry_count = row["retry_count"] if next_state.stage == state.stage else 0
    if event == Event.RETRY or retry_at is not None:
        retry_count += 1
    db.execute(
        """UPDATE executions SET version=?, state_json=?, updated_at=?, ready_since=?,
           retry_at=?, retry_count=?, dependency_wait_json=NULL WHERE id=?""",
        (version, state_json, now, ready_since, retry_at, retry_count, row["id"]),
    )
    record(db, row["id"], version, event, details, state_json, now, attempt_id)
    if state.status == Status.WAITING_FOR_EXTERNAL:
        db.execute("DELETE FROM external_rechecks WHERE execution_id=?", (row["id"],))
    if state.pending_output is None and next_state.pending_output is not None:
        enqueue(db, row["id"], version)
    if attempt_id and event != Event.CLAIM:
        db.execute("UPDATE attempts SET ended_at=? WHERE id=?", (now, attempt_id))
    now_ms = int(clock.timestamp() * 1000)
    if next_state.status == Status.QUEUED:
        dependencies.refresh_wait(db, row["item_id"], row["id"], now_ms)
    if dependencies.milestone_evidence(state) != dependencies.milestone_evidence(next_state):
        dependencies.refresh_dependents(db, row["item_id"], now_ms)
    return execution_view(execution_row(db, row["item_id"], row["id"]))


def claim_attempt(db, row, state, *, runner_id, lease_digest, lease_expires_at, now=None,
                  continued_from=None):
    """Persist a claim after PH-06 selects and admits it in this same write transaction."""
    now = time() if now is None else now
    if state.status != Status.QUEUED or lease_expires_at <= now:
        raise Conflict("Claim requires a queued execution and a live lease")
    resolved, unmet = dependencies.resolve_dependencies(db, row["item_id"])
    if unmet:
        dependencies.update_wait(db, row["id"], unmet, int(now * 1000))
        return None
    snapshot = [asdict(link) for link in resolved]
    attempt_id = uuid4().hex
    db.execute(
        """INSERT INTO attempts
           (id, execution_id, stage, runner_id, lease_digest, lease_expires_at, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (attempt_id, row["id"], state.stage, runner_id, lease_digest, lease_expires_at, timestamp()),
    )
    resume = db.execute(
        """SELECT event, details_json FROM transition_history
           WHERE execution_id=? AND version=? AND event IN (?, ?)""",
        (row["id"], row["version"], Event.REPLY, Event.RECHECK),
    ).fetchone()
    details = {"runner_id": runner_id, "ready_since_ms": row["ready_since"], "dependencies": snapshot}
    if continued_from is not None:
        details["continued_from"] = continued_from
    execution = apply(db, row, state, Event.CLAIM, attempt_id=attempt_id, details=details)
    claim = {"attempt_id": attempt_id, "execution": execution, "dependencies": snapshot}
    if resume is not None:
        field = "input_reply" if resume["event"] == Event.REPLY else "external_recheck"
        claim[field] = json.loads(resume["details_json"])
    return claim


def check_attempt(db, execution_id, state, payload, lease_digest):
    attempt = db.execute(
        """SELECT * FROM attempts WHERE id=? AND execution_id=? AND ended_at IS NULL""",
        (payload.attempt_id, execution_id),
    ).fetchone()
    if (attempt is None or payload.stage != state.stage or attempt["stage"] != state.stage
            or attempt["lease_expires_at"] <= int(time())
            or not compare_digest(attempt["lease_digest"], lease_digest)):
        raise Conflict("Attempt, stage, or lease is no longer current")
    return attempt


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
    # A scheduled external wait has released its attempt and has no outstanding effects.
    if state.status == Status.WAITING_FOR_EXTERNAL:
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


def stopped_retry_at(row, now):
    """Use the same budget and delay for returned failures and expired attempts."""
    if row["retry_count"] >= 4:
        return None
    return int(now * 1000) + (5 * 2 ** row["retry_count"]) * 1000


def result(db, item_id, execution_id, payload, lease_digest):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    check_attempt(db, execution_id, state, payload, lease_digest)
    if payload.review_notes is not None and (
        state.stage not in {Stage.SELF_REVIEW, Stage.PR_REVISION}
        or state.candidate is None
        or state.candidate != (payload.reviewed_candidate if payload.outcome == Outcome.SUCCEEDED
                               else payload.candidate)
    ):
        raise Conflict("Review notes require self-review or revision evidence for the current candidate")
    if payload.local_checks is not None and (
        state.stage != Stage.PRE_PR_VALIDATION
        or state.candidate is None
        or state.candidate != (payload.prevalidated_candidate if payload.outcome == Outcome.SUCCEEDED
                               else payload.candidate)
    ):
        raise Conflict("Local checks require pre-validation evidence for the current candidate")
    if payload.local_checks is not None and payload.outcome == Outcome.SUCCEEDED:
        if len({check.name for check in payload.local_checks}) != len(payload.local_checks):
            raise Conflict("Successful pre-validation requires one disposition per local check")
        if any(check.outcome == "failed" or (
            check.outcome != "passed" and not (check.reason and check.authorization)
        ) for check in payload.local_checks):
            raise Conflict("Successful pre-validation requires passing checks or documented bypasses")
    wait = Wait(**payload.wait.model_dump()) if payload.wait else None
    if payload.question is not None:
        wait = Wait(wait.reason if wait else payload.question.prompt, "owner reply")
    excluded = {"report_markdown", "expected_version"}
    excluded.update(field for field in ("review_notes", "local_checks")
                    if getattr(payload, field) is None)
    details = payload.model_dump(mode="json", exclude=excluded)
    retry_at = None
    if payload.retry_after_seconds is not None and payload.failure_reason is None:
        raise Conflict("A retry delay requires a technical failure reason")
    if payload.failure_reason is not None:
        if payload.outcome != Outcome.FAILED or wait is not None:
            raise Conflict("A technical failure reason requires a failed result without an explicit wait")
        now = time()
        retry_at = stopped_retry_at(row, now)
        if retry_at is None:
            attempts = ("technical failures" if payload.retry_after_seconds is not None
                        else "stopped worker attempts")
            wait = Wait(f"Five {attempts} require a recovery decision.\n\n{payload.failure_reason}",
                        "owner intervention")
        else:
            if payload.retry_after_seconds is not None:
                retry_at = max(retry_at, int(now * 1000) + payload.retry_after_seconds * 1000)
            details["retry_at_ms"] = retry_at
    if payload.report_review_notes is not None:
        if state.stage != Stage.REPORT_REVIEW or payload.outcome not in {
            Outcome.SUCCEEDED, Outcome.CHANGES_REQUIRED,
        }:
            raise Conflict("Review notes require a successful review or changes-required result")
        details["report_review_notes"] = payload.report_review_notes
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

    findings = bypass = None
    if payload.outcome == Outcome.CHANGES_REQUIRED and state.stage in {Stage.SELF_REVIEW, Stage.PR_REVISION}:
        findings = payload.review_notes
    elif payload.outcome == Outcome.CHANGES_REQUIRED and payload.local_checks:
        findings = "\n".join(f"Local check {check.name} {check.outcome}: {check.reason or 'no detail'}"
                             for check in payload.local_checks if check.outcome == "failed")[:16000] or None
    elif payload.outcome == Outcome.SUCCEEDED and payload.local_checks:
        skipped = [check for check in payload.local_checks if check.outcome != "passed"]
        if skipped:
            bypass = Bypass(tuple(check.name for check in skipped),
                            "\n".join(f"{check.name} ({check.outcome}): {check.reason}" for check in skipped),
                            "\n".join(f"{check.name}: {check.authorization}" for check in skipped))

    return apply(
        db, row, state, Event.RESULT, details=details, attempt_id=payload.attempt_id,
        outcome=payload.outcome, wait=wait, retry_at=retry_at, retry_allowed=retry_at is not None,
        evidence=Evidence(candidate=payload.candidate, reviewed_candidate=payload.reviewed_candidate,
                          prevalidated_candidate=payload.prevalidated_candidate, bypass=bypass,
                          report_draft=draft_revision, reviewed_report=draft_revision,
                          report_review_notes=payload.report_review_notes,
                          findings=findings),
    )

def draft(db, item_id, execution_id, revision):
    execution_row(db, item_id, execution_id)
    row = db.execute("SELECT markdown FROM report_drafts WHERE execution_id=? AND revision=?",
                     (execution_id, revision)).fetchone()
    if row is None:
        raise Missing("Report draft not found")
    return row["markdown"]
