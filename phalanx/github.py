"""Runner-side GitHub HTTP primitives. Callers own policy and reconciliation."""

import http.client
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ATTRIBUTION = "Spearheaded by [Phalanx](https://github.com/caltseng/phalanx)"
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubError(Exception):
    """Provider rejection or failed read."""

    def __init__(self, operation: str, reason: str, status: int | None = None):
        self.status = status
        self.operation = operation
        super().__init__(f"{operation}: {reason}")


class UncertainGitHubWrite(GitHubError):
    """The operation may have taken effect. Reconcile before retrying."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[Request, float], HTTPResponse]


class _NoRedirect(HTTPRedirectHandler):
    # Do not replay writes or forward credentials on redirects.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send(request: Request, timeout: float) -> HTTPResponse:
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        return HTTPResponse(response.code, dict(response.headers.items()), response.read())


@dataclass(frozen=True)
class CommitChecks:
    sha: str
    check_runs: tuple[dict[str, Any], ...]
    statuses: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PullRequest:
    number: int
    node_id: str
    url: str
    state: str
    draft: bool
    merged: bool | None
    head_sha: str
    head_ref: str
    head_owner: str | None
    head_repository: str | None
    base_sha: str
    base_ref: str
    base_repository: str
    merge_commit_sha: str | None
    mergeable: bool | None


def _pull_request(data: dict, *, exact: bool = False) -> PullRequest:
    head, base = data["head"], data["base"]
    head_repo = head.get("repo")
    result = PullRequest(
        number=data["number"],
        node_id=data["node_id"],
        url=data["html_url"],
        state=data["state"],
        draft=data["draft"],
        merged=data["merged"] if exact else data.get("merged"),
        head_sha=head["sha"],
        head_ref=head["ref"],
        head_owner=head["user"]["login"] if head.get("user") else None,
        head_repository=head_repo["full_name"] if head_repo is not None else None,
        base_sha=base["sha"],
        base_ref=base["ref"],
        base_repository=base["repo"]["full_name"],
        merge_commit_sha=data["merge_commit_sha"],
        mergeable=data["mergeable"] if exact else data.get("mergeable"),
    )
    if (
        type(result.number) is not int or result.number < 1
        or not all(
            isinstance(value, str) and value
            for value in (result.node_id, result.url, result.head_ref,
                          result.base_ref, result.base_repository)
        )
        or result.state not in ("open", "closed")
        or type(result.draft) is not bool
        or (exact and type(result.merged) is not bool)
        or (result.mergeable is not None and type(result.mergeable) is not bool)
        or (result.head_owner is not None and not isinstance(result.head_owner, str))
        or (
            head_repo is not None
            and (
                not isinstance(result.head_repository, str)
                or not re.fullmatch(r"[^/]+/[^/]+", result.head_repository)
            )
        )
        or not _SHA.fullmatch(result.head_sha)
        or not _SHA.fullmatch(result.base_sha)
        or (result.merge_commit_sha is not None and not _SHA.fullmatch(result.merge_commit_sha))
    ):
        raise ValueError("invalid pull request fields")
    return result


class GitHubClient:
    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        token: str,
        api_url: str = "https://api.github.com",
        graphql_url: str | None = None,
        api_version: str = "2022-11-28",
        transport: Transport = _send,
        timeout: float = 30,
    ):
        self._api_url = api_url.rstrip("/")
        # GitHub Enterprise Server uses /api/v3 and /api/graphql.
        self._graphql_url = graphql_url or (
            self._api_url.removesuffix("/v3") + "/graphql"
        )
        for endpoint in (self._api_url, self._graphql_url):
            url = urlsplit(endpoint)
            if (
                url.scheme != "https" or not url.hostname or url.username is not None
                or url.query or url.fragment
            ):
                raise ValueError("GitHub endpoints must be HTTPS URLs without credentials or query")
        if not token.strip() or "\r" in token or "\n" in token:
            raise ValueError("a local GitHub token is required")
        for segment in (owner, repo):
            if segment in ("", ".", "..") or "/" in segment or "\\" in segment:
                raise ValueError("owner and repo must be separate path segments")
        self._repository = f"{owner}/{repo}"
        self._repo_url = f"{self._api_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Phalanx",
            "X-GitHub-Api-Version": api_version,
        }
        self._transport = transport
        self._timeout = timeout

    @property
    def repository(self) -> str:
        return self._repository

    def _request(self, operation, method, url, *, payload=None, expected=200):
        headers = self._headers.copy()
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        request = Request(url, data=body, headers=headers, method=method)
        error_type = GitHubError if method == "GET" else UncertainGitHubWrite
        try:
            response = self._transport(request, self._timeout)
        except (OSError, http.client.HTTPException) as error:
            raise error_type(operation, f"transport failure ({type(error).__name__})") from None
        if response.status != expected:
            # A timeout or unexpected success/redirect/server error can hide a write.
            rejected = 400 <= response.status < 500 and response.status != 408
            failure = GitHubError if rejected else error_type
            raise failure(operation, f"GitHub returned HTTP {response.status}", response.status)
        try:
            return json.loads(response.body), response.headers
        except (ValueError, UnicodeError):
            raise error_type(operation, "invalid JSON response", response.status) from None

    def _pages(
        self, operation: str, url: str, key: str | None = None, *,
        authorize: Callable[[], None] | None = None, page_limit: int | None = None,
    ) -> Iterator[dict]:
        origin = urlsplit(url)
        visited = set()
        while url:
            if url in visited:
                raise GitHubError(operation, "pagination loop")
            if page_limit is not None and len(visited) >= page_limit:
                raise GitHubError(operation, "pagination limit reached")
            visited.add(url)
            if authorize is not None:
                authorize()
            data, headers = self._request(operation, "GET", url)
            records = data if key is None else (data.get(key) if isinstance(data, dict) else None)
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                raise GitHubError(operation, "invalid page response")
            yield from records
            link = next((value for name, value in headers.items() if name.lower() == "link"), "")
            match = _NEXT_LINK.search(link)
            if not match:
                return
            url = urljoin(url, match[1])
            target = urlsplit(url)
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.fragment:
                raise GitHubError(operation, "untrusted pagination URL")

    def get_pull_request(self, number: int) -> PullRequest:
        operation = "get pull request"
        data, _ = self._request(operation, "GET", self._pull_url(number))
        try:
            pull = _pull_request(data, exact=True)
            if pull.number != number or pull.base_repository.casefold() != self._repository.casefold():
                raise ValueError("unexpected pull request identity")
        except (AttributeError, KeyError, TypeError, ValueError):
            raise GitHubError(operation, "invalid pull request response") from None
        return pull

    def _pull_url(self, number: int) -> str:
        if type(number) is not int or number < 1:
            raise ValueError("a positive PR number is required")
        return f"{self._repo_url}/pulls/{number}"

    def list_matching_pull_requests(
        self, *, head_owner: str, head: str, base: str | None = None,
        authorize: Callable[[], None] | None = None, page_limit: int | None = None,
    ) -> tuple[PullRequest, ...]:
        """Include closed PRs. Omit base to find PRs retargeted within the repo."""
        operation = "list matching pull requests"
        filters = {"state": "all", "head": f"{head_owner}:{head}", "per_page": "100"}
        if base is not None:
            filters["base"] = base
        query = urlencode(filters)
        matches = []
        for data in self._pages(
            operation, f"{self._repo_url}/pulls?{query}",
            authorize=authorize, page_limit=page_limit,
        ):
            try:
                pull = _pull_request(data)
            except (AttributeError, KeyError, TypeError, ValueError):
                raise GitHubError(operation, "invalid pull request response") from None
            # GitHub applies the owner-qualified filter even when a deleted
            # fork no longer has an owner in the returned head object.
            if (
                pull.head_ref == head and (base is None or pull.base_ref == base)
                and pull.base_repository.casefold() == self._repository.casefold()
                and (pull.head_owner is None or pull.head_owner.casefold() == head_owner.casefold())
            ):
                matches.append(pull)
        return tuple(matches)

    def create_draft(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        operation = "create draft"
        head_owner, separator, head_ref = head.rpartition(":")
        head_owner = head_owner if separator else self._repository.split("/")[0]
        lines = [line for line in body.splitlines() if line.strip() != ATTRIBUTION]
        description = "\n".join(lines).rstrip()
        payload = {
            "head": head,
            "base": base,
            "title": title,
            "body": f"{description}\n\n{ATTRIBUTION}" if description else ATTRIBUTION,
            "draft": True,
        }
        data, _ = self._request(operation, "POST", f"{self._repo_url}/pulls",
                                payload=payload, expected=201)
        try:
            pull = _pull_request(data)
            if (
                pull.state != "open" or not pull.draft or pull.base_ref != base
                or pull.head_ref != head_ref
                or pull.head_owner is None or pull.head_owner.casefold() != head_owner.casefold()
                or pull.base_repository.casefold() != self._repository.casefold()
            ):
                raise ValueError("unexpected draft")
        except (AttributeError, KeyError, TypeError, ValueError):
            raise UncertainGitHubWrite(operation, "invalid draft response", 201) from None
        return pull

    def mark_ready(self, node_id: str) -> None:
        """Promote by node ID. Read the exact PR before and after this write."""
        if not node_id.strip():
            raise ValueError("a PR node ID is required")
        operation = "mark pull request ready"
        query = """
            mutation ($id: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $id}) {
                pullRequest { id isDraft }
              }
            }
        """
        data, _ = self._request(operation, "POST", self._graphql_url,
                                payload={"query": query, "variables": {"id": node_id}})
        try:
            pull = data["data"]["markPullRequestReadyForReview"]["pullRequest"]
            if data.get("errors") or pull["id"] != node_id or pull["isDraft"] is not False:
                raise ValueError("promotion unconfirmed")
        except (AttributeError, KeyError, TypeError, ValueError):
            raise UncertainGitHubWrite(operation, "GitHub did not confirm promotion", 200) from None

    def get_commit_checks(self, sha: str) -> CommitChecks:
        """Read provider evidence for one immutable revision, including legacy statuses."""
        if not _SHA.fullmatch(sha):
            raise ValueError("a full commit SHA is required")
        commit = f"{self._repo_url}/commits/{sha}"
        runs = tuple(self._pages("list check runs", f"{commit}/check-runs?per_page=100", "check_runs"))
        statuses = tuple(self._pages("list commit statuses", f"{commit}/statuses?per_page=100"))
        return CommitChecks(sha, runs, statuses)

    def merge(self, number: int, *, expected_head: str, method: str) -> str:
        """Request a direct merge. Verify its result by rereading the PR."""
        if not _SHA.fullmatch(expected_head):
            raise ValueError("a full expected head SHA is required")
        if method not in ("merge", "squash", "rebase"):
            raise ValueError("an explicit GitHub merge method is required")
        operation = "merge pull request"
        data, _ = self._request(operation, "PUT", f"{self._pull_url(number)}/merge",
                                payload={"sha": expected_head, "merge_method": method})
        try:
            if data["merged"] is True and _SHA.fullmatch(data["sha"]):
                return data["sha"]
        except (KeyError, TypeError):
            pass
        raise UncertainGitHubWrite(operation, "GitHub did not confirm the merge", 200)
    def get_branch_tip(self, branch: str) -> str:
        """Read the exact branch independently of any pull request snapshot."""
        operation = "get branch tip"
        ref = f"heads/{branch}"
        data, _ = self._request(operation, "GET", f"{self._repo_url}/git/ref/{quote(ref, safe='/')}")
        try:
            sha = data["object"]["sha"]
            if (
                data["ref"] == f"refs/{ref}" and data["object"]["type"] == "commit"
                and _SHA.fullmatch(sha)
            ):
                return sha
        except (KeyError, TypeError):
            pass
        raise GitHubError(operation, "invalid branch reference response")

    def get_commit_parents(self, sha: str) -> tuple[str, ...]:
        """Read the ordered parents of one exact Git commit object."""
        if not _SHA.fullmatch(sha):
            raise ValueError("a full commit SHA is required")
        operation = "get commit parents"
        data, _ = self._request(operation, "GET", f"{self._repo_url}/git/commits/{sha}")
        try:
            if data["sha"] == sha and isinstance(data["parents"], list):
                parents = tuple(parent["sha"] for parent in data["parents"])
                if all(_SHA.fullmatch(parent) for parent in parents):
                    return parents
        except (KeyError, TypeError):
            pass
        raise GitHubError(operation, "invalid commit response")
