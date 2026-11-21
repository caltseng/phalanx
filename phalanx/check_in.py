"""Runner-side coordination for direct GitHub check-in with a merge commit."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from phalanx.github import GitHubClient, GitHubError, PullRequest, UncertainGitHubWrite
from phalanx.transitions import PullRequest as VerifiedPullRequest
from phalanx.transitions import Revision, Target
from phalanx.validation import GateResult, GateState


@dataclass(frozen=True)
class PrimaryPull:
    """Identity pinned by runner-owned publication and revision verification."""

    repository: str
    number: int
    node_id: str
    url: str
    head_ref: str
    base_ref: str
    revision: Revision


@dataclass(frozen=True)
class CheckInGates:
    """Fresh normalized evidence from trusted review and policy adapters.

    Direct merge assurance must cover the credential, allowed merge method,
    target branch, and current revision. Supply it only when the repository
    permits REST's base race and any base-sensitive evidence is protected by
    a verified repository mechanism. Queue-required and exact-base atomic
    policies cannot authorize this path.
    """

    primary: PrimaryPull
    checks: GateResult
    review_current: bool
    approvals_satisfied: bool
    conversations_resolved: bool
    merge_authorized: bool
    direct_merge_assurance: str | None = None


@dataclass(frozen=True)
class MergeIntent:
    """Persist before sending a merge request. There is no write on recovery."""

    primary: PrimaryPull
    assurance: str


@dataclass(frozen=True)
class CheckInOutcome:
    status: Literal["ready", "merged", "blocked", "stale", "rejected", "unresolved"]
    reason: str
    pull: VerifiedPullRequest | None = None
    acknowledged_sha: str | None = None


def _matches(primary: PrimaryPull, pull: PullRequest) -> bool:
    return (
        pull.number == primary.number and pull.node_id == primary.node_id
        and pull.url == primary.url and pull.head_ref == primary.head_ref
        and pull.head_sha == primary.revision.head and pull.base_ref == primary.base_ref
        and pull.head_repository is not None
        and pull.head_repository.casefold() == primary.repository.casefold()
        and pull.base_repository.casefold() == primary.repository.casefold()
    )


class CheckInCoordinator:
    def __init__(self, github: GitHubClient):
        self.github = github

    def check_in(
        self,
        primary: PrimaryPull,
        target: Target,
        *,
        read_gates: Callable[[PrimaryPull], CheckInGates],
        assert_claim: Callable[[], None],
        reserve: Callable[[MergeIntent], bool],
    ) -> CheckInOutcome:
        """Verify readiness and, for a merge target, make at most one request.

        reserve must atomically persist the intent under the current claim and
        return True only to its new writer. It must refuse a second reservation
        while a prior attempt remains open for this execution or PR, including
        revision changes. Recovery must use that stored intent with reconcile.
        The caller owns recording results and retiring resolved reservations.
        A blocked or stale result from the reserving writer means no request was sent.
        assert_claim must raise if the runner no longer owns the write.
        """
        if target not in (Target.READY, Target.MERGED):
            raise ValueError("check-in requires a PR completion target")
        if primary.repository.casefold() != self.github.repository.casefold():
            return CheckInOutcome("stale", "The GitHub client does not match the primary repository")

        try:
            gates = read_gates(primary)
        except GitHubError as error:
            return CheckInOutcome("blocked", str(error))

        problem = self._verify_ready(primary)
        if problem is not None:
            return problem
        problem = self._verify_gates(primary, target, gates)
        if problem is not None:
            return problem

        if target == Target.READY:
            return CheckInOutcome("ready", "The primary PR is ready for check-in",
                                  VerifiedPullRequest(primary.url, primary.revision))
        assert_claim()
        assurance = gates.direct_merge_assurance
        assert assurance is not None  # Validated by _verify_gates for a merge target.
        if not reserve(MergeIntent(primary, assurance)):
            return CheckInOutcome("unresolved", "Reconcile the existing merge reservation before another write")
        try:
            current_gates = read_gates(primary)
        except GitHubError as error:
            return CheckInOutcome("blocked", str(error))
        problem = self._verify_gates(primary, target, current_gates)
        if problem is not None:
            return problem
        if current_gates.direct_merge_assurance != assurance:
            return CheckInOutcome("stale", "Repository assurance changed during merge reservation")
        problem = self._verify_ready(primary)
        if problem is not None:
            # The reserving writer can retire this intent because it did not send a request.
            return problem
        assert_claim()
        try:
            sha = self.github.merge(primary.number, expected_head=primary.revision.head, method="merge")
        except UncertainGitHubWrite:
            return self._observe_merge(primary)
        except GitHubError as error:
            return CheckInOutcome("rejected", str(error))
        return self._observe_merge(primary, sha)

    @staticmethod
    def _verify_gates(primary: PrimaryPull, target: Target, gates: CheckInGates) -> CheckInOutcome | None:
        if gates.primary != primary:
            return CheckInOutcome("stale", "Review and checks must cover the current primary revision")
        if gates.checks.state not in (GateState.SATISFIED, GateState.NOT_CONFIGURED):
            return CheckInOutcome("blocked", "; ".join(gates.checks.reasons) or "Required checks are unsatisfied")
        if not (gates.review_current and gates.approvals_satisfied and gates.conversations_resolved):
            return CheckInOutcome("blocked", "Current review, approvals, and conversation resolution are required")
        if target == Target.MERGED:
            if not gates.merge_authorized:
                return CheckInOutcome("blocked", "Current merge authorization is required")
            if not gates.direct_merge_assurance or not gates.direct_merge_assurance.strip():
                return CheckInOutcome("blocked", "Verified repository assurance for a direct merge commit is required")
        return None

    def _verify_ready(self, primary: PrimaryPull) -> CheckInOutcome | None:
        try:
            pull = self.github.get_pull_request(primary.number)
            base_tip = self.github.get_branch_tip(primary.base_ref)
        except GitHubError as error:
            return CheckInOutcome("blocked", str(error))

        if (
            not _matches(primary, pull)
            or pull.base_sha != primary.revision.base
            or base_tip != primary.revision.base
        ):
            return CheckInOutcome("stale", "The primary PR or target revision changed")
        if pull.merged or pull.state != "open" or pull.draft or pull.mergeable is not True:
            return CheckInOutcome("blocked", "The primary PR must be open, ready, and mergeable")
        return None

    def reconcile(self, intent: MergeIntent, *, acknowledged_sha: str | None = None) -> CheckInOutcome:
        """Read a persisted attempt after interruption, even if authorization changed.

        An open PR alone cannot exclude an in-flight write. Keep the intent
        reserved until convergence or an explicit disposition establishes that
        another write is safe. Retain any acknowledged SHA with the intent.
        """
        return self._observe_merge(intent.primary, acknowledged_sha)

    def _observe_merge(self, primary: PrimaryPull, acknowledged_sha: str | None = None) -> CheckInOutcome:
        def unresolved(reason: str) -> CheckInOutcome:
            return CheckInOutcome("unresolved", reason, acknowledged_sha=acknowledged_sha)

        if primary.repository.casefold() != self.github.repository.casefold():
            return unresolved("Use the GitHub client for the reserved repository")
        try:
            pull = self.github.get_pull_request(primary.number)
            if not _matches(primary, pull):
                return unresolved("Observed PR identity or head differs from the reserved candidate")
            if pull.merged is not True or pull.draft or pull.state != "closed" or not pull.merge_commit_sha:
                return unresolved("GitHub has yet to confirm the reserved merge")
            if acknowledged_sha is not None and pull.merge_commit_sha != acknowledged_sha:
                return unresolved("The observed merge SHA differs from the merge response")
            parents = self.github.get_commit_parents(pull.merge_commit_sha)
        except GitHubError as error:
            return unresolved(str(error))

        if parents != (primary.revision.base, primary.revision.head):
            return unresolved("Merge commit parents differ from the reserved base and head")
        verified = VerifiedPullRequest(primary.url, primary.revision,
                                       merge_commit=pull.merge_commit_sha, closed=True)
        return CheckInOutcome("merged", "GitHub confirmed the reserved merge", verified, acknowledged_sha)
