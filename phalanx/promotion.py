"""Promote an exact primary PR only after its draft gates are verified."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .git_publication import PublicationError
from .github import GitHubClient, GitHubError, PullRequest
from .transitions import Revision
from .validation import (
    GateResult, GateState, Phase, PublishedCandidate, PublishedEvidence, RequiredCheck,
    evaluate_published_checks,
)
from .workspaces import _item_branch


@dataclass(frozen=True)
class DraftChecks:
    required: tuple[RequiredCheck, ...] | None
    observations: tuple[PublishedEvidence, ...] = ()
    merge_sha: str | None = None


@dataclass(frozen=True)
class Promotion:
    status: Literal["verified", "revision_changed", "gates_unresolved", "provider_pending", "claim_lost"]
    reason: str
    pull: PullRequest | None = None
    gates: GateResult | None = None
    checks: DraftChecks | None = None


class _ClaimLost(Exception):
    pass


def promote(
    github: GitHubClient,
    *,
    item_id: str,
    number: int,
    base_ref: str,
    revision: Revision,
    collect: Callable[[PullRequest], DraftChecks],
    claim_current: Callable[[], bool],
) -> Promotion:
    """Observe, evaluate, then converge on ready for the server-selected primary.

    The runner supplies the validated candidate and the PR's observed base.
    Draft evidence binds to that PR revision, not the live target tip: GitHub
    doesn't advance a PR's base.sha when the target moves, and an ordinary
    advance is covered by mergeability here and GitHub's rules at check-in.
    GitHub's ready mutation has no revision precondition. Reobserve the PR
    afterward, including after an ambiguous response, before reporting
    success. The caller still submits evidence with a versioned claim.
    """
    head_ref = _item_branch(item_id)
    repository = github.repository.lower()

    def authorize():
        if not claim_current():
            raise _ClaimLost

    def observe():
        authorize()
        pull = github.get_pull_request(number)
        return pull, (
            pull.number == number
            and (pull.head_repository or "").lower() == repository
            and pull.base_repository.lower() == repository
            and pull.head_ref == head_ref
            and pull.base_ref == base_ref
            and Revision(pull.head_sha, pull.base_sha) == revision
            and pull.mergeable is not False
            and pull.state == "open"
            and pull.merged is False
        )

    changed = "The primary PR changed; reconcile before advancing"
    try:
        pull, matches = observe()
        if not matches:
            return Promotion("revision_changed", changed, pull)
        authorize()
        checks = collect(pull)
        gates = evaluate_published_checks(
            PublishedCandidate(pull.url, revision, pull.draft, revision.head, checks.merge_sha),
            Phase.DRAFT, checks.required, checks.observations,
        )
        if gates.state not in {GateState.SATISFIED, GateState.NOT_CONFIGURED}:
            return Promotion("gates_unresolved", "Draft gates have an unresolved disposition",
                             pull, gates, checks)

        # Collecting tests may take long enough for an unrelated push or retarget.
        before, matches = observe()
        if not matches or before.node_id != pull.node_id or before.url != pull.url:
            return Promotion("revision_changed", changed, before)
        if before.draft:
            reason = "GitHub has not confirmed promotion"
            authorize()
            try:
                github.mark_ready(before.node_id)
            except GitHubError as error:
                # Read back once even on error. A later retry starts with a new
                # observation and never blindly replays the failed request.
                reason = str(error)
            after, matches = observe()
            if not matches or after.node_id != before.node_id or after.url != before.url:
                return Promotion("revision_changed", changed, after)
            if after.draft:
                return Promotion("provider_pending", reason,
                                 after, gates, checks)
        else:
            after = before
        authorize()
        return Promotion("verified", "Verified the ready primary PR", after, gates, checks)
    except _ClaimLost:
        return Promotion("claim_lost", "Promotion requires a current runner claim")
    except (GitHubError, PublicationError) as error:
        return Promotion("provider_pending", str(error))
