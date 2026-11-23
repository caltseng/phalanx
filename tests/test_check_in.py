import json
import unittest

from phalanx.check_in import MergeTarget, check_in, observe_merge
from phalanx.github import GitHubClient, HTTPResponse
from phalanx.transitions import Revision

HEAD, BASE, MERGE, OTHER = (letter * 40 for letter in "abcd")
TARGET = MergeTarget(12, "https://github.com/owner/repo/pull/12", "phalanx/topic", "release/main",
                     Revision(HEAD, BASE))


class CheckInTests(unittest.TestCase):
    def setUp(self):
        self.pull = {
            "number": 12, "node_id": "PR_node", "html_url": TARGET.url, "state": "open", "draft": False,
            "merged": False, "mergeable": True, "merge_commit_sha": OTHER,
            "head": {"sha": HEAD, "ref": TARGET.head_ref, "repo": {"full_name": "owner/repo"}},
            "base": {"sha": BASE, "ref": TARGET.base_ref, "repo": {"full_name": "owner/repo"}},
        }
        self.parents = [BASE, HEAD]
        self.puts = 0
        self.mode = "success"
        self.claims = 0

    def complete_merge(self):
        self.pull.update(state="closed", merged=True, merge_commit_sha=MERGE)

    def send(self, request, timeout):
        route = request.full_url.removeprefix("https://api.github.com/repos/owner/repo")
        method = request.get_method()
        if (method, route) == ("GET", "/pulls/12"):
            body = self.pull
        elif (method, route) == ("GET", f"/git/commits/{MERGE}"):
            body = {"sha": MERGE, "parents": [{"sha": parent} for parent in self.parents]}
        elif (method, route) == ("PUT", "/pulls/12/merge"):
            self.assertEqual(json.loads(request.data), {"sha": HEAD, "merge_method": "merge"})
            self.puts += 1
            if self.mode == "refused":
                return HTTPResponse(405, {}, b'{"message": "Required status check is expected"}')
            self.complete_merge()
            if self.mode == "lost":
                raise TimeoutError
            body = {"merged": True, "sha": MERGE}
        else:
            self.fail(f"Unexpected GitHub request {method} {route}")
        return HTTPResponse(200, {}, json.dumps(body).encode())

    def run_check_in(self):
        def assert_claim():
            self.claims += 1

        return check_in(GitHubClient("owner", "repo", token="fixture", transport=self.send), TARGET,
                        assert_claim=assert_claim)

    def test_merges_under_the_claim_and_verifies_the_merge_commit(self):
        result = self.run_check_in()
        self.assertEqual((result.status, self.puts, self.claims), ("merged", 1, 1))
        self.assertEqual(result.pull.merge_commit_sha, MERGE)

    def test_lost_response_is_reconciled_without_a_second_merge(self):
        self.mode = "lost"
        self.assertEqual(self.run_check_in().status, "pending")
        result = self.run_check_in()
        self.assertEqual((result.status, self.puts), ("merged", 1))

    def test_squash_and_rebase_merges_of_the_recorded_head_verify_without_parents(self):
        self.complete_merge()
        self.pull["base"]["sha"] = OTHER
        result = self.run_check_in()
        self.assertEqual((result.status, result.pull.merge_commit_sha, self.puts), ("merged", MERGE, 0))

    def test_observed_merge_of_another_head_is_a_mismatch(self):
        github = GitHubClient("owner", "repo", token="fixture", transport=self.send)
        self.complete_merge()
        self.assertEqual(observe_merge(github, 12, TARGET.url, TARGET.head_ref, None, heads={HEAD}).status,
                         "merged")
        self.pull["head"]["sha"] = OTHER
        self.assertEqual(observe_merge(github, 12, TARGET.url, TARGET.head_ref, None, heads={HEAD}).status,
                         "mismatch")
        self.pull["head"]["ref"] = "other"
        self.assertEqual(observe_merge(github, 12, TARGET.url, TARGET.head_ref, None, heads={HEAD}).status,
                         "unresolved")

    def test_unmerged_or_unreadable_pr_is_pending(self):
        github = GitHubClient("owner", "repo", token="fixture", transport=self.send)
        self.assertEqual(observe_merge(github, 12, TARGET.url, TARGET.head_ref, None, heads={HEAD}).status,
                         "pending")
        failing = GitHubClient("owner", "repo", token="fixture",
                               transport=lambda request, timeout: HTTPResponse(502, {}, b"{}"))
        self.assertEqual(observe_merge(failing, 12, TARGET.url, TARGET.head_ref, None, heads={HEAD}).status,
                         "pending")

    def test_moved_head_or_conflict_returns_to_development_without_a_write(self):
        self.pull["mergeable"] = False
        self.assertEqual(self.run_check_in().status, "conflict")
        self.pull["head"]["sha"] = OTHER
        self.assertEqual(self.run_check_in().status, "moved")
        self.assertEqual((self.puts, self.claims), (0, 0))

    def test_github_rules_refusal_waits(self):
        self.mode = "refused"
        self.assertEqual(self.run_check_in().status, "refused")


if __name__ == "__main__":
    unittest.main()
