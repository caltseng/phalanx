"""Owner replies and runner-scheduled external rechecks."""

import json
from random import randint
from time import time

from . import intake
from .transitions import Event, Status, Wait


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
    return schedule(db, row, state, payload.attempt_id, payload.reason, payload.retry_after_seconds,
                    {"gate": payload.gate, "revision": payload.revision.model_dump()})


def schedule(db, row, state, attempt_id, reason, retry_after_seconds, details):
    """Record a verified stage's wait and its recheck in the same transaction."""
    # A small floor stops a misconfigured runner from hammering the provider.
    delay = max(5, retry_after_seconds)
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
