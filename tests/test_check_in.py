import json
import unittest

from phalanx.check_in import MergeTarget, check_in, owner_merge
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

    def test_moved_head_or_conflict_returns_to_development_without_a_write(self):
        self.pull["mergeable"] = False
        self.assertEqual(self.run_check_in().status, "conflict")
        self.pull["head"]["sha"] = OTHER
        self.assertEqual(self.run_check_in().status, "moved")
        self.assertEqual((self.puts, self.claims), (0, 0))

    def test_github_rules_refusal_waits(self):
        self.mode = "refused"
        self.assertEqual(self.run_check_in().status, "refused")

    def test_owner_merge_accepts_any_method_only_at_the_ready_head(self):
        github = GitHubClient("owner", "repo", token="fixture", transport=self.send)
        self.assertIsNone(owner_merge(github, TARGET))
        self.pull.update(state="closed", merged=True, merge_commit_sha=MERGE)
        self.assertEqual(owner_merge(github, TARGET).merge_commit_sha, MERGE)
        self.pull["head"]["sha"] = OTHER
        with self.assertRaisesRegex(ValueError, "not its verified head"):
            owner_merge(github, TARGET)
        self.assertEqual(self.puts, 0)


if __name__ == "__main__":
    unittest.main()
