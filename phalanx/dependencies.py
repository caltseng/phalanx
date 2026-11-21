"""Dependency links and claim-time resolution against durable execution state."""

from dataclasses import asdict, dataclass
import json
import sqlite3
from typing import Iterable

from .transitions import Execution, PullRequest, Revision, Stage, Status, StoredReport, Target


@dataclass(frozen=True)
class Dependency:
    upstream_id: str
    milestone: Target


@dataclass(frozen=True)
class ResolvedDependency:
    upstream_id: str
    milestone: Target
    execution_id: str
    reference: str
    revision: str
    reviewed_revision: Revision | None = None


@dataclass(frozen=True)
class UnmetDependency:
    upstream_id: str
    milestone: Target
    execution_id: str | None
    reason: str


def replace_dependencies(
    db: sqlite3.Connection, item_id: str, dependencies: Iterable[Dependency],
) -> None:
    """Validate and replace links inside the caller's BEGIN IMMEDIATE transaction."""
    from .intake import Conflict, STATE, current_execution, item_row

    if not db.in_transaction:
        raise RuntimeError("Dependency edits require a write transaction")
    item_row(db, item_id)
    links = tuple(dependencies)
    seen = set()
    for link in links:
        if link.upstream_id == item_id:
            raise Conflict("A work item cannot depend on itself")
        if link.upstream_id in seen:
            raise Conflict(f"Duplicate prerequisite {link.upstream_id}")
        seen.add(link.upstream_id)
        upstream = current_execution(db, link.upstream_id)
        target = STATE.validate_json(upstream["state_json"]).target
        if (target == Target.REPORT) != (link.milestone == Target.REPORT):
            raise Conflict(f"Prerequisite {link.upstream_id} has an incompatible output type")
        cycle = db.execute(
            """WITH RECURSIVE reachable(id) AS (
                   VALUES (?)
                   UNION
                   SELECT d.upstream_id FROM work_dependencies AS d
                   JOIN reachable ON d.item_id = reachable.id
               )
               SELECT 1 FROM reachable WHERE id = ? LIMIT 1""",
            (link.upstream_id, item_id),
        ).fetchone()
        if cycle:
            raise Conflict(f"Prerequisite {link.upstream_id} would create a dependency cycle")

    db.execute("DELETE FROM work_dependencies WHERE item_id = ?", (item_id,))
    db.executemany(
        "INSERT INTO work_dependencies (item_id, upstream_id, milestone) VALUES (?, ?, ?)",
        ((item_id, link.upstream_id, link.milestone) for link in links),
    )


def dependents(db: sqlite3.Connection, upstream_id: str) -> tuple[str, ...]:
    return tuple(row["item_id"] for row in db.execute(
        "SELECT item_id FROM work_dependencies WHERE upstream_id = ? ORDER BY item_id",
        (upstream_id,),
    ))


def configured_dependencies(db: sqlite3.Connection, item_id: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT upstream_id, milestone FROM work_dependencies WHERE item_id = ? ORDER BY upstream_id",
        (item_id,),
    )]


def milestone_evidence(state: Execution):
    """The portions of state that can change a dependent's admission."""
    disposition = state.stopping or state.pending_output
    if disposition:
        return disposition
    if state.status in (Status.FAILED, Status.CANCELLED):
        return state.status
    if state.status == Status.COMPLETED:
        return state.final_output
    if (state.stage in (Stage.READY_FOR_CHECK_IN, Stage.CHECK_IN)
            and state.ready_revision and state.ready_revision.head == state.candidate
            and state.primary_pr):
        return state.primary_pr, state.ready_revision
    return None


