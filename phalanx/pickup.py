"""One authoritative pickup decision for Captain discovery, Scribe and queue flags."""

import json

from . import dispatch
from .captains import active_holds, is_live
from .transitions import Stage


def live_captains(db, now):
    return [{"id": row["id"], **json.loads(row["pickup_json"])}
            for row in db.execute("SELECT * FROM captains WHERE state='active' ORDER BY id")
            if is_live(row, now)]


def context(db, now, *, capacity=None):
    capacity = dispatch.settings(db) if capacity is None else capacity
    rework = dispatch.first_rework(db)
    return {
        "captains": live_captains(db, now),
        "holds": active_holds(db, now),
        "capacity": capacity,
        "rework": rework,
        "admission": {stage: dispatch.admission_wait(db, stage, capacity=capacity, rework=rework)
                      for stage in Stage},
    }


def decision(row, now, current):
    """Evaluate a queued execution with no writer; IDs in charter prose are not an allow-list."""
    def blocked(code, reason, trigger):
        return {"eligible": False, "code": code, "reason": reason, "trigger": trigger}

    state = json.loads(row["state_json"])
    if state.get("stopping") or state.get("pending_output"):
        return blocked("closing", "Execution is stopping or finalizing", "Finish the pending finalization")
    if current["capacity"]["paused"]:
        return blocked("paused", "Dispatch is paused", "The owner resumes dispatch")
    unmet = json.loads(row["dependency_wait_json"] or "[]")
    if row["dependency_wait_json"] is not None:
        reason = ("Waiting on dependency " + "; ".join(
            f"{link['upstream_id']}: {link['reason']}" for link in unmet)
            if unmet else "Persisted dependency wait requires reconciliation")
        return blocked("dependency", reason, "Prerequisite milestone or dependency reconciliation")
    if row["retry_at"] is not None and row["retry_at"] > int(now.timestamp() * 1000):
        return blocked("retry", f"Retry scheduled for {row['retry_at']} ms since epoch",
                       "The retry delay elapses")
    if row["ready_since"] is None:
        return blocked("not_ready", "Execution has no ready scheduling timestamp",
                       "A Captain reconciles the execution's scheduling state")
    if row["ready_since"] > int(now.timestamp() * 1000):
        return blocked("not_ready", f"Execution scheduled for {row['ready_since']} ms since epoch",
                       "The scheduling delay elapses")
    hold = current["holds"].get(row["id"])
    if hold:
        return blocked("held", hold, "The Captain clears the hold or it expires")
    captains = [captain for captain in current["captains"] if captain["repositories"]]
    unknown = [captain["id"] for captain in current["captains"] if not captain["repositories"]]
    if not captains and unknown:
        return blocked("uninitialized", "Captain pickup scope/readiness is uninitialized",
                       "An updated Captain heartbeat publishes the current charter and readiness")
    if not captains:
        return blocked("no_captain", "No live active Captain with pickup authority",
                       "Start or heartbeat an authorized Captain")
    scoped = [captain for captain in captains if row["repository_id"] in captain["repositories"]]
    if not scoped:
        if unknown:
            return blocked("uninitialized", "Repository has no reported pickup scope; "
                           f"Captain scope/readiness is uninitialized: {', '.join(unknown)}",
                           "An updated Captain heartbeat publishes the current charter and readiness")
        return blocked("out_of_scope", f"Repository {row['repository_id']} is outside active Captain charter scope",
                       "The owner assigns a Captain whose charter includes this repository")
    affinity = row["runner_affinity"]
    mapped = [captain for captain in scoped if not affinity or captain["runner_id"] == affinity]
    if not mapped:
        return blocked("affinity", f"Waiting for runner affinity {affinity}",
                       "An authorized Captain on the assigned runner heartbeats")
    reasons = [captain["repositories"][row["repository_id"]] for captain in mapped]
    if all(reason is not None for reason in reasons):
        return blocked("repository_blocked", "; ".join(dict.fromkeys(reasons)),
                       "Resolve the checkout mapping or owner trust blocker; heartbeat readiness")
    waiting = current["admission"][row["stage"]]
    if waiting:
        return blocked("capacity", waiting, "Capacity becomes available; a Captain retries admission")
    return {"eligible": True, "code": "eligible", "reason": None,
            "captain_ids": [captain["id"] for captain in mapped
                            if captain["repositories"][row["repository_id"]] is None],
            "trigger": "A Captain launches this item's writer"}
