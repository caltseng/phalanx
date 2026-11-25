"""Transactions for durable intake and versioned lifecycle events."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from hashlib import sha256
import json
from time import time
from uuid import uuid4

from pydantic import TypeAdapter

from . import dependencies
from .finalization_schema import enqueue
from .pr_models import GITHUB_REMOTE
from .transitions import (
    Event, Evidence, Execution, Outcome, PublicationMode, Stage, Status, Target, TERMINAL, Wait, decide, start,
)


STATE = TypeAdapter(Execution)
PRIORITY = {"low": 0, "normal": 1, "urgent": 2}
REWORK_STAGES = {Stage.SELF_REVIEW}
REWORK_TARGETS = {Stage.DEVELOPMENT}
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


def latest_reply(db, execution_id):
    reply = db.execute(
        """SELECT version, created_at, details_json FROM transition_history
           WHERE execution_id=? AND event=? ORDER BY version DESC LIMIT 1""",
        (execution_id, Event.REPLY),
    ).fetchone()
    if reply is None:
        return None
    details = json.loads(reply["details_json"])
    return {"version": reply["version"], "created_at": reply["created_at"],
            "answer": details["answer"], "answered_by": details.get("answered_by", "owner")}


def inspect(db, item_id):
    item = item_row(db, item_id)
    execution = current_execution(db, item_id)
    return {
        "id": item["id"], "created_at": item["created_at"],
        **json.loads(item["request_json"]),
        "dependencies": dependencies.configured_dependencies(db, item_id),
        "execution": execution_view(execution),
        "last_reply": latest_reply(db, execution["id"]),
    }


def display_title(db, item_id):
    item_row(db, item_id)
    row = db.execute(
        "SELECT * FROM item_titles WHERE item_id=? ORDER BY revision DESC LIMIT 1", (item_id,),
    ).fetchone()
    return (dict(row) if row else
            {"item_id": item_id, "revision": 0, "title": None, "changed_by": None, "created_at": None})


def set_display_title(db, item_id, payload):
    current = display_title(db, item_id)
    if current["revision"] != payload.expected_revision:
        raise Conflict("Display title revision has changed")
    db.execute("INSERT INTO item_titles VALUES (?, ?, ?, 'owner', ?)",
               (item_id, current["revision"] + 1, payload.title, timestamp()))
    return display_title(db, item_id)


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
        "version": row["version"], "event": "github_observation", "attempt_id": None,
        "created_at": row["observed_at"],
        "details": {**json.loads(row["observation_json"]), "last_observed_at": row["last_observed_at"]},
        "state": states[row["version"]],
    } for row in db.execute(
        "SELECT * FROM github_observations WHERE execution_id=?", (execution_id,),
    )]
    events += [{
        "version": row["version"], "event": "local_checks", "attempt_id": None,
        "created_at": row["checked_at"],
        "details": {"head": row["head_sha"], "failures": json.loads(row["failures_json"])},
        "state": states[row["version"]],
    } for row in db.execute(
        "SELECT * FROM local_check_results WHERE execution_id=?", (execution_id,),
    )]
    events += [{
        "version": row["version"], "event": "captain_action", "attempt_id": None,
        "created_at": row["created_at"],
        "details": {"captain_id": row["captain_id"], "action": row["action"], "reason": row["reason"]},
        "state": states[row["version"]],
    } for row in db.execute(
        "SELECT * FROM captain_actions WHERE execution_id=?", (execution_id,),
    )]
    return sorted(events, key=lambda event: (event["version"], event["created_at"]))


def record(db, execution_id, version, event, details, state_json, now, attempt_id=None):
    db.execute("INSERT INTO transition_history VALUES (?, ?, ?, ?, ?, ?, ?)",
               (execution_id, version, attempt_id, event, encode(details), state_json, now))


def new_execution(db, item_id, target, *, previous_id=None):
    execution_id = uuid4().hex
    clock = datetime.now(UTC)
    now = clock.isoformat(timespec="microseconds")
    item = item_row(db, item_id)
    request = json.loads(item["request_json"])
    mode = PublicationMode(request.get("publication_mode", PublicationMode.READY))
    state_json = encode(asdict(start(target, mode)))
    affinity = request.get("runner_affinity") or ""
    db.execute(
        """INSERT INTO executions (id, item_id, previous_id, version, state_json,
               created_at, updated_at, queue_repository, queue_affinity, queue_priority, ready_since)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
        (execution_id, item_id, previous_id, state_json, now, now, item["repository_id"],
         affinity, PRIORITY[request.get("priority", "normal")],
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
    for name in ("runner_affinity", "label"):
        if request[name] is None:
            del request[name]
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
    edits = payload.model_dump(mode="json", exclude_unset=True, exclude={"expected_version", "steered_by"})
    target_only = state.status not in TERMINAL and set(edits) == {"target"}
    if (state.status != Status.QUEUED and not target_only) or state.stopping or state.pending_output:
        raise Conflict("Steering requires queued work or a target-only change to open PR work. "
                       "Resolve the active execution or rerun it first.")

    if not edits or any(value is None for name, value in edits.items() if name not in ("runner_affinity", "label")):
        raise Conflict("Steering requires at least one edit. Use an empty list to clear list fields.")
    state_json = row["state_json"]
    if "target" in edits:
        if Target.REPORT in (state.target, payload.target):
            raise Conflict("Steering can change only between ready_for_check_in and checked_in. "
                           "A report and a PR are different outputs; file a new item instead.")
        state_json = encode(asdict(replace(state, target=payload.target)))
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
        """UPDATE executions SET version=?, state_json=?, updated_at=?, queue_priority=?,
               queue_affinity=?
           WHERE id=?""",
        (row["version"] + 1, state_json, now, PRIORITY[request.get("priority", "normal")],
         request.get("runner_affinity") or "", execution_id),
    )
    record(db, execution_id, row["version"] + 1, "steer",
           {"before": previous, "after": edits, "steered_by": payload.steered_by}, state_json, now)
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
           WHERE execution_id=? AND event=? AND json_extract(state_json, '$.stage') = ?
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


