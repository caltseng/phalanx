"""Runner-owned validation stages for attended reservations.

Each handler journals its request before sending it, so an interrupted
submission replays the same body and idempotency key.
"""

from dataclasses import asdict
import os
from pathlib import Path

from .attended import RunnerError
from .attended_drafts import github_repository
from .candidate_local import CandidateLocal
from .github import GitHubClient, GitHubError
from .github_checks import collect_github_checks
from .pr_models import ExpectedRefs, GateObservation, GitHubPull, Validation
from .promotion import DraftChecks, promote
from .published_validation_stage import PublishedValidationAssignment, PublishedValidationRunner
from .transitions import Outcome, Revision, Stage
from .validation import GateState, Phase
from .validation_policy import ValidationPolicyError, load_validation_policy
from .workspaces import _item_branch

# GitHub usually reflects a ready mutation or new base within seconds.
_PROVIDER_RETRY_SECONDS = 30


def run(runner, snapshot):
    stage = snapshot["claim"]["execution"]["state"]["stage"]
    submission = snapshot.get("submission")
    if stage == Stage.PR_VALIDATION and submission is None:
        execution = _published_validation(runner, snapshot)
        if execution is not None:
            return {"status": "published_validation_submitted", "execution": execution}
        submission = snapshot["submission"]
    if submission is None:
        handler = _pre_validation if stage == Stage.PRE_PR_VALIDATION else _promotion
        submission = _save(runner, snapshot, handler(runner, snapshot))
    claim = snapshot["claim"]
    key = f"{stage}:{claim['attempt_id']}:{claim['execution']['version']}"
    if stage == Stage.PRE_PR_VALIDATION and submission["body"]["outcome"] == Outcome.SUCCEEDED:
        _candidate(runner, snapshot)._inspect()
    execution = runner._send_submission(snapshot, submission, key)
    return {"status": f"{stage}_submitted", "execution": execution,
            **({"reason": submission["reason"]} if "reason" in submission else {})}


def _save(runner, snapshot, submission):
    def save(record, db):
        runner._ensure_reservation(record, snapshot["lease"])
        record.setdefault("submission", submission)
        return record["submission"]

    snapshot["submission"] = runner._transaction(save)
    return snapshot["submission"]


def _event(snapshot):
    claim = snapshot["claim"]
    return {"attempt_id": claim["attempt_id"], "stage": claim["execution"]["state"]["stage"],
            "expected_version": claim["execution"]["version"]}


def _question(snapshot, reason, handoff):
    state = snapshot["claim"]["execution"]["state"]
    body = {**_event(snapshot), "outcome": Outcome.NEEDS_INPUT,
            "question": {"prompt": reason[:16000], "handoff": handoff[:16000]}}
    if state["stage"] == Stage.PRE_PR_VALIDATION:
        body["candidate"] = state["candidate"]
    return {"body": body, "reason": reason}


def _retry(snapshot, reason, delay=None):
    return {"body": {**_event(snapshot), "outcome": Outcome.FAILED, "failure_reason": reason[:16000],
                     "retry_after_seconds": delay or _PROVIDER_RETRY_SECONDS}, "reason": reason}


def _policy(runner, snapshot):
    path = runner.config.get("validation_policy")
    if not isinstance(path, str) or not path:
        raise ValidationPolicyError("Set validation_policy in the runner configuration")
    return load_validation_policy(Path(path), snapshot["claim"]["request"]["repository_id"])


def _candidate(runner, snapshot):
    claim = snapshot["claim"]
    repo = claim["request"]["repository_id"]
    manager = runner._manager(runner.config["repositories"][repo], snapshot["repositories"][repo])
    return CandidateLocal(runner.api, manager, claim, runner_id=snapshot["runner_id"], lease=snapshot["lease"])


def _github(snapshot):
    token = os.environ.get("PHALANX_GITHUB_TOKEN")
    if not token:
        raise RunnerError("Set PHALANX_GITHUB_TOKEN in the runner environment and retry")
    repository = github_repository(snapshot["repositories"][snapshot["claim"]["request"]["repository_id"]])
    if repository is None:
        raise RunnerError("PR validation requires a registered github.com remote")
    owner, name = repository.split("/")
    return GitHubClient(owner, name, token=token)


def _pre_validation(runner, snapshot):
    local = _candidate(runner, snapshot)
    try:
        checks = _policy(runner, snapshot).local_checks
    except ValidationPolicyError as error:
        return _question(snapshot, str(error), "Repair the runner's trusted validation policy, then reply.")
    return {"body": local.pre_validate(checks)}


