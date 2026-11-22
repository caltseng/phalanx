"""Pure validation policy for normalized, adapter-supplied evidence."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from phalanx.transitions import Revision

# Pending CI rechecks: runner config defaults and the bounds the API and preflight accept.
CHECK_RECHECK_SECONDS = 30
CHECK_RECHECK_RANGE = (5, 86400)
CHECK_WAIT_LIMIT_SECONDS = 2 * 60 * 60
CHECK_WAIT_LIMIT_RANGE = (60, 30 * 86400)
# Phalanx never returns a ready PR to draft, so it skips draft-only checks a ready PR lacks.
DRAFT_SKIP_REASON = "PR already ready; draft-only checks gate only the first promotion"


class GateState(StrEnum):
    SATISFIED = "satisfied"
    NOT_CONFIGURED = "not_configured"
    WAITING = "waiting"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class GateResult:
    state: GateState
    reasons: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


class PreOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INAPPLICABLE = "inapplicable"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class PreCheck:
    name: str
    allow_inapplicable: bool = False
    allow_skip: bool = False


@dataclass(frozen=True)
class PreEvidence:
    candidate: str
    name: str
    outcome: PreOutcome
    reason: str = ""
    authorization: str = ""


def evaluate_pre_validation(
    candidate: str,
    required: tuple[PreCheck, ...] | None,
    observations: Iterable[PreEvidence],
) -> GateResult:
    """Evaluate current per-check dispositions. The adapter retains prior attempts."""
    policy_result = _policy_result(required)
    if policy_result is not None:
        return policy_result

    current = {observation.name: observation for observation in observations
               if observation.candidate == candidate}

    problems = []
    for check in required:
        result = current.get(check.name)
        if result is None:
            problems.append((GateState.WAITING, f"{check.name} has no result for the candidate"))
            continue
        if result.outcome == PreOutcome.PASSED:
            continue
        if result.outcome == PreOutcome.FAILED:
            problems.append((GateState.FAILED, f"{check.name} failed"))
            continue
        allowed = (
            (result.outcome == PreOutcome.INAPPLICABLE and check.allow_inapplicable)
            or (result.outcome == PreOutcome.SKIPPED and check.allow_skip)
        )
        if not (allowed and result.reason.strip() and result.authorization.strip()):
            problems.append((GateState.BLOCKED, f"{check.name} needs a policy-authorized disposition and reason"))

    return _summarize(problems)


class Phase(StrEnum):
    DRAFT = "draft"
    READY = "ready"


class CheckKind(StrEnum):
    CHECK_RUN = "check_run"
    STATUS = "status"
    PREVIEW = "preview"


class CheckOutcome(StrEnum):
    PASSED = "passed"
    PENDING = "pending"
    FAILED = "failed"
    NEUTRAL = "neutral"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class PublishedCandidate:
    """Expected candidate and verified observation of the primary PR and target tip."""

    pr_url: str
    revision: Revision
    draft: bool
    candidate: str
    merge_sha: str | None = None


@dataclass(frozen=True)
class RequiredCheck:
    name: str
    kind: CheckKind
    app_id: int | None = None
    use_merge_sha: bool = False
    require_execution: bool = False


@dataclass(frozen=True)
class PublishedEvidence:
    pr_url: str
    revision: Revision
    tested_sha: str
    phase: Phase
    name: str
    kind: CheckKind
    outcome: CheckOutcome
    app_id: int | None = None


def evaluate_published_checks(
    current: PublishedCandidate,
    phase: Phase,
    required: tuple[RequiredCheck, ...] | None,
    observations: Iterable[PublishedEvidence],
) -> GateResult:
    """Evaluate one phase against the verified PR identity, head, and base.

    Supply complete policy for this phase, including separate requirements
    when a check run and commit status share a required name. The adapter
    selects the authoritative head or correlated test merge SHA per check,
    and normalizes the latest observation per source. Preview observations
    represent validation of the served revision and configured environment.
    """
    if current.revision.head != current.candidate:
        return GateResult(GateState.BLOCKED, ("The primary PR head differs from the expected candidate",))
    if phase == Phase.READY and current.draft:
        return GateResult(GateState.WAITING, ("The PR is still a draft",))
    policy_result = _policy_result(required)
    if policy_result is not None:
        return policy_result

    matching: dict[tuple[str, CheckKind, str], list[PublishedEvidence]] = {}
    for result in observations:
        if (result.pr_url == current.pr_url and result.revision == current.revision
                and result.phase == phase):
            matching.setdefault((result.name, result.kind, result.tested_sha), []).append(result)

    problems = []
    skipped = []
    for check in required:
        label = f"{check.name} ({check.kind})"
        if check.app_id is not None:
            label += f" from app {check.app_id}"
        sha = current.merge_sha if check.use_merge_sha else current.revision.head
        results = matching.get((check.name, check.kind, sha), []) if sha else []
        results = [
            result for result in results
            if check.app_id is None or result.app_id == check.app_id
        ]
        if not results or any(result.outcome == CheckOutcome.PENDING for result in results):
            if not results and phase == Phase.DRAFT and not current.draft:
                skipped.append(label)
            else:
                reason = "is pending" if results else "has no result for the current revision"
                if not sha:
                    reason = "needs a verified test merge SHA"
                problems.append((GateState.WAITING, f"{label} {reason}"))
        if any(not _accepts(check, result.outcome) for result in results
               if result.outcome != CheckOutcome.PENDING):
            problems.append((GateState.FAILED, f"{label} did not pass"))

    result = _summarize(problems)
    return GateResult(result.state, result.reasons, tuple(skipped)) if skipped else result


def _accepts(check: RequiredCheck, outcome: CheckOutcome) -> bool:
    return outcome == CheckOutcome.PASSED or (
        check.kind == CheckKind.CHECK_RUN
        and not check.require_execution
        and outcome in (CheckOutcome.NEUTRAL, CheckOutcome.SKIPPED)
    )


def _policy_result(required: tuple | None) -> GateResult | None:
    if required is None:
        return GateResult(GateState.BLOCKED, ("Validation policy could not be verified",))
    if not required:
        return GateResult(GateState.NOT_CONFIGURED)
    return None


def _summarize(problems: list[tuple[GateState, str]]) -> GateResult:
    for state in (GateState.FAILED, GateState.BLOCKED, GateState.WAITING):
        if any(problem_state == state for problem_state, _ in problems):
            return GateResult(state, tuple(reason for _, reason in problems))
    return GateResult(GateState.SATISFIED)
