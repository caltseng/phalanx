"""Trusted runner attestations for GitHub publication and validation stages."""

import json
import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from . import intake, validation
from .intake_models import AttemptEvent, Payload
from .transitions import Evidence, Event, Outcome, PullRequest, Revision, Stage

SHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=255)]
Repository = Annotated[str, StringConstraints(max_length=256, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Flag = Annotated[bool, Field(strict=True)]


class ExpectedRefs(Payload):
    head_ref: Name
    base_ref: Name
    base_sha: SHA


class GitHubPull(Payload):
    number: PositiveInt
    head_repository: Repository
    base_repository: Repository
    head_ref: Name
    base_ref: Name
    head_sha: SHA
    base_sha: SHA
    state: Literal["open", "closed"]
    draft: Flag
    merged: Flag


class RequiredCheck(Payload):
    name: Name
    kind: validation.CheckKind
    app_id: PositiveInt | None = None
    use_merge_sha: Flag = False
    require_execution: Flag = False


class CheckObservation(Payload):
    name: Name
    kind: validation.CheckKind
    tested_sha: SHA
    outcome: validation.CheckOutcome
    app_id: PositiveInt | None = None


class GateObservation(Payload):
    phase: validation.Phase
    number: PositiveInt
    head_sha: SHA
    base_sha: SHA
    test_merge_sha: SHA | None = None
    # Empty means the runner resolved policy and this phase has no requirements.
    required: list[RequiredCheck] = Field(max_length=100)
    observations: list[CheckObservation] = Field(max_length=200)


class Publication(AttemptEvent):
    stage: Literal[Stage.PR_CREATION]
    expected: ExpectedRefs
    pull: GitHubPull
    duplicate_numbers: list[PositiveInt] = Field(default_factory=list, max_length=30)


class Validation(AttemptEvent):
    stage: Literal[Stage.PR_PROMOTION, Stage.PR_VALIDATION]
    expected: ExpectedRefs
    pull: GitHubPull
    gates: GateObservation


_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([a-z0-9_.-]+/[a-z0-9_.-]+?)(?:\.git)?/?", re.IGNORECASE | re.ASCII,
)


def submit(db, item_id, execution_id, body, lease_digest):
    row, state = intake.check_current(db, item_id, execution_id, body.expected_version)
    intake.check_attempt(db, execution_id, state, body, lease_digest)
    item = intake.item_row(db, item_id)
    remote, = db.execute("SELECT remote FROM repositories WHERE id=?",
                         (item["repository_id"],)).fetchone()
    match = _GITHUB_REMOTE.fullmatch(remote)
    pull = body.pull
    if match is None:
        raise intake.Conflict("PR observations require a registered github.com remote")
    repository = match[1].lower()
    if repository != pull.base_repository.lower() or repository != pull.head_repository.lower():
        raise intake.Conflict("PR repositories must match the registered GitHub repository")

    expected = body.expected
    target_branch = json.loads(item["request_json"])["target_branch"]
    if target_branch is not None and expected.base_ref != target_branch:
        raise intake.Conflict("Expected base branch must match the work item target")
    if (pull.head_ref != expected.head_ref or pull.base_ref != expected.base_ref
            or pull.head_sha != state.candidate or pull.base_sha != expected.base_sha):
        raise intake.Conflict("Observed PR must match the expected refs and candidate revision")
    if pull.state != "open" or pull.merged:
        raise intake.Conflict("Expected an open, unmerged PR")

    url = f"https://github.com/{repository}/pull/{pull.number}"
    if state.primary_pr is not None and state.primary_pr != url:
        raise intake.Conflict("Observation must match the recorded primary PR")
    revision = Revision(pull.head_sha, pull.base_sha)
    pr = PullRequest(url, revision, draft=pull.draft)
    details = body.model_dump(mode="json")
    details["pr_url"] = url
    outcome = Outcome.SUCCEEDED
    if isinstance(body, Publication):
        if pull.number in body.duplicate_numbers or len(set(body.duplicate_numbers)) != len(body.duplicate_numbers):
            raise intake.Conflict("Duplicate PR numbers must be distinct and exclude the primary PR")
        evidence = Evidence(pr=pr)
    else:
        if state.primary_pr is None or (body.stage == Stage.PR_VALIDATION and pull.draft):
            raise intake.Conflict("Validation requires an open, ready primary PR")
        gates = evaluate_gates(body, url, revision)
        if gates.state in {validation.GateState.SATISFIED, validation.GateState.NOT_CONFIGURED}:
            evidence = (Evidence(pr=pr, draft_checks=revision) if body.stage == Stage.PR_PROMOTION
                        else Evidence(pr=pr, ci_and_previews=revision))
        elif gates.state is validation.GateState.FAILED:
            evidence = Evidence(pr=pr)
            outcome = Outcome.CHANGES_REQUIRED
        else:
            # The existing API has no durable scheduled external recheck.
            raise intake.Conflict("PR gates did not pass: " + "; ".join(gates.reasons))
        details["gate_result"] = {"state": gates.state, "reasons": gates.reasons}
    details["outcome"] = outcome
    return intake.apply(db, row, state, Event.RESULT, details=details,
                        attempt_id=body.attempt_id, outcome=outcome, evidence=evidence)


def evaluate_gates(body, url, revision):
    gates = body.gates
    phase = validation.Phase.DRAFT if body.stage == Stage.PR_PROMOTION else validation.Phase.READY
    if (gates.phase != phase or gates.number != body.pull.number
            or Revision(gates.head_sha, gates.base_sha) != revision):
        raise intake.Conflict("Gate evidence must match the phase and exact PR revision")
    required = tuple(validation.RequiredCheck(**check.model_dump()) for check in gates.required)
    if len({(check.kind, check.name, check.app_id) for check in required}) != len(required):
        raise intake.Conflict("Gate policy contains duplicate requirements")
    if any(check.tested_sha not in (revision.head, gates.test_merge_sha)
           for check in gates.observations):
        raise intake.Conflict("Check observations must name the head or correlated test merge SHA")
    observations = [
        validation.PublishedEvidence(url, revision, phase=phase, **check.model_dump())
        for check in gates.observations
    ]
    return validation.evaluate_published_checks(
        validation.PublishedCandidate(url, revision, body.pull.draft, revision.head,
                                      gates.test_merge_sha),
        phase, required, observations,
    )
