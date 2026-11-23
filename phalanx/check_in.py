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
from phalanx.pr_models import ExpectedRefs, GitHubPull, MergedPull
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
    method: Literal["merge", "squash_or_rebase"] | None = None


def check_in(github: GitHubClient, target: MergeTarget, *, assert_claim: Callable[[], None]) -> CheckInOutcome:
    """Verify a merge already made, or request one merge of the ready head.

    assert_claim must raise if the runner no longer owns the check-in attempt.
    """
    observed = observe_merge(github, target)
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
    return observe_merge(github, target)


def observe_merge(github: GitHubClient, target: MergeTarget) -> CheckInOutcome:
    """Read the PR and verify a merge of the ready head; an unmerged PR is ``pending``.

    GitHub's PR record is authoritative for the final head. A merge commit must
    have that head as its second parent. Squash and rebase merges leave one
    parent, so their merge commit must instead be on the base branch.
    """
    try:
        pull = github.get_pull_request(target.number)
    except GitHubError as error:
        return CheckInOutcome("pending", str(error), retry_after_seconds=error.retry_after_seconds)
    repository = github.repository.casefold()
    if (pull.number != target.number or pull.url.casefold() != target.url.casefold()
            or (pull.head_ref, pull.base_ref) != (target.head_ref, target.base_ref)
            or (pull.head_repository or "").casefold() != repository
            or pull.base_repository.casefold() != repository):
        return CheckInOutcome("unresolved", "The PR no longer matches the recorded branch and target", pull)
    if not pull.merged:
        return CheckInOutcome("pending", "GitHub has yet to show the merge", pull)
    if pull.state != "closed" or pull.draft or not pull.merge_commit_sha:
        return CheckInOutcome("pending", "GitHub has yet to show the merge commit", pull)
    if pull.head_sha != target.revision.head:
        return CheckInOutcome("unresolved", f"GitHub merged the PR at head {pull.head_sha}, not the "
                              f"validated candidate {target.revision.head}", pull)
    try:
        parents = github.get_commit_parents(pull.merge_commit_sha)
        # The base may have advanced before the merge; the ready head must be the merged head parent.
        if len(parents) == 2 and parents[1] == target.revision.head:
            return CheckInOutcome("merged", "GitHub confirmed the merge of the ready head", pull, parents,
                                  method="merge")
        on_base = len(parents) == 1 and github.is_on_branch(pull.merge_commit_sha, target.base_ref)
    except GitHubError as error:
        return CheckInOutcome("pending", str(error), pull, retry_after_seconds=error.retry_after_seconds)
    if on_base:
        return CheckInOutcome("merged", f"GitHub squashed or rebased the ready head onto {target.base_ref}",
                              pull, parents, method="squash_or_rebase")
    return CheckInOutcome("unresolved", "GitHub's merge commit neither has the ready head as its head "
                          f"parent nor is a single-parent commit on {target.base_ref}", pull, parents)


def merge_evidence(model, event: dict, outcome: CheckInOutcome, base_sha: str, **fields) -> dict:
    """The server payload for a ``merged`` outcome, carrying GitHub's PR record."""
    pull = outcome.pull
    return model(
        **event, **fields,
        expected=ExpectedRefs(head_ref=pull.head_ref, base_ref=pull.base_ref, base_sha=base_sha),
        pull=MergedPull.model_validate({
            **{field: getattr(pull, field) for field in GitHubPull.model_fields},
            "merge_commit": pull.merge_commit_sha, "merged_at": pull.merged_at,
        }),
        merge_parent_shas=list(outcome.parents), merge_method=outcome.method,
    ).model_dump(mode="json")