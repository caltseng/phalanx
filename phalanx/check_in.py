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
    """``merged`` carries the read-back and ordered merge parents.

    ``pending`` retries under a new attempt, ``moved`` and ``conflict`` return
    to development, ``refused`` waits on GitHub's rules, and ``unresolved``
    needs the owner.
    """

    status: Literal["merged", "pending", "moved", "conflict", "refused", "unresolved"]
    reason: str
    pull: PullRequest | None = None
    parents: tuple[str, ...] = ()
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


def owner_merge(github: GitHubClient, target: MergeTarget) -> PullRequest | None:
    """Return the PR if the owner merged it at the ready head by any method, else ``None``.

    Raises GitHubError when GitHub cannot be read and ValueError when the PR
    no longer matches the delivered one, so the caller can surface it.
    """
    pull = github.get_pull_request(target.number)
    if not _matches(github, target, pull):
        raise ValueError(f"{target.url} no longer matches the delivered branch and target")
    if not pull.merged or pull.state != "closed" or pull.draft or not pull.merge_commit_sha:
        return None
    if pull.head_sha != target.revision.head:
        raise ValueError(f"{target.url} was merged at {pull.head_sha}, not its verified head "
                         f"{target.revision.head}. Rerun the prerequisite or replace it.")
    return pull


def _matches(github: GitHubClient, target: MergeTarget, pull: PullRequest) -> bool:
    repository = github.repository.casefold()
    return (pull.number == target.number and pull.url.casefold() == target.url.casefold()
            and (pull.head_ref, pull.base_ref) == (target.head_ref, target.base_ref)
            and (pull.head_repository or "").casefold() == repository
            and pull.base_repository.casefold() == repository)


def _observe(github: GitHubClient, target: MergeTarget) -> CheckInOutcome:
    """Read the PR and verify a merge of the ready head; an unmerged PR is ``pending``."""
    try:
        pull = github.get_pull_request(target.number)
    except GitHubError as error:
        return CheckInOutcome("pending", str(error), retry_after_seconds=error.retry_after_seconds)
    if not _matches(github, target, pull):
        return CheckInOutcome("unresolved", "The PR no longer matches the recorded branch and target", pull)
    if not pull.merged:
        return CheckInOutcome("pending", "GitHub has yet to show the merge", pull)
    if pull.state != "closed" or pull.draft or not pull.merge_commit_sha:
        return CheckInOutcome("pending", "GitHub has yet to show the merge commit", pull)
    try:
        parents = github.get_commit_parents(pull.merge_commit_sha)
    except GitHubError as error:
        return CheckInOutcome("pending", str(error), pull, retry_after_seconds=error.retry_after_seconds)
    # The base may have advanced before the merge; the ready head must be the merged head parent.
    if pull.head_sha != target.revision.head or len(parents) != 2 or parents[1] != target.revision.head:
        return CheckInOutcome("unresolved", "GitHub merged the PR without the ready head as the merge "
                              "commit's head parent", pull, parents)
    return CheckInOutcome("merged", "GitHub confirmed the merge of the ready head", pull, parents)
