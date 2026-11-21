import json
import unittest
from dataclasses import replace

from phalanx.check_in import CheckInCoordinator, CheckInGates, PrimaryPull
from phalanx.github import GitHubClient, HTTPResponse
from phalanx.transitions import Revision, Target
from phalanx.validation import GateResult, GateState

HEAD, BASE, MERGE, OTHER = (letter * 40 for letter in "abcd")
PRIMARY = PrimaryPull("owner/repo", 12, "PR_node", "https://github.com/owner/repo/pull/12",
                      "phalanx/topic", "release/main", Revision(HEAD, BASE))


class CheckInTests(unittest.TestCase):
    def setUp(self):
        self.reset()

    def reset(self):
        self.pull = {
            "number": PRIMARY.number,
            "node_id": PRIMARY.node_id,
            "html_url": PRIMARY.url,
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "merge_commit_sha": OTHER,
            "head": {"sha": HEAD, "ref": PRIMARY.head_ref, "repo": {"full_name": PRIMARY.repository}},
            "base": {"sha": BASE, "ref": PRIMARY.base_ref, "repo": {"full_name": PRIMARY.repository}},
        }
        self.tip = BASE
        self.parents = (BASE, HEAD)
        self.gates = CheckInGates(PRIMARY, GateResult(GateState.SATISFIED),
                                  True, True, True, True)
        self.intent = None
        self.puts = 0
        self.mode = "success"
        self.coordinator = self.new_coordinator()

    def new_coordinator(self):
        return CheckInCoordinator(GitHubClient("owner", "repo", token="fixture", transport=self.send))

    def complete_merge(self):
        self.pull.update(state="closed", merged=True, merge_commit_sha=MERGE)
        self.tip = MERGE

    def send(self, request, timeout):
        prefix = "https://api.github.com/repos/owner/repo"
        route = request.full_url.removeprefix(prefix)
        method = request.get_method()
        if (method, route) == ("GET", "/pulls/12"):
            body = self.pull
        elif (method, route) == ("GET", "/git/ref/heads/release/main"):
            body = {"ref": "refs/heads/release/main", "object": {"type": "commit", "sha": self.tip}}
        elif (method, route) == ("GET", f"/git/commits/{MERGE}"):
            body = {"sha": MERGE, "parents": [{"sha": parent} for parent in self.parents]}
        elif (method, route) == ("PUT", "/pulls/12/merge"):
            self.assertIsNotNone(self.intent)
            self.assertEqual(json.loads(request.data), {"sha": HEAD, "merge_method": "merge"})
            self.puts += 1
            if self.mode == "pending":
                raise TimeoutError
            if self.mode == "rejected":
                return HTTPResponse(409, {}, b"{}")
            if self.mode != "ack_pending":
                self.complete_merge()
            if self.mode == "lost":
                raise TimeoutError
            body = {"merged": True, "sha": MERGE}
        else:
            self.fail(f"Unexpected GitHub request {method} {route}")
        return HTTPResponse(200, {}, json.dumps(body).encode())

    def reserve(self, intent):
        if self.intent is not None:
            return False
        self.intent = intent
        return True

    def run_check_in(self, target=Target.MERGED, *, assert_claim=lambda: None):
        return self.coordinator.check_in(PRIMARY, target, read_gates=lambda _: self.gates,
                                         assert_claim=assert_claim, reserve=self.reserve)

    def test_ready_only_never_reserves_or_writes_even_with_merge_authorization(self):
        outcome = self.run_check_in(Target.READY)
        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.pull.revision, PRIMARY.revision)
        self.assertIsNone(self.intent)
        self.assertEqual(self.puts, 0)

    def test_ready_requires_current_claim_before_returning_evidence(self):
        def assert_claim():
            raise RuntimeError("claim lost")

        with self.assertRaisesRegex(RuntimeError, "claim lost"):
            self.run_check_in(Target.READY, assert_claim=assert_claim)
        self.assertIsNone(self.intent)
        self.assertEqual(self.puts, 0)

    def test_blocked_gates_never_write(self):
        for changes in (
            {"merge_authorized": False},
            {"review_current": False},
            {"approvals_satisfied": False},
            {"conversations_resolved": False},
            {"checks": GateResult(GateState.WAITING)},
        ):
            with self.subTest(changes=changes):
                self.reset()
                self.gates = replace(self.gates, **changes)
                self.assertEqual(self.run_check_in().status, "blocked")
                self.assertIsNone(self.intent)
                self.assertEqual(self.puts, 0)

    def test_stale_primary_or_evidence_never_writes(self):
        original_head, original_base = self.pull["head"].copy(), self.pull["base"].copy()
        self.pull["head"]["sha"] = OTHER
        self.assertEqual(self.run_check_in().status, "stale")
        self.pull["head"] = {**original_head, "repo": {"full_name": "outside/repo"}}
        self.assertEqual(self.run_check_in().status, "stale")
        self.pull["head"] = original_head
        self.pull["base"]["ref"] = "outside"
        self.assertEqual(self.run_check_in().status, "stale")
        self.pull["base"] = original_base
        self.tip = OTHER
        self.assertEqual(self.run_check_in().status, "stale")
        self.tip = BASE
        self.gates = replace(self.gates, primary=replace(PRIMARY, revision=Revision(HEAD, OTHER)))
        self.assertEqual(self.run_check_in().status, "stale")
        self.assertIsNone(self.intent)
        self.assertEqual(self.puts, 0)

    def test_write_and_lost_response_both_require_verified_merge_commit(self):
        for mode in ("success", "lost"):
            with self.subTest(mode=mode):
                self.reset()
                self.mode = mode
                result = self.run_check_in()
                self.assertEqual(result.status, "merged")
                self.assertEqual(result.pull.merge_commit, MERGE)
                self.assertEqual(result.pull.revision, PRIMARY.revision)
                self.assertTrue(result.pull.closed)
                self.assertEqual(self.puts, 1)

    def test_pending_attempt_is_read_only_on_restart_and_converges(self):
        self.mode = "pending"
        self.assertEqual(self.run_check_in().status, "unresolved")
        self.coordinator = self.new_coordinator()
        self.assertEqual(self.coordinator.reconcile(self.intent).status, "unresolved")
        self.assertEqual(self.run_check_in().status, "unresolved")
        self.complete_merge()
        self.assertEqual(self.coordinator.reconcile(self.intent).status, "merged")
        self.assertEqual(self.puts, 1)

    def test_merge_against_an_outside_base_remains_unresolved(self):
        self.parents = (OTHER, HEAD)
        result = self.run_check_in()
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.pull)
        self.assertEqual(result.acknowledged_sha, MERGE)
        self.assertEqual(self.puts, 1)

    def test_reconcile_checks_acknowledged_sha(self):
        self.mode = "ack_pending"
        result = self.run_check_in()
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.acknowledged_sha, MERGE)
        self.complete_merge()
        self.pull["merge_commit_sha"] = OTHER
        result = self.coordinator.reconcile(self.intent, acknowledged_sha=result.acknowledged_sha)
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(self.puts, 1)

    def test_provider_rejection_requires_caller_disposition(self):
        self.mode = "rejected"
        self.assertEqual(self.run_check_in().status, "rejected")
        self.assertEqual(self.run_check_in().status, "unresolved")
        self.assertEqual(self.puts, 1)

    def test_lost_claim_after_reservation_prevents_write(self):
        def assert_claim():
            if self.intent is not None:
                raise RuntimeError("claim lost")

        with self.assertRaisesRegex(RuntimeError, "claim lost"):
            self.run_check_in(assert_claim=assert_claim)
        self.assertIsNotNone(self.intent)
        self.assertEqual(self.puts, 0)

    def test_external_merge_without_an_attempt_is_not_adopted(self):
        self.complete_merge()
        self.assertEqual(self.run_check_in().status, "stale")
        self.assertIsNone(self.intent)
        self.assertEqual(self.puts, 0)

    def test_base_advance_during_reservation_prevents_write(self):
        def reserve_and_advance(intent):
            self.assertTrue(self.reserve(intent))
            self.tip = OTHER
            return True

        result = self.coordinator.check_in(PRIMARY, Target.MERGED, read_gates=lambda _: self.gates,
                                           assert_claim=lambda: None, reserve=reserve_and_advance)
        self.assertEqual(result.status, "stale")
        self.assertEqual(self.puts, 0)

    def test_merge_authorization_withdrawn_during_reservation_prevents_write(self):
        def reserve_and_withdraw(intent):
            self.assertTrue(self.reserve(intent))
            self.gates = replace(self.gates, merge_authorized=False)
            return True

        result = self.coordinator.check_in(PRIMARY, Target.MERGED, read_gates=lambda _: self.gates,
                                           assert_claim=lambda: None, reserve=reserve_and_withdraw)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.puts, 0)
