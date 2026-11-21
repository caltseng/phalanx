from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from phalanx.github import GitHubClient, HTTPResponse
from phalanx.published_validation_stage import (
    PublishedValidationAssignment, PublishedValidationPolicy, PublishedValidationRunner,
)
from phalanx.validation import CheckKind, RequiredCheck
from phalanx.workspaces import _item_branch

from test_github import BASE, HEAD, pull, response


class PublishedValidationRunnerTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = Path(temporary.name)
        self.assignment = PublishedValidationAssignment("item", "execution", "runner", "attempt", 9,
                                                        "fixture-lease")
        self.snapshot = pull(head=_item_branch("item"), draft=False)
        self.snapshot["html_url"] = "https://github.com/owner/repo/pull/7"
        self.snapshot["head"]["repo"]["full_name"] = "Owner/Repo"
        self.authority = {
            "current": True, "pr_url": self.snapshot["html_url"],
            "promotion": {
                "number": 7, "head_repository": "Owner/Repo", "base_repository": "Owner/Repo",
                "head_ref": _item_branch("item"), "base_ref": "main", "head_sha": HEAD,
                "base_sha": BASE, "state": "open", "draft": False, "merged": False,
            },
        }
        self.calls = []
        self.responses = deque()
        self.drop = False
        self.runner = self.new_runner()

    def new_runner(self):
        def send(request, timeout):
            self.assertEqual(request.method, "GET")
            result = self.responses.popleft()
            if isinstance(result, Exception):
                raise result
            return result

        github = GitHubClient("owner", "repo", token="fixture", transport=send)
        return PublishedValidationRunner(self.journal, self, github)

    def call(self, path, body, *, lease, key=None):
        self.assertEqual(lease, self.assignment.lease)
        route = path.rsplit("/", 1)[1]
        self.calls.append((route, body, key))
        if route == "pr-validation-claim":
            return self.authority
        if self.drop:
            self.drop = False
            raise TimeoutError("fixture lost API reply")
        return {"committed": True}

    def verify_snapshot(self, *checks):
        tip = response({"ref": "refs/heads/main",
                        "object": {"sha": self.snapshot["base"]["sha"], "type": "commit"}})
        snapshot = response(self.snapshot)
        self.responses.extend((snapshot, snapshot, tip, *checks, snapshot, tip))

    def test_replays_sealed_pending_payload_after_lost_response_and_policy_change(self):
        self.verify_snapshot(response([{"context": "build", "state": "pending"}]))
        self.drop = True
        policy = PublishedValidationPolicy((RequiredCheck("build", CheckKind.STATUS),))
        with self.assertRaises(TimeoutError):
            self.runner.run(self.assignment, policy)
        self.assertFalse(self.responses)
        self.authority = {"current": False}
        self.assertEqual(self.new_runner().run(self.assignment, PublishedValidationPolicy(None)),
                         {"committed": True})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[1], self.calls[2])
        route, body, key = self.calls[1]
        self.assertEqual(route, "pr-observation")
        self.assertTrue(body["schedule_recheck"])
        self.assertEqual(body["gates"]["observations"][0]["outcome"], "pending")
        self.assertEqual((body["gates"]["head_sha"], body["gates"]["base_sha"]), (HEAD, BASE))
        self.assertEqual(key, "published-validation:attempt:9")

    def test_unknown_policy_opens_input_wait_without_github_reads(self):
        self.runner.run(self.assignment, PublishedValidationPolicy(None))
        route, body, _ = self.calls[-1]
        self.assertEqual((route, body["outcome"]), ("result", "needs_input"))
        self.assertIn("validation policy", body["question"]["prompt"])
        self.assertIn(BASE, body["question"]["handoff"])

    def test_transient_initial_transport_failure_uses_bounded_dispatch_retry(self):
        self.responses.append(TimeoutError("fixture transport timeout"))
        self.runner.run(self.assignment, PublishedValidationPolicy(()))
        route, body, _ = self.calls[-1]
        self.assertEqual((route, body["outcome"], body["retry_after_seconds"]),
                         ("result", "failed", 0))
        self.assertIn("transport failure", body["failure_reason"])

    def test_collection_preserves_transient_errors_and_distinguishes_access_denial(self):
        for failure, outcome, delay in (
            (HTTPResponse(503, {}, b"{}"), "failed", 0),
            (HTTPResponse(403, {"Retry-After": "120"}, b"{}"), "failed", 120),
            (HTTPResponse(429, {}, b"{}"), "failed", 60),
            (HTTPResponse(403, {}, b"{}"), "needs_input", None),
        ):
            with self.subTest(status=failure.status, headers=failure.headers):
                snapshot = response(self.snapshot)
                self.responses.extend((snapshot, snapshot, failure))
                assignment = PublishedValidationAssignment(
                    "item", "execution", "runner", "attempt", len(self.calls) + 10,
                    self.assignment.lease,
                )
                self.runner.run(assignment, PublishedValidationPolicy(()))
                route, body, _ = self.calls[-1]
                self.assertEqual((route, body["outcome"], body.get("retry_after_seconds")),
                                 ("result", outcome, delay))

    def test_empty_policy_attests_only_after_exact_ready_snapshot(self):
        self.verify_snapshot()
        self.runner.run(self.assignment, PublishedValidationPolicy(()))
        self.assertFalse(self.responses)
        route, body, _ = self.calls[-1]
        self.assertEqual(route, "pr-observation")
        self.assertEqual(body["gates"]["required"], [])

    def test_fresh_target_tip_is_bound_to_new_evidence(self):
        advanced = "d" * 40
        self.snapshot["base"]["sha"] = advanced
        self.verify_snapshot(response([{"context": "build", "state": "success"}]))
        policy = PublishedValidationPolicy((RequiredCheck("build", CheckKind.STATUS),))
        self.runner.run(self.assignment, policy)
        route, body, _ = self.calls[-1]
        self.assertEqual(route, "pr-observation")
        self.assertEqual(body["expected"]["base_sha"], advanced)
        self.assertEqual(body["gates"]["base_sha"], advanced)

    def test_moved_target_tip_retries_instead_of_asking_owner(self):
        tip = response({"ref": "refs/heads/main", "object": {"sha": "d" * 40, "type": "commit"}})
        snapshot = response(self.snapshot)
        self.responses.extend((snapshot, snapshot, tip))
        self.runner.run(self.assignment, PublishedValidationPolicy(()))
        route, body, _ = self.calls[-1]
        self.assertEqual((route, body["outcome"]), ("result", "failed"))
        self.assertGreater(body["retry_after_seconds"], 0)

    def test_outside_retarget_and_lost_claim_do_not_attest(self):
        self.authority["current"] = False
        self.assertIsNone(self.runner.run(self.assignment, PublishedValidationPolicy(())))
        self.assertEqual(len(self.calls), 1)
        self.authority["current"] = True
        self.snapshot["base"]["ref"] = "outside"
        self.responses.append(response(self.snapshot))
        self.runner.run(self.assignment, PublishedValidationPolicy(()))
        route, body, _ = self.calls[-1]
        self.assertEqual((route, body["outcome"]), ("result", "needs_input"))
        self.assertIn("recorded branch and candidate", body["question"]["prompt"])
