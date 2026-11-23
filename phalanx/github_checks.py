"""Read-only collection of one configured validation phase for the primary PR."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from phalanx.github import CommitChecks, GitHubClient, GitHubError, PullRequest
from phalanx.transitions import Revision
from phalanx.validation import (
    CheckKind,
    CheckOutcome,
    GateResult,
    GateState,
    Phase,
    PublishedCandidate,
    PublishedEvidence,
    RequiredCheck,
    evaluate_published_checks,
)


@dataclass(frozen=True)
class CheckCollection:
    gate: GateResult
    candidate: PublishedCandidate | None = None
    observations: tuple[PublishedEvidence, ...] = ()
    retry_after_seconds: int | None = None


# A repository-specific adapter verifies the preview provider, environment, and
# served SHA (including any smoke test) before returning evidence. Returning
# None means missing; raise GitHubError for an inaccessible provider.
PreviewReader = Callable[
    [PublishedCandidate, Phase, RequiredCheck, str], PublishedEvidence | None
]

# A moved target or a concurrent PR update is observed afresh on the next attempt.
_CHANGED_RETRY_SECONDS = 30

_RUN_OUTCOMES = {
    "success": CheckOutcome.PASSED,
    "neutral": CheckOutcome.NEUTRAL,
    "skipped": CheckOutcome.SKIPPED,
    **dict.fromkeys(
        ("action_required", "cancelled", "failure", "stale", "startup_failure", "timed_out"),
        CheckOutcome.FAILED,
    ),
}
_STATUS_OUTCOMES = {
    "success": CheckOutcome.PASSED,
    "error": CheckOutcome.FAILED,
    "failure": CheckOutcome.FAILED,
}


def _identity(pull: PullRequest) -> tuple:
    return (
        pull.number, pull.node_id, pull.url, pull.head_repository, pull.head_ref,
        pull.head_sha, pull.base_repository, pull.base_ref, pull.base_sha,
    )


def _check_rows(
    pull: PullRequest, checks: CommitChecks, requirements: tuple[RequiredCheck, ...],
) -> Iterator[tuple[str, CheckKind, CheckOutcome, int | None]]:
    run_names = {check.name for check in requirements if check.kind == CheckKind.CHECK_RUN}
    status_names = {check.name for check in requirements if check.kind == CheckKind.STATUS}
    app_bound_statuses = {
        check.name for check in requirements
        if check.kind == CheckKind.STATUS and check.app_id is not None
    }
    latest_runs = {}
    for row in checks.check_runs:
        if row.get("name") not in run_names or row.get("head_sha") != checks.sha:
            continue
        # An empty association is valid for commit-scoped checks (including
        # forks). An explicit association with another PR cannot establish ours.
        associations = row["pull_requests"]
        if not isinstance(associations, list):
            raise GitHubError("collect check runs", "invalid pull request associations")
        if associations and not any(
            pr["number"] == pull.number
            and (pr["head"]["ref"], pr["head"]["sha"]) == (pull.head_ref, pull.head_sha)
            and (pr["base"]["ref"], pr["base"]["sha"]) == (pull.base_ref, pull.base_sha)
            for pr in associations
        ):
            continue
        app = row["app"]
        app_id = app["id"] if app is not None else None
        if app is not None and (type(app_id) is not int or app_id < 1):
            raise GitHubError("collect check runs", "invalid source app ID")
        run_id = row["id"]
        if type(run_id) is not int or run_id < 1:
            raise GitHubError("collect check runs", "invalid check run ID")
        key = row["name"], app_id
        previous = latest_runs.get(key)
        if previous is None or run_id > previous["id"]:
            latest_runs[key] = row
        elif run_id == previous["id"] and row != previous:
            raise GitHubError("collect check runs", "conflicting observations for one run")

    for (name, app_id), row in latest_runs.items():
        outcome = CheckOutcome.PENDING
        if row.get("status") == "completed":
            outcome = _RUN_OUTCOMES.get(row.get("conclusion"), CheckOutcome.PENDING)
        yield name, CheckKind.CHECK_RUN, outcome, app_id

    # GitHub returns legacy statuses newest first, and the context is their
    # identity. The REST response does not attest the owning GitHub App.
    seen = set()
    for row in checks.statuses:
        name = row.get("context")
        if name in seen or name not in status_names:
            continue
        if name in app_bound_statuses:
            raise GitHubError("collect commit statuses", f"cannot verify the source app for {name}")
        seen.add(name)
        outcome = _STATUS_OUTCOMES.get(row.get("state"), CheckOutcome.PENDING)
        yield name, CheckKind.STATUS, outcome, None


def collect_github_checks(
    github: GitHubClient, expected: PullRequest, phase: Phase,
    required: tuple[RequiredCheck, ...] | None, *,
    read_preview: PreviewReader | None = None,
) -> CheckCollection:
    """Read a bounded snapshot using the caller's complete, phase-specific policy.

    None means policy could not be established. An explicit empty tuple means
    the caller verified no applicable checks. Check names and sources must
    identify the configured phase when a provider publishes commit-wide results.
    """
    try:
        pull = github.get_pull_request(expected.number)
        if pull.state != "open" or _identity(pull) != _identity(expected):
            return _blocked("The primary PR identity, revision, or open state changed",
                            retry_after_seconds=_CHANGED_RETRY_SECONDS)

        # Bind evidence to the PR's own head and base. GitHub doesn't advance
        # base.sha when the target moves; mergeability and the repo's rules at
        # merge cover an ordinary advance, so the live tip isn't compared.
        revision = Revision(pull.head_sha, pull.base_sha)
        merge_sha = None
        gather = bool(required) and (phase == Phase.DRAFT or not pull.draft)
        if gather and any(check.use_merge_sha for check in required):
            if pull.mergeable is True and pull.merge_commit_sha:
                try:
                    parents = github.get_commit_parents(pull.merge_commit_sha)
                except GitHubError as error:
                    if error.status != 404:
                        raise
                else:
                    if parents == (revision.base, revision.head):
                        merge_sha = pull.merge_commit_sha
        current = PublishedCandidate(pull.url, revision, pull.draft, expected.head_sha, merge_sha)
        observations = []
        if gather:
            for use_merge, sha in ((False, revision.head), (True, merge_sha)):
                if sha is None:
                    continue
                selected = tuple(check for check in required if check.use_merge_sha == use_merge)
                include_runs = any(check.kind == CheckKind.CHECK_RUN for check in selected)
                include_statuses = any(check.kind == CheckKind.STATUS for check in selected)
                if include_runs or include_statuses:
                    checks = github.get_commit_checks(
                        sha, include_runs=include_runs, include_statuses=include_statuses,
                        all_runs=True,
                    )
                    observations.extend(
                        PublishedEvidence(pull.url, revision, sha, phase, name, kind, outcome, app)
                        for name, kind, outcome, app in _check_rows(pull, checks, selected)
                    )
                for check in selected:
                    if check.kind != CheckKind.PREVIEW:
                        continue
                    if read_preview is None:
                        return _blocked(f"No preview reader is configured for {check.name}")
                    preview = read_preview(current, phase, check, sha)
                    if preview is not None and (
                        preview.pr_url, preview.revision, preview.tested_sha,
                        preview.phase, preview.name, preview.kind,
                    ) == (pull.url, revision, sha, phase, check.name, CheckKind.PREVIEW):
                        observations.append(preview)

        after = github.get_pull_request(pull.number)
        if (
            _identity(after) != _identity(pull) or after.state != "open"
            or after.draft != pull.draft
            or (merge_sha is not None and (
                after.merge_commit_sha != merge_sha or after.mergeable is not True
            ))
        ):
            return _blocked("The primary PR changed during check collection",
                            retry_after_seconds=_CHANGED_RETRY_SECONDS)
        return CheckCollection(
            evaluate_published_checks(current, phase, required, observations),
            current, tuple(observations),
        )
    except GitHubError as error:
        return _blocked(str(error), retry_after_seconds=error.retry_after_seconds)
    except (KeyError, TypeError):
        return _blocked("GitHub returned malformed check data")


def _blocked(reason: str, *, retry_after_seconds: int | None = None) -> CheckCollection:
    return CheckCollection(GateResult(GateState.BLOCKED, (reason,)),
                           retry_after_seconds=retry_after_seconds)