def resolve_dependencies(
    db: sqlite3.Connection, item_id: str,
) -> tuple[tuple[ResolvedDependency, ...], tuple[UnmetDependency, ...]]:
    """Read the latest durable evidence. Resolve and persist the snapshot in the claim transaction."""
    from .intake import STATE

    resolved = []
    unmet = []
    for row in db.execute(
        """SELECT d.upstream_id, d.milestone, e.id, e.state_json,
                  EXISTS (SELECT 1 FROM reports WHERE execution_id = e.id) AS report_exists
           FROM work_dependencies AS d
           LEFT JOIN executions AS e ON e.item_id = d.upstream_id
           WHERE d.item_id = ?
             AND NOT EXISTS (SELECT 1 FROM executions WHERE previous_id = e.id)
           ORDER BY d.upstream_id""",
        (item_id,),
    ):
        upstream_id, milestone, execution_id = row["upstream_id"], Target(row["milestone"]), row["id"]
        if execution_id is None:
            unmet.append(UnmetDependency(
                upstream_id, milestone, None, "Prerequisite execution is missing. Repair or replace it.",
            ))
            continue
        state = STATE.validate_json(row["state_json"])
        output = state.final_output
        reason = f"Waiting for prerequisite milestone {milestone}"
        if state.status in (Status.FAILED, Status.CANCELLED) or state.stopping or state.pending_output:
            disposition = state.stopping or state.pending_output or state.status
            reason = f"Prerequisite disposition is {disposition}. Rerun or replace the prerequisite."
        elif milestone == Target.REPORT:
            if (state.status == Status.COMPLETED and state.target == Target.REPORT
                    and isinstance(output, StoredReport) and output.draft_revision and row["report_exists"]):
                resolved.append(ResolvedDependency(
                    upstream_id, milestone, execution_id, output.reference, output.draft_revision,
                ))
                continue
        elif milestone == Target.MERGED:
            if state.status == Status.COMPLETED and isinstance(output, PullRequest) and output.merge_commit:
                resolved.append(ResolvedDependency(
                    upstream_id, milestone, execution_id, output.url, output.merge_commit, output.revision,
                ))
                continue
            if state.status == Status.COMPLETED:
                reason = "Prerequisite completed without check-in. Rerun it with check-in or replace it."
        elif state.status == Status.COMPLETED and isinstance(output, PullRequest):
            resolved.append(ResolvedDependency(
                upstream_id, milestone, execution_id, output.url, output.revision.head, output.revision,
            ))
            continue
        elif (state.stage in (Stage.READY_FOR_CHECK_IN, Stage.CHECK_IN)
              and state.ready_revision and state.primary_pr
              and state.ready_revision.head == state.candidate):
            resolved.append(ResolvedDependency(
                upstream_id, milestone, execution_id, state.primary_pr,
                state.ready_revision.head, state.ready_revision,
            ))
            continue
        unmet.append(UnmetDependency(upstream_id, milestone, execution_id, reason))
    return tuple(resolved), tuple(unmet)


def update_wait(db, execution_id, unmet, now_ms):
    """Start eligibility aging when the last prerequisite clears."""
    wait = json.dumps([asdict(link) for link in unmet], separators=(",", ":")) if unmet else None
    db.execute(
        """UPDATE executions SET dependency_wait_json = ?,
                  ready_since = CASE WHEN ? IS NULL AND dependency_wait_json IS NOT NULL
                                     THEN max(?, coalesce(retry_at, 0)) ELSE ready_since END
           WHERE id = ? AND status = 'queued' AND dependency_wait_json IS NOT ?""",
        (wait, wait, now_ms, execution_id, wait),
    )


def refresh_wait(db, item_id, execution_id, now_ms):
    _, unmet = resolve_dependencies(db, item_id)
    update_wait(db, execution_id, unmet, now_ms)


def refresh_dependents(db, upstream_id, now_ms):
    rows = db.execute(
        """SELECT e.item_id, e.id
           FROM work_dependencies AS d
           JOIN executions AS e ON e.item_id = d.item_id
           WHERE d.upstream_id = ? AND e.status = 'queued'
             AND e.status NOT IN ('completed', 'failed', 'cancelled')""",
        (upstream_id,),
    ).fetchall()
    for row in rows:
        refresh_wait(db, row["item_id"], row["id"], now_ms)


def reconcile_waits(db, now_ms):
    """Rebuild queued dependency waits on server startup from durable evidence."""
    rows = db.execute(
        """SELECT e.item_id, e.id
           FROM (SELECT DISTINCT item_id FROM work_dependencies) AS d
           JOIN executions AS e ON e.item_id = d.item_id
           WHERE e.status = 'queued'
             AND e.status NOT IN ('completed', 'failed', 'cancelled')"""
    ).fetchall()
    for row in rows:
        refresh_wait(db, row["item_id"], row["id"], now_ms)
