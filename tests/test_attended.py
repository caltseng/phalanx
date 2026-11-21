import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from phalanx.attended import AttendedRunner, RunnerError


class StubAPI:
    def __init__(self, checkout):
        self.checkout = checkout
        self.attempt = uuid4().hex
        self.claim_leases = []
        self.posts = []
        self.drop_claim = True
        self.drop_result = True
        self.stage = "report_drafting"
        self.current_version = 3

    def call(self, path, body=None, *, lease=None, key=None):
        if path == "/repositories":
            return [{"id": "repo", "remote": str(self.checkout)}]
        if path == "/items/item":
            return {"execution": {"id": "execution", "version": self.current_version,
                                  "state": {"status": "running"}}}
        if path == "/dispatch/claim":
            self.claim_leases.append(lease)
            if self.drop_claim:
                self.drop_claim = False
                raise RunnerError("lost claim response")
            return {
                "status": "claimed", "item_id": "item", "attempt_id": self.attempt,
                "lease_expires_at": 900,
                "execution": {"id": "execution", "version": 3, "state": {"stage": self.stage}},
                "request": {"repository_id": "repo", "goal": "Document the test suite",
                            "target": "report" if self.stage == "report_drafting" else "ready_for_check_in"},
            }
        if path == f"/dispatch/runners/desk/attempts/{self.attempt}/renew":
            return {"attempt_id": self.attempt, "lease_expires_at": 901}
        if path == "/items/item/executions/execution/result":
            self.posts.append((body, lease, key))
            if self.drop_result:
                self.drop_result = False
                raise RunnerError("lost result response")
            return {"version": 4}
        raise AssertionError(path)


class AttendedTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        self.git(self.checkout, "init", "-b", "main")
        self.git(self.checkout, "remote", "add", "origin", str(self.checkout))
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Base")
        self.git(self.checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.root = root / "runner"
        self.config = {"runner_id": "desk", "repositories": {"repo": {
            "checkout": str(self.checkout), "base_branch": "main", "host_id": "local",
            "project_id": "project",
        }}}
        self.api = StubAPI(self.checkout)

    def git(self, path, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
            cwd=path, stderr=subprocess.STDOUT, text=True,
        ).strip()

    def runner(self):
        return AttendedRunner(self.root, self.config, self.api)

    def write_result(self, runner, marker, evidence):
        p = runner.status()["packet"]
        fields = ("item_id", "attempt_id", "state_version", "stage", "repository",
                  "worktree", "branch", "expected_revision")
        result = {"contract_version": 1, **{key: p[key] for key in fields},
                  "outcome": "succeeded", "details": {"evidence": evidence}}
        (self.root / "results" / f"{marker}.json").write_text(json.dumps(result), encoding="utf-8")

    def test_report_roundtrip_with_lost_claim_and_result_responses(self):
        with self.assertRaisesRegex(RunnerError, "lost claim"):
            self.runner().prepare()
        self.assertEqual(self.runner().status()["status"], "claim_pending")
        launch = self.runner().prepare()
        marker = launch["marker"]
        self.assertEqual(self.api.claim_leases[0], self.api.claim_leases[1])
        self.assertNotIn(self.api.claim_leases[0], json.dumps(launch))
        self.assertEqual(launch["create_thread"]["target"]["projectId"], "project")
        self.assertIn(marker, launch["create_thread"]["prompt"])
        with self.assertRaisesRegex(RunnerError, "already issued"):
            self.runner().prepare()
        self.runner().receipt(marker, "local", client_thread_id=str(uuid4()))
        thread_id = str(uuid4())
        with self.assertRaisesRegex(RunnerError, "native receipt"):
            self.runner().submit(marker, "local", thread_id)
        self.runner().receipt(marker, "local", thread_id=thread_id)
        self.write_result(self.runner(), marker, "# Test report")
        with self.assertRaisesRegex(RunnerError, "lost result"):
            self.runner().submit(marker, "local", thread_id)
        self.assertEqual(self.runner().status()["status"], "submitting")
        self.assertEqual(self.runner().submit(marker, "local", thread_id), {"version": 4})
        self.assertEqual(self.api.posts[0], self.api.posts[1])
        self.assertEqual(self.api.posts[0][0], {
            "attempt_id": self.api.attempt, "stage": "report_drafting", "expected_version": 3,
            "outcome": "succeeded", "report_markdown": "# Test report",
        })
        self.assertEqual(self.runner().status(), {"status": "idle"})

    def test_claim_changed_before_launch_retains_reservation(self):
        self.api.drop_claim = False
        self.api.current_version = 4
        with self.assertRaisesRegex(RunnerError, "changed before launch"):
            self.runner().prepare()
        self.assertEqual(self.runner().status()["status"], "claimed")
        self.assertEqual(self.api.posts, [])

    def test_development_requires_verified_candidate(self):
        self.api.drop_claim = self.api.drop_result = False
        self.api.stage = "development"
        runner = self.runner()
        marker = runner.prepare()["marker"]
        packet = runner.status()["packet"]
        thread_id = str(uuid4())
        runner.receipt(marker, "local", thread_id=thread_id)
        self.write_result(runner, marker, packet["expected_revision"])
        with self.assertRaisesRegex(RunnerError, "new candidate"):
            runner.submit(marker, "local", thread_id)
        self.assertEqual(self.api.posts, [])
        path = Path(packet["worktree"])
        (path / "guide.md").write_text("Guide")
        self.git(path, "add", "guide.md")
        self.git(path, "commit", "-m", "Write guide")
        head = self.git(path, "rev-parse", "HEAD")
        self.write_result(runner, marker, head)
        runner.submit(marker, "local", thread_id)
        self.assertEqual(self.api.posts[0][0]["candidate"], head)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), packet["expected_revision"])


if __name__ == "__main__":
    unittest.main()
