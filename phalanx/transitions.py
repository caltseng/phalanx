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
    SELF_REVIEW = "self_review"
    REPORT_DRAFTING = "report_drafting"
    REPORT_REVIEW = "report_review"
    REPORT_STORAGE = "report_storage"
    # A writer publishes the chosen PR mode after self-review, then the server follows
    # the reconciler's GitHub observations to delivery.
    PUBLICATION = "publication"
    DELIVERY = "delivery"


# A writer's stages after self-review, which take no changes_required: fixes go to the open PR.
WRITER_PR_STAGES = frozenset({Stage.PUBLICATION, Stage.DELIVERY})


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


class PublicationMode(StrEnum):
    READY = "ready"
    DRAFT = "draft"


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    CHANGES_REQUIRED = "changes_required"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


class Event(StrEnum):
    CLAIM = "claim"
    RESULT = "result"
    REPLY = "reply"
    RETRY = "retry"
    CANCEL = "cancel"
    CLOSE_FAILURE = "close_failure"
    STOP_CONFIRMED = "stop_confirmed"
    PR_OBSERVED = "pr_observed"
    FINALIZE = "finalize"


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
    # "merge" or "squash_or_rebase", and GitHub's merged_at, for a verified merge.
    merge_method: str | None = None
    merged_at: str | None = None


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
DELIVERY_WAIT = Wait("GitHub checks, reviews and merge of the primary PR", "reconciler observation")
DRAFT_DELIVERY_WAIT = Wait("Verify the primary PR is an open draft at the reviewed candidate and target base",
                           "reconciler observation")


@dataclass(frozen=True)
class Execution:
    target: Target
    stage: Stage
    status: Status = Status.QUEUED
    candidate: str | None = None
    primary_pr: str | None = None
    report_draft: str | None = None
    wait: Wait | None = None
    stopping: Status | None = None
    pending_output: Status | None = None
    ready_revision: Revision | None = None
    final_output: FinalOutput | None = None
    report_review_findings: str | None = None
    # Findings carried into the next development attempt.
    findings: str | None = None
    publication_mode: PublicationMode = PublicationMode.READY

    def __post_init__(self) -> None:
        if (self.status in TERMINAL) != (self.final_output is not None):
            raise ValueError("Exactly one final output is required on terminal executions")


def start(target: Target, publication_mode: PublicationMode = PublicationMode.READY) -> Execution:
    stage = Stage.REPORT_DRAFTING if target is Target.REPORT else Stage.DEVELOPMENT
    return Execution(target, stage, publication_mode=publication_mode)


def item_branch(item_id: str) -> str:
    """Deterministic PR head branch shared by direct clients and the server."""
    return f"phalanx/{hashlib.sha256(item_id.encode()).hexdigest()}"


@dataclass(frozen=True)
class Evidence:
    """Writer stage evidence and independently observed GitHub facts."""

    candidate: str | None = None
    pr: PullRequest | None = None
    reviewed_candidate: str | None = None
    report_draft: str | None = None
    reviewed_report: str | None = None
    stored_report: StoredReport | None = None
    report_review_notes: str | None = None
    findings: str | None = None
    # The observed PR head is open and ready, with required checks green, no unresolved
    # feedback and no conflict.
    pr_ready: bool = False
    # The observed PR's base branch isn't the item's target branch.
    off_base: bool = False
    # The PR URL a writer reports when publication succeeds.
    published_pr: str | None = None
    # The reconciler proved this commit is an ancestor of the merged PR's head.
    merged_head_ancestor: str | None = None


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


def _finalize_report(state: Execution, status: Status, report: StoredReport | None) -> Execution:
    if report is not None:
        return _close(state, status, report)
    return _wait(state, Status.WAITING_FOR_EXTERNAL,
                 Wait("Store the final Markdown report", "retry finalization"),
                 stopping=None, pending_output=status)


