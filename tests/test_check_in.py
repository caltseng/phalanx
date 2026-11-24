import json
import unittest

from phalanx.check_in import MergeTarget, check_in
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
        self.compare = "ahead"
        self.compares = 0

    def complete_merge(self):
        self.pull.update(state="closed", merged=True, merge_commit_sha=MERGE)

    def send(self, request, timeout):
        route = request.full_url.removeprefix("https://api.github.com/repos/owner/repo")
        method = request.get_method()
        if (method, route) == ("GET", "/pulls/12"):
            body = self.pull
        elif (method, route) == ("GET", f"/git/commits/{MERGE}"):
            body = {"sha": MERGE, "parents": [{"sha": parent} for parent in self.parents]}
        elif (method, route) == ("GET", f"/compare/{MERGE}...release/main"):
            self.compares += 1
            body = {"status": self.compare}
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
        self.assertEqual((result.status, result.parents, self.puts, self.claims), ("merged", (BASE, HEAD), 1, 1))
        self.assertEqual(result.pull.merge_commit_sha, MERGE)

    def test_lost_response_is_reconciled_without_a_second_merge(self):
        self.mode = "lost"
        self.assertEqual(self.run_check_in().status, "pending")
        result = self.run_check_in()
        self.assertEqual((result.status, self.puts), ("merged", 1))

    def test_merge_after_the_base_advanced_is_verified_by_its_head_parent(self):
        self.complete_merge()
        self.pull["base"]["sha"] = OTHER
        self.parents = [OTHER, HEAD]
        self.assertEqual(self.run_check_in().status, "merged")
        self.parents = [BASE, OTHER]
        self.assertEqual(self.run_check_in().status, "unresolved")
        self.assertEqual(self.puts, 0)

    def test_external_squash_or_rebase_merge_of_the_ready_head_is_verified_on_the_base_branch(self):
        # Churchtree #55: one parent (the prior base), merge commit on the base branch.
        self.complete_merge()
        self.parents = [BASE]
        result = self.run_check_in()
        self.assertEqual((result.status, result.method, result.parents, self.puts), ("merged", "squash_or_rebase",
                                                                                     (BASE,), 0))
        self.compare = "diverged"
        self.assertEqual(self.run_check_in().status, "unresolved")
        self.assertEqual(self.compares, 2)

    def test_external_merge_of_another_head_is_never_accepted(self):
        self.complete_merge()
        self.pull["head"]["sha"] = OTHER
        self.parents = [BASE]
        result = self.run_check_in()
        self.assertEqual(result.status, "unresolved")
        self.assertIn(OTHER, result.reason)
        self.parents = [BASE, OTHER]
        self.assertEqual(self.run_check_in().status, "unresolved")
        self.assertEqual((self.puts, self.compares), (0, 0))

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
