"""PR lifecycle payloads shared by the runner and the server."""

from datetime import timedelta
import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from . import validation
from .intake_models import AttemptEvent, Identifier, Payload, Text, WaitLimit
from .transitions import Outcome, Stage

SHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=255)]
Repository = Annotated[str, StringConstraints(max_length=256, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Flag = Annotated[bool, Field(strict=True)]
DEPLOYMENT_WINDOW = timedelta(hours=24)


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
    merged_at: AwareDatetime | None = None


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
    """An existing PR on the item branch that prevents recording the first draft."""

    stage: Literal[Stage.PR_CREATION]
    expected: ExpectedRefs
    reason: Text
    pull: GitHubPull


class Validation(AttemptEvent):
    stage: Literal[Stage.PR_PROMOTION, Stage.PR_VALIDATION]
    expected: ExpectedRefs
    pull: GitHubPull
    gates: GateObservation
    schedule_recheck: Flag = False
    # Pending ready checks recheck after this interval until they have waited the limit.
    retry_after_seconds: Annotated[
        int, Field(strict=True, ge=0, le=validation.CHECK_RECHECK_RANGE[1])] | None = None
    wait_limit_seconds: WaitLimit | None = None


class ReviewRecord(Payload):
    reviewer: Text
    summary: Text
    findings: list[Text] = Field(max_length=100)


class IndependentReview(AttemptEvent):
    stage: Literal[Stage.ADVERSARIAL_REVIEW]
    expected: ExpectedRefs
    pull: GitHubPull
    review: ReviewRecord
    outcome: Literal[Outcome.SUCCEEDED, Outcome.CHANGES_REQUIRED] = Outcome.SUCCEEDED

    @model_validator(mode="after")
    def blocking_findings(self):
        if self.outcome == Outcome.CHANGES_REQUIRED and not self.review.findings:
            raise ValueError("Changes required needs at least one finding")
        return self


class MergeIntent(Payload):
    operation_id: Identifier
    authorization: Text


class Reconciliation(AttemptEvent):
    """Ready gates observed on the candidate after mergeability and feedback were cleared.

    A merge intent authorizes check-in once every gate is current.
    """

    stage: Literal[Stage.PR_RECONCILIATION]
    expected: ExpectedRefs
    pull: GitHubPull
    gates: GateObservation
    merge_intent: MergeIntent | None = None


class Revised(AttemptEvent):
    """A pr_revision commit pushed to the open PR, with the PR as observed after the push.

    A pull whose head is not the candidate records a push that lost to a foreign one.
    """

    stage: Literal[Stage.PR_REVISION]
    expected: ExpectedRefs
    pull: GitHubPull
    candidate: SHA


class Rework(AttemptEvent):
    """Routine PR state that development (before promotion) or pr_revision resolves.

    Merge conflicts always go to pr_revision. ``merge_conflict`` and ``feedback``
    observe the candidate on the PR;
    ``unexpected_head`` observes a head Phalanx did not publish. A conflict
    carries the target branch tip, since GitHub's PR ``base.sha`` can lag it.
    """

    stage: Literal[Stage.PR_PROMOTION, Stage.PR_VALIDATION, Stage.PR_RECONCILIATION, Stage.CHECK_IN]
    expected: ExpectedRefs
    pull: GitHubPull
    issue: Literal["merge_conflict", "unexpected_head", "feedback"]
    base_tip: SHA | None = None
    findings: list[Text] = Field(default_factory=list, max_length=100)
    feedback_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=100)

    @model_validator(mode="after")
    def described(self):
        if ((self.issue == "feedback") != bool(self.findings and self.feedback_ids)
                or len(self.findings) != len(self.feedback_ids)):
            raise ValueError("Feedback rework names the feedback and its IDs; other issues carry neither")
        return self


class MergeEvidence(AttemptEvent):
    """GitHub's authoritative record of a merged primary PR.

    A merge commit has the PR's final head as its second parent. A squash or
    rebase merge has one parent; the runner verified that commit is on the base
    branch. GitHub does not report which of the two it was.
    """

    expected: ExpectedRefs
    pull: MergedPull
    merge_parent_shas: list[SHA] = Field(min_length=1, max_length=2)
    merge_method: Literal["merge", "squash_or_rebase"] = "merge"

    @model_validator(mode="after")
    def provenance(self):
        parents = self.merge_parent_shas
        if self.merge_method == "merge":
            if len(parents) != 2 or parents[1] != self.pull.head_sha:
                raise ValueError("A merge commit's head parent must be the PR head")
        elif len(parents) != 1:
            raise ValueError("A squash or rebase merge has one parent")
        return self


class VerifiedMerge(MergeEvidence):
    stage: Literal[Stage.CHECK_IN]
    operation_id: Identifier


class ExternalMerge(MergeEvidence):
    """The repository merged the validated candidate before Phalanx's check-in."""

    stage: Literal[Stage.PR_PROMOTION, Stage.PR_VALIDATION, Stage.PR_RECONCILIATION]


Observation = Annotated[
    Publication | Validation | IndependentReview | Reconciliation | Revised | VerifiedMerge,
    Field(discriminator="stage"),
]


GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([a-z0-9_.-]+/[a-z0-9_.-]+?)(?:\.git)?/?", re.IGNORECASE | re.ASCII,
)