def _promotion(runner, snapshot):
    claim = snapshot["claim"]
    state = claim["execution"]["state"]
    candidate = state["candidate"]
    url = state.get("primary_pr") or ""
    handoff = f"{url}, candidate {candidate}"
    try:
        draft_checks = _policy(runner, snapshot).draft_checks
    except ValidationPolicyError as error:
        return _question(snapshot, str(error), "Repair the runner's trusted validation policy, then reply.")
    number = url.rsplit("/", 1)[-1]
    if not number.isdigit():
        return _question(snapshot, "Restore the recorded primary PR before promotion.", handoff)
    github = _github(snapshot)
    mapping = runner.config["repositories"][claim["request"]["repository_id"]]
    base_ref = claim["request"].get("target_branch") or mapping["base_branch"]
    local = _candidate(runner, snapshot)
    blocked = []

    def claim_current():
        try:
            local._authorize()
        except RunnerError:
            return False
        return True

    def collect(pull):
        if not draft_checks:
            return DraftChecks(draft_checks)
        checks = collect_github_checks(github, pull, Phase.DRAFT, draft_checks)
        if checks.gate.state is GateState.BLOCKED:
            blocked.extend(checks.gate.reasons)
        return DraftChecks(draft_checks, checks.observations,
                           checks.candidate.merge_sha if checks.candidate else None)

    try:
        pull = github.get_pull_request(int(number))
    except GitHubError as error:
        return _retry(snapshot, str(error), error.retry_after_seconds)
    result = promote(
        github, item_id=claim["item_id"], number=int(number), base_ref=base_ref,
        revision=Revision(candidate, pull.base_sha), collect=collect,
        read_base=lambda: github.get_branch_tip(base_ref), claim_current=claim_current,
    )
    if result.status == "claim_lost":
        raise RunnerError("Promotion claim changed. Reconcile the reservation before continuing")
    if result.status == "provider_pending":
        return _retry(snapshot, result.reason)
    if result.status == "revision_changed":
        observed = result.pull
        if (observed is None or observed.head_sha != candidate or observed.state != "open"
                or observed.merged or observed.head_ref != _item_branch(claim["item_id"])
                or observed.base_ref != base_ref):
            return _question(snapshot, result.reason, handoff)
        # Only the target moved. A later attempt observes the new base revision.
        return _retry(snapshot, "The target branch moved during promotion; observing the new base")
    gates = result.gates
    if blocked or (result.status == "gates_unresolved" and gates.state is not GateState.FAILED):
        if not blocked and gates.state is GateState.WAITING:
            return _retry(snapshot, "; ".join(gates.reasons) or "Draft checks are pending")
        return _question(snapshot, "; ".join(blocked or gates.reasons) or result.reason, handoff)
    # Verified evidence advances; failed draft checks return the item to development.
    return {"route": "pr-observation", "body": _validation(
        snapshot, result.pull, base_ref, draft_checks, result.checks,
    ), **({"reason": result.reason} if result.status != "verified" else {})}


def _validation(snapshot, pull, base_ref, required, checks):
    claim = snapshot["claim"]
    required = required or ()
    return Validation(
        **_event(snapshot),
        expected=ExpectedRefs(head_ref=_item_branch(claim["item_id"]), base_ref=base_ref,
                              base_sha=pull.base_sha),
        pull=GitHubPull.model_validate({field: getattr(pull, field) for field in GitHubPull.model_fields}),
        gates=GateObservation(
            phase=Phase.DRAFT, number=pull.number, head_sha=pull.head_sha, base_sha=pull.base_sha,
            test_merge_sha=checks.merge_sha, required=[asdict(check) for check in required],
            observations=[
                {"name": evidence.name, "kind": evidence.kind, "tested_sha": evidence.tested_sha,
                 "outcome": evidence.outcome, "app_id": evidence.app_id}
                for evidence in checks.observations
                if any(check.kind == evidence.kind and check.name == evidence.name
                       and (check.app_id is None or check.app_id == evidence.app_id) for check in required)
            ],
        ),
    ).model_dump(mode="json")


def _published_validation(runner, snapshot):
    claim = snapshot["claim"]
    try:
        policy = _policy(runner, snapshot).ready
    except ValidationPolicyError as error:
        _save(runner, snapshot, _question(
            snapshot, str(error), "Repair the runner's trusted validation policy, then reply."))
        return None
    execution = claim["execution"]
    assignment = PublishedValidationAssignment(
        claim["item_id"], execution["id"], snapshot["runner_id"], claim["attempt_id"],
        execution["version"], snapshot["lease"],
    )
    result = PublishedValidationRunner(runner.root / "publication", runner.api, _github(snapshot)).run(
        assignment, policy,
    )
    if result is None:
        raise RunnerError("Published validation claim changed. Reconcile the reservation before continuing")

    def finish(record, db):
        if record.get("lease") == snapshot["lease"]:
            record.clear()

    runner._transaction(finish)
    return result
