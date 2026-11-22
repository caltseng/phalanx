import json
import unittest
from collections import deque
from urllib.parse import parse_qs, urlsplit

from phalanx.github import (
    ATTRIBUTION,
    GitHubClient,
    GitHubError,
    HTTPResponse,
    UncertainGitHubWrite,
)

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
API = "https://github.example/api/v3"


def response(data, *, status=200, next_url=None):
    headers = {"link": f'<{next_url}>; rel="next"'} if next_url else {}
    return HTTPResponse(status, headers, json.dumps(data).encode())


def pull(number=7, *, head="phalanx/topic", base="main", draft=True, merged=False):
    return {
        "number": number,
        "node_id": f"PR_{number}",
        "html_url": f"https://github.example/owner/repo/pull/{number}",
        "state": "closed" if merged else "open",
        "draft": draft,
        "merged": merged,
        "head": {
            "ref": head, "sha": HEAD, "user": {"login": "Owner"},
            "repo": {"full_name": "Owner/SourceFork"},
        },
        "base": {"ref": base, "sha": BASE, "repo": {"full_name": "Owner/Repo"}},
        "mergeable": None,
        "merge_commit_sha": MERGE if merged else None,
    }


class GitHubClientTests(unittest.TestCase):
    def setUp(self):
        self.responses = deque()
        self.requests = []
        self.client = GitHubClient("owner", "repo", token="fake", api_url=API,
                                   transport=self.send)

    def send(self, request, timeout):
        self.requests.append(request)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake")
        self.assertEqual(timeout, 30)
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def test_convert_to_draft_separates_refusal_rate_limit_and_unconfirmed(self):
        self.responses.extend([
            response({"data": {"convertPullRequestToDraft": {"pullRequest": {"id": "PR_7", "isDraft": True}}}}),
            response({"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]}),
            response({"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}),
            response({"data": None}),
        ])
        self.client.convert_to_draft("PR_7")
        self.assertIn("convertPullRequestToDraft", json.loads(self.requests[0].data)["query"])
        with self.assertRaises(GitHubError) as refused:
            self.client.convert_to_draft("PR_7")
        self.assertNotIsInstance(refused.exception, UncertainGitHubWrite)
        self.assertIsNone(refused.exception.retry_after_seconds)
        with self.assertRaises(GitHubError) as limited:
            self.client.convert_to_draft("PR_7")
        self.assertEqual(limited.exception.retry_after_seconds, 60)
        with self.assertRaises(UncertainGitHubWrite):
            self.client.convert_to_draft("PR_7")

    def test_exact_pull_includes_current_head_and_merge_evidence(self):
        self.responses.append(response(pull(draft=False, merged=True)))
        result = self.client.get_pull_request(7)
        self.assertEqual(self.requests[0].full_url, f"{API}/repos/owner/repo/pulls/7")
        self.assertEqual((result.head_sha, result.base_sha, result.merge_commit_sha),
                         (HEAD, BASE, MERGE))
        self.assertEqual((result.state, result.draft, result.merged), ("closed", False, True))
        self.assertEqual(result.head_repository, "Owner/SourceFork")
        self.assertIsNone(result.mergeable)

    def test_matching_pulls_follow_link_and_include_closed_matches(self):
        next_url = f"{API}/repositories/123/pulls?page=2"
        self.responses.extend([
            response([pull(), pull(8, head="phalanx/other")], next_url=next_url),
            response([pull(9, draft=False, merged=True), pull(10, base="other")]),
        ])
        result = self.client.list_matching_pull_requests(
            head_owner="owner", head="phalanx/topic", base="main"
        )
        self.assertEqual([item.number for item in result], [7, 9])
        query = parse_qs(urlsplit(self.requests[0].full_url).query)
        self.assertEqual(query, {"head": ["owner:phalanx/topic"], "base": ["main"],
                                 "state": ["all"], "per_page": ["100"]})
        self.assertEqual(self.requests[1].full_url, next_url)

    def test_head_only_search_includes_retargeted_and_closed_pulls(self):
        closed = pull(8, base="release")
        closed["state"] = "closed"
        closed["head"]["repo"] = None
        closed["head"]["user"] = None
        self.responses.append(response([pull(base="release"), closed]))
        result = self.client.list_matching_pull_requests(head_owner="owner", head="phalanx/topic")
        self.assertEqual([(item.number, item.base_ref, item.state) for item in result],
                         [(7, "release", "open"), (8, "release", "closed")])
        self.assertEqual(result[0].head_repository, "Owner/SourceFork")
        self.assertIsNone(result[1].head_repository)
        query = parse_qs(urlsplit(self.requests[0].full_url).query)
        self.assertEqual(query, {"head": ["owner:phalanx/topic"], "state": ["all"],
                                 "per_page": ["100"]})

    def test_publication_lookup_guards_each_page_and_does_not_accept_partial_results(self):
        next_url = f"{API}/repositories/123/pulls?page=2"
        self.responses.extend([response([pull()], next_url=next_url), response([])])
        calls = []

        def checked():
            calls.append(len(self.requests))

        with self.assertRaisesRegex(GitHubError, "pagination limit"):
            self.client.list_matching_pull_requests(
                head_owner="owner", head="phalanx/topic", authorize=checked, page_limit=1,
            )
        self.assertEqual(calls, [0])
        self.responses.appendleft(response([pull()], next_url=next_url))
        matches = self.client.list_matching_pull_requests(
            head_owner="owner", head="phalanx/topic", authorize=checked, page_limit=2,
        )
        self.assertEqual([p.number for p in matches], [7])
        self.assertEqual(calls, [0, 1, 2])

    def test_pagination_rejects_a_link_to_another_host(self):
        self.responses.append(response([pull()], next_url="https://elsewhere.example/pulls"))
        with self.assertRaisesRegex(GitHubError, "untrusted pagination"):
            self.client.list_matching_pull_requests(head_owner="owner", head="phalanx/topic",
                                                    base="main")
        self.assertEqual(len(self.requests), 1)

    def test_create_draft_normalizes_footer(self):
        self.responses.append(response(pull(), status=201))
        self.client.create_draft(head="phalanx/topic", base="main", title="Title",
                                 body=f"Body\r\n\r\n{ATTRIBUTION}\r\n{ATTRIBUTION}")
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {
            "head": "phalanx/topic", "base": "main", "title": "Title",
            "body": f"Body\n\n{ATTRIBUTION}", "draft": True,
        })

    def test_writes_with_lost_or_unusable_responses_are_uncertain(self):
        self.responses.extend([
            TimeoutError(),
            HTTPResponse(201, {}, b"truncated"),
            response({}, status=503),
            response({"number": 7}, status=201),
        ])
        for call_count in range(1, 5):
            with self.assertRaises(UncertainGitHubWrite):
                self.client.create_draft(head="phalanx/topic", base="main", title="Title", body="")
            self.assertEqual(len(self.requests), call_count)

    def test_promotion_uses_node_id_and_surfaces_graphql_errors(self):
        self.responses.extend([
            response({"data": {"markPullRequestReadyForReview": {
                "pullRequest": {"id": "PR_7", "isDraft": False},
            }}}),
            response({"data": None, "errors": [{"message": "Unknown outcome"}]}),
        ])
        self.client.mark_ready("PR_7")
        request = self.requests[0]
        self.assertEqual(request.full_url, "https://github.example/api/graphql")
        self.assertEqual(request.method, "POST")
        payload = json.loads(request.data)
        self.assertIn("markPullRequestReadyForReview", payload["query"])
        self.assertEqual(payload["variables"], {"id": "PR_7"})
        with self.assertRaises(UncertainGitHubWrite):
            self.client.mark_ready("PR_7")
        self.assertEqual(len(self.requests), 2)

    def test_feedback_pages_past_resolved_threads(self):
        def page(threads, cursor):
            return response({"data": {"repository": {"pullRequest": {
                "latestOpinionatedReviews": {"nodes": [
                    {"id": "R1", "state": "CHANGES_REQUESTED", "body": "Fix it", "author": {"login": "ann"}}]},
                "reviewThreads": {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                                  "nodes": threads}}}}})

        def thread(node, resolved):
            return {"id": node, "isResolved": resolved, "isOutdated": False, "path": "a.py",
                    "comments": {"nodes": [{"body": "Rename", "author": {"login": "bob"}}]}}

        self.responses.extend([page([thread("T1", True)], "c1"), page([thread("T2", False)], "c2")])
        feedback = self.client.get_actionable_feedback(7, page_limit=2)
        self.assertEqual([node for node, _ in feedback], ["R1", "T2"])
        self.assertEqual([json.loads(r.data)["variables"]["after"] for r in self.requests], [None, "c1"])

    def test_checks_and_statuses_use_sha_and_paginate_independently(self):
        url = f"{API}/repos/owner/repo/commits/{HEAD}"
        self.responses.extend([
            response({"check_runs": [{"id": 1}]}, next_url=f"{url}/check-runs?page=2"),
            response({"check_runs": [{"id": 2}]}),
            response([{"id": 3}], next_url=f"{url}/statuses?page=2"),
            response([{"id": 4}]),
        ])
        evidence = self.client.get_commit_checks(HEAD)
        self.assertEqual(evidence.sha, HEAD)
        self.assertEqual(evidence.check_runs, ({"id": 1}, {"id": 2}))
        self.assertEqual(evidence.statuses, ({"id": 3}, {"id": 4}))
        self.assertTrue(all(request.full_url.startswith(url) for request in self.requests))

    def test_merge_requires_explicit_method_and_head(self):
        with self.assertRaises(ValueError):
            self.client.merge(7, expected_head="phalanx/topic", method="squash")
        with self.assertRaises(ValueError):
            self.client.merge(7, expected_head=HEAD, method="auto")
        self.assertEqual(self.requests, [])
        self.responses.append(response({"merged": True, "sha": MERGE}))
        self.assertEqual(self.client.merge(7, expected_head=HEAD, method="squash"), MERGE)
        request = self.requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertTrue(request.full_url.endswith("/pulls/7/merge"))
        self.assertEqual(json.loads(request.data), {"sha": HEAD, "merge_method": "squash"})

    def test_merge_distinguishes_stale_head_from_ambiguous_success(self):
        self.responses.extend([response({}, status=409), response({"merged": True})])
        with self.assertRaises(GitHubError) as failure:
            self.client.merge(7, expected_head=HEAD, method="rebase")
        self.assertNotIsInstance(failure.exception, UncertainGitHubWrite)
        self.assertEqual(failure.exception.status, 409)
        with self.assertRaises(UncertainGitHubWrite):
            self.client.merge(7, expected_head=HEAD, method="rebase")
        self.assertEqual(len(self.requests), 2)


if __name__ == "__main__":
    unittest.main()