def _pr_observed(state: Execution, evidence: Evidence) -> Execution:
    """Follow GitHub's view of a writer's PR. GitHub's head, URL and merge win (decision 69)."""
    pr = evidence.pr
    # Only the PR this execution's writer published can deliver it, never an earlier
    # execution's PR on the shared item branch.
    _require(state.stage is Stage.DELIVERY and state.target is not Target.REPORT
             and pr and pr.url and pr.revision.head and pr.revision.base
             and state.primary_pr == pr.url,
             "A PR observation requires the PR this execution's writer published, in delivery")
    if state.status is Status.COMPLETED:
        output = state.final_output
        if (state.target is not Target.READY or not isinstance(output, PullRequest)
                or output.draft or output.merge_commit or not state.ready_revision
                or not pr.closed or pr.draft or not pr.merge_commit or not pr.merged_at or evidence.off_base
                or (pr.revision.head != output.revision.head
                    and evidence.merged_head_ancestor != output.revision.head)):
            return state
        return replace(state, candidate=pr.revision.head, ready_revision=pr.revision, final_output=pr)
    if pr.merge_commit and evidence.off_base:
        # A merge can't be undone, and this one didn't deliver into the item's target.
        return _wait(state, Status.WAITING_FOR_INPUT,
                     Wait(f"{pr.url} was merged into a branch other than the item's target",
                          "owner reply"), ready_revision=None)
    if pr.merge_commit:
        # A merge by any method, by anyone, delivers either PR target. GitHub's head is the
        # candidate, since a writer's push can race auto-merge (decision 81).
        _require(not pr.draft, "A verified merge cannot be a draft")
        return _close(replace(state, candidate=pr.revision.head, ready_revision=pr.revision),
                      Status.COMPLETED, pr)
    if state.status is not Status.WAITING_FOR_EXTERNAL:
        return state
    if state.publication_mode is PublicationMode.DRAFT and not pr.closed:
        if pr.revision.head != state.candidate:
            # A draft delivers only a self-reviewed head, so a push after review is reviewed again.
            return _queue(state, Stage.SELF_REVIEW, candidate=pr.revision.head, ready_revision=None)
        if pr.draft and not evidence.off_base:
            return _close(replace(state, ready_revision=None), Status.COMPLETED, pr)
        return replace(state, ready_revision=None)
    if state.publication_mode is not PublicationMode.DRAFT:
        state = replace(state, candidate=pr.revision.head)
    if pr.closed:
        return _wait(state, Status.WAITING_FOR_INPUT,
                     Wait(f"{pr.url} was closed without merging", "owner reply"), ready_revision=None)
    if not evidence.pr_ready or evidence.off_base:
        return replace(state, ready_revision=None)
    if state.target is Target.READY:
        return _close(replace(state, ready_revision=pr.revision), Status.COMPLETED, pr)
    # Ready for check-in: dependents may start; GitHub auto-merge delivers it.
    return replace(state, ready_revision=pr.revision)


def _success(state: Execution, evidence: Evidence) -> Execution:
    stage = state.stage
    candidate = state.candidate
    if stage is Stage.DEVELOPMENT:
        _require(evidence.candidate, "Development requires a recoverable candidate")
        return _queue(state, Stage.SELF_REVIEW, candidate=evidence.candidate, findings=None)
    if stage is Stage.PUBLICATION:
        # The writer pushed and opened the PR. The reconciler's observations of that exact PR
        # decide what happens next.
        _require(evidence.published_pr, "Publication requires the published PR's URL")
        delivery_wait = DRAFT_DELIVERY_WAIT if state.publication_mode is PublicationMode.DRAFT else DELIVERY_WAIT
        return _wait(state, Status.WAITING_FOR_EXTERNAL, delivery_wait, stage=Stage.DELIVERY,
                     primary_pr=evidence.published_pr)
    if stage is Stage.DELIVERY:
        # The writer acted on an owner reply.
        delivery_wait = DRAFT_DELIVERY_WAIT if state.publication_mode is PublicationMode.DRAFT else DELIVERY_WAIT
        return _wait(state, Status.WAITING_FOR_EXTERNAL, delivery_wait)
    if stage is Stage.SELF_REVIEW:
        _require(candidate and evidence.reviewed_candidate == candidate,
                 "Self-review evidence must match the candidate")
        return _queue(state, Stage.PUBLICATION)
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
            return _finalize_report(state, Status.COMPLETED, None)
        _require(report and state.report_draft and report.draft_revision == state.report_draft,
                 "Storage must persist the reviewed report draft")
        return _close(state, Status.COMPLETED, report)

    raise ValueError("Unsupported successful stage result")


