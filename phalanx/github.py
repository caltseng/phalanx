"""Read-only GitHub HTTP primitives for intake and independent reconciliation."""

import http.client
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from time import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_SHA = re.compile(r"[0-9a-fA-F]{40}")
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def github_repository(remote):
    from .pr_models import GITHUB_REMOTE

    match = GITHUB_REMOTE.fullmatch(remote)
    return match[1].lower() if match else None


class GitHubError(Exception):
    """Provider rejection or failed read."""

    def __init__(self, operation: str, reason: str, status: int | None = None, *,
                 retry_after_seconds: int | None = None):
        self.status = status
        self.operation = operation
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{operation}: {reason}")


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _read_retry_delay(response: HTTPResponse) -> int | None:
    headers = {name.lower(): value for name, value in response.headers.items()}
    limited = response.status == 429 or (response.status == 403 and (
        headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
        or b"secondary rate limit" in response.body.lower()
    ))
    if not limited and response.status != 408 and not 500 <= response.status <= 599:
        return None
    delay = 0
    if limited:
        reset = headers.get("x-ratelimit-reset", "")
        if headers.get("x-ratelimit-remaining") == "0" and re.fullmatch(r"[0-9]{1,12}", reset):
            delay = max(0, int(reset) - int(time()))
    retry = headers.get("retry-after", "")
    if re.fullmatch(r"[0-9]{1,10}", retry):
        return max(delay, int(retry))
    return delay or (60 if limited else 0)


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
class Issue:
    number: int
    url: str
    title: str
    body: str


@dataclass(frozen=True)
class IssueComment:
    url: str
    author: str
    body: str


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
    merged_at: str | None = None
    additions: int | None = None
    deletions: int | None = None


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
        merged_at=data.get("merged_at"),
        additions=data.get("additions"),
        deletions=data.get("deletions"),
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
        or (result.merged_at is not None and not isinstance(result.merged_at, str))
    ):
        raise ValueError("invalid pull request fields")
    return result


