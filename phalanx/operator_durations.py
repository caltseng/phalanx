"""Durations for the operator board, derived only from recorded timestamps.

Each transition_history row records the state an execution entered at that version, so
consecutive rows partition its time without overlap. Time between rows is never
estimated: a missing version makes the affected interval unavailable and the result
partial.
"""

from datetime import datetime
from hashlib import sha256

TERMINAL = frozenset({"completed", "failed", "cancelled"})
PHASES = {"queued": "queued", "running": "executing",
          "waiting_for_input": "waiting", "waiting_for_external": "waiting"}
VISIT_LIMIT = 50

# Item lifecycle rows across every execution of one item, oldest first.
LIFECYCLE = """
    SELECT history.execution_id, history.version, history.event, history.attempt_id, history.created_at,
           json_extract(history.state_json, '$.stage') AS stage,
           json_extract(history.state_json, '$.status') AS status,
           json_extract(history.details_json, '$.outcome') AS outcome,
           coalesce(json_extract(history.details_json, '$.reason'),
                    json_extract(history.state_json, '$.wait.reason')) AS reason,
           history.event = 'retry' OR json_extract(history.details_json, '$.retry_at_ms') IS NOT NULL
               AS retried
    FROM executions AS execution
    JOIN transition_history AS history ON history.execution_id = execution.id
    WHERE execution.item_id = ?
    ORDER BY execution.created_at, execution.id, history.version
"""

# Follow each open attempt back through the attempts that handed it their runner slot.
# A continuation is claimed in the transaction that ended its predecessor, so the slot
# stayed occupied from the first attempt's start.
OCCUPANCY = """
    WITH RECURSIVE chain(origin, attempt_id, execution_id, started_at, handoffs) AS (
        SELECT id, id, execution_id, started_at, 0 FROM attempts
        WHERE ended_at IS NULL AND id IN ({ids})
        UNION ALL
        SELECT chain.origin, prior.id, prior.execution_id, prior.started_at, chain.handoffs + 1
        FROM chain
        JOIN transition_history AS claim
            ON claim.execution_id = chain.execution_id AND claim.attempt_id = chain.attempt_id
           AND claim.event = 'claim'
        JOIN attempts AS prior ON prior.id = json_extract(claim.details_json, '$.continued_from')
    )
    SELECT origin, min(started_at) AS occupied_since, max(handoffs) AS handoffs
    FROM chain GROUP BY origin
"""


def parse(stamp):
    return datetime.fromisoformat(stamp)


def elapsed(start, end):
    """Whole seconds from start to end, never negative; None when either end is unknown."""
    if not start or not end:
        return None
    start = parse(start) if isinstance(start, str) else start
    end = parse(end) if isinstance(end, str) else end
    return max(0, int((end - start).total_seconds()))


def occupancy(db, attempt_ids):
    """Map each open attempt to (occupied_since, handoffs) for its runner slot."""
    ids = sorted(set(filter(None, attempt_ids)))
    if not ids:
        return {}
    rows = db.execute(OCCUPANCY.format(ids=",".join("?" * len(ids))), ids)
    return {row["origin"]: (row["occupied_since"], row["handoffs"]) for row in rows}


def runner_label(runner_id):
    # Runner IDs stay off the board; a short digest still tells slots on different runners apart.
    return "runner " + sha256(runner_id.encode("utf-8")).hexdigest()[:8]


def slot_activity(status, blocker, lease_expires_at, now):
    if status == "waiting_for_external":
        return "external_wait"
    if lease_expires_at is not None and lease_expires_at <= now.timestamp():
        return "lease_expired"
    if blocker:
        return "resource_wait"
    return "executing"


def slot(attempt, occupied, status, blocker, lease_expires_at, now):
    """Runner slot view for an open attempt; occupancy spans retained-slot handoffs."""
    since, handoffs = occupied.get(attempt["id"], (attempt["started_at"], 0))
    return {
        "attempt_id": attempt["id"],
        "attempt_started_at": attempt["started_at"],
        "attempt_age_seconds": elapsed(attempt["started_at"], now),
        "occupied_since": since,
        "occupied_seconds": elapsed(since, now),
        "handoffs": handoffs,
        "activity": slot_activity(status, blocker, lease_expires_at, now),
    }


