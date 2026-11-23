"""Runner-side direct check-in of the primary PR with a merge commit.

The server records the merge intent with the ready revision before it queues
check-in, so every attempt starts by reading the PR and reconciles an earlier,
possibly lost, write before sending another. GitHub's guard on the expected
head makes a repeated request safe, and its branch rules (required reviews and
checks) gate the merge itself.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from phalanx.github import GitHubClient, GitHubError, PullRequest, UncertainGitHubWrite
from phalanx.transitions import Revision


@dataclass(frozen=True)
class MergeTarget:
    """The primary PR and the ready revision named by the recorded merge intent."""

    number: int
    url: str
    head_ref: str
    base_ref: str
    revision: Revision


@dataclass(frozen=True)
class CheckInOutcome:
    """``merged`` carries the read-back.

    ``pending`` retries under a new attempt, ``moved`` and ``conflict`` return
    to development, ``refused`` waits on GitHub's rules, and ``unresolved``
    and ``mismatch`` need the owner.
    """

    status: Literal["merged", "pending", "moved", "conflict", "refused", "unresolved", "mismatch"]
    reason: str
    pull: PullRequest | None = None
    retry_after_seconds: int | None = None


def check_in(github: GitHubClient, target: MergeTarget, *, assert_claim: Callable[[], None]) -> CheckInOutcome:
    """Verify a merge already made, or request one merge of the ready head.

    assert_claim must raise if the runner no longer owns the check-in attempt.
    """
    observed = _observe(github, target)
    pull = observed.pull
    if observed.status != "pending" or pull is None or pull.merged:
        return observed
    if pull.state != "open" or pull.draft:
        return CheckInOutcome("unresolved", "The primary PR was closed or returned to draft before check-in", pull)
    if pull.head_sha != target.revision.head:
        return CheckInOutcome("moved", f"The PR head {pull.head_sha} is not the ready candidate", pull)
    if pull.mergeable is None:
        return CheckInOutcome("pending", "GitHub is still computing mergeability", pull)
    if not pull.mergeable:
        return CheckInOutcome("conflict", f"The PR conflicts with {target.base_ref}", pull)

    assert_claim()
    try:
        github.merge(target.number, expected_head=target.revision.head, method="merge")
    except UncertainGitHubWrite as error:
        return CheckInOutcome("pending", f"{error}; the next attempt reconciles the merge before any retry", pull)
    except GitHubError as error:
        if error.status in (408, 429):
            return CheckInOutcome("pending", str(error), pull, retry_after_seconds=error.retry_after_seconds)
        if error.status == 409:
            return CheckInOutcome("pending", "The PR head changed during the merge request; reobserving it", pull)
        if error.status == 405:
            return CheckInOutcome("refused", f"GitHub has not allowed the merge yet; its required reviews "
                                  f"and checks gate it ({error})", pull)
        return CheckInOutcome("unresolved", f"GitHub refused the merge ({error}). Grant the runner's token "
                              "permission to merge or resolve the refusal", pull)
    return _observe(github, target)


def _observe(github: GitHubClient, target: MergeTarget) -> CheckInOutcome:
    """Verify a merge of the ready head; an unmerged PR is ``pending``."""
    return observe_merge(github, target.number, target.url, target.head_ref, target.base_ref,
                         heads={target.revision.head})


def observe_merge(github: GitHubClient, number: int, url: str, head_ref: str, base_ref: str | None, *,
                  heads: set[str]) -> CheckInOutcome:
    """Read the primary PR and verify GitHub merged one of the recorded heads.

    This is the one merge verifier for Phalanx's own check-in and for merges
    made by the owner or GitHub. GitHub's merged flag and the PR's final head
    prove what merged, so merge, squash and rebase merges all verify without
    reading merge-commit parents. A merge of any other head is ``mismatch``;
    an unmerged PR or a GitHub error is ``pending`` and never counts as merged.
    """
    try:
        pull = github.get_pull_request(number)
    except GitHubError as error:
        return CheckInOutcome("pending", str(error), retry_after_seconds=error.retry_after_seconds)
    repository = github.repository.casefold()
    if (pull.number != number or pull.url.casefold() != url.casefold() or pull.head_ref != head_ref
            or (base_ref is not None and pull.base_ref != base_ref)
            or (pull.head_repository or repository).casefold() != repository
            or pull.base_repository.casefold() != repository):
        return CheckInOutcome("unresolved", "The PR no longer matches the recorded branch and target", pull)
    if not pull.merged:
        return CheckInOutcome("pending", "GitHub has yet to show the merge", pull)
    if pull.state != "closed" or pull.draft or not pull.merge_commit_sha:
        return CheckInOutcome("pending", "GitHub has yet to show the merge commit", pull)
    if pull.head_sha not in heads:
        return CheckInOutcome("mismatch", f"GitHub merged head {pull.head_sha}, which Phalanx never "
                              "recorded for this PR", pull)
    return CheckInOutcome("merged", "GitHub confirmed the merge of the recorded head", pull)
