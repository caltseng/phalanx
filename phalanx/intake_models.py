from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .transitions import Outcome, PublicationMode, Stage, Target
from .validation import CHECK_WAIT_LIMIT_RANGE, PreOutcome


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]
WaitLimit = Annotated[int, Field(strict=True, ge=CHECK_WAIT_LIMIT_RANGE[0], le=CHECK_WAIT_LIMIT_RANGE[1])]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
RevisionID = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Markdown = Annotated[str, StringConstraints(min_length=1, max_length=256000, pattern=r"\S")]
# A free-form group name for related items, such as a campaign or a muster.
LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
Label = Annotated[str, StringConstraints(pattern=LABEL_PATTERN)]
PullUrl = Annotated[str, StringConstraints(
    pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]{0,9}$")]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Registration(Payload):
    id: Identifier
    remote: Text


class Prerequisite(Payload):
    upstream_id: Identifier
    milestone: Target = Target.MERGED


class Intake(Payload):
    repository_id: Identifier
    goal: Text
    target: Target = Target.MERGED
    context_links: list[Text] = Field(default_factory=list, max_length=50)
    acceptance_criteria: list[Text] = Field(default_factory=list, max_length=50)
    target_branch: Text | None = None
    priority: Literal["low", "normal", "urgent"] = "normal"
    runner_affinity: Identifier | None = None
    label: Label | None = None
    dependencies: list[Prerequisite] = Field(default_factory=list, max_length=100)
    publication_mode: PublicationMode = PublicationMode.READY

    @model_validator(mode="after")
    def publication_target(self):
        if self.target is Target.REPORT and self.publication_mode is PublicationMode.DRAFT:
            raise ValueError("Draft publication requires a PR target")
        return self


class Versioned(Payload):
    expected_version: Annotated[int, Field(strict=True, ge=1)]


class Steering(Versioned):
    goal: Text | None = None
    context_links: list[Text] | None = Field(default=None, max_length=50)
    acceptance_criteria: list[Text] | None = Field(default=None, max_length=50)
    priority: Literal["low", "normal", "urgent"] | None = None
    runner_affinity: Identifier | None = None
    label: Label | None = None
    dependencies: list[Prerequisite] | None = Field(default=None, max_length=100)
    target: Target | None = None
    steered_by: Literal["owner", "captain"] = "owner"


class Cancel(Versioned):
    reason: Text


class Rerun(Versioned):
    target: Target


class AttemptEvent(Versioned):
    attempt_id: Identifier
    stage: Stage


class WaitInput(Payload):
    reason: Text
    trigger: Text


class Question(Payload):
    prompt: Text
    options: list[Text] = Field(default_factory=list, max_length=20)
    proposed_default: Text | None = None
    handoff: Text


class Reply(Versioned):
    answer: Text
    answered_by: Literal["owner", "captain"] = "owner"


class GateRevision(Payload):
    head: Text
    base: Text


class ExternalWait(AttemptEvent):
    reason: Text
    gate: Identifier
    revision: GateRevision
    retry_after_seconds: Annotated[int, Field(strict=True, ge=0, le=86400)] = 0
    # When set, ask the owner once this gate has waited this long on one revision.
    wait_limit_seconds: WaitLimit | None = None


class LocalCheckResult(Payload):
    name: Text
    outcome: PreOutcome
    reason: Text | None = None
    authorization: Text | None = None


class Result(AttemptEvent):
    outcome: Outcome
    retry_after_seconds: Annotated[int, Field(strict=True, ge=0, le=10**12)] | None = None
    wait: WaitInput | None = None
    question: Question | None = None
    failure_reason: Text | None = None
    candidate: Text | None = None
    report_markdown: Markdown | None = None
    report_revision: RevisionID | None = None
    report_review_notes: Text | None = None
    reviewed_candidate: Text | None = None
    prevalidated_candidate: Text | None = None
    review_notes: Text | None = None
    local_checks: list[LocalCheckResult] | None = Field(default=None, max_length=50)
    # The PR a writer opened at publication. The reconciler's observation records the primary PR.
    pr_url: PullUrl | None = None
