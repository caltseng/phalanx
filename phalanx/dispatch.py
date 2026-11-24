"""Atomic admission, lease heartbeats, and stopped-attempt reconciliation."""

from hmac import compare_digest
import json
from time import time

from . import intake
from .config import DEFAULT_LEASE_SECONDS
from .transitions import RUNNER_ONLY_SLOTS, RUNNER_ONLY_STAGES, TERMINAL, Event, Stage, Status, Wait


# The server sets this once at startup from PHALANX_LEASE_SECONDS.
LEASE_SECONDS = DEFAULT_LEASE_SECONDS
DEFAULT_CAPACITY = 4
PRIORITIES = range(3)
QUEUE_BUCKET = """
    SELECT id, item_id, ready_since FROM executions {index}
    WHERE status = 'queued' AND ready_since IS NOT NULL AND ready_since <= ?
      AND dependency_wait_json IS NULL
      AND queue_repository = ? AND queue_affinity = ? AND queue_priority = ?
"""
QUEUE_ORDER = "ORDER BY ready_since, item_id, id LIMIT 1"
QUEUE_HEAD = QUEUE_BUCKET.format(index="") + QUEUE_ORDER
STAGE_QUEUE_HEAD = (
    QUEUE_BUCKET.format(index="INDEXED BY queue_stage_heads") + "AND stage = ? " + QUEUE_ORDER
)
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


def queue_head(db, runner_id, repository_ids, now_ms, aging_ms, stages=()):
    # Within each bucket an older item can only gain as much or more priority.
    best_rank = None
    best = None
    query = STAGE_QUEUE_HEAD if stages else QUEUE_HEAD
    stage_buckets = [(stage,) for stage in stages] if stages else [()]
    for repository_id in repository_ids:
        for affinity in ("", runner_id):
            for priority in PRIORITIES:
                for stage in stage_buckets:
                    row = db.execute(query, (now_ms, repository_id, affinity, priority, *stage)).fetchone()
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
    filters = {"runner_id": request.runner_id, "repository_ids": sorted(set(request.repository_ids)),
               "slots": request.slots}
    if request.stages is not None:
        filters["stages"] = sorted(set(request.stages))
    return intake.replayable(
        db, "dispatch:claim", lease_digest, filters,
        lambda: admit(db, request, lease_digest),
        should_record=lambda result: result["status"] == "claimed",
    )


def admit(db, request, lease_digest):
    settings = db.execute("SELECT * FROM dispatch_settings WHERE id=1").fetchone()
    if settings["paused"]:
        return {"status": "paused"}
    capacity = _capacity(db, request.runner_id)
    if request.slots is not None:
        capacity = min(capacity, request.slots)
    reserved, agents = _reserved(db, request.runner_id)
    full = {"status": "at_capacity", "reserved": reserved, "capacity": capacity}
    if reserved >= capacity + RUNNER_ONLY_SLOTS:
        return full

    now = time()
    repository_ids = sorted(set(request.repository_ids))
    stages = sorted(set(request.stages or ()))
    restricted = agents >= capacity
    if restricted:
        # Agent workers hold the whole capacity; only runner-only stages may use the extra slots.
        stages = sorted(str(stage) for stage in set(stages or Stage) & RUNNER_ONLY_STAGES)
        if not stages:
            return full
    expires = int(now) + LEASE_SECONDS
    # Each stale head is removed from the index by claim_attempt. Bound the
    # repair work in one poll so other writers can acquire the transaction.
    for _ in range(8):
        head = queue_head(db, request.runner_id, repository_ids, int(now * 1000),
                          settings["aging_seconds"] * 1000, stages)
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
    return full if restricted else {"status": "no_eligible_work"}


def _capacity(db, runner_id):
    limit = db.execute("SELECT capacity FROM runner_limits WHERE runner_id=?", (runner_id,)).fetchone()
    return limit["capacity"] if limit else DEFAULT_CAPACITY


def _reserved(db, runner_id):
    """Count the runner's open attempts, and those at stages that run an agent worker."""
    runner_only = sorted(map(str, RUNNER_ONLY_STAGES))
    row = db.execute(
        f"""SELECT count(*), coalesce(sum(stage NOT IN ({",".join("?" * len(runner_only))})), 0)
            FROM attempts WHERE runner_id=? AND ended_at IS NULL""",
        (*runner_only, runner_id),
    ).fetchone()
    return row[0], row[1]


