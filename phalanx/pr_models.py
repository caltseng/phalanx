"""PR lifecycle payloads shared by the runner and the server."""

import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from . import validation
from .intake_models import AttemptEvent, Identifier, Payload, Text
from .transitions import Stage

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


class PublishedValidationClaimCheck(AttemptEvent):
    stage: Literal[Stage.PR_VALIDATION]
    runner_id: Identifier


class Publication(AttemptEvent):
    stage: Literal[Stage.PR_CREATION]
    expected: ExpectedRefs
    pull: GitHubPull
    duplicate_numbers: list[PositiveInt] = Field(default_factory=list, max_length=30)


class PublicationBlocker(AttemptEvent):
    """Verified external state that prevents recording the first draft.

    Name either the moved base revision or one PR on the item branch.
    """

    stage: Literal[Stage.PR_CREATION]
    expected: ExpectedRefs
    reason: Text
    observed_base_sha: SHA | None = None
    pull: GitHubPull | None = None

    @model_validator(mode="after")
    def one_blocker(self):
        if (self.observed_base_sha is None) == (self.pull is None):
            raise ValueError("Name exactly one of observed_base_sha or pull")
        return self


class Validation(AttemptEvent):
    stage: Literal[Stage.PR_PROMOTION, Stage.PR_VALIDATION]
    expected: ExpectedRefs
    pull: GitHubPull
    gates: GateObservation
    schedule_recheck: Flag = False


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


GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([a-z0-9_.-]+/[a-z0-9_.-]+?)(?:\.git)?/?", re.IGNORECASE | re.ASCII,
)
