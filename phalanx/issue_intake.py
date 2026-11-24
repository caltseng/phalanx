"""Explicit local GitHub issue snapshots submitted through owner API intake."""

import os
from hashlib import sha256

from .github import github_repository
from .github import GitHubClient
from .intake_models import Intake


def ingest_issue(api_request, base_url, repository_id, number, target, *, key=None, publication_mode="ready"):
    if not os.environ.get("PHALANX_OWNER_TOKEN"):
        raise ValueError("PHALANX_OWNER_TOKEN is required for this command")
    token = os.environ.get("PHALANX_GITHUB_TOKEN", "")
    if not token:
        raise ValueError("Set PHALANX_GITHUB_TOKEN in the local environment to read GitHub issues")
    repositories = api_request(base_url, "/v1/repositories")
    registered = next((row for row in repositories if row["id"] == repository_id), None)
    if registered is None:
        raise ValueError("Repository not registered; register its expected remote before importing")
    repository = github_repository(registered["remote"])
    if repository is None:
        raise ValueError("Issue ingestion requires a registered github.com repository remote")
    owner, name = repository.split("/")
    github = GitHubClient(owner, name, token=token)
    issue = github.get_issue(number)
    goal = f"{issue.title}\n\nGitHub issue: {issue.url}\n\n{issue.body}".rstrip()
    _check_size(goal)
    for comment in github.issue_comments(number):
        goal += f"\n\n## Comment by @{comment.author}\n{comment.url}\n\n{comment.body}"
        _check_size(goal)
    payload = Intake(repository_id=repository_id, goal=goal, target=target,
                     context_links=[issue.url], publication_mode=publication_mode)
    # Keep issue edits from silently creating another item after an uncertain POST.
    identity = f"{repository_id}:{repository}:{number}"
    key = key or f"github-issue:{sha256(identity.encode()).hexdigest()}"
    return api_request(base_url, "/v1/items", key=key,
                       body=payload.model_dump(mode="json"))


def _check_size(goal):
    if len(goal) > 16000:
        raise ValueError(
            "Issue and comments exceed the 16000-character intake limit; "
            "file a scoped summary with intake instead. No work item was created."
        )
