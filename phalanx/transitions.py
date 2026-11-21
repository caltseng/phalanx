"""Server-owned lifecycle decisions. Verification and I/O belong to the caller.

The caller authenticates events, checks the expected version and current
attempt/lease, and commits the decision, evidence and timeline entry atomically.
Provider observations and policy decisions must come from trusted adapters.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib


class Stage(StrEnum):
    DEVELOPMENT = "development"
    PR_CREATION = "pr_creation"
    SELF_REVIEW = "self_review"
    PRE_PR_VALIDATION = "pre_pr_validation"
    PR_PROMOTION = "pr_promotion"
    PR_VALIDATION = "pr_validation"
    ADVERSARIAL_REVIEW = "adversarial_review"
    PR_RECONCILIATION = "pr_reconciliation"
    READY_FOR_CHECK_IN = "ready_for_check_in"
    CHECK_IN = "check_in"
    REPORT_DRAFTING = "report_drafting"
    REPORT_REVIEW = "report_review"
    REPORT_STORAGE = "report_storage"


class Status(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_EXTERNAL = "waiting_for_external"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Target(StrEnum):
    REPORT = "report"
    READY = "ready_for_check_in"
    MERGED = "checked_in"


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    CHANGES_REQUIRED = "changes_required"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


class Event(StrEnum):
    CLAIM = "claim"
    RESULT = "result"
    INPUT_WAIT = "input_wait"
    EXTERNAL_WAIT = "external_wait"
    RECHECK = "recheck"
    REPLY = "reply"
    ACCEPT_READY_ONLY = "accept_ready_only"
    RETRY = "retry"
    CANCEL = "cancel"
    CLOSE_FAILURE = "close_failure"
    STOP_CONFIRMED = "stop_confirmed"
    MERGE_VERIFIED = "merge_verified"
    FINALIZE = "finalize"


class Issue(StrEnum):
    BASE_CHANGED_BEFORE_PUBLICATION = "base_changed_before_publication"
    PR_BLOCKER = "pr_blocker"


@dataclass(frozen=True)
class Revision:
    head: str
    base: str


@dataclass(frozen=True)
class PullRequest:
    """Verified PR observation, using the pre-merge base for a verified merge."""

    url: str
    revision: Revision
    draft: bool = False
    merge_commit: str | None = None
    closed: bool = False


@dataclass(frozen=True)
class StoredReport:
    reference: str
    draft_revision: str | None = None


type FinalOutput = PullRequest | StoredReport


@dataclass(frozen=True)
class Wait:
    reason: str
    trigger: str

    def __post_init__(self) -> None:
        if not self.reason.strip() or not self.trigger.strip():
            raise ValueError("A wait requires both a reason and a resume trigger")


TERMINAL = frozenset({Status.COMPLETED, Status.FAILED, Status.CANCELLED})


@dataclass(frozen=True)
class Execution:
    target: Target
    stage: Stage
    status: Status = Status.QUEUED
    candidate: str | None = None
    primary_pr: str | None = None
    publication_conflict: bool = False
    report_draft: str | None = None
    wait: Wait | None = None
    stopping: Status | None = None
    pending_output: Status | None = None
    ready_revision: Revision | None = None
    final_output: FinalOutput | None = None
    report_review_findings: str | None = None
    development_findings: str | None = None

    def __post_init__(self) -> None:
        if (self.status in TERMINAL) != (self.final_output is not None):
            raise ValueError("Exactly one final output is required on terminal executions")


def start(target: Target) -> Execution:
    stage = Stage.REPORT_DRAFTING if target is Target.REPORT else Stage.DEVELOPMENT
    return Execution(target, stage)


def item_branch(item_id: str) -> str:
    """Deterministic PR head branch shared by the runner and the server."""
    return f"phalanx/{hashlib.sha256(item_id.encode()).hexdigest()}"


@dataclass(frozen=True)
class Bypass:
    checks: tuple[str, ...]
    reason: str
    authorization: str


@dataclass(frozen=True)
class Evidence:
    """Current stage result plus verified facts for applicable gates.

    A missing gate revision means the gate is unverified. Adapters can attest
    a gate with no applicable checks after evaluating repository policy.
    matching_publication_pr attests that the observed URL belongs to this
    publication intent, independently of the observed head or draft status.
    """

    candidate: str | None = None
    pr: PullRequest | None = None
    matching_publication_pr: str | None = None
    reviewed_candidate: str | None = None
    prevalidated_candidate: str | None = None
    bypass: Bypass | None = None
    draft_checks: Revision | None = None
    ci_and_previews: Revision | None = None
    independent_review: Revision | None = None
    actionable_feedback_cleared: Revision | None = None
    approvals_and_mergeability: Revision | None = None
    report_draft: str | None = None
    reviewed_report: str | None = None
    stored_report: StoredReport | None = None
    merge_authorized: bool = False
    issue: Issue | None = None
    report_review_notes: str | None = None
    development_findings: str | None = None


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _queue(state: Execution, stage: Stage | None = None, **updates) -> Execution:
    return replace(state, stage=stage or state.stage, status=Status.QUEUED,
                   wait=None, **updates)


def _wait(state: Execution, status: Status, wait: Wait | None, **updates) -> Execution:
    _require(wait, "Waiting requires a reason and resume trigger")
    return replace(state, status=status, wait=wait, **updates)


def _close(state: Execution, status: Status, output: FinalOutput) -> Execution:
    _require(status in TERMINAL, "Closure requires a terminal status")
    if status is not Status.COMPLETED or state.target is Target.REPORT:
        _require(isinstance(output, StoredReport) and output.reference.strip(),
                 "Closure requires a stored Markdown report")
    else:
        _require(isinstance(output, PullRequest), "Closure requires a verified PR")
    return replace(state, status=status, final_output=output, wait=None,
                   stopping=None, pending_output=None)


def _disposition(state: Execution, status: Status, report: StoredReport | None) -> Execution:
    if report is not None:
        return _close(state, status, report)
    return _wait(state, Status.WAITING_FOR_EXTERNAL,
                 Wait("Store the final Markdown report", "retry finalization"),
                 stopping=None, pending_output=status)


def _primary(state: Execution, evidence: Evidence) -> PullRequest:
    pr = evidence.pr
    if (pr is None or not state.primary_pr or pr.url != state.primary_pr
            or not pr.revision.head or not pr.revision.base):
        raise ValueError("A verified observation of the primary PR is required")
    return pr


def _merge_verified(state: Execution, evidence: Evidence) -> Execution:
    pr = _primary(state, evidence)
    _require(state.target is Target.MERGED and state.ready_revision == pr.revision
             and state.candidate == pr.revision.head
             and not pr.draft and pr.merge_commit,
             "Verified merge must match the primary PR and expected candidate")
    return _close(state, Status.COMPLETED, pr)


def _success(state: Execution, evidence: Evidence, wait: Wait | None) -> Execution:
    stage = state.stage
    candidate = state.candidate
    if stage is Stage.DEVELOPMENT:
        _require(evidence.candidate, "Development requires a recoverable candidate")
        next_stage = Stage.SELF_REVIEW if state.primary_pr else Stage.PR_CREATION
        return _queue(state, next_stage, candidate=evidence.candidate, development_findings=None)
    if stage is Stage.PR_CREATION:
        pr = evidence.pr
        _require(pr and pr.url and pr.draft and not pr.closed and not pr.merge_commit
                 and pr.revision.base and candidate and pr.revision.head == candidate
                 and (not state.primary_pr or state.primary_pr == pr.url),
                 "Publication requires a verified draft for the candidate")
        return _queue(state, Stage.SELF_REVIEW, primary_pr=pr.url)
    if stage is Stage.SELF_REVIEW:
        _require(candidate and evidence.reviewed_candidate == candidate,
                 "Self-review evidence must match the candidate")
        return _queue(state, Stage.PRE_PR_VALIDATION)
    if stage is Stage.PRE_PR_VALIDATION:
        bypass = evidence.bypass
        _require(candidate and evidence.prevalidated_candidate == candidate,
                 "Pre-validation evidence must match the candidate")
        if bypass is not None:
            _require(bypass.checks and all(check.strip() for check in bypass.checks)
                     and bypass.reason.strip() and bypass.authorization.strip(),
                     "A bypass requires skipped checks, a reason, and authorization")
        return _queue(state, Stage.PR_PROMOTION)
    if stage is Stage.REPORT_DRAFTING:
        _require(evidence.report_draft, "Drafting requires a recoverable report")
        return _queue(state, Stage.REPORT_REVIEW, report_draft=evidence.report_draft,
                      report_review_findings=None)
    if stage is Stage.REPORT_REVIEW:
        _require(state.report_draft and evidence.reviewed_report == state.report_draft,
                 "Review must verify the current report draft")
        return _queue(state, Stage.REPORT_STORAGE)
    if stage is Stage.REPORT_STORAGE:
        report = evidence.stored_report
        if report is None:
            _require(state.report_draft and evidence.report_draft == state.report_draft,
                     "Storage requires the current reviewed report draft")
            return _disposition(state, Status.COMPLETED, None)
        _require(report and state.report_draft and report.draft_revision == state.report_draft,
                 "Storage must persist the reviewed report draft")
        return _close(state, Status.COMPLETED, report)

    pr = _primary(state, evidence)
    revision = pr.revision
    if pr.closed and not pr.merge_commit:
        return _wait(state, Status.WAITING_FOR_INPUT, wait)
    if stage is Stage.PR_RECONCILIATION and evidence.candidate:
        _require(evidence.candidate != candidate, "Candidate changes require a new revision")
        return _queue(state, Stage.SELF_REVIEW, candidate=evidence.candidate,
                      publication_conflict=False)
    if candidate != revision.head or evidence.issue is Issue.PR_BLOCKER:
        if stage is Stage.PR_RECONCILIATION:
            return _wait(state, Status.WAITING_FOR_INPUT, wait)
        return _queue(state, Stage.PR_RECONCILIATION)
    if stage is Stage.PR_RECONCILIATION and state.publication_conflict and not pr.merge_commit:
        return _queue(state, Stage.SELF_REVIEW, publication_conflict=False)
    if stage is Stage.PR_PROMOTION:
        _require(not pr.draft and not pr.merge_commit and evidence.draft_checks == revision,
                 "Promotion requires a ready PR and current draft-only gates")
        return _queue(state, Stage.PR_VALIDATION)
    if pr.merge_commit:
        if stage is Stage.CHECK_IN and state.ready_revision == revision:
            return _merge_verified(state, evidence)
        return _wait(state, Status.WAITING_FOR_INPUT, wait)
    _require(not pr.draft and not pr.merge_commit, "Expected an open, ready PR")
    if stage is Stage.ADVERSARIAL_REVIEW:
        _require(evidence.independent_review == revision,
                 "Independent review must match the current revision")
        return _queue(state, Stage.PR_RECONCILIATION)
    if stage is Stage.PR_VALIDATION:
        _require(evidence.ci_and_previews == revision, "CI and previews must be current")
        next_stage = (Stage.PR_RECONCILIATION if evidence.independent_review == revision
                      else Stage.ADVERSARIAL_REVIEW)
        return _queue(state, next_stage)
    if stage is Stage.PR_RECONCILIATION:
        if evidence.ci_and_previews != revision:
            return _queue(state, Stage.PR_VALIDATION)
        if evidence.independent_review != revision:
            return _queue(state, Stage.ADVERSARIAL_REVIEW)
        if evidence.actionable_feedback_cleared != revision:
            return _wait(state, Status.WAITING_FOR_INPUT, wait)
        if evidence.approvals_and_mergeability != revision:
            return _wait(state, Status.WAITING_FOR_EXTERNAL, wait)
        return _queue(state, Stage.READY_FOR_CHECK_IN, ready_revision=revision)

    if (state.ready_revision != revision or evidence.ci_and_previews != revision
            or evidence.independent_review != revision
            or evidence.actionable_feedback_cleared != revision
            or evidence.approvals_and_mergeability != revision):
        return _queue(state, Stage.PR_RECONCILIATION)
    if stage is Stage.READY_FOR_CHECK_IN and state.target is Target.READY:
        return _close(state, Status.COMPLETED, pr)
    if not evidence.merge_authorized:
        return _wait(state, Status.WAITING_FOR_INPUT, wait)
    if stage is Stage.READY_FOR_CHECK_IN:
        return _queue(state, Stage.CHECK_IN)
    raise ValueError("Check-in completion requires GitHub's verified merge commit")


CHANGES = {
    Stage.SELF_REVIEW: Stage.DEVELOPMENT,
    Stage.PRE_PR_VALIDATION: Stage.DEVELOPMENT,
    Stage.PR_PROMOTION: Stage.DEVELOPMENT,
    Stage.PR_VALIDATION: Stage.PR_RECONCILIATION,
    Stage.ADVERSARIAL_REVIEW: Stage.PR_RECONCILIATION,
    Stage.READY_FOR_CHECK_IN: Stage.PR_RECONCILIATION,
    Stage.CHECK_IN: Stage.PR_RECONCILIATION,
    Stage.REPORT_REVIEW: Stage.REPORT_DRAFTING,
}


def _changes(state: Execution, evidence: Evidence) -> Execution:
    stage = state.stage
    if stage is Stage.PR_CREATION:
        if evidence.issue is Issue.BASE_CHANGED_BEFORE_PUBLICATION and not state.primary_pr:
            return _queue(state, Stage.DEVELOPMENT)
        pr = evidence.pr
        _require(evidence.issue is Issue.PR_BLOCKER and pr and pr.url
                 and pr.revision.head and pr.revision.base,
                 "Publication conflict requires a verified PR observation")
        if state.primary_pr:
            _primary(state, evidence)
        else:
            _require(evidence.matching_publication_pr == pr.url,
                     "Discovered PR must match this publication intent")
        return _queue(state, Stage.PR_RECONCILIATION, primary_pr=pr.url,
                      publication_conflict=True)
    if stage is Stage.REPORT_REVIEW:
        _require(state.report_draft and evidence.report_draft == state.report_draft,
                 "Findings must match the report draft")
        return _queue(state, Stage.REPORT_DRAFTING, report_review_findings=evidence.report_review_notes)
    elif stage in {Stage.SELF_REVIEW, Stage.PRE_PR_VALIDATION}:
        _require(state.candidate and evidence.candidate == state.candidate,
                 "Findings must match the candidate")
        return _queue(state, Stage.DEVELOPMENT, development_findings=evidence.development_findings)
    else:
        pr = _primary(state, evidence)
        if stage is Stage.PR_PROMOTION and (evidence.issue is Issue.PR_BLOCKER
                                           or pr.revision.head != state.candidate):
            return _queue(state, Stage.PR_RECONCILIATION)
    _require(stage in CHANGES, "Changes-required outcome is unsupported at this stage")
    return _queue(state, CHANGES[stage])


def decide(
    state: Execution,
    event: Event,
    *,
    outcome: Outcome | None = None,
    evidence: Evidence = Evidence(),
    wait: Wait | None = None,
    retry_allowed: bool = False,
) -> Execution:
    """Decide one event or raise ValueError for an inadmissible event.

    Retry admission follows session/effect reconciliation and bounded retry
    policy. Scheduling a retry or recheck is the caller's responsibility.
    STOP_CONFIRMED attests that the worker and in-flight effects were accounted
    for. A successful RESULT ends the current attempt, even on a stage loop.
    """
    _require(state.status not in TERMINAL, "A closed execution must be rerun as a new execution")
    if state.pending_output is not None:
        _require(event is Event.FINALIZE, "Only finalization may resume this execution")
        if evidence.stored_report is None and wait is not None:
            return replace(state, wait=wait)
        if state.pending_output is Status.COMPLETED and evidence.stored_report is not None:
            _require(state.stage is Stage.REPORT_STORAGE
                     and state.report_draft == evidence.stored_report.draft_revision,
                     "Finalization requires the reviewed report revision")
        return _disposition(state, state.pending_output, evidence.stored_report)
    if event is Event.MERGE_VERIFIED:
        _require(state.stage is Stage.CHECK_IN, "Merge verification requires check-in")
        return _merge_verified(state, evidence)
    if state.stopping is not None:
        _require(event is Event.STOP_CONFIRMED, "Reconcile outstanding effects before closing")
        return _disposition(state, state.stopping, evidence.stored_report)
    if event in {Event.CANCEL, Event.CLOSE_FAILURE}:
        status = Status.CANCELLED if event is Event.CANCEL else Status.FAILED
        if state.status in {Status.RUNNING, Status.WAITING_FOR_EXTERNAL}:
            return _wait(state, Status.WAITING_FOR_EXTERNAL,
                         Wait("Reconcile the worker and outstanding effects", "stop confirmed"),
                         stopping=status)
        return _disposition(state, status, evidence.stored_report)

    if event is Event.CLAIM:
        _require(state.status is Status.QUEUED, "Only queued work can be claimed")
        return replace(state, status=Status.RUNNING)
    if event is Event.ACCEPT_READY_ONLY:
        _require(state.stage is Stage.READY_FOR_CHECK_IN
                 and state.status is Status.WAITING_FOR_INPUT
                 and state.target is Target.MERGED,
                 "Ready-only disposition requires a waiting merge target at the ready stage")
        return _queue(state, target=Target.READY)
    if event is Event.REPLY:
        _require(state.status is Status.WAITING_FOR_INPUT, "No input wait is active")
        return _queue(state)
    if event is Event.RECHECK:
        _require(state.status is Status.WAITING_FOR_EXTERNAL, "No external wait is active")
        return _queue(state)
    if event is Event.RETRY:
        _require(state.status in {Status.RUNNING, Status.WAITING_FOR_INPUT},
                 "Retry requires an active attempt or input wait")
        if retry_allowed:
            return _queue(state)
        return _wait(state, Status.WAITING_FOR_INPUT, wait)
    if event is Event.INPUT_WAIT:
        return _wait(state, Status.WAITING_FOR_INPUT, wait)
    if event is Event.EXTERNAL_WAIT:
        _require(state.status in {Status.RUNNING, Status.WAITING_FOR_EXTERNAL},
                 "External wait requires an active attempt or external wait")
        return _wait(state, Status.WAITING_FOR_EXTERNAL, wait)

    _require(event is Event.RESULT and state.status is Status.RUNNING,
             "Stage results require a running attempt")
    if outcome is Outcome.SUCCEEDED:
        return _success(state, evidence, wait)
    if outcome is Outcome.CHANGES_REQUIRED:
        return _changes(state, evidence)
    if outcome is Outcome.FAILED and retry_allowed:
        return _queue(state)
    _require(outcome in {Outcome.NEEDS_INPUT, Outcome.FAILED}, "Missing stage outcome")
    return _wait(state, Status.WAITING_FOR_INPUT, wait)
