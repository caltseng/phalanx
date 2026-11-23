"""GitHub and git observations from the background reconciler, and the flags derived from them.

Observations are timestamped facts, and they never block work. They advance only a writer's
delivery stage, and only for the PR that writer published, where GitHub decides: a merge into the
target by any method delivers, and a ready PR reaches ready_for_check_in (decision 69). Elsewhere
they leave the execution's state and version alone, so recording one can't race a running attempt.
"""

from datetime import UTC, datetime, timedelta
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from . import intake
from .intake_models import Payload
from .operator_schema import OPEN
from .pr_models import GITHUB_REMOTE, SHA
from .transitions import (
    TERMINAL, Event, Evidence, PullRequest, Revision, Stage, Status, decide, item_branch,
)
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
    # For a merge into the target: "merge" or "squash_or_rebase" as the reconciler verified it
    # (None when it couldn't), and GitHub's merged_at.
    merge_method: Literal["merge", "squash_or_rebase"] | None = None
    merged_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def merge_details(self):
        if not self.merged and (self.merge_method or self.merged_at):
            raise ValueError("Merge method and time describe a merged PR")
        return self


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
    # An item branch reachable from its base, yet no PR exists.
    merged_branch: bool = False
    # The branch the PR must merge into: the item's target_branch, else the repository's base_branch
    # from the reconciler's mapping. The server prefers the item's own target_branch.
    target_base: Ref | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.pr is None and (self.checks or self.check_reasons or self.feedback):
            raise ValueError("Checks and feedback describe a PR")
        if self.pr is not None and self.merged_branch:
            raise ValueError("merged_branch describes an item branch with no PR")
        if (self.pr is None) != (self.target_base is None):
            raise ValueError("A PR observation names the branch it must merge into, and only it does")
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
    if body.pr is not None:
        payload["target_base"] = json.loads(item["request_json"]).get("target_branch") or body.target_base
    stamp = observed_at.isoformat(timespec="microseconds")
    key = (execution_id, url, head, intake.digest(intake.encode(payload)))
    latest, = db.execute("SELECT max(last_observed_at) FROM github_observations WHERE execution_id=?",
                         (execution_id,)).fetchone()
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
    # An older snapshot than one already seen only ever adds a merge, which can't be undone.
    if body.pr is not None and (latest is None or stamp >= latest or body.pr.merged):
        _advance(db, row, url, body, payload["target_base"], stamp)
    return {"recorded": seen is None, "local_checks_recorded": bool(checked)}


def _advance(db, row, url, body, target_base, stamp):
    """Apply every post, not only new ones, so a lost apply recovers on the next pass."""
    state = intake.STATE.validate_json(row["state_json"])
    pull = body.pr
    # Only the PR this execution's writer published moves it; an earlier execution's PR on the
    # shared item branch is recorded and flagged, never delivered.
    if (state.stage is not Stage.DELIVERY or state.primary_pr != url or state.stopping
            or state.pending_output or (pull.merged and pull.merge_commit is None)):
        return
    pr = PullRequest(url, Revision(pull.head_sha, pull.base_sha), draft=pull.draft,
                     merge_commit=pull.merge_commit if pull.merged else None, closed=pull.state == "closed",
                     merge_method=pull.merge_method,
                     merged_at=pull.merged_at and pull.merged_at.isoformat())
    ready = (pull.state == "open" and not pull.draft and pull.mergeable is not False and not body.feedback
             and body.checks in (GateState.SATISFIED, GateState.NOT_CONFIGURED))
    evidence = Evidence(pr=pr, pr_ready=ready, off_base=pull.base_ref != target_base)
    following = decide(state, Event.PR_OBSERVED, evidence=evidence)
    if following == state:
        return
    details = {"pr": url, "pr_state": "merged" if pull.merged else pull.state, "head": pull.head_sha,
               "observed_at": stamp}
    if following.status == Status.WAITING_FOR_INPUT:
        action = ("Reopen it and reply to resume delivery, or cancel the item. "
                  "Phalanx never opens a replacement PR" if not pr.merge_commit
                  else f"A merge can't be undone. Rerun the item to deliver into {target_base}, or cancel it")
        details["question"] = {
            "prompt": f"{following.wait.reason}. {action}.",
            "handoff": f"Item branch {item_branch(row['item_id'])}, head {pull.head_sha}",
        }
    if pr.merge_commit and following.status in TERMINAL:
        details["merge_commit"] = pr.merge_commit
        if state.candidate and state.candidate != pull.head_sha:
            details["head_drift"] = f"Merged head differs from the recorded candidate {state.candidate}"
    attempt = db.execute("SELECT id FROM attempts WHERE execution_id=? AND ended_at IS NULL",
                         (row["id"],)).fetchone()
    view = intake.apply(db, row, state, Event.PR_OBSERVED, details=details,
                        attempt_id=attempt and attempt["id"], evidence=evidence)
    if view["state"]["status"] in TERMINAL:
        db.execute("""UPDATE writers SET ended_at=?, end_reason=?, attempt_id=NULL
                      WHERE execution_id=? AND ended_at IS NULL""",
                   (intake.timestamp(), f"closed as {view['state']['status']}", row["id"]))


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
    target = observation.get("target_base")
    if pull["state"] == "open" and pull["base_ref"] != target:
        flags.append(_flag("wrong_base", row, f"{url} merges into {pull['base_ref']}, not the item's target {target}",
                           "The writer retargets the PR; delivery waits until it does", seen))
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