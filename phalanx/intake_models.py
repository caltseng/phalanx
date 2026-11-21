from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .transitions import Outcome, Stage, Target
from .validation import PreOutcome


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
RevisionID = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Markdown = Annotated[str, StringConstraints(min_length=1, max_length=256000, pattern=r"\S")]


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
    target: Target
    context_links: list[Text] = Field(default_factory=list, max_length=50)
    acceptance_criteria: list[Text] = Field(default_factory=list, max_length=50)
    target_branch: Text | None = None
    priority: Literal["low", "normal", "urgent"] = "normal"
    runner_affinity: Identifier | None = None
    dependencies: list[Prerequisite] = Field(default_factory=list, max_length=100)


class Versioned(Payload):
    expected_version: Annotated[int, Field(strict=True, ge=1)]


class Steering(Versioned):
    goal: Text | None = None
    context_links: list[Text] | None = Field(default=None, max_length=50)
    acceptance_criteria: list[Text] | None = Field(default=None, max_length=50)
    priority: Literal["low", "normal", "urgent"] | None = None
    runner_affinity: Identifier | None = None
    dependencies: list[Prerequisite] | None = Field(default=None, max_length=100)


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


class GateRevision(Payload):
    head: Text
    base: Text


class ExternalWait(AttemptEvent):
    reason: Text
    gate: Identifier
    revision: GateRevision
    retry_after_seconds: Annotated[int, Field(strict=True, ge=0, le=86400)] = 0


class LocalCheckResult(Payload):
    name: Text
    outcome: PreOutcome
    reason: Text | None = None
    authorization: Text | None = None


class Result(AttemptEvent):
    outcome: Outcome
    wait: WaitInput | None = None
    question: Question | None = None
    failure_reason: Text | None = None
    candidate: Text | None = None
    report_markdown: Markdown | None = None
    report_revision: RevisionID | None = None
    reviewed_candidate: Text | None = None
    prevalidated_candidate: Text | None = None
    review_notes: Text | None = None
    local_checks: list[LocalCheckResult] | None = Field(default=None, max_length=50)
