import json
import unittest
from collections import deque
from urllib.parse import parse_qs, urlsplit

from phalanx.github import (
    GitHubClient,
    GitHubError,
    HTTPResponse,
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

    def test_exact_pull_includes_current_head_and_merge_evidence(self):
        self.responses.append(response({**pull(draft=False, merged=True), "additions": 17, "deletions": 3}))
        result = self.client.get_pull_request(7)
        self.assertEqual(self.requests[0].full_url, f"{API}/repos/owner/repo/pulls/7")
        self.assertEqual((result.head_sha, result.base_sha, result.merge_commit_sha),
                         (HEAD, BASE, MERGE))
        self.assertEqual((result.state, result.draft, result.merged), ("closed", False, True))
        self.assertEqual(result.head_repository, "Owner/SourceFork")
        self.assertIsNone(result.mergeable)
        self.assertEqual((result.additions, result.deletions), (17, 3))

    def test_issue_snapshot_and_paginated_comments_preserve_sources(self):
        issue_url = "https://github.example/Owner/Repo/issues/7"
        self.responses.extend([
            response({"number": 7, "html_url": issue_url, "title": "Fix this", "body": None}),
            response([{"id": 1, "html_url": issue_url + "#issuecomment-1",
                       "user": {"login": "reviewer"}, "body": "First comment"}],
                     next_url=f"{API}/repos/owner/repo/issues/7/comments?page=2"),
            response([{"id": 2, "html_url": issue_url + "#issuecomment-2",
                       "user": None, "body": "Second comment"}]),
        ])
        issue = self.client.get_issue(7)
        self.assertEqual((issue.number, issue.title, issue.body, issue.url),
                         (7, "Fix this", "", issue_url))
        comments = list(self.client.issue_comments(7))
        self.assertEqual([(c.author, c.body) for c in comments],
                         [("reviewer", "First comment"), ("deleted user", "Second comment")])
        self.assertEqual(comments[-1].url, issue_url + "#issuecomment-2")
        self.assertEqual([r.method for r in self.requests], ["GET"] * 3)
        self.assertEqual(self.requests[0].full_url, f"{API}/repos/owner/repo/issues/7")

    def test_issue_reads_reject_pr_wrong_identity_and_invalid_provider_fields(self):
        issue = {"number": 7, "html_url": "https://github.example/owner/repo/issues/7",
                 "title": "Fix this", "body": "Context"}
        for edit in ({"pull_request": {}}, {"number": 8}, {"number": True},
                     {"html_url": "https://github.example/other/repo/issues/7"},
                     {"title": ""}, {"body": []}):
            with self.subTest(edit=edit):
                self.responses.append(response(issue | edit))
                with self.assertRaises(GitHubError):
                    self.client.get_issue(7)
        for number in (0, -1, True):
            with self.assertRaises(ValueError):
                self.client.get_issue(number)
        self.responses.append(response([{"id": 1, "html_url": issue["html_url"] + "#issuecomment-2",
                                        "user": {"login": "reviewer"}, "body": "Comment"}]))
        with self.assertRaisesRegex(GitHubError, "invalid issue comment"):
            list(self.client.issue_comments(7))

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

    def test_lookup_does_not_accept_partial_results(self):
        next_url = f"{API}/repositories/123/pulls?page=2"
        self.responses.extend([response([pull()], next_url=next_url), response([])])
        with self.assertRaisesRegex(GitHubError, "pagination limit"):
            self.client.list_matching_pull_requests(
                head_owner="owner", head="phalanx/topic", page_limit=1,
            )
        self.responses.appendleft(response([pull()], next_url=next_url))
        matches = self.client.list_matching_pull_requests(
            head_owner="owner", head="phalanx/topic", page_limit=2,
        )
        self.assertEqual([p.number for p in matches], [7])

    def test_pagination_rejects_a_link_to_another_host(self):
        self.responses.append(response([pull()], next_url="https://elsewhere.example/pulls"))
        with self.assertRaisesRegex(GitHubError, "untrusted pagination"):
            self.client.list_matching_pull_requests(head_owner="owner", head="phalanx/topic",
                                                    base="main")
        self.assertEqual(len(self.requests), 1)


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


if __name__ == "__main__":
    unittest.main()