def hand_off(db, item_id, attempt_id, view, continuation):
    """Keep the ended attempt's slot for the next stage, or record why it was released.

    Runs in the transaction that ended the attempt, so no other claim can take
    the slot in between. The next stage still gets its own attempt and lease.
    """
    attempt = db.execute("SELECT runner_id, ended_at FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if attempt is None or attempt["ended_at"] is None:
        return view
    runner_id = attempt["runner_id"]
    row = intake.execution_row(db, item_id, view["id"])
    state = intake.STATE.validate_json(row["state_json"])
    now = time()
    reason = _release_reason(db, row, state, runner_id, continuation, now)
    assignment = None
    expires = int(now) + LEASE_SECONDS
    if reason is None:
        assignment = intake.claim_attempt(
            db, row, state, runner_id=runner_id, lease_digest=intake.digest(continuation["lease"]),
            lease_expires_at=expires, now=now, continued_from=attempt_id,
        )
        reason = None if assignment is not None else "dependency wait"
    if assignment is None:
        db.execute(
            """UPDATE transition_history SET details_json=json_set(details_json, '$.capacity_release', ?)
               WHERE execution_id=? AND attempt_id=? AND event != ? AND version=(
                   SELECT max(version) FROM transition_history
                   WHERE execution_id=? AND attempt_id=? AND event != ?)""",
            (reason, row["id"], attempt_id, Event.CLAIM, row["id"], attempt_id, Event.CLAIM),
        )
        return view
    work_item = intake.item_row(db, row["item_id"])
    return {**view, "continuation": {
        "status": "claimed", **assignment, "runner_id": runner_id, "lease_expires_at": expires,
        "item_id": row["item_id"], "request": json.loads(work_item["request_json"]),
    }}


def _release_reason(db, row, state, runner_id, continuation, now):
    if state.status in TERMINAL:
        return f"closed as {state.status}"
    if state.pending_output is not None:
        return "finalizing output"
    if state.stopping is not None:
        return "stop requested"
    if state.status == Status.WAITING_FOR_INPUT:
        return "input wait"
    if state.status == Status.WAITING_FOR_EXTERNAL:
        return "external wait"
    if state.status != Status.QUEUED:
        return str(state.status)
    if row["retry_at"] is not None or row["ready_since"] > int(now * 1000):
        return "retry backoff"
    if row["dependency_wait_json"] is not None:
        return "dependency wait"
    if continuation is None:
        return "runner did not offer a continuation"
    if state.stage not in continuation["stages"]:
        return f"runner cannot run {state.stage}"
    if row["queue_affinity"] not in ("", runner_id):
        return "pinned to another runner"
    if db.execute("SELECT paused FROM dispatch_settings WHERE id=1").fetchone()["paused"]:
        return "dispatch paused"
    capacity = _capacity(db, runner_id)
    reserved, agents = _reserved(db, runner_id)
    if reserved >= capacity + RUNNER_ONLY_SLOTS or (
        state.stage not in RUNNER_ONLY_STAGES and agents >= capacity
    ):
        return "runner at capacity"
    return None


def open_attempt(db, runner_id, attempt_id):
    attempt = db.execute(
        "SELECT * FROM attempts WHERE id=? AND runner_id=? AND ended_at IS NULL",
        (attempt_id, runner_id),
    ).fetchone()
    if attempt is None:
        raise intake.Conflict("The attempt is no longer reserved by this runner")
    return attempt


def renew(db, runner_id, attempt_id, lease_digest, worker=None, blocker=None):
    attempt = open_attempt(db, runner_id, attempt_id)
    if not compare_digest(attempt["lease_digest"], lease_digest):
        raise intake.Conflict("The lease secret does not match the current attempt")
    now = time()
    if attempt["lease_expires_at"] <= now:
        raise intake.Conflict("Lease expired. Check the runner's recovery candidates")
    expires = max(attempt["lease_expires_at"], int(now) + LEASE_SECONDS)
    # Each renewal reports the runner's current blocker; omitting it clears the last one.
    db.execute("UPDATE attempts SET lease_expires_at=?, blocker=? WHERE id=?", (expires, blocker, attempt_id))
    if worker is not None:
        db.execute("INSERT OR REPLACE INTO attempt_workers VALUES (?, ?, ?)",
                   (attempt_id, worker.reference, worker.url))
    return {"attempt_id": attempt_id, "lease_expires_at": expires}


def report_recovery_wait(db, runner_id, attempt_id, blocker):
    # An expired lease cannot renew, so recovery reports its owner wait here; None clears it.
    attempt = open_attempt(db, runner_id, attempt_id)
    if attempt["lease_expires_at"] > time():
        raise intake.Conflict("The lease is live. Report blockers on renewal")
    db.execute("UPDATE attempts SET blocker=? WHERE id=?", (blocker, attempt_id))
    return {"attempt_id": attempt_id, "blocker": blocker}


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
    details = {"runner_id": runner_id, "reconciliation": body.reconciliation,
               "capacity_release": "lease expired"}
    if state.stopping in {Status.CANCELLED, Status.FAILED}:
        return intake.confirm_stop(db, row, state, attempt_id, details=details)
    if state.status != Status.RUNNING or state.stage != attempt["stage"]:
        raise intake.Conflict("This attempt has no supported stopped-worker recovery")
    retry_at = intake.stopped_retry_at(row, now)
    if retry_at is None:
        return intake.apply(
            db, row, state, Event.RETRY, attempt_id=attempt_id, details=details,
            wait=Wait("Five stopped worker attempts require a recovery decision", "owner intervention"),
        )
    return intake.apply(
        db, row, state, Event.RETRY, attempt_id=attempt_id,
        details={**details, "retry_at_ms": retry_at}, retry_at=retry_at, retry_allowed=True,
    )


def recover_claim(db, runner_id, attempt_id, lease_digest, body):
    # Re-lease the same expired attempt so its writer, checkpoint and saved
    # submission stay bound to one attempt. The old secret stops working.
    attempt = open_attempt(db, runner_id, attempt_id)
    now = time()
    if attempt["lease_expires_at"] > now:
        raise intake.Conflict("The existing lease is still live. Renew it instead")
    row, state = intake.check_current(db, body.item_id, attempt["execution_id"], body.expected_version)
    if state.status != Status.RUNNING or state.stage != attempt["stage"]:
        raise intake.Conflict("This attempt can no longer be adopted")
    expires = int(now) + LEASE_SECONDS
    db.execute("UPDATE attempts SET lease_digest=?, lease_expires_at=? WHERE id=?",
               (lease_digest, expires, attempt_id))
    db.execute("INSERT INTO attempt_adoptions VALUES (?, ?, ?, ?, ?, ?)",
               (row["id"], row["version"], attempt_id, runner_id, body.reconciliation,
                intake.timestamp()))
    return {"attempt_id": attempt_id, "execution_id": row["id"], "version": row["version"],
            "lease_expires_at": expires}
