"""Trusted runner attestations for GitHub PR lifecycle stages."""

import json
import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from . import intake, validation
from .intake_models import AttemptEvent, Identifier, Payload, Text
from .transitions import Evidence, Event, Outcome, PullRequest, Revision, Stage, Status, Target
from .workspaces import _item_branch

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


class MergedPull(GitHubPull):
    state: Literal["closed"]
    draft: Literal[False]
    merged: Literal[True]
    merge_commit: SHA


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


class PublicationClaimCheck(AttemptEvent):
    stage: Literal[Stage.PR_CREATION]
    runner_id: Identifier


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


class ReviewRecord(Payload):
    reviewer: Text
    summary: Text
    findings: list[Text] = Field(max_length=100)


class IndependentReview(AttemptEvent):
    stage: Literal[Stage.ADVERSARIAL_REVIEW]
    expected: ExpectedRefs
    pull: GitHubPull
    review: ReviewRecord


class MergeIntent(Payload):
    operation_id: Identifier
    authorization: Text


class Reconciliation(AttemptEvent):
    stage: Literal[Stage.PR_RECONCILIATION, Stage.READY_FOR_CHECK_IN]
    expected: ExpectedRefs
    pull: GitHubPull
    gates: GateObservation
    actionable_feedback_cleared: Literal[True]
    approvals_and_mergeability: Literal[True]
    merge_intent: MergeIntent | None = None


class VerifiedMerge(AttemptEvent):
    stage: Literal[Stage.CHECK_IN]
    expected: ExpectedRefs
    pull: MergedPull
    operation_id: Identifier
    merge_parent_shas: tuple[SHA, SHA]


Observation = Annotated[
    Publication | Validation | IndependentReview | Reconciliation | VerifiedMerge,
    Field(discriminator="stage"),
]


_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([a-z0-9_.-]+/[a-z0-9_.-]+?)(?:\.git)?/?", re.IGNORECASE | re.ASCII,
)


def latest_observation(db, execution_id, stage):
    row = db.execute(
        """SELECT details_json FROM transition_history
           WHERE execution_id=? AND json_extract(details_json, '$.stage')=?
             AND json_type(details_json, '$.pr_url')='text'
           ORDER BY version DESC LIMIT 1""",
        (execution_id, stage),
    ).fetchone()
    return json.loads(row["details_json"]) if row else None


def reviewed_revision(db, execution_id, url, expected, revision):
    review = latest_observation(db, execution_id, Stage.ADVERSARIAL_REVIEW)
    if (review and review["pr_url"] == url and review["expected"] == expected.model_dump()
            and review["pull"]["head_sha"] == revision.head):
        return revision
    return None