def _phases():
    return {"queued_seconds": 0, "waiting_seconds": 0, "executing_seconds": 0}


def lifecycle(rows, item_created_at, now):
    """Ordered stage visits and per-stage totals for one item.

    Rows come from LIFECYCLE. A visit is a maximal run of one stage within one execution,
    so a return to an earlier stage or a rerun opens a new visit. Each row's interval ends
    at the next recorded row, at its own time when terminal, or at `now` while the latest
    execution is open.
    """
    now_stamp = now.isoformat(timespec="microseconds")
    visits, notes = [], []
    seen = set()
    groups = []
    for row in rows:
        if not groups or groups[-1][0]["execution_id"] != row["execution_id"]:
            groups.append([])
        groups[-1].append(row)
    closed_at = None
    for index, group in enumerate(groups):
        latest = index == len(groups) - 1
        visit = None
        if group[0]["version"] != 1:
            notes.append("History before version %d of execution %s is missing."
                         % (group[0]["version"], group[0]["execution_id"]))
        for position, row in enumerate(group):
            following = group[position + 1] if position + 1 < len(group) else None
            if visit is None or visit["stage"] != row["stage"]:
                visit = {
                    "execution_id": row["execution_id"], "stage": row["stage"],
                    "entered_at": row["created_at"], "exited_at": None, "open": False,
                    "seconds": 0, **_phases(), "attempt_ids": [], "retries": 0,
                    "returned": row["stage"] in seen, "outcome": None, "reason": None,
                    "partial": position == 0 and row["version"] != 1,
                }
                seen.add(row["stage"])
                visits.append(visit)
            if row["event"] == "claim" and row["attempt_id"]:
                visit["attempt_ids"].append(row["attempt_id"])
            if row["retried"]:
                visit["retries"] += 1
            if row["status"] in TERMINAL:
                visit.update(exited_at=row["created_at"], outcome=row["status"], reason=row["reason"])
                if latest:
                    closed_at = row["created_at"]
                break
            if following is None:
                if latest:
                    end = now_stamp
                    visit["open"] = True
                else:
                    end = None
                    notes.append("Execution %s ended without a recorded close." % row["execution_id"])
            elif following["version"] != row["version"] + 1:
                end = None
                notes.append("History of execution %s is missing versions %d to %d."
                             % (row["execution_id"], row["version"] + 1, following["version"] - 1))
            else:
                end = following["created_at"]
            duration = elapsed(row["created_at"], end)
            if duration is None:
                visit["partial"] = True
            else:
                visit[PHASES[row["status"]] + "_seconds"] += duration
                visit["seconds"] += duration
            if following is not None and following["stage"] != row["stage"]:
                visit.update(exited_at=following["created_at"],
                             outcome=following["outcome"] or following["event"],
                             reason=following["reason"])

    stages = {}
    totals = {"seconds": 0, **_phases()}
    for visit in visits:
        total = stages.setdefault(visit["stage"], {"stage": visit["stage"], "visits": 0, "seconds": 0,
                                                   **_phases(), "partial": False})
        total["visits"] += 1
        total["partial"] |= visit["partial"]
        for key in totals:
            total[key] += visit[key]
            totals[key] += visit[key]
    partial = bool(notes)
    return {
        "item_created_at": item_created_at,
        # Total age since intake stops when the latest execution closes.
        "age_seconds": elapsed(item_created_at, closed_at or now),
        "closed_at": closed_at,
        "coverage": "partial" if partial else "complete",
        "coverage_note": " ".join(notes) if partial else None,
        **totals,
        "stages": list(stages.values()),
        "visits": visits[-VISIT_LIMIT:],
        "visits_omitted": max(0, len(visits) - VISIT_LIMIT),
    }
