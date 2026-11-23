"""GitHub and git observations from the background reconciler, and the flags derived from them.

Observations are timestamped facts. They never change an execution's state or
version, so recording one can't race a running attempt, and they never block work.
"""

from datetime import UTC, datetime, timedelta
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from . import intake
from .intake_models import Payload
from .operator_schema import OPEN
from .pr_models import GITHUB_REMOTE, SHA
from .transitions import item_branch
from .validation import GateState


RECONCILER_STALE = timedelta(minutes=15)
# Runner clocks may run a little ahead of the server's.
CLOCK_SKEW = timedelta(minutes=5)
Ref = Annotated[str, StringConstraints(min_length=1, max_length=255)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
WRITER_RECOVERY = "The writer acts on it, or a Captain relaunches the item"


class ObservedPull(Payload):
    number: Annotated[int, Field(strict=True, ge=1)]
    state: Literal["open", "closed"]
    merged: bool
    draft: bool
    head_ref: Ref
    head_sha: SHA
    base_ref: Ref
    base_sha: SHA
    merge_commit: SHA | None = None
    mergeable: bool | None = None


class LocalChecks(Payload):
    """Trusted runner-local checks on one observed PR head. Empty failures means they passed."""

    head: SHA
    failures: list[Reason] = Field(default_factory=list, max_length=50)


class Observation(Payload):
    observed_at: AwareDatetime
    pr: ObservedPull | None = None
    checks: GateState | None = None
    check_reasons: list[Reason] = Field(default_factory=list, max_length=20)
    feedback: list[Reason] = Field(default_factory=list, max_length=50)
    # The item branch is reachable from its base, yet no PR exists.
    merged_branch: bool = False

    @model_validator(mode="after")
    def consistent(self):
        if self.pr is None and (self.checks or self.check_reasons or self.feedback):
            raise ValueError("Checks and feedback describe a PR")
        if self.pr is not None and self.merged_branch:
            raise ValueError("merged_branch describes an item branch with no PR")
        return self


def _open_execution(db, item_id, execution_id):
    row = intake.execution_row(db, item_id, execution_id)
    if json.loads(row["state_json"])["final_output"] is not None:
        raise intake.Conflict("Execution is closed")
    return row


def record(db, item_id, execution_id, body: Observation):
    row = _open_execution(db, item_id, execution_id)
    state = json.loads(row["state_json"])
    observed_at = body.observed_at.astimezone(UTC)
    if observed_at > datetime.now(UTC) + CLOCK_SKEW:
        raise intake.Conflict("observed_at is in the future")
    url = head = ""
    if body.pr is not None:
        item = intake.item_row(db, item_id)
        remote, = db.execute("SELECT remote FROM repositories WHERE id=?",
                             (item["repository_id"],)).fetchone()
        match = GITHUB_REMOTE.fullmatch(remote)
        if match is None:
            raise intake.Conflict("GitHub observations require a registered github.com remote")
        if body.pr.head_ref != item_branch(item_id):
            raise intake.Conflict("The PR head branch must be the item branch")
        url = f"https://github.com/{match[1]}/pull/{body.pr.number}"
        primary = state.get("primary_pr")
        if primary and primary.lower() != url.lower():
            raise intake.Conflict("The PR must be the item's primary PR")
        url, head = primary or url, body.pr.head_sha
    payload = body.model_dump(mode="json", exclude={"observed_at"})
    payload["pr"] = payload["pr"] and dict(payload["pr"], url=url)
    stamp = observed_at.isoformat(timespec="microseconds")
    key = (execution_id, url, head, intake.digest(intake.encode(payload)))
    # A repeat only advances when it was last seen, so the timeline shows each fact once.
    seen = db.execute(
        """UPDATE github_observations SET last_observed_at = max(last_observed_at, ?)
           WHERE execution_id=? AND pr_url=? AND head_sha=? AND digest=? RETURNING 1""",
        (stamp, *key),
    ).fetchone()
    if seen is None:
        db.execute("INSERT INTO github_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (*key, row["version"], stamp, stamp, intake.encode(payload)))
    checked = head and db.execute(
        "SELECT 1 FROM local_check_results WHERE execution_id=? AND head_sha=?", (execution_id, head),
    ).fetchone()
    return {"recorded": seen is None, "local_checks_recorded": bool(checked)}


def record_local_checks(db, item_id, execution_id, body: LocalChecks):
    """Keep the first result for an observed head; it never re-asserts the PR snapshot."""
    row = _open_execution(db, item_id, execution_id)
    if db.execute("SELECT 1 FROM github_observations WHERE execution_id=? AND head_sha=? LIMIT 1",
                  (execution_id, body.head)).fetchone() is None:
        raise intake.Conflict("Observe the PR head before reporting its local checks")
    recorded = db.execute(
        "INSERT INTO local_check_results VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING 1",
        (execution_id, body.head, row["version"], intake.timestamp(), intake.encode(body.failures)),
    ).fetchone()
    return {"recorded": recorded is not None}


def heartbeat(db, runner_id):
    now = intake.timestamp()
    db.execute("""INSERT INTO reconciler_heartbeats VALUES (?, ?)
                  ON CONFLICT DO UPDATE SET last_seen = excluded.last_seen""", (runner_id, now))
    return {"runner_id": runner_id, "last_seen": now}


def _flag(kind, row, reason, recovery, first_seen, notify_owner=False):
    return {"kind": kind, "item_id": row["item_id"] if row else "",
            "execution_id": row["execution_id"] if row else "", "captain_id": "",
            "reason": reason, "recovery": recovery, "first_seen": first_seen,
            "notify_owner": notify_owner, "source": "server"}


def observation_flags(db, now):
    """Flag the latest observation of each open execution, so flags clear when GitHub does.

    An observation older than RECONCILER_STALE may no longer hold, so its flags say when it
    was seen and an observation_stale flag asks for the reconciler's error on that item.
    """
    flags = []
    for row in db.execute(
        f"""WITH latest AS (
                SELECT observation.*, execution.item_id, execution.state_json,
                       row_number() OVER (PARTITION BY observation.execution_id
                           ORDER BY observation.last_observed_at DESC, observation.observed_at DESC) AS rank
                FROM github_observations AS observation
                JOIN executions AS execution ON execution.id = observation.execution_id
                WHERE {OPEN}
            )
            SELECT latest.*, checked.checked_at, checked.failures_json
            FROM latest
            LEFT JOIN local_check_results AS checked
                ON checked.execution_id = latest.execution_id AND checked.head_sha = latest.head_sha
            WHERE rank = 1 ORDER BY latest.execution_id"""
    ):
        found = _observed(row, json.loads(row["observation_json"]))
        last = datetime.fromisoformat(row["last_observed_at"])
        if now - last >= RECONCILER_STALE:
            for flag in found:
                flag["reason"] += f" (last observed {row['last_observed_at']})"
            found.insert(0, _flag(
                "observation_stale", row, f"GitHub was last observed for this item at {row['last_observed_at']}",
                "A Captain checks the reconciler's pass log for this item's error and repairs it",
                (last + RECONCILER_STALE).isoformat(timespec="microseconds"),
            ))
        flags += found
    return flags


def _observed(row, observation):
    seen, pull, flags = row["observed_at"], observation["pr"], []
    if pull is None:
        if observation["merged_branch"]:
            flags.append(_flag(
                "unpublished_branch_merged", row,
                f"{item_branch(row['item_id'])} is reachable from its base, but no PR exists",
                "A Captain confirms the work landed and closes the item, or reopens it", seen,
            ))
        return flags
    url = pull["url"]
    candidate = json.loads(row["state_json"]).get("candidate")
    if candidate and pull["head_sha"] != candidate:
        flags.append(_flag(
            "head_drift", row,
            f"{url} head {pull['head_sha']} differs from the recorded candidate {candidate}",
            "Informational: the writer records the new head with its next result", seen,
        ))
    if pull["state"] == "closed" and not pull["merged"]:
        flags.append(_flag("pr_closed", row, f"{url} was closed without merging",
                           "A Captain asks the owner whether to reopen it; never open a replacement", seen))
    if pull["state"] == "open" and pull["mergeable"] is False:
        flags.append(_flag("merge_conflict", row, f"{url} conflicts with {pull['base_ref']}",
                           WRITER_RECOVERY, seen))
    if observation["checks"] == GateState.FAILED:
        reasons = "; ".join(observation["check_reasons"]) or "required checks failed"
        flags.append(_flag("checks_failed", row, f"{url}: {reasons}", WRITER_RECOVERY, seen))
    if observation["feedback"]:
        count = len(observation["feedback"])
        flags.append(_flag("review_feedback", row,
                           f"{url} has {count} unresolved review item{'s' if count != 1 else ''}",
                           WRITER_RECOVERY, seen))
    failures = json.loads(row["failures_json"] or "[]")
    if failures:
        flags.append(_flag("pre_pr_checks_failed", row,
                           f"Local checks failed on {pull['head_sha']}: " + "; ".join(
                               failure.splitlines()[0] for failure in failures),
                           WRITER_RECOVERY, row["checked_at"]))
    return flags


def reconciler_stale(db, now):
    """Flag each reconciler that has gone quiet, so a live one can't hide a dead one."""
    flags = []
    for runner_id, last_seen in db.execute(
            "SELECT runner_id, last_seen FROM reconciler_heartbeats ORDER BY runner_id"):
        seen = datetime.fromisoformat(last_seen)
        if now - seen >= RECONCILER_STALE:
            flags.append(_flag(
                "reconciler_stale", None, f"Reconciler {runner_id} has sent no heartbeat since {last_seen}",
                f"A Captain restarts reconciler {runner_id}, or reports it to the owner",
                (seen + RECONCILER_STALE).isoformat(timespec="microseconds"),
            ))
    return flags


DERIVED_FLAGS = (observation_flags, reconciler_stale)