def end_writers(db, execution_id, status):
    """End every open writer of an execution that closed as the terminal status."""
    db.execute("UPDATE executions SET handoff_head=NULL WHERE id=?", (execution_id,))
    db.execute("""UPDATE writers SET ended_at=?, end_reason=?, attempt_id=NULL,
                  building=0, building_requested_at=NULL, capacity_revision=capacity_revision+1
                  WHERE execution_id=? AND ended_at IS NULL""",
               (timestamp(), f"closed as {status}", execution_id))


def claim_attempt(db, row, state, *, writer_id, now=None, continued_from=None):
    """Persist the writer's stage claim in the admission transaction."""
    now = time() if now is None else now
    if state.status != Status.QUEUED:
        raise Conflict("Claim requires a queued execution")
    resolved, unmet = dependencies.resolve_dependencies(db, row["item_id"])
    if unmet:
        dependencies.update_wait(db, row["id"], unmet, int(now * 1000))
        return None
    snapshot = [asdict(link) for link in resolved]
    attempt_id = uuid4().hex
    db.execute(
        """INSERT INTO attempts
           (id, execution_id, stage, started_at) VALUES (?, ?, ?, ?)""",
        (attempt_id, row["id"], state.stage, timestamp()),
    )
    resume = db.execute(
        """SELECT event, details_json FROM transition_history
           WHERE execution_id=? AND version=? AND event=?""",
        (row["id"], row["version"], Event.REPLY),
    ).fetchone()
    details = {"writer_id": writer_id, "ready_since_ms": row["ready_since"], "dependencies": snapshot}
    if continued_from is not None:
        details["continued_from"] = continued_from
    execution = apply(db, row, state, Event.CLAIM, attempt_id=attempt_id, details=details)
    claim = {"attempt_id": attempt_id, "execution": execution, "dependencies": snapshot}
    if resume is not None:
        claim["input_reply"] = json.loads(resume["details_json"])
    return claim


def check_attempt(db, execution_id, state, payload, writer_id):
    attempt = db.execute(
        """SELECT attempt.* FROM attempts AS attempt
           JOIN writers AS writer ON writer.attempt_id=attempt.id
               AND writer.execution_id=attempt.execution_id
           WHERE attempt.id=? AND attempt.execution_id=? AND attempt.ended_at IS NULL
               AND writer.id=? AND writer.ended_at IS NULL""",
        (payload.attempt_id, execution_id, writer_id),
    ).fetchone()
    if attempt is None or payload.stage != state.stage or attempt["stage"] != state.stage:
        raise Conflict("Writer, attempt, or stage is no longer current")
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
    # Delivery writers can still push or merge without an open attempt.
    if state.status == Status.WAITING_FOR_EXTERNAL:
        active = db.execute(
            "SELECT 1 FROM attempts WHERE execution_id=? AND ended_at IS NULL", (execution_id,),
        ).fetchone()
        writer = db.execute(
            "SELECT 1 FROM writers WHERE execution_id=? AND ended_at IS NULL", (execution_id,),
        ).fetchone()
        if active is None and writer is None:
            row, state = check_current(db, item_id, execution_id, updated["version"])
            return apply(db, row, state, Event.STOP_CONFIRMED, details=details)
    return updated


