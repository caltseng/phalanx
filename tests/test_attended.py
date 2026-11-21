from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from phalanx.attended import AttendedRunner, RunnerAPI, RunnerError
from phalanx.finalization import attempt
from phalanx.store import connect, write_transaction
from phalanx.workspaces import WorkspaceManager

from test_bootstrap import OWNER, RUNNER, ServerFixture


class StubAPI:
    def __init__(self, checkout):
        self.checkout = checkout
        self.attempt = uuid4().hex
        self.claim_leases = []
        self.posts = []
        self.drop_claim = True
        self.drop_result = True
        self.stage = "report_drafting"
        self.dependencies = []
        self.current_version = 3

    def call(self, path, body=None, *, lease=None, key=None):
        if path == "/repositories":
            return [{"id": "repo", "remote": str(self.checkout)}]
        if path == "/items/item":
            return {"execution": {"id": "execution", "version": self.current_version,
                                  "state": {"status": "running"}}}
        if path == "/dispatch/claim":
            assert body["stages"] == ["development", "report_drafting", "report_review", "report_storage"], body
            self.claim_leases.append(lease)
            if self.drop_claim:
                self.drop_claim = False
                raise RunnerError("lost claim response")
            return {
                "status": "claimed", "item_id": "item", "attempt_id": self.attempt,
                "lease_expires_at": 900,
                "execution": {"id": "execution", "version": 3, "state": {"stage": self.stage}},
                "dependencies": self.dependencies,
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


class AttendedTest(ServerFixture, unittest.TestCase):
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

    def write_result(self, runner, marker, evidence, outcome="succeeded"):
        p = runner.status()["packet"]
        fields = ("item_id", "attempt_id", "state_version", "stage", "repository",
                  "worktree", "branch", "expected_revision")
        result = {"contract_version": 1, **{key: p[key] for key in fields},
                  "outcome": outcome, "details": evidence if isinstance(evidence, dict) else {"evidence": evidence}}
        if p["stage"] == "report_review":
            result["report_revision"] = p["report_checkpoint"]["revision"]
        (self.root / "results" / f"{marker}.json").write_text(json.dumps(result), encoding="utf-8")

    def test_report_roundtrip_with_lost_claim_and_result_responses(self):
        self.api.dependencies = [{
            "upstream_id": "upstream", "milestone": "report", "execution_id": "upstream-execution",
            "reference": "/v1/report", "revision": "pinned-report-revision", "reviewed_revision": None,
        }]
        with self.assertRaisesRegex(RunnerError, "lost claim"):
            self.runner().prepare()
        self.assertEqual(self.runner().status()["status"], "claim_pending")
        launch = self.runner().prepare()
        marker = launch["marker"]
        self.assertEqual(self.api.claim_leases[0], self.api.claim_leases[1])
        self.assertNotIn(self.api.claim_leases[0], json.dumps(launch))
        self.assertEqual(launch["create_thread"]["target"]["projectId"], "project")
        self.assertIn(marker, launch["create_thread"]["prompt"])
        self.assertIn("pinned-report-revision", launch["create_thread"]["prompt"])
        self.assertEqual(self.runner().status()["packet"]["dependencies"], self.api.dependencies)
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

    def test_report_review_and_storage_recovery_over_http(self):
        database = self.root.parent / "queue.sqlite3"
        self.start_server(database)
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "repo", "remote": str(self.checkout),
        })[0], 200)
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Document the test suite", "target": "report",
            "acceptance_criteria": ["Include the test command"],
        })
        self.assertEqual(status, 201, item)
        execution = item["execution"]["id"]
        route = f"/v1/items/{item['id']}/executions/{execution}"
        self.api = RunnerAPI(self.url, RUNNER)

        def submit(launch, details, outcome="succeeded"):
            self.assertEqual(launch["status"], "launch_intent")
            marker = launch["marker"]
            thread_id = str(uuid4())
            self.runner().receipt(marker, "local", thread_id=thread_id)
            self.write_result(self.runner(), marker, details, outcome)
            return self.runner().submit(marker, "local", thread_id)

        draft = "# Tests\n"
        first = submit(self.runner().prepare(), draft)["state"]["report_draft"]
        review = self.runner().prepare()
        prompt = review["create_thread"]["prompt"]
        self.assertIn(json.dumps(draft), prompt)
        self.assertIn(first, prompt)
        self.assertIn("Include the test command", prompt)
        findings = "Add the unittest command."
        redo = submit(review, {"findings": findings}, "changes_required")
        self.assertEqual(redo["state"]["stage"], "report_drafting")

        rewrite = self.runner().prepare()
        self.assertIn(findings, rewrite["create_thread"]["prompt"])
        self.assertIn(first, rewrite["create_thread"]["prompt"])
        markdown = "# Tests \u2713\r\nRun `python -m unittest`.  \n"
        second = submit(rewrite, markdown)["state"]["report_draft"]
        self.assertNotEqual(first, second)
        review = self.runner().prepare()
        self.assertIn(second, review["create_thread"]["prompt"])
        self.assertNotIn(first, review["create_thread"]["prompt"])
        verified = submit(review, "Verified the command against the test configuration.")
        self.assertEqual(verified["state"]["stage"], "report_storage")

        with write_transaction(database) as db:
            db.execute("""CREATE TRIGGER storage_outage BEFORE INSERT ON reports
                          BEGIN SELECT RAISE(ABORT, 'simulated outage'); END""")
        call = self.api.call
        posts = []

        def drop_first_response(path, body=None, **kwargs):
            response = call(path, body, **kwargs)
            if path.endswith("/result"):
                posts.append((body, kwargs))
                if len(posts) == 1:
                    raise RunnerError("lost storage response")
            return response

        with patch.object(self.api, "call", side_effect=drop_first_response):
            with self.assertRaisesRegex(RunnerError, "lost storage response"):
                self.runner().prepare()
            self.assertEqual(self.runner().status()["status"], "submitting")
            storage = self.runner().prepare()
        self.assertEqual(storage["status"], "report_storage_submitted")
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(posts[0][0]["report_revision"], second)
        self.assertNotIn("report_markdown", posts[0][0])
        self.assertEqual(storage["execution"]["state"]["status"], "waiting_for_external")
        self.assertEqual(self.runner().status(), {"status": "idle"})

        with write_transaction(database) as db:
            db.execute("DROP TRIGGER storage_outage")
            db.execute("UPDATE report_finalizations SET due_at=0 WHERE execution_id=?", (execution,))
        attempt(database, execution)
        self.assertEqual(self.get(route + "/report", OWNER), (200, markdown))
        self.assertEqual(self.get(route + "/drafts/" + first, OWNER), (200, draft))
        timeline = self.get(route + "/timeline", OWNER)[1]
        self.assertTrue(any(event["details"].get("report_review_notes") == findings for event in timeline))
        with connect(database, mode="ro") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reports").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 5)

    def test_claim_changed_before_launch_retains_reservation(self):
        self.api.drop_claim = False
        self.api.current_version = 4
        with self.assertRaisesRegex(RunnerError, "changed before launch"):
            self.runner().prepare()
        self.assertEqual(self.runner().status()["status"], "claimed")
        self.assertEqual(self.api.posts, [])

    def test_renew_can_run_while_git_prepares_workspace(self):
        self.api.drop_claim = False
        entered = Event()
        resume = Event()
        original = WorkspaceManager.prepare

        def slow_prepare(manager, *args, **kwargs):
            entered.set()
            if not resume.wait(timeout=10):
                raise AssertionError("Workspace preparation did not resume")
            return original(manager, *args, **kwargs)

        with patch.object(WorkspaceManager, "prepare", slow_prepare), ThreadPoolExecutor(2) as pool:
            preparing = pool.submit(self.runner().prepare)
            try:
                self.assertTrue(entered.wait(timeout=10))
                renewal = pool.submit(self.runner().renew).result(timeout=5)
                self.assertEqual(renewal["lease_expires_at"], 901)
            finally:
                resume.set()
            self.assertEqual(preparing.result(timeout=15)["status"], "launch_intent")

    def test_failure_and_owner_question_send_distinct_results(self):
        self.api.drop_claim = self.api.drop_result = False
        outcomes = [
            ("failed", {"reason": "Build unavailable", "attempted": "Retried locally",
                        "retry_guidance": "Try from a new worker"}),
            ("needs_input", {"question": "Which source?", "options": "Source A or B", "handoff": "No edits"}),
        ]
        runner = self.runner()
        for outcome, details in outcomes:
            self.api.attempt = uuid4().hex
            marker = runner.prepare()["marker"]
            thread_id = str(uuid4())
            runner.receipt(marker, "local", thread_id=thread_id)
            self.write_result(runner, marker, details, outcome)
            runner.submit(marker, "local", thread_id)
        technical, question = [post[0] for post in self.api.posts]
        self.assertIn("Build unavailable", technical["failure_reason"])
        self.assertNotIn("wait", technical)
        self.assertNotIn("failure_reason", question)
        self.assertEqual(question["wait"]["trigger"], "owner reply")
        self.assertIn("Which source?", question["wait"]["reason"])

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
