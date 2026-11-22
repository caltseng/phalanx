"""Owner replies and runner-scheduled external rechecks."""

import json
from random import randint
from time import time

from . import intake
from .transitions import Event, Outcome, Status, Wait
from .validation import CHECK_RECHECK_RANGE


def inbox(db, limit, after_time, after_id):
    rows = db.execute(
        """SELECT queued.*, history.details_json FROM (
               SELECT id, item_id, stage, version, updated_at, state_json
               FROM executions WHERE status = 'waiting_for_input' AND (updated_at, id) > (?, ?)
               ORDER BY updated_at, id LIMIT ?
           ) AS queued
           JOIN transition_history AS history
               ON history.execution_id=queued.id AND history.version=queued.version
           ORDER BY queued.updated_at, queued.id""",
        (after_time, after_id, limit),
    )
    return [{
        "item_id": row["item_id"], "execution_id": row["id"], "stage": row["stage"],
        "version": row["version"], "waiting_since": row["updated_at"],
        "wait": json.loads(row["state_json"])["wait"],
        "question": json.loads(row["details_json"]).get("question"),
    } for row in rows]


def reply(db, item_id, execution_id, payload):
    row, state = intake.check_current(db, item_id, execution_id, payload.expected_version)
    details = {"answer": payload.answer, "answered_by": payload.answered_by,
               "wait_version": row["version"]}
    waiting = db.execute(
        "SELECT details_json FROM transition_history WHERE execution_id=? AND version=?",
        (execution_id, row["version"]),
    ).fetchone()
    details["question"] = json.loads(waiting["details_json"]).get("question")
    return intake.apply(db, row, state, Event.REPLY, details=details)


def external_wait(db, item_id, execution_id, payload, lease_digest):
    row, state = intake.check_current(db, item_id, execution_id, payload.expected_version)
    if state.status != Status.RUNNING or state.stopping is not None:
        raise intake.Conflict("Scheduling a new external wait requires a running attempt")
    intake.check_attempt(db, execution_id, state, payload, lease_digest)
    details = {"gate": payload.gate, "revision": payload.revision.model_dump()}
    if payload.wait_limit_seconds is None:
        return schedule(db, row, state, payload.attempt_id, payload.reason, payload.retry_after_seconds,
                        details)
    return wait_or_ask(db, row, state, payload.attempt_id, payload.reason, payload.retry_after_seconds,
                       payload.wait_limit_seconds, details,
                       _STALLED_PROMPTS.get(payload.gate,
                                            f"Resolve the stalled {payload.gate} gate, then reply to resume."))


# Draft-only CI often triggers only on push, so a draft head may never get a run.
_STALLED_PROMPTS = {
    "draft_checks": "Draft-only checks never reported on this head. Re-run them, or remove them "
                    "from the runner's validation policy, then reply to resume.",
}


def wait_or_ask(db, row, state, attempt_id, reason, retry_after_seconds, limit_seconds, details, question):
    """Recheck a pending gate until it has waited `limit_seconds` on one revision, then ask the owner.

    The window starts at the first wait for this gate and revision since the stage began or
    the owner last replied, so a reply grants a fresh window.
    """
    now = int(time() * 1000)
    since, = db.execute("""
        SELECT coalesce(max(version), 0) FROM transition_history
        WHERE execution_id=? AND (event='reply' OR json_extract(state_json, '$.stage')<>?)
    """, (row["id"], state.stage)).fetchone()
    previous = db.execute("""
        SELECT json_extract(details_json, '$.pending_since_ms') FROM transition_history
        WHERE execution_id=? AND version>? AND event='external_wait'
          AND json_extract(details_json, '$.gate')=?
          AND json_extract(details_json, '$.revision.head')=?
          AND json_extract(details_json, '$.revision.base')=?
        ORDER BY version DESC LIMIT 1
    """, (row["id"], since, details["gate"], details["revision"]["head"],
          details["revision"]["base"])).fetchone()
    pending_since = previous[0] if previous and previous[0] is not None else now
    if now - pending_since >= limit_seconds * 1000:
        details.update(question={"prompt": question, "handoff": reason}, outcome=Outcome.NEEDS_INPUT)
        return intake.apply(
            db, row, state, Event.RESULT, details=details, attempt_id=attempt_id,
            outcome=Outcome.NEEDS_INPUT, wait=Wait(reason, "owner reply"),
        )
    details["pending_since_ms"] = pending_since
    return schedule(db, row, state, attempt_id, reason, retry_after_seconds, details)


def schedule(db, row, state, attempt_id, reason, retry_after_seconds, details):
    """Record a verified stage's wait and its recheck in the same transaction."""
    # A small floor stops a misconfigured runner from hammering the provider.
    delay = max(CHECK_RECHECK_RANGE[0], retry_after_seconds)
    due_at = int(time() * 1000) + delay * 1000 + randint(0, 5000)
    updated = intake.apply(
        db, row, state, Event.EXTERNAL_WAIT, details={**details, "due_at_ms": due_at},
        attempt_id=attempt_id, wait=Wait(reason, "scheduled external recheck"),
    )
    db.execute("INSERT INTO external_rechecks VALUES (?, ?, ?)",
               (row["id"], updated["version"], due_at))
    return updated


def due_rechecks(db, limit):
    rows = db.execute(
        """SELECT due.due_at_ms, e.id, e.item_id, e.version, e.stage, e.state_json, h.details_json
           FROM external_rechecks AS due
           JOIN executions AS e ON e.id=due.execution_id AND e.version=due.version
           JOIN transition_history AS h ON h.execution_id=e.id AND h.version=e.version
           WHERE due.due_at_ms <= ? AND e.status = 'waiting_for_external'
           ORDER BY due.due_at_ms, due.execution_id LIMIT ?""",
        (int(time() * 1000), limit),
    )
    return [{
        "item_id": row["item_id"], "execution_id": row["id"], "version": row["version"],
        "stage": row["stage"], "wait": json.loads(row["state_json"])["wait"],
        **_gate_details(row["details_json"]),
        "due_at_ms": row["due_at_ms"],
    } for row in rows]


def recheck(db, item_id, execution_id, payload):
    row, state = intake.check_current(db, item_id, execution_id, payload.expected_version)
    due = db.execute(
        """SELECT h.details_json, r.due_at_ms FROM external_rechecks AS r
           JOIN transition_history AS h ON h.execution_id=r.execution_id AND h.version=r.version
           WHERE r.execution_id=? AND r.version=?""",
        (execution_id, row["version"]),
    ).fetchone()
    if state.status != Status.WAITING_FOR_EXTERNAL or due is None:
        raise intake.Conflict("No scheduled external recheck is active")
    if due["due_at_ms"] > int(time() * 1000):
        raise intake.Conflict("External recheck is not due yet")
    details = _gate_details(due["details_json"])
    details["due_at_ms"] = due["due_at_ms"]
    details["wait_version"] = row["version"]
    return intake.apply(db, row, state, Event.RECHECK, details=details)


def _gate_details(encoded):
    details = json.loads(encoded)
    return {"gate": details["gate"], "revision": details["revision"]}