def stop_confirmed(db, item_id, execution_id, payload, writer_id):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    check_attempt(db, execution_id, state, payload, writer_id)
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
    """Bound retries after technical failures or stopped attempts."""
    if row["retry_count"] >= 4:
        return None
    return int(now * 1000) + (5 * 2 ** row["retry_count"]) * 1000


def result(db, item_id, execution_id, payload, writer_id):
    row, state = check_current(db, item_id, execution_id, payload.expected_version)
    check_attempt(db, execution_id, state, payload, writer_id)
    if payload.review_notes is not None and (
        state.stage != Stage.SELF_REVIEW
        or state.candidate is None
        or state.candidate != (payload.reviewed_candidate if payload.outcome == Outcome.SUCCEEDED
                               else payload.candidate)
    ):
        raise Conflict("Review notes require self-review evidence for the current candidate")
    wait = Wait(**payload.wait.model_dump()) if payload.wait else None
    if payload.question is not None:
        wait = Wait(wait.reason if wait else payload.question.prompt, "owner reply")
    excluded = {"report_markdown", "expected_version"}
    excluded.update(field for field in ("review_notes", "pr_url")
                    if getattr(payload, field) is None)
    details = payload.model_dump(mode="json", exclude=excluded)
    publishing = state.stage == Stage.PUBLICATION and payload.outcome == Outcome.SUCCEEDED
    if (payload.pr_url is not None) != publishing:
        raise Conflict("A successful publication result reports the PR URL, and only it does")
    published = None
    if publishing:
        remote, = db.execute("SELECT remote FROM repositories WHERE id=?",
                             (item_row(db, item_id)["repository_id"],)).fetchone()
        match = GITHUB_REMOTE.fullmatch(remote)
        if match is None or not payload.pr_url.lower().startswith(f"https://github.com/{match[1].lower()}/pull/"):
            raise Conflict("The PR must be in the item's registered GitHub repository")
        # The reconciler's URL for the same PR, so its observations match primary_pr exactly.
        published = f"https://github.com/{match[1]}/pull/{payload.pr_url.rsplit('/', 1)[1]}"
        # Every execution shares the item branch, so an earlier execution's PR may still be found there.
        if db.execute(
            """SELECT 1 FROM executions AS execution
               LEFT JOIN github_observations AS observation ON observation.execution_id = execution.id
               WHERE execution.item_id=? AND execution.id<>?
                 AND ((lower(observation.pr_url)=lower(?)
                       AND json_extract(observation.observation_json, '$.pr.merged'))
                      OR (lower(json_extract(execution.state_json, '$.final_output.url'))=lower(?)
                          AND json_extract(execution.state_json, '$.final_output.merge_commit') IS NOT NULL))
               LIMIT 1""",
            (item_id, execution_id, published, published),
        ).fetchone():
            raise Conflict(f"{published} was already merged for an earlier execution. Publish this "
                           "execution's work in a new PR")
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

    findings = payload.review_notes if payload.outcome == Outcome.CHANGES_REQUIRED else None
    return apply(
        db, row, state, Event.RESULT, details=details, attempt_id=payload.attempt_id,
        outcome=payload.outcome, wait=wait, retry_at=retry_at, retry_allowed=retry_at is not None,
        evidence=Evidence(candidate=payload.candidate, reviewed_candidate=payload.reviewed_candidate,
                          report_draft=draft_revision, reviewed_report=draft_revision,
                          report_review_notes=payload.report_review_notes,
                          findings=findings, published_pr=published),
    )

def draft(db, item_id, execution_id, revision):
    execution_row(db, item_id, execution_id)
    row = db.execute("SELECT markdown FROM report_drafts WHERE execution_id=? AND revision=?",
                     (execution_id, revision)).fetchone()
    if row is None:
        raise Missing("Report draft not found")
    return row["markdown"]