def _changes(state: Execution, evidence: Evidence, wait: Wait | None) -> Execution:
    stage = state.stage
    _require(stage not in WRITER_PR_STAGES,
             "Push fixes to the open PR; publication and delivery take no changes_required")
    if stage is Stage.REPORT_REVIEW:
        _require(state.report_draft and evidence.report_draft == state.report_draft,
                 "Findings must match the report draft")
        return _queue(state, Stage.REPORT_DRAFTING, report_review_findings=evidence.report_review_notes)
    if stage is Stage.SELF_REVIEW:
        _require(state.candidate and evidence.candidate == state.candidate,
                 "Findings must match the candidate")
        return _queue(state, Stage.DEVELOPMENT, findings=evidence.findings)
    raise ValueError("Changes-required requires a self-review or report review")


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
    policy. Scheduling a retry is the caller's responsibility.
    STOP_CONFIRMED attests that the worker and in-flight effects were accounted
    for. A successful RESULT ends the current attempt, even on a stage loop.
    Self-review leads to publication; PR_OBSERVED follows GitHub through delivery.
    """
    if state.status is Status.COMPLETED and event is Event.PR_OBSERVED:
        return _pr_observed(state, evidence)
    _require(state.status not in TERMINAL, "A closed execution must be rerun as a new execution")
    if state.pending_output is not None:
        _require(event is Event.FINALIZE, "Only finalization may resume this execution")
        if evidence.stored_report is None and wait is not None:
            return replace(state, wait=wait)
        if state.pending_output is Status.COMPLETED and evidence.stored_report is not None:
            _require(state.stage is Stage.REPORT_STORAGE
                     and state.report_draft == evidence.stored_report.draft_revision,
                     "Finalization requires the reviewed report revision")
        return _finalize_report(state, state.pending_output, evidence.stored_report)
    if state.stopping is not None:
        if (event is Event.PR_OBSERVED and evidence.pr and evidence.pr.merge_commit
                and not evidence.off_base):
            return _pr_observed(state, evidence)
        _require(event is Event.STOP_CONFIRMED, "Reconcile outstanding effects before closing")
        return _finalize_report(state, state.stopping, evidence.stored_report)
    if event is Event.PR_OBSERVED:
        return _pr_observed(state, evidence)
    if event in {Event.CANCEL, Event.CLOSE_FAILURE}:
        status = Status.CANCELLED if event is Event.CANCEL else Status.FAILED
        if state.status in {Status.RUNNING, Status.WAITING_FOR_EXTERNAL}:
            return _wait(state, Status.WAITING_FOR_EXTERNAL,
                         Wait("Reconcile the worker and outstanding effects", "stop confirmed"),
                         stopping=status)
        return _finalize_report(state, status, evidence.stored_report)

    if event is Event.CLAIM:
        _require(state.status is Status.QUEUED, "Only queued work can be claimed")
        return replace(state, status=Status.RUNNING)
    if event is Event.REPLY:
        _require(state.status is Status.WAITING_FOR_INPUT, "No input wait is active")
        return _queue(state)
    if event is Event.RETRY:
        _require(state.status in {Status.RUNNING, Status.WAITING_FOR_INPUT},
                 "Retry requires an active attempt or input wait")
        if retry_allowed:
            return _queue(state)
        return _wait(state, Status.WAITING_FOR_INPUT, wait)
    _require(event is Event.RESULT and state.status is Status.RUNNING,
             "Stage results require a running attempt")
    if outcome is Outcome.SUCCEEDED:
        return _success(state, evidence)
    if outcome is Outcome.CHANGES_REQUIRED:
        return _changes(state, evidence, wait)
    if outcome is Outcome.FAILED and retry_allowed:
        return _queue(state)
    _require(outcome in {Outcome.NEEDS_INPUT, Outcome.FAILED}, "Missing stage outcome")
    return _wait(state, Status.WAITING_FOR_INPUT, wait)
