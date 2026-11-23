import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .transitions import Outcome, Stage, Target
from .validation import CHECK_WAIT_LIMIT_RANGE, PreOutcome


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]
WaitLimit = Annotated[int, Field(strict=True, ge=CHECK_WAIT_LIMIT_RANGE[0], le=CHECK_WAIT_LIMIT_RANGE[1])]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
RevisionID = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Markdown = Annotated[str, StringConstraints(min_length=1, max_length=256000, pattern=r"\S")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120, pattern=r"^[^\r\n]+$")]
TITLE_LIMIT = 80


def short_title(goal, limit=TITLE_LIMIT, fallback="Untitled work"):
    """A one-line title from the goal's first sentence, cut at a word boundary."""
    line = next((line for line in goal.splitlines() if line.strip()), goal).strip().lstrip("#*-> ").strip()
    sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].rstrip(".").strip() or line or fallback
    if len(sentence) <= limit:
        return sentence
    cut = sentence[:limit - 1]
    return (cut.rsplit(None, 1)[0] if " " in cut.strip() else cut).rstrip(" ,;:-") + "…"


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
    title: Title | None = None
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
