"""Atomic admission, lease heartbeats, and stopped-attempt reconciliation."""

from hmac import compare_digest
import json
from time import time

from . import intake
from .transitions import Event, Status, Wait


LEASE_SECONDS = 90
DEFAULT_CAPACITY = 4
PRIORITIES = range(3)
QUEUE_HEAD = """
    SELECT id, item_id, ready_since FROM executions
    WHERE status = 'queued' AND ready_since IS NOT NULL AND ready_since <= ?
      AND dependency_wait_json IS NULL
      AND queue_repository = ? AND queue_affinity = ? AND queue_priority = ?
    ORDER BY ready_since, item_id, id LIMIT 1
"""
RECOVERY_CANDIDATES = """
    SELECT a.id AS attempt_id, a.execution_id, a.lease_expires_at,
           e.item_id, e.version, e.stage, e.status
    FROM attempts AS a JOIN executions AS e ON e.id = a.execution_id
    WHERE a.runner_id = ? AND a.ended_at IS NULL AND a.lease_expires_at <= ?
    ORDER BY a.lease_expires_at, a.id
"""


def set_dispatch(db, settings):
    db.execute("UPDATE dispatch_settings SET paused=?, aging_seconds=? WHERE id=1",
               (settings.paused, settings.aging_seconds))
    return settings.model_dump()


def set_capacity(db, runner_id, capacity):
    db.execute(
        """INSERT INTO runner_limits VALUES (?, ?)
           ON CONFLICT(runner_id) DO UPDATE SET capacity=excluded.capacity""",
        (runner_id, capacity),
    )
    return {"runner_id": runner_id, "capacity": capacity}


def queue_head(db, runner_id, repository_ids, now_ms, aging_ms):
    # Within each bucket an older item can only gain as much or more priority.
    best_rank = None
    best = None
    for repository_id in repository_ids:
        for affinity in ("", runner_id):
            for priority in PRIORITIES:
                row = db.execute(QUEUE_HEAD, (now_ms, repository_id, affinity, priority)).fetchone()
                if row is None:
                    continue
                ready_since = row["ready_since"]
                effective = min(2, priority + (now_ms - ready_since) // aging_ms)
                rank = (-effective, ready_since, row["item_id"], row["id"])
                if best_rank is None or rank < best_rank:
                    best_rank, best = rank, row
    return best


def claim(db, request, lease_digest):
    # One fresh caller-generated secret per logical claim. Empty polls have
    # no receipt, so polling does not grow durable history or pin a miss.
    return intake.replayable(
        db, "dispatch:claim", lease_digest,
        {"runner_id": request.runner_id, "repository_ids": sorted(set(request.repository_ids)),
         "slots": request.slots},
        lambda: admit(db, request, lease_digest),
        should_record=lambda result: result["status"] == "claimed",
    )


def admit(db, request, lease_digest):
    settings = db.execute("SELECT * FROM dispatch_settings WHERE id=1").fetchone()
    if settings["paused"]:
        return {"status": "paused"}
    limit = db.execute("SELECT capacity FROM runner_limits WHERE runner_id=?",
                       (request.runner_id,)).fetchone()
    capacity = limit["capacity"] if limit else DEFAULT_CAPACITY
    if request.slots is not None:
        capacity = min(capacity, request.slots)
    reserved = db.execute(
        "SELECT count(*) FROM attempts WHERE runner_id=? AND ended_at IS NULL",
        (request.runner_id,),
    ).fetchone()[0]
    if reserved >= capacity:
        return {"status": "at_capacity", "reserved": reserved, "capacity": capacity}

    now = time()
    repository_ids = sorted(set(request.repository_ids))
    expires = int(now) + LEASE_SECONDS
    # Each stale head is removed from the index by claim_attempt. Bound the
    # repair work in one poll so other writers can acquire the transaction.
    for _ in range(8):
        head = queue_head(db, request.runner_id, repository_ids, int(now * 1000),
                          settings["aging_seconds"] * 1000)
        if head is None:
            break
        row = intake.execution_row(db, head["item_id"], head["id"])
        state = intake.STATE.validate_json(row["state_json"])
        assignment = intake.claim_attempt(
            db, row, state, runner_id=request.runner_id, lease_digest=lease_digest,
            lease_expires_at=expires, now=now,
        )
        if assignment is not None:
            work_item = intake.item_row(db, row["item_id"])
            return {
                "status": "claimed", **assignment, "runner_id": request.runner_id,
                "lease_expires_at": expires, "item_id": row["item_id"],
                "request": json.loads(work_item["request_json"]),
            }
    return {"status": "no_eligible_work"}


def open_attempt(db, runner_id, attempt_id):
    attempt = db.execute(
        "SELECT * FROM attempts WHERE id=? AND runner_id=? AND ended_at IS NULL",
        (attempt_id, runner_id),
    ).fetchone()
    if attempt is None:
        raise intake.Conflict("The attempt is no longer reserved by this runner")
    return attempt


def renew(db, runner_id, attempt_id, lease_digest):
    attempt = open_attempt(db, runner_id, attempt_id)
    if not compare_digest(attempt["lease_digest"], lease_digest):
        raise intake.Conflict("The lease secret does not match the current attempt")
    now = time()
    if attempt["lease_expires_at"] <= now:
        raise intake.Conflict("Lease expired. Check the runner's recovery candidates")
    expires = max(attempt["lease_expires_at"], int(now) + LEASE_SECONDS)
    db.execute("UPDATE attempts SET lease_expires_at=? WHERE id=?", (expires, attempt_id))
    return {"attempt_id": attempt_id, "lease_expires_at": expires}


def recovery_candidates(db, runner_id):
    return [
        {**dict(row), "reason": "lease_expired",
         "next_action": "reconcile_worker_and_outputs"}
        for row in db.execute(RECOVERY_CANDIDATES, (runner_id, time()))
    ]


def recover_stopped(db, runner_id, attempt_id, body):
    attempt = open_attempt(db, runner_id, attempt_id)
    now = time()
    if attempt["lease_expires_at"] > now:
        raise intake.Conflict("The existing lease is still live")
    row, state = intake.check_current(db, body.item_id, attempt["execution_id"], body.expected_version)
    details = {"runner_id": runner_id, "reconciliation": body.reconciliation}
    if state.stopping == Status.CANCELLED:
        return intake.confirm_cancellation(db, row, state, attempt_id, details=details)
    if state.status != Status.RUNNING or state.stage != attempt["stage"]:
        raise intake.Conflict("This attempt has no supported stopped-worker recovery")
    if row["retry_count"] >= 4:
        return intake.apply(
            db, row, state, Event.RETRY, attempt_id=attempt_id, details=details,
            wait=Wait("Five stopped worker attempts require a recovery decision", "owner intervention"),
        )
    retry_at = int(now * 1000) + min(120, 5 * 2 ** row["retry_count"]) * 1000
    return intake.apply(
        db, row, state, Event.RETRY, attempt_id=attempt_id,
        details={**details, "retry_at_ms": retry_at}, retry_at=retry_at, retry_allowed=True,
    )
