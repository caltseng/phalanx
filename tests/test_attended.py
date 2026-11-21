from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
from contextlib import closing
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from phalanx.attended import AttendedRunner, RunnerAPI, RunnerError
from phalanx.draft_stage import DraftStageResult
from phalanx.workspaces import WorkspaceManager

from test_bootstrap import OWNER, RUNNER, ServerFixture


class StubAPI:
    def __init__(self, checkout, item_id="item"):
        self.item_id = item_id
        self.checkout = checkout
        self.remote = str(checkout)
        self.attempt = uuid4().hex
        self.claim_leases = []
        self.posts = []
        self.drop_claim = True
        self.drop_result = True
        self.stage = "report_drafting"
        self.candidate = None
        self.dependencies = []
        self.current_version = 3

    def call(self, path, body=None, *, lease=None, key=None):
        if path == "/repositories":
            return [{"id": "repo", "remote": self.remote}]
        if path == f"/items/{self.item_id}":
            return {"execution": {"id": "execution", "version": self.current_version,
                                  "state": {"status": "running"}}}
        if path == "/dispatch/claim":
            expected = ["pr_creation"] if self.stage == "pr_creation" else ["development", "report_drafting"]
            assert body["stages"] == expected, body
            self.claim_leases.append(lease)
            if self.drop_claim:
                self.drop_claim = False
                raise RunnerError("lost claim response")
            return {
                "status": "claimed", "item_id": self.item_id, "attempt_id": self.attempt,
                "lease_expires_at": 900,
                "execution": {"id": "execution", "version": 3, "state": {"stage": self.stage, "candidate": self.candidate}},
                "dependencies": self.dependencies,
                "request": {"repository_id": "repo", "goal": "Document the test suite",
                            "target": "report" if self.stage == "report_drafting" else "ready_for_check_in"},
            }
        if path == f"/dispatch/runners/desk/attempts/{self.attempt}/renew":
            return {"attempt_id": self.attempt, "lease_expires_at": 901}
        if path == f"/items/{self.item_id}/executions/execution/result":
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

    def runner(self, slot=1):
        return AttendedRunner(self.root, self.config, self.api, slot=slot)

    def write_result(self, runner, marker, evidence, outcome="succeeded"):
        p = runner.status()["packet"]
        fields = ("item_id", "attempt_id", "state_version", "stage", "repository",
                  "worktree", "branch", "expected_revision")
        result = {"contract_version": 1, **{key: p[key] for key in fields},
                  "outcome": outcome, "details": evidence if isinstance(evidence, dict) else {"evidence": evidence}}
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

    def test_parallel_slots_use_distinct_worktrees_and_exact_receipts(self):
        self.config["capacity"] = 2
        second_api = StubAPI(self.checkout, "item-2")
        for api in (self.api, second_api):
            api.drop_claim = api.drop_result = False
        first = self.runner()
        second = AttendedRunner(self.root, self.config, second_api, slot=2)
        original_call = StubAPI.call
        claims = Barrier(2)

        def call(api, path, body=None, **kwargs):
            if path == "/dispatch/claim":
                self.assertEqual(body["slots"], 2)
                claims.wait(timeout=10)
            return original_call(api, path, body, **kwargs)

        with patch.object(StubAPI, "call", call), ThreadPoolExecutor(2) as pool:
            first_launch = pool.submit(first.prepare)
            second_launch = pool.submit(second.prepare)
            launches = [first_launch.result(timeout=30), second_launch.result(timeout=30)]
        self.assertEqual([launch["slot"] for launch in launches], [1, 2])
        self.assertNotEqual(first.status()["packet"]["worktree"], second.status()["packet"]["worktree"])
        self.assertNotEqual(first.status()["packet"]["branch"], second.status()["packet"]["branch"])
        self.assertNotEqual(self.api.claim_leases, second_api.claim_leases)
        self.assertEqual([row["slot"] for row in first.status(all_slots=True)["reservations"]], [1, 2])
        with self.assertRaisesRegex(RunnerError, "Slot exceeds local capacity"):
            self.runner(3).prepare()

        marker = launches[0]["marker"]
        native_id = str(uuid4())
        with self.assertRaisesRegex(RunnerError, "Receipt does not match"):
            second.receipt(marker, "local", thread_id=native_id)
        first.receipt(marker, "local", thread_id=native_id)
        self.write_result(first, marker, "# Report")
        entered = Event()
        resume = Event()

        def slow_submit(api, path, body=None, **kwargs):
            if path.endswith("/result"):
                entered.set()
                if not resume.wait(timeout=10):
                    raise AssertionError("Submission did not resume")
            return original_call(api, path, body, **kwargs)

        with patch.object(StubAPI, "call", slow_submit), ThreadPoolExecutor(2) as pool:
            submitted = pool.submit(first.submit, marker, "local", native_id)
            try:
                self.assertTrue(entered.wait(timeout=10))
                heartbeat = pool.submit(second.renew)
                self.assertEqual(heartbeat.result(timeout=5)["attempt_id"], second_api.attempt)
            finally:
                resume.set()
            self.assertEqual(submitted.result(timeout=15), {"version": 4})

        self.assertEqual(first.status(), {"status": "idle"})
        self.assertEqual(second.status()["status"], "launch_uncertain")
        # Reducing capacity preserves the outstanding high-numbered slot and
        # counts it against admission of a replacement in a lower slot.
        self.config["capacity"] = 1
        with self.assertRaisesRegex(RunnerError, "Local capacity is full"):
            self.runner().prepare()
        self.assertEqual(self.runner(2).status()["attempt_id"], second_api.attempt)

    def test_legacy_pending_claim_can_replay_while_another_slot_launches(self):
        with self.assertRaisesRegex(RunnerError, "lost claim"):
            self.runner().prepare()
        with closing(sqlite3.connect(self.runner().journal)) as db, db:
            record = json.loads(db.execute("SELECT record FROM slot WHERE id=1").fetchone()[0])
            del record["slots"]
            db.execute("UPDATE slot SET record=? WHERE id=1", (json.dumps(record),))

        self.config["capacity"] = 2
        self.config["repositories"]["broken"] = {"checkout": str(self.root / "missing")}
        second_api = StubAPI(self.checkout, "item-2")
        second_api.drop_claim = False
        second = AttendedRunner(self.root, self.config, second_api, slot=2)
        original_call = StubAPI.call

        def call(api, path, body=None, **kwargs):
            if path == "/repositories":
                return original_call(api, path, body, **kwargs) + [
                    {"id": "broken", "remote": str(self.checkout)}
                ]
            if path == "/dispatch/claim":
                self.assertEqual(body["repository_ids"], ["repo"])
                self.assertEqual(body["slots"], 1 if api is self.api else 2)
            return original_call(api, path, body, **kwargs)

        with patch.object(StubAPI, "call", call):
            launch = second.prepare()
            self.assertEqual(launch["unverified_mappings"], ["broken"])
            self.assertEqual(self.runner().status()["status"], "claim_pending")
            self.assertEqual(self.runner().prepare()["status"], "launch_intent")
        self.assertEqual(self.api.claim_leases, [record["lease"], record["lease"]])

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
        self.assertEqual(question["question"], {
            "prompt": "Which source?", "options": ["Source A or B"], "handoff": "No edits",
        })

    def test_owner_reply_reaches_the_next_attended_launch(self):
        self.start_server(self.checkout.parent / "queue.sqlite3")
        self.api = RunnerAPI(self.url, RUNNER)
        status, registration = self.request(
            "/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)},
        )
        self.assertEqual(status, 200, registration)
        status, item = self.request(
            "/v1/items", OWNER, {"repository_id": "repo", "goal": "Review sources", "target": "report"},
        )
        self.assertEqual(status, 201, item)
        runner = self.runner()
        marker = runner.prepare()["marker"]
        thread_id = str(uuid4())
        runner.receipt(marker, "local", thread_id=thread_id)
        self.write_result(runner, marker, {
            "question": "Which source?", "options": "Source A or B", "handoff": "Draft in notes.md",
        }, "needs_input")
        waiting = runner.submit(marker, "local", thread_id)
        self.assertEqual(waiting["state"]["status"], "waiting_for_input")
        self.assertEqual(runner.status(), {"status": "idle"})
        status, inbox = self.get("/v1/owner/inbox", OWNER)
        self.assertEqual(status, 200, inbox)
        question = {
            "prompt": "Which source?", "options": ["Source A or B"],
            "handoff": "Draft in notes.md", "proposed_default": None,
        }
        self.assertEqual(inbox[0]["question"], question)
        route = f"/v1/items/{item['id']}/executions/{waiting['id']}/reply"
        status, queued = self.request(route, OWNER, {
            "expected_version": waiting["version"], "answer": "Source B only",
        }, {"Idempotency-Key": "answer"})
        self.assertEqual(status, 200, queued)
        self.assertEqual(queued["state"]["status"], "queued")
        resumed = self.runner()
        launch = resumed.prepare()
        self.assertNotEqual(launch["marker"], marker)
        packet = resumed.status()["packet"]
        self.assertEqual(packet["stage"], "report_drafting")
        self.assertEqual(json.loads(packet["handoff"]), {
            "question": question, "answer": "Source B only", "wait_version": waiting["version"],
        })
        self.assertIn("Source B only", launch["create_thread"]["prompt"])
        self.assertIn("Draft in notes.md", launch["create_thread"]["prompt"])

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

    def start_development(self):
        self.api.remote = "git@github.com:example/repo.git"
        self.git(self.checkout, "remote", "set-url", "origin", self.api.remote)
        self.api.drop_claim = self.api.drop_result = False
        self.api.stage = "development"
        runner = self.runner()
        marker = runner.prepare()["marker"]
        packet = runner.status()["packet"]
        thread = str(uuid4())
        runner.receipt(marker, "local", thread_id=thread)
        worktree = Path(packet["worktree"])
        self.git(worktree, "commit", "--allow-empty", "-m", "Candidate")
        candidate = self.git(worktree, "rev-parse", "HEAD")
        self.write_result(runner, marker, candidate)
        return runner, marker, thread, worktree, candidate, packet["expected_revision"]

    def test_accepted_development_checkpoint_keeps_pinned_base_for_draft(self):
        runner, marker, thread, worktree, candidate, base = self.start_development()
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Outside base advance")
        self.git(self.checkout, "update-ref", "refs/remotes/origin/main", "HEAD")

        call = self.api.call
        def reject(path, *args, **kwargs):
            if path.endswith("/result"):
                raise RunnerError("rejected development result")
            return call(path, *args, **kwargs)

        with patch.object(self.api, "call", side_effect=reject):
            with self.assertRaisesRegex(RunnerError, "rejected"):
                runner.submit(marker, "local", thread)
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), candidate)
        pending = runner._snapshot()
        self.assertEqual(pending["submission"]["checkpoint"]["base"], {"ref": "main", "sha": base})
        runner.submit(marker, "local", thread)

        self.api.attempt = uuid4().hex
        self.api.stage, self.api.candidate = "pr_creation", candidate
        with patch.dict(os.environ, PHALANX_GITHUB_TOKEN="runner-only"), patch(
            "phalanx.attended.publish_draft",
            return_value=DraftStageResult(execution={"id": "execution", "version": 4}),
        ) as publish:
            result = self.runner().prepare(draft=True)
        checkpoint = publish.call_args.args[4]
        self.assertEqual((checkpoint.checkout, checkpoint.candidate, checkpoint.base_ref, checkpoint.base_sha),
                         (worktree, candidate, "main", base))
        self.assertEqual(result, {"status": "draft_submitted", "execution": {"id": "execution", "version": 4}, "slot": 1})
        self.assertEqual(self.runner().status(), {"status": "idle"})
        self.assertNotIn("runner-only", json.dumps(result))

    def test_legacy_development_launch_preserves_candidate_and_blocks_draft(self):
        runner, marker, thread, worktree, candidate, _ = self.start_development()
        runner._transaction(lambda record, db: record.pop("development_base"))
        runner.submit(marker, "local", thread)
        self.api.attempt = uuid4().hex
        self.api.stage, self.api.candidate = "pr_creation", candidate
        with patch.dict(os.environ, PHALANX_GITHUB_TOKEN="runner-only"), patch(
            "phalanx.attended.publish_draft",
        ) as publish:
            result = self.runner().prepare(draft=True)
        publish.assert_not_called()
        self.assertEqual(result["status"], "draft_blocked")
        self.assertIn("no accepted development base checkpoint", result["reason"])
        self.assertEqual(self.runner().status()["reason"], result["reason"])
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), candidate)


if __name__ == "__main__":
    unittest.main()
