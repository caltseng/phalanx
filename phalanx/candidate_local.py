"""Runner operations for candidate-local stages under a reserved item claim."""

from uuid import UUID

from .attended import RunnerAPI, RunnerError
from .handoff import WorkerResult, WorkPacket
from .local_checks import CheckCommandError, LocalCheck, run_check
from .transitions import Outcome, Stage
from .validation import GateState, PreEvidence, PreOutcome, evaluate_pre_validation
from .workspaces import Workspace, WorkspaceError, WorkspaceManager

# Default host limit on concurrent local check runs; configurable as local_check_capacity.
LOCAL_CHECK_CAPACITY = 1
LOCAL_CHECK_CAPACITY_RANGE = (1, 64)
# A timeout with no identified failure may be host contention, so validation retries it first.
TIMEOUT_RETRIES = 2
TIMEOUT_RETRY_SECONDS = 60

class CandidateLocal:
    """Collect result bodies for a single claim. The dispatcher owns reservation and lease renewal.

    Journal the returned body before submission so an uncertain response can be replayed.
    Self-review collection requires a reconciled, stopped worker with validated handoff evidence.
    A check timeout stops the check's process tree and records a failed check. When every
    failed check only timed out with no failure in its output, validation retries with
    backoff up to TIMEOUT_RETRIES times before returning the candidate to development.
    """

    def __init__(self, api: RunnerAPI, workspace: WorkspaceManager, claim: dict,
                 *, runner_id: str, lease: str):
        self.api = api
        self.workspace = workspace
        self.claim = claim
        self.runner_id = runner_id
        self.lease = lease
        self.item_id = claim["item_id"]
        self.execution = claim["execution"]
        self.candidate = self.execution["state"]["candidate"]
        self.attempt = claim["attempt_id"]
        if not self.candidate:
            raise RunnerError("Candidate-local stages require a recorded candidate")

    def _body(self, stage: Stage) -> dict:
        if self.execution["state"]["stage"] != stage:
            raise RunnerError(f"The reservation is not for {stage}")
        return {"attempt_id": self.attempt, "stage": stage,
                "expected_version": self.execution["version"]}

    def _authorize(self) -> None:
        self.api.call(f"/dispatch/runners/{self.runner_id}/attempts/{self.attempt}/renew",
                      {}, lease=self.lease)
        current = self.api.call(f"/items/{self.item_id}")["execution"]
        if (current["id"] != self.execution["id"]
                or current["version"] != self.execution["version"]
                or current["state"]["stage"] != self.execution["state"]["stage"]
                or current["state"]["status"] != "running"
                or current["state"].get("stopping")
                or current["state"].get("candidate") != self.candidate):
            raise RunnerError("Candidate claim changed. Reconcile the reservation before continuing")

    def _inspect(self) -> Workspace:
        workspace = self.workspace.inspect(self.item_id)
        if workspace.dirty or workspace.head != self.candidate:
            raise WorkspaceError("Restore the clean item worktree at the recorded candidate before retrying")
        return workspace

    def self_review_packet(self, *, base_branch: str, context: str = "",
                           handoff: str = "") -> WorkPacket:
        body = self._body(Stage.SELF_REVIEW)
        self._authorize()
        workspace = self._inspect()
        request = self.claim["request"]
        return WorkPacket(
            item_id=self.item_id, attempt_id=UUID(self.attempt),
            state_version=body["expected_version"], stage=Stage.SELF_REVIEW,
            repository=request["repository_id"], worktree=workspace.path,
            branch=workspace.branch, expected_revision=self.candidate,
            goal=request["goal"], output_type="pull_request", base_branch=base_branch,
            context=context, handoff=handoff, evidence_requirements=(
                "Review the exact candidate diff against the target branch and check the "
                "acceptance criteria. Leave the checkout unchanged. Summarize what you reviewed "
                "and report actionable findings as changes_required."
            ),
        )

    def self_review(self, result: WorkerResult) -> dict:
        body = self._body(Stage.SELF_REVIEW)
        packet = result.packet
        if (packet.stage != body["stage"] or packet.attempt_id != UUID(self.attempt)
                or packet.state_version != body["expected_version"] or packet.item_id != self.item_id
                or packet.repository != self.claim["request"]["repository_id"]
                or packet.expected_revision != self.candidate):
            raise RunnerError("Review evidence does not match the claimed candidate and attempt")
        self._authorize()
        workspace = self._inspect()
        if (workspace.path, workspace.branch) != (packet.worktree, packet.branch):
            raise RunnerError("Review evidence does not match the item worktree")
        outcome = Outcome(result.outcome)
        notes = (result.details["question"] if outcome == Outcome.NEEDS_INPUT
                 else "\n\n".join(result.details.values()))
        body.update(outcome=outcome, review_notes=notes)
        if outcome == Outcome.SUCCEEDED:
            body["reviewed_candidate"] = self.candidate
        else:
            body["candidate"] = self.candidate
        if outcome == Outcome.NEEDS_INPUT:
            body["question"] = {"prompt": notes, "options": result.details["options"],
                                "proposed_default": result.details.get("proposed_default"),
                                "handoff": result.details["handoff"]}
        elif outcome == Outcome.FAILED:
            body["failure_reason"] = notes
        return body

    def pre_validate(self, checks: tuple[LocalCheck, ...] | None) -> dict:
        body = self._body(Stage.PRE_PR_VALIDATION)
        if checks is not None and (len(checks) > 50
                                   or len({check.policy.name.strip() for check in checks}) != len(checks)):
            raise RunnerError("Local check policy requires at most 50 uniquely named checks")
        observations = []
        inconclusive = set()
        body["local_checks"] = records = []
        try:
            self._authorize()
            self._inspect()
            for check in checks or ():
                problem = None
                if check.disposition is None:
                    self._authorize()
                    workspace = self._inspect()
                    try:
                        outcome, reason, timed_out = run_check(check, workspace.path)
                        if timed_out:
                            inconclusive.add(check.policy.name)
                        self._inspect()
                    except (WorkspaceError, CheckCommandError) as exc:
                        problem = f"{check.policy.name}: {exc}"
                        outcome, reason = PreOutcome.FAILED, problem
                else:
                    outcome, reason = check.disposition, check.reason
                observations.append(PreEvidence(self.candidate, check.policy.name, outcome,
                                                reason, check.authorization))
                records.append({"name": check.policy.name, "outcome": outcome,
                                "reason": reason or None, "authorization": check.authorization or None})
                if problem:
                    return self._wait(body, problem, handoff="Reconcile the configured check and "
                                      "verify the item worktree before retrying.")
            self._inspect()
        except WorkspaceError as exc:
            return self._wait(body, str(exc), handoff="Verify the item worktree is clean at "
                              "the recorded candidate before retrying.")

        policies = None if checks is None else tuple(check.policy for check in checks)
        gate = evaluate_pre_validation(self.candidate, policies, observations)
        if gate.state in (GateState.SATISFIED, GateState.NOT_CONFIGURED):
            return dict(body, outcome=Outcome.SUCCEEDED, prevalidated_candidate=self.candidate)
        if gate.state == GateState.FAILED:
            failed = [record["name"] for record in records if record["outcome"] == PreOutcome.FAILED]
            retries = self.execution.get("retry_count", 0)
            if failed and inconclusive.issuperset(failed) and retries < TIMEOUT_RETRIES:
                delay = TIMEOUT_RETRY_SECONDS * 2 ** retries
                return dict(body, outcome=Outcome.FAILED, candidate=self.candidate,
                            retry_after_seconds=delay, failure_reason=(
                                f"Local check {', '.join(failed)} timed out with no failure identified; "
                                f"retrying validation in {delay} seconds "
                                f"(retry {retries + 1} of {TIMEOUT_RETRIES})."))
            return dict(body, outcome=Outcome.CHANGES_REQUIRED, candidate=self.candidate)
        return self._wait(body, "\n".join(gate.reasons),
                          handoff="Verify repository check policy and any proposed bypass.")

    def _wait(self, body: dict, reason: str, *, handoff: str) -> dict:
        return dict(body, outcome=Outcome.NEEDS_INPUT, candidate=self.candidate,
                    question={"prompt": reason, "handoff": handoff})

    def submit(self, body: dict) -> dict:
        """Post a journaled result using the existing attempt and version preconditions."""
        stage = Stage(body["stage"])
        if stage not in (Stage.SELF_REVIEW, Stage.PRE_PR_VALIDATION) or any(
            body.get(key) != value for key, value in self._body(stage).items()
        ):
            raise RunnerError("The result does not match this reservation")
        if body["outcome"] == Outcome.SUCCEEDED:
            self._inspect()
        return self.api.call(f"/items/{self.item_id}/executions/{self.execution['id']}/result",
                             body, lease=self.lease, key=f"candidate-local-{self.attempt}")