def check_publication_claim(db, item_id, execution_id, body, lease_digest):
    try:
        _, state = intake.check_current(db, item_id, execution_id, body.expected_version)
        attempt = intake.check_attempt(db, execution_id, state, body, lease_digest)
    except (intake.Conflict, intake.Missing):
        return {"current": False}
    if (
        attempt["runner_id"] != body.runner_id or state.status != Status.RUNNING
        or state.stopping is not None or state.primary_pr is not None
        or not re.fullmatch(r"[0-9a-f]{40}", state.candidate or "")
    ):
        return {"current": False}

    item = intake.item_row(db, item_id)
    remote, = db.execute("SELECT remote FROM repositories WHERE id=?",
                         (item["repository_id"],)).fetchone()
    match = _GITHUB_REMOTE.fullmatch(remote)
    if match is None:
        raise intake.Conflict("Draft publication requires a registered github.com remote")
    return {
        "current": True,
        "candidate": state.candidate,
        "head_ref": _item_branch(item_id),
        "target_branch": json.loads(item["request_json"])["target_branch"],
        "repository": match[1].lower(),
        "remote": remote,
    }


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
    if expected.head_ref != _item_branch(item_id):
        raise intake.Conflict("Expected head branch must match the deterministic item branch")
    target_branch = json.loads(item["request_json"])["target_branch"]
    if target_branch is not None and expected.base_ref != target_branch:
        raise intake.Conflict("Expected base branch must match the work item target")
    if (pull.head_ref != expected.head_ref or pull.base_ref != expected.base_ref
            or pull.head_sha != state.candidate):
        raise intake.Conflict("Observed PR must match the expected refs and candidate head")
    url = f"https://github.com/{repository}/pull/{pull.number}"
    if state.primary_pr != url and (state.primary_pr or not isinstance(body, Publication)):
        raise intake.Conflict("Observation must match the recorded primary PR")
    if not isinstance(body, Publication):
        publication = latest_observation(db, execution_id, Stage.PR_CREATION)
        if not publication or publication["expected"]["base_ref"] != expected.base_ref:
            raise intake.Conflict("Expected base branch must match the published primary PR")

    revision = Revision(pull.head_sha, expected.base_sha)
    details = body.model_dump(mode="json")
    details["pr_url"] = url
    if isinstance(body, VerifiedMerge):
        ready = latest_observation(db, execution_id, Stage.READY_FOR_CHECK_IN)
        intent = ready.get("merge_intent") if ready else None
        if (not intent or intent["operation_id"] != body.operation_id
                or ready["pr_url"] != url or ready["expected"] != expected.model_dump()
                or ready["pull"]["head_sha"] != revision.head):
            raise intake.Conflict("Verified merge must match the recorded authorized merge intent")
        if body.merge_parent_shas != (revision.base, revision.head):
            raise intake.Conflict("Merge commit parents must match the authorized base and candidate head")
        pr = PullRequest(url, revision, merge_commit=pull.merge_commit, closed=True)
        return intake.apply(db, row, state, Event.MERGE_VERIFIED, details=details,
                            attempt_id=body.attempt_id, evidence=Evidence(pr=pr))

    if pull.base_sha != expected.base_sha or pull.state != "open" or pull.merged:
        raise intake.Conflict("Expected an open, unmerged PR at the observed base revision")
    pr = PullRequest(url, revision, draft=pull.draft)
    outcome = Outcome.SUCCEEDED
    if isinstance(body, Publication):
        if pull.number in body.duplicate_numbers or len(set(body.duplicate_numbers)) != len(body.duplicate_numbers):
            raise intake.Conflict("Duplicate PR numbers must be distinct and exclude the primary PR")
        evidence = Evidence(pr=pr)
    elif isinstance(body, IndependentReview):
        check_review_assignment(db, execution_id, expected, pull)
        evidence = Evidence(pr=pr, independent_review=revision)
    else:
        if body.stage != Stage.PR_PROMOTION and pull.draft:
            raise intake.Conflict("Expected an open, ready primary PR")
        gates = evaluate_gates(body, url, revision)
        details["gate_result"] = {"state": gates.state, "reasons": gates.reasons}
        if gates.state in {validation.GateState.SATISFIED, validation.GateState.NOT_CONFIGURED}:
            evidence = gate_evidence(db, execution_id, body, state, pr)
        elif (gates.state is validation.GateState.FAILED
              and body.stage != Stage.PR_RECONCILIATION):
            evidence = Evidence(pr=pr)
            outcome = Outcome.CHANGES_REQUIRED
        else:
            raise intake.Conflict("PR gates did not pass: " + "; ".join(gates.reasons))
    details["outcome"] = outcome
    return intake.apply(db, row, state, Event.RESULT, details=details,
                        attempt_id=body.attempt_id, outcome=outcome, evidence=evidence)


def check_review_assignment(db, execution_id, expected, pull):
    row = db.execute(
        """SELECT details_json FROM transition_history
           WHERE execution_id=? AND json_extract(state_json, '$.stage')=?
             AND json_extract(state_json, '$.status')='queued'
             AND json_type(details_json, '$.gate_result')='object'
           ORDER BY version DESC LIMIT 1""",
        (execution_id, Stage.ADVERSARIAL_REVIEW),
    ).fetchone()
    assignment = json.loads(row["details_json"]) if row else None
    if (not assignment or assignment["expected"] != expected.model_dump()
            or assignment["pull"]["head_sha"] != pull.head_sha
            or assignment["pull"]["number"] != pull.number):
        raise intake.Conflict("Review must match the revision queued by passing PR gates")


def gate_evidence(db, execution_id, body, state, pr):
    revision = pr.revision
    if body.stage == Stage.PR_PROMOTION:
        return Evidence(pr=pr, draft_checks=revision)
    review = reviewed_revision(db, execution_id, pr.url, body.expected, revision)
    if body.stage == Stage.PR_VALIDATION:
        return Evidence(pr=pr, ci_and_previews=revision, independent_review=review)

    needs_intent = (body.stage == Stage.READY_FOR_CHECK_IN and state.target == Target.MERGED
                    and state.ready_revision == revision and review == revision)
    if needs_intent != (body.merge_intent is not None):
        raise intake.Conflict("Merge intent requires a merge target and the current ready revision")
    return Evidence(pr=pr, ci_and_previews=revision, independent_review=review,
                    actionable_feedback_cleared=revision, approvals_and_mergeability=revision,
                    merge_authorized=needs_intent)


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