def _author(node: dict) -> str:
    author = node.get("author")
    return f"@{author['login']}" if author else "A deleted user"


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
        try:
            response = self._transport(request, self._timeout)
        except (OSError, http.client.HTTPException) as error:
            raise GitHubError(
                operation, f"transport failure ({type(error).__name__})",
                retry_after_seconds=0 if method == "GET" else None,
            ) from None
        if response.status != expected:
            raise GitHubError(
                operation, f"GitHub returned HTTP {response.status}", response.status,
                retry_after_seconds=_read_retry_delay(response) if method == "GET" else None,
            )
        try:
            return json.loads(response.body), response.headers
        except (ValueError, UnicodeError):
            raise GitHubError(operation, "invalid JSON response", response.status) from None

    def _pages(
        self, operation: str, url: str, key: str | None = None, *,
        page_limit: int | None = None,
    ) -> Iterator[dict]:
        origin = urlsplit(url)
        visited = set()
        while url:
            if url in visited:
                raise GitHubError(operation, "pagination loop")
            if page_limit is not None and len(visited) >= page_limit:
                raise GitHubError(operation, "pagination limit reached")
            visited.add(url)
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

    def get_issue(self, number: int) -> Issue:
        if type(number) is not int or number < 1:
            raise ValueError("a positive issue number is required")
        operation = "get issue"
        data, _ = self._request(operation, "GET", f"{self._repo_url}/issues/{number}")
        try:
            issue = Issue(data["number"], data["html_url"], data["title"], data["body"] or "")
            if (
                "pull_request" in data or type(issue.number) is not int or issue.number != number
                or not isinstance(issue.url, str)
                or issue.url.casefold() != self._issue_web_url(number).casefold()
                or not isinstance(issue.title, str) or not issue.title.strip()
                or data["body"] is not None and not isinstance(data["body"], str)
            ):
                raise ValueError("unexpected issue identity or fields")
        except (KeyError, TypeError, ValueError):
            raise GitHubError(operation, "invalid issue response (expected an issue, not a PR)") from None
        return issue

    def _issue_web_url(self, number: int) -> str:
        endpoint = urlsplit(self._api_url)
        host = "github.com" if endpoint.netloc == "api.github.com" else endpoint.netloc
        return f"https://{host}/{self._repository}/issues/{number}"

    def issue_comments(self, number: int) -> Iterator[IssueComment]:
        if type(number) is not int or number < 1:
            raise ValueError("a positive issue number is required")
        operation = "list issue comments"
        for data in self._pages(
            operation, f"{self._repo_url}/issues/{number}/comments?per_page=100",
        ):
            try:
                user = data["user"]
                author = user["login"] if user is not None else "deleted user"
                comment = IssueComment(data["html_url"], author, data["body"])
                if (
                    type(data["id"]) is not int or data["id"] < 1
                    or not isinstance(comment.url, str)
                    or comment.url.casefold()
                    != f"{self._issue_web_url(number)}#issuecomment-{data['id']}".casefold()
                    or not isinstance(author, str) or not author.strip()
                    or not isinstance(comment.body, str)
                ):
                    raise ValueError("invalid comment fields")
            except (KeyError, TypeError, ValueError):
                raise GitHubError(operation, "invalid issue comment response") from None
            yield comment

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
        page_limit: int | None = None,
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
            page_limit=page_limit,
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


    def get_actionable_feedback(self, number: int, *, page_limit: int = 10) -> tuple[tuple[str, str], ...]:
        """Return (node ID, text) for requested changes and unresolved current review threads.

        Review threads are paged so newer threads are seen even when older ones stay resolved.
        Past the page limit the feedback read so far is returned rather than stalling the item.
        """
        operation = "read pull request feedback"
        query = """
            query ($owner: String!, $name: String!, $number: Int!, $after: String) {
              repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                  latestOpinionatedReviews(first: 100) {
                    nodes { id state body author { login } }
                  }
                  reviewThreads(first: 100, after: $after) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      id isResolved isOutdated path
                      comments(first: 1) { nodes { body author { login } } }
                    }
                  }
                }
              }
            }
        """
        owner, name = self._repository.split("/")
        feedback, after = [], None
        for page in range(page_limit):
            data, _ = self._request(operation, "POST", self._graphql_url, payload={
                "query": query, "variables": {"owner": owner, "name": name, "number": number, "after": after}})
            try:
                if data.get("errors"):
                    raise ValueError("query errors")
                pull = data["data"]["repository"]["pullRequest"]
                threads = pull["reviewThreads"]
                if page == 0:
                    feedback += [
                        (review["id"], f"{_author(review)} requested changes: {review['body'] or 'no summary'}")
                        for review in pull["latestOpinionatedReviews"]["nodes"]
                        if review["state"] == "CHANGES_REQUESTED"
                    ]
                for thread in threads["nodes"]:
                    if thread["isResolved"] or thread["isOutdated"] or not thread["comments"]["nodes"]:
                        continue
                    comment = thread["comments"]["nodes"][0]
                    feedback.append((thread["id"], f"{_author(comment)} on {thread['path']}: {comment['body']}"))
                if any(not isinstance(node, str) or not node or not isinstance(text, str)
                       for node, text in feedback):
                    raise ValueError("invalid feedback")
                after = threads["pageInfo"]["endCursor"] if threads["pageInfo"]["hasNextPage"] else None
                if after is not None and not isinstance(after, str):
                    raise ValueError("invalid cursor")
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise GitHubError(operation, f"invalid feedback response ({error})") from None
            if after is None:
                break
        return tuple(feedback)

    def get_commit_checks(
        self, sha: str, *, page_limit: int = 10,
        include_runs: bool = True, include_statuses: bool = True,
        all_runs: bool = False,
    ) -> CommitChecks:
        """Read provider evidence for one immutable revision, including legacy statuses."""
        if not _SHA.fullmatch(sha):
            raise ValueError("a full commit SHA is required")
        commit = f"{self._repo_url}/commits/{sha}"
        run_filter = "&filter=all" if all_runs else ""
        runs = tuple(self._pages(
            "list check runs", f"{commit}/check-runs?per_page=100{run_filter}", "check_runs",
            page_limit=page_limit,
        )) if include_runs else ()
        statuses = tuple(self._pages(
            "list commit statuses", f"{commit}/statuses?per_page=100",
            page_limit=page_limit,
        )) if include_statuses else ()
        return CommitChecks(sha, runs, statuses)

    def get_branch_tip(self, branch: str) -> str:
        operation = "get target branch tip"
        ref = f"refs/heads/{branch}"
        data, _ = self._request(
            operation, "GET", f"{self._repo_url}/git/ref/{quote(ref[5:], safe='/')}",
        )
        try:
            sha = data["object"]["sha"]
            if data["ref"] == ref and data["object"]["type"] == "commit" and _SHA.fullmatch(sha):
                return sha
        except (KeyError, TypeError):
            pass
        raise GitHubError(operation, "invalid target branch response")

    def get_commit_parents(self, sha: str) -> tuple[str, ...]:
        if not _SHA.fullmatch(sha):
            raise ValueError("a full commit SHA is required")
        operation = "get test merge commit"
        data, _ = self._request(operation, "GET", f"{self._repo_url}/git/commits/{sha}")
        try:
            parents = tuple(parent["sha"] for parent in data["parents"])
            if data["sha"] == sha and all(_SHA.fullmatch(parent) for parent in parents):
                return parents
        except (KeyError, TypeError):
            pass
        raise GitHubError(operation, "invalid test merge commit response")

    def is_on_branch(self, sha: str, branch: str) -> bool:
        """Whether the commit is reachable from the branch tip."""
        if not _SHA.fullmatch(sha):
            raise ValueError("a full commit SHA is required")
        operation = "compare commit with branch"
        data, _ = self._request(
            operation, "GET", f"{self._repo_url}/compare/{sha}...{quote(branch, safe='/')}",
        )
        status = data.get("status") if isinstance(data, dict) else None
        if status in ("identical", "ahead", "behind", "diverged"):
            return status in ("identical", "ahead")
        raise GitHubError(operation, "invalid comparison response")


def merge_method(github, merge_commit: str, head: str, base_ref: str) -> tuple[str | None, tuple[str, ...]]:
    """How GitHub merged a PR whose final head is ``head``, with the merge commit's parents.

    "merge" when the merge commit's second parent is the head, "squash_or_rebase" when it is a
    single-parent commit on the base branch, else None. GitHub doesn't say which method it used.
    """
    parents = github.get_commit_parents(merge_commit)
    if len(parents) == 2 and parents[1] == head:
        return "merge", parents
    if len(parents) == 1 and github.is_on_branch(merge_commit, base_ref):
        return "squash_or_rebase", parents
    return None, parents
