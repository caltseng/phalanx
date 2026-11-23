from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
from contextlib import closing
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from phalanx import validation_stages
from phalanx.attended import AttendedRunner, RunnerAPI, RunnerError
from phalanx.attended_drafts import draft_title
from phalanx.attended_recovery_service import RecoveryJournal, run_recovery
from phalanx.captain_loop import CaptainLoop
from phalanx.draft_publication import DraftOutcome
from phalanx.draft_stage import DraftStageResult
from phalanx.finalization import attempt
from phalanx.git_publication import PublicationResult
from phalanx.github import ATTRIBUTION, GitHubClient
from phalanx.store import connect, write_transaction
from phalanx.workspaces import WorkspaceManager, _item_branch

from test_bootstrap import OWNER, RUNNER, ServerFixture
from test_github import pull, response
from timing import WAIT_SECONDS


class FakeGitHub:
    """GitHub transport for one primary PR on owner/repo."""

    def __init__(self, head, base, item_id):
        self.head, self.base, self.branch = head, base, _item_branch(item_id)
        self.pull = None
        self.runs = []
        self.comments = []
        self.threads = []
        self.lose_merge = False
        self.tip = None

    def __call__(self, request, _timeout):
        path = urlsplit(request.full_url).path
        if request.method == "PUT" and path.endswith("/pulls/7/merge"):
            self.pull.update(state="closed", merged=True, merge_commit_sha="e" * 40)
            if self.lose_merge:
                self.lose_merge = False
                raise TimeoutError
            return response({"merged": True, "sha": "e" * 40})
        if path.endswith("/git/commits/" + "e" * 40):
            return response({"sha": "e" * 40, "parents": [{"sha": self.base}, {"sha": self.head}]})
        if request.method == "POST" and path == "/graphql":
            if "reviewThreads" in json.loads(request.data)["query"]:
                return response({"data": {"repository": {"pullRequest": {
                    "latestOpinionatedReviews": {"nodes": []},
                    "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                      "nodes": self.threads}}}}})
            self.pull["draft"] = False
            return response({"data": {"markPullRequestReadyForReview": {
                "pullRequest": {"id": self.pull["node_id"], "isDraft": False}}}})
        if path.endswith("/issues/7/comments"):
            if request.method == "POST":
                self.comments.append(json.loads(request.data)["body"])
                return response({}, status=201)
            return response([{"body": body} for body in self.comments])
        if request.method == "PATCH":
            self.pull["body"] = json.loads(request.data)["body"]
            return response(self.pull)
        if request.method == "POST":
            self.pull = pull(head=self.branch)
            self.pull["head"].update(sha=self.head, repo={"full_name": "Owner/Repo"})
            self.pull.update(html_url="https://github.com/owner/repo/pull/7",
                             body=json.loads(request.data)["body"])
            self.pull["base"]["sha"] = self.base
            return response(self.pull, status=201)
        if path.endswith("/pulls"):
            return response([self.pull] if self.pull else [])
        if "/git/ref/" in path:
            return response({"ref": "refs/heads/main", "object": {"sha": self.tip or self.base, "type": "commit"}})
        if path.endswith("/check-runs"):
            return response({"check_runs": self.runs})
        if path.endswith("/statuses"):
            return response([])
        return response(self.pull)


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
            expected = [self.stage] if self.stage in ("pr_creation", "pre_pr_validation") else ["adversarial_review", "development", "pr_revision", "report_drafting", "report_review",
                                                                                     "self_review"]
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
        self.assertEqual(launch["session"]["project_id"], "project")
        self.assertIn(marker, launch["session"]["prompt"])
        self.assertIn("pinned-report-revision", launch["session"]["prompt"])
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

    def test_correlated_queued_receipt_remains_idempotent_on_native_replays(self):
        self.api.drop_claim = False
        launch = self.runner().prepare()
        queued, native = str(uuid4()), str(uuid4())
        runner = self.runner()
        runner.receipt(launch["marker"], "local", client_thread_id=queued)
        runner.receipt(launch["marker"], "local", thread_id=native)
        for _ in range(2):
            response = runner.receipt(launch["marker"], "local", thread_id=native)
            self.assertEqual(response, {"status": "receipt_recorded", "thread_id": native,
                                        "client_thread_id": queued})
        with self.assertRaisesRegex(RunnerError, "conflicts"):
            runner.receipt(launch["marker"], "local", thread_id=str(uuid4()))
        with self.assertRaisesRegex(RunnerError, "marker and host"):
            runner.receipt("wrong-marker", "local", thread_id=native)
        self.write_result(runner, launch["marker"], "# Verified report")
        self.api.drop_result = False
        runner.submit(launch["marker"], "local", native)
        self.assertEqual(runner.status(), {"status": "idle"})

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
        prompt = review["session"]["prompt"]
        self.assertIn(json.dumps(draft), prompt)
        self.assertIn(first, prompt)
        self.assertIn("Include the test command", prompt)
        findings = "Add the unittest command."
        redo = submit(review, {"findings": findings}, "changes_required")
        self.assertEqual(redo["state"]["stage"], "report_drafting")

        rewrite = self.runner().prepare()
        self.assertIn(findings, rewrite["session"]["prompt"])
        self.assertIn(first, rewrite["session"]["prompt"])
        waiting = submit(rewrite, {
            "question": "Which command should the report use?",
            "options": ["Discovery", "A named test"],
            "handoff": "Review findings still need a command.",
        }, "needs_input")
        self.assertEqual(waiting["state"]["status"], "waiting_for_input")
        status, queued = self.request(route + "/reply", OWNER, {
            "expected_version": waiting["version"], "answer": "Use discovery.",
        }, {"Idempotency-Key": "ph16-reply"})
        self.assertEqual(status, 200, queued)
        rewrite = self.runner().prepare()
        prompt = rewrite["session"]["prompt"]
        for text in (findings, first, "Use discovery."):
            self.assertIn(text, prompt)
        markdown = "# Tests \u2713\r\nRun `python -m unittest`.  \n"
        second = submit(rewrite, markdown)["state"]["report_draft"]
        self.assertNotEqual(first, second)
        review = self.runner().prepare()
        self.assertIn(second, review["session"]["prompt"])
        self.assertNotIn(first, review["session"]["prompt"])
        verified = submit(review, "Verified the command against the test configuration.")
        self.assertEqual(verified["state"]["stage"], "report_storage")
        self.assertEqual(self.runner().prepare()["status"], "no_eligible_work")

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
                self.runner().prepare(report_storage=True)
            self.assertEqual(self.runner().status()["status"], "submitting")
            storage = self.runner().prepare(report_storage=True)
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
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 6)

    def test_completed_review_and_storage_survive_lease_expiry(self):
        database = self.root.parent / "queue.sqlite3"
        self.start_server(database)
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "repo", "remote": str(self.checkout),
        })[0], 200)
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Document the test suite", "target": "report",
        })
        self.assertEqual(status, 201, item)
        execution = item["execution"]["id"]
        route = f"/v1/items/{item['id']}/executions/{execution}"
        self.api = RunnerAPI(self.url, RUNNER)
        call = self.api.call

        def unsent(path, body=None, **kwargs):
            if path.endswith("/result"):
                raise RunnerError("result unsent")
            return call(path, body, **kwargs)

        def expire_and_adopt():
            with write_transaction(database) as db:
                db.execute("UPDATE attempts SET lease_expires_at=0 WHERE ended_at IS NULL")
            # A restarted runner reconciles from its journal alone.
            decision = run_recovery(self.root, "desk", self.api, RecoveryJournal(self.root, 1))
            self.assertEqual(decision["status"], "adopted", decision)

        markdown = "# Tests\nRun `python -m unittest`.\n"
        launch = self.runner().prepare()
        thread_id = str(uuid4())
        self.runner().receipt(launch["marker"], "local", thread_id=thread_id)
        self.write_result(self.runner(), launch["marker"], markdown)
        revision = self.runner().submit(launch["marker"], "local", thread_id)["state"]["report_draft"]

        review = self.runner().prepare()
        thread_id = str(uuid4())
        self.runner().receipt(review["marker"], "local", thread_id=thread_id)
        self.write_result(self.runner(), review["marker"], "Verified the command.")
        with patch.object(self.api, "call", side_effect=unsent), \
                self.assertRaisesRegex(RunnerError, "result unsent"):
            self.runner().submit(review["marker"], "local", thread_id)
        expire_and_adopt()
        reviewed = self.runner().submit(review["marker"], "local", thread_id)
        self.assertEqual(reviewed["state"]["stage"], "report_storage")
        self.assertEqual(reviewed["state"]["report_draft"], revision)

        with patch.object(self.api, "call", side_effect=unsent), \
                self.assertRaisesRegex(RunnerError, "result unsent"):
            self.runner().prepare(report_storage=True)
        expire_and_adopt()
        stored = self.runner().prepare(report_storage=True)
        self.assertEqual(stored["status"], "report_storage_submitted", stored)
        self.assertEqual(self.runner().status(), {"status": "idle"})

        # The stored report is served by the API alone, with no runner involved.
        self.assertEqual(self.get(route + "/report", OWNER), (200, markdown))
        self.assertEqual(self.get(route + "/drafts/" + revision, OWNER), (200, markdown))
        with connect(database, mode="ro") as db:
            stages = [row[0] for row in db.execute("SELECT stage FROM attempts ORDER BY started_at")]
            self.assertEqual(db.execute("SELECT count(*) FROM report_drafts").fetchone()[0], 1)
        self.assertEqual(stages, ["report_drafting", "report_review", "report_storage"])

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
            if not resume.wait(timeout=WAIT_SECONDS):
                raise AssertionError("Workspace preparation did not resume")
            return original(manager, *args, **kwargs)

        with patch.object(WorkspaceManager, "prepare", slow_prepare), ThreadPoolExecutor(2) as pool:
            preparing = pool.submit(self.runner().prepare)
            try:
                self.assertTrue(entered.wait(timeout=WAIT_SECONDS))
                renewal = pool.submit(self.runner().renew).result(timeout=WAIT_SECONDS)
                self.assertEqual(renewal["lease_expires_at"], 901)
            finally:
                resume.set()
            self.assertEqual(preparing.result(timeout=WAIT_SECONDS)["status"], "launch_intent")

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
                claims.wait(timeout=WAIT_SECONDS)
            return original_call(api, path, body, **kwargs)

        with patch.object(StubAPI, "call", call), ThreadPoolExecutor(2) as pool:
            first_launch = pool.submit(first.prepare)
            second_launch = pool.submit(second.prepare)
            launches = [first_launch.result(timeout=WAIT_SECONDS), second_launch.result(timeout=WAIT_SECONDS)]
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
                if not resume.wait(timeout=WAIT_SECONDS):
                    raise AssertionError("Submission did not resume")
            return original_call(api, path, body, **kwargs)

        with patch.object(StubAPI, "call", slow_submit), ThreadPoolExecutor(2) as pool:
            submitted = pool.submit(first.submit, marker, "local", native_id)
            try:
                self.assertTrue(entered.wait(timeout=WAIT_SECONDS))
                heartbeat = pool.submit(second.renew)
                self.assertEqual(heartbeat.result(timeout=WAIT_SECONDS)["attempt_id"], second_api.attempt)
            finally:
                resume.set()
            self.assertEqual(submitted.result(timeout=WAIT_SECONDS), {"version": 4})

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
            ("needs_input", {"question": "Which source?", "options": ["Source A", "Source B"], "handoff": "No edits"}),
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
            "prompt": "Which source?", "options": ["Source A", "Source B"], "proposed_default": None,
            "handoff": "No edits",
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
        runner.receipt(marker, "local", thread_id=thread_id, worker_url="https://example.test/chat/1")
        runner.renew()
        execution_id = self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["id"]
        board = f"/v1/operator/executions/{execution_id}/timeline"
        running = self.get(board, OWNER)[1]
        self.assertEqual((running["item"]["worker_reference"], running["item"]["worker_url"]),
                         (thread_id, "https://example.test/chat/1"))
        self.assertIsNone(running["question"])
        self.write_result(runner, marker, {
            "question": "Which source?", "options": ["Source A", "Source B"], "handoff": "Draft in notes.md",
            "proposed_default": "Source B",
        }, "needs_input")
        waiting = runner.submit(marker, "local", thread_id)
        self.assertEqual(waiting["state"]["status"], "waiting_for_input")
        self.assertEqual(runner.status(), {"status": "idle"})
        status, inbox = self.get("/v1/owner/inbox", OWNER)
        self.assertEqual(status, 200, inbox)
        question = {
            "prompt": "Which source?", "options": ["Source A", "Source B"],
            "handoff": "Draft in notes.md", "proposed_default": "Source B",
        }
        self.assertEqual(inbox[0]["question"], question)
        asked = self.get(board, OWNER)[1]
        self.assertEqual(asked["question"], question)
        self.assertEqual(asked["item"]["version"], waiting["version"])
        self.assertEqual(asked["item"]["worker_reference"], thread_id)
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
            "question": question, "answer": "Source B only", "answered_by": "owner",
            "wait_version": waiting["version"],
        })
        self.assertIn("Source B only", launch["session"]["prompt"])
        self.assertIn("Draft in notes.md", launch["session"]["prompt"])

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

    def test_development_retry_may_keep_the_recorded_candidate(self):
        self.api.drop_claim = self.api.drop_result = False
        self.api.stage = "development"
        self.api.candidate = self.git(self.checkout, "rev-parse", "HEAD")
        runner = self.runner()
        marker = runner.prepare()["marker"]
        packet = runner.status()["packet"]
        thread_id = str(uuid4())
        runner.receipt(marker, "local", thread_id=thread_id)
        (Path(packet["worktree"]) / "scratch.md").write_text("Uncommitted")
        self.write_result(runner, marker, self.api.candidate)
        with self.assertRaisesRegex(RunnerError, "new candidate"):
            runner.submit(marker, "local", thread_id)
        (Path(packet["worktree"]) / "scratch.md").unlink()
        runner.submit(marker, "local", thread_id)
        self.assertEqual(self.api.posts[0][0]["candidate"], self.api.candidate)

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

    def test_unconfirmed_draft_retries_under_the_same_claim(self):
        runner, marker, thread, worktree, candidate, _ = self.start_development()
        runner.submit(marker, "local", thread)
        self.api.attempt = uuid4().hex
        self.api.stage, self.api.candidate = "pr_creation", candidate
        claims = len(self.api.claim_leases)
        results = [
            DraftStageResult(DraftOutcome("pending", "Draft creation is unconfirmed")),
            DraftStageResult(DraftOutcome("base_changed", "The remote base moved"),
                             {"id": "execution", "version": 4}),
        ]
        with patch.dict(os.environ, PHALANX_GITHUB_TOKEN="runner-only"), patch(
            "phalanx.attended.publish_draft", side_effect=results,
        ) as publish:
            self.assertEqual(self.runner().prepare(draft=True)["status"], "draft_pending")
            self.assertEqual(self.runner().status()["status"], "draft_pending")
            routed = self.runner().prepare(draft=True)
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(len(self.api.claim_leases), claims + 1)
        self.assertEqual(publish.call_args_list[0].args[2]["lease"], publish.call_args_list[1].args[2]["lease"])
        self.assertEqual((routed["status"], routed["reason"]), ("draft_submitted", "The remote base moved"))
        self.assertEqual(self.runner().status(), {"status": "idle"})

    def test_validation_stages_run_from_draft_to_ready_check_in_over_http(self):
        self.live_review_path(blocking=False)

    def test_blocking_review_sends_findings_to_pr_revision_over_http(self):
        self.live_review_path(blocking=True)

    def test_merge_conflict_is_revised_on_the_ready_pr_over_http(self):
        self.live_review_path(blocking=False, conflict=True)

    def test_github_feedback_is_revised_on_the_ready_pr_over_http(self):
        self.live_review_path(blocking=False, feedback=True)

    def test_check_in_merges_and_reconciles_a_lost_merge_response_over_http(self):
        self.live_review_path(blocking=False, merge=True)

    def live_review_path(self, *, blocking, conflict=False, feedback=False, merge=False):
        remote = "git@github.com:owner/repo.git"
        self.git(self.checkout, "remote", "set-url", "origin", remote)
        base = self.git(self.checkout, "rev-parse", "HEAD")
        self.start_server(self.root.parent / "queue.sqlite3")
        self.assertEqual(self.request("/v1/repositories", OWNER, {"id": "repo", "remote": remote})[0], 200)
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Fix tests",
            "target": "checked_in" if merge else "ready_for_check_in",
            "target_branch": "main",
        })
        self.assertEqual(status, 201, item)
        self.api = RunnerAPI(self.url, RUNNER)
        policy = self.root.parent / "validation-policy.json"
        policy.write_text(json.dumps({"version": 1, "repositories": {"repo": {
            "local_checks": [{"name": "unit tests", "argv": [sys.executable, "-c", "print('ok')"],
                              "timeout_seconds": WAIT_SECONDS}],
            "draft_checks": [{"name": "draft", "kind": "check_run", "app_id": 15368}],
            "ready_checks": [{"name": "test", "kind": "check_run", "app_id": 15368}],
        }}}), encoding="utf-8")
        self.config["validation_policy"] = str(policy)
        self.config["check_recheck_seconds"] = 7

        def work(details, outcome="succeeded"):
            launch = self.runner().prepare()
            self.assertEqual(launch["status"], "launch_intent", launch)
            thread = str(uuid4())
            self.runner().receipt(launch["marker"], "local", thread_id=thread)
            if callable(details):
                details = details(Path(self.runner().status()["packet"]["worktree"]))
            self.write_result(self.runner(), launch["marker"], details, outcome)
            return self.runner().submit(launch["marker"], "local", thread)

        def commit(worktree):
            self.git(worktree, "commit", "--allow-empty", "-m", "Candidate")
            return self.git(worktree, "rev-parse", "HEAD")

        self.assertEqual(work(commit)["state"]["stage"], "pr_creation")
        candidate = self.runner()._transaction(
            lambda record, db: json.loads(db.execute("SELECT record FROM completed").fetchone()[0])
        )["submission"]["body"]["candidate"]
        github = FakeGitHub(candidate, base, item["id"])

        class Git:
            expected_remote = ("github", "owner/repo")

            def __init__(self, checkout, remote, item_id):
                self.head = None
                self.ref = f"refs/heads/{_item_branch(item_id)}"

            def verify_candidate(self, *args, **kwargs):
                pass

            def read_branch(self, branch, *, authorize):
                authorize()
                return base

            def read_head(self, *, authorize):
                authorize()
                return self.head

            def publish(self, revision, *, expected, authorize, rewrite=False):
                authorize()
                self.head = revision
                return PublicationResult("success", "fixture push")

        def client(owner, name, *, token):
            return GitHubClient(owner, name, token=token, transport=github)

        with patch.dict(os.environ, PHALANX_GITHUB_TOKEN="runner-only"), \
                patch("phalanx.draft_stage.GitPublication", Git), \
                patch("phalanx.attended_drafts.GitHubClient", client), \
                patch("phalanx.pr_notes.GitHubClient", client), \
                patch("phalanx.validation_stages.GitHubClient", client):
            draft = self.runner().prepare(draft=True)
            self.assertEqual(draft["execution"]["state"]["stage"], "self_review", draft)
            reviewed = work("Reviewed the candidate diff against the criteria.")
            self.assertEqual(reviewed["state"]["stage"], "pre_pr_validation")
            result = self.runner().prepare(stages=["pre_pr_validation"])
            self.assertEqual(result["execution"]["state"]["stage"], "pr_promotion", result)

            def recheck():
                with write_transaction(self.root.parent / "queue.sqlite3") as db:
                    db.execute("UPDATE external_rechecks SET due_at_ms=0")
                CaptainLoop._recheck_due(SimpleNamespace(api=self.api))

            draft_run = {"id": 2, "name": "draft", "head_sha": candidate, "status": "queued",
                         "conclusion": None, "app": {"id": 15368}, "pull_requests": []}
            github.runs = [draft_run]
            started = int(time.time() * 1000)
            waiting = self.runner().prepare(stages=["pr_promotion"])
            state = waiting["execution"]["state"]
            self.assertEqual((state["stage"], state["status"]), ("pr_promotion", "waiting_for_external"),
                             waiting)
            with write_transaction(self.root.parent / "queue.sqlite3") as db:
                due_at = db.execute("SELECT due_at_ms FROM external_rechecks").fetchone()[0]
            # The configured interval, not the default 30s, schedules the draft recheck.
            self.assertGreaterEqual(due_at, started + 7_000)
            self.assertLess(due_at, started + 30_000)
            self.assertTrue(github.pull["draft"])
            recheck()
            draft_run.update(status="completed", conclusion="success")
            result = self.runner().prepare(stages=["pr_promotion"])
            self.assertEqual(result["execution"]["state"]["stage"], "pr_validation", result)
            self.assertFalse(github.pull["draft"])
            run = {"id": 1, "name": "test", "head_sha": candidate, "status": "in_progress",
                   "conclusion": None, "app": {"id": 15368}, "pull_requests": []}
            github.runs = [run]
            pending = self.runner().prepare(stages=["pr_validation"])["execution"]
            self.assertEqual(pending["state"]["status"], "waiting_for_external", pending)
            recheck()
            run.update(status="completed", conclusion="success")
            ready = self.runner().prepare(stages=["pr_validation"])
            self.assertEqual(ready["status"], "published_validation_submitted", ready)
            state = ready["execution"]["state"]
            self.assertEqual((state["stage"], state["status"]), ("pr_reconciliation", "queued"), state)
            retry = self.runner().prepare(stages=["pr_reconciliation"])
            self.assertIn("mergeability", retry["reason"], retry)
            with write_transaction(self.root.parent / "queue.sqlite3") as db:
                db.execute("UPDATE executions SET retry_at=NULL, ready_since=0")
            github.pull["mergeable"] = True
            unreviewed = self.runner().prepare(stages=["pr_reconciliation"])
            self.assertEqual(unreviewed["execution"]["state"]["stage"], "adversarial_review", unreviewed)

            launch = self.runner().prepare()
            review = json.loads(self.runner().status()["packet"]["context"])["review"]
            self.assertEqual((review["pr_url"], review["head_sha"]),
                             ("https://github.com/owner/repo/pull/7", candidate))
            reviewer = str(uuid4())
            self.runner().receipt(launch["marker"], "local", thread_id=reviewer)
            if blocking:
                self.write_result(self.runner(), launch["marker"],
                                  {"findings": "P1: The new branch is untested"}, "changes_required")
                routed = self.runner().submit(launch["marker"], "local", reviewer)
                self.assertEqual(routed["state"]["stage"], "pr_revision", routed)
                self.runner().prepare()
                context = json.loads(self.runner().status()["packet"]["context"])
                self.assertIn("P1: The new branch is untested", context["review_findings"])
                return
            self.write_result(self.runner(), launch["marker"], "No blocking findings on this head.")
            routed = self.runner().submit(launch["marker"], "local", reviewer)
            self.assertEqual(routed["state"]["stage"], "pr_reconciliation", routed)
            if conflict:
                # main moved past the PR's REST base.sha, which GitHub does not advance.
                github.tip = "c" * 40
                github.pull["mergeable"] = False
                routed = self.runner().prepare(stages=["pr_reconciliation"])
                self.assertEqual(routed["execution"]["state"]["stage"], "pr_revision", routed)
                self.runner().prepare()
                context = json.loads(self.runner().status()["packet"]["context"])
                self.assertIn(f"conflicts with main at {github.tip}", context["review_findings"])
                return
            if feedback:
                github.threads = [{"id": "PRRT_1", "isResolved": False, "isOutdated": False, "path": "app.py",
                                   "comments": {"nodes": [{"body": "Handle the empty list",
                                                           "author": {"login": "octo"}}]}}]
                routed = self.runner().prepare(stages=["pr_reconciliation"])
                self.assertEqual(routed["execution"]["state"]["stage"], "pr_revision", routed)
                self.runner().prepare()
                context = json.loads(self.runner().status()["packet"]["context"])
                self.assertIn("@octo on app.py: Handle the empty list", context["review_findings"])
                return
            done = self.runner().prepare(stages=["pr_reconciliation"])
            if merge:
                self.assertEqual(done["execution"]["state"]["stage"], "check_in", done)
                github.lose_merge = True
                pending = self.runner().prepare(stages=["check_in"])
                self.assertIn("reconciles the merge", pending["reason"], pending)
                with write_transaction(self.root.parent / "queue.sqlite3") as db:
                    db.execute("UPDATE executions SET retry_at=NULL, ready_since=0")
                done = self.runner().prepare(stages=["check_in"])
        state = done["execution"]["state"]
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["final_output"]["revision"]["head"], candidate)
        self.assertEqual(state["final_output"]["merge_commit"], "e" * 40 if merge else None)
        self.assertEqual(self.runner().status(), {"status": "idle"})

    def test_findings_are_fixed_in_development_then_revised_on_the_ready_pr(self):
        remote = "git@github.com:owner/repo.git"
        self.git(self.checkout, "remote", "set-url", "origin", remote)
        base = self.git(self.checkout, "rev-parse", "HEAD")
        self.start_server(self.root.parent / "queue.sqlite3")
        self.assertEqual(self.request("/v1/repositories", OWNER, {"id": "repo", "remote": remote})[0], 200)
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Fix tests", "target": "ready_for_check_in",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        policy = self.root.parent / "validation-policy.json"
        policy.write_text(json.dumps({"version": 1, "repositories": {"repo": {
            "local_checks": [{"name": "platform", "allow_skip": True, "bypass": {
                "outcome": "skipped", "reason": "Needs the other OS", "authorization": "Policy 3"}}],
            "draft_checks": [],
            "ready_checks": [{"name": "test", "kind": "check_run", "app_id": 15368}],
        }}}), encoding="utf-8")
        self.config["validation_policy"] = str(policy)

        def work(details, outcome="succeeded"):
            launch = self.runner().prepare()
            thread = str(uuid4())
            self.runner().receipt(launch["marker"], "local", thread_id=thread)
            packet = self.runner().status()["packet"]
            if callable(details):
                details = details(Path(packet["worktree"]))
            self.write_result(self.runner(), launch["marker"], details, outcome)
            return packet, self.runner().submit(launch["marker"], "local", thread)

        def commit(worktree):
            self.git(worktree, "commit", "--allow-empty", "-m", "Candidate")
            return self.git(worktree, "rev-parse", "HEAD")

        first = work(commit)[1]["state"]
        self.assertEqual(first["stage"], "pr_creation")
        github = FakeGitHub(first["candidate"], base, item["id"])
        pushes = []

        class Git:
            expected_remote = ("github", "owner/repo")

            def __init__(self, checkout, remote, item_id):
                self.ref = f"refs/heads/{_item_branch(item_id)}"
                self.head = github.pull["head"]["sha"] if github.pull else None

            def verify_candidate(self, *args, **kwargs):
                pass

            def read_branch(self, branch, *, authorize):
                return base

            def read_head(self, *, authorize):
                authorize()
                return self.head

            def publish(self, revision, *, expected, authorize, rewrite=False):
                authorize()
                pushes.append((expected, revision, rewrite))
                self.head = revision
                if github.pull:
                    github.pull["head"]["sha"] = revision
                return PublicationResult("success", "fixture push")

        def client(owner, name, *, token):
            return GitHubClient(owner, name, token=token, transport=github)

        with patch.dict(os.environ, PHALANX_GITHUB_TOKEN="runner-only"), \
                patch("phalanx.draft_stage.GitPublication", Git), \
                patch("phalanx.validation_stages.GitPublication", Git), \
                patch("phalanx.attended_drafts.GitHubClient", client), \
                patch("phalanx.pr_notes.GitHubClient", client), \
                patch("phalanx.validation_stages.GitHubClient", client):
            self.assertEqual(self.runner().prepare(draft=True)["execution"]["state"]["stage"], "self_review")
            state = work({"findings": "Rename the helper."}, "changes_required")[1]["state"]
            self.assertEqual((state["stage"], state["findings"]),
                             ("development", "Rename the helper."))
            packet, revised = work(commit)
            self.assertEqual(json.loads(packet["context"])["review_findings"], "Rename the helper.")
            self.assertIn("review_findings", packet["evidence_requirements"])
            self.assertEqual(len(github.comments), 1)
            self.assertIn(f"`{first['candidate']}`", github.comments[0])
            self.assertIn("Rename the helper.", github.comments[0])
            self.assertIn("### Known gaps", github.pull["body"])
            self.assertIn("Rename the helper.", github.pull["body"])
            revised = revised["state"]
            self.assertEqual((revised["stage"], revised["findings"]), ("self_review", None))
            self.assertNotEqual(revised["candidate"], first["candidate"])
            work("Reviewed the revised diff.")
            self.assertEqual(self.runner().prepare(stages=["pre_pr_validation"])["execution"]["state"]["stage"],
                             "pr_promotion")
            promoted = self.runner().prepare(stages=["pr_promotion"])
            self.assertEqual(promoted["execution"]["state"]["stage"], "pr_validation", promoted)
            self.assertEqual(pushes[-1], (first["candidate"], revised["candidate"], True))
            self.assertEqual((github.pull["head"]["sha"], github.pull["draft"]), (revised["candidate"], False))
            github.runs = [{"id": 1, "name": "test", "head_sha": revised["candidate"], "status": "completed",
                            "conclusion": "failure", "app": {"id": 15368}, "pull_requests": []}]
            validated = self.runner().prepare(stages=["pr_validation"])
            self.assertEqual(validated["execution"]["state"]["stage"], "pr_reconciliation", validated)
            github.pull["mergeable"] = True
            failed = self.runner().prepare(stages=["pr_reconciliation"])
            self.assertEqual(failed["execution"]["state"]["stage"], "pr_revision", failed)
            self.assertIn("Required checks did not pass", failed["execution"]["state"]["findings"])
            packet, fixed = work(commit)
            self.assertIn("Required checks did not pass", json.loads(packet["context"])["review_findings"])
            fixed = fixed["state"]
            self.assertEqual(fixed["stage"], "pr_validation", fixed)
            self.assertEqual(pushes[-1][:2], (revised["candidate"], fixed["candidate"]))
            self.assertEqual((github.pull["head"]["sha"], github.pull["draft"]), (fixed["candidate"], False))
        self.assertEqual(len(github.comments), 3)
        self.assertIn("Local check platform skipped: Needs the other OS", github.comments[1])
        self.assertIn("Required checks did not pass", github.comments[2])
        body = github.pull["body"]
        self.assertIn("Required checks did not pass", body)
        self.assertNotIn("Rename the helper.", body)
        self.assertTrue(body.endswith(ATTRIBUTION))
        self.assertEqual(body.count(ATTRIBUTION), 1)
        _, timeline = self.request(f"/v1/items/{item['id']}/executions/{failed['execution']['id']}/timeline",
                                   OWNER)
        self.assertIn([{"name": "platform", "outcome": "skipped", "reason": "Needs the other OS",
                        "authorization": "Policy 3"}],
                      [entry["details"].get("local_checks") for entry in timeline])

    def _promotion_fixture(self, draft, timeline, gates):
        from phalanx import validation_stages
        from phalanx.promotion import Promotion

        candidate = "b" * 40
        snapshot = {"claim": {
            "attempt_id": "a", "item_id": "item-1",
            "request": {"repository_id": "repo", "target_branch": "main"},
            "execution": {"id": "e", "version": 3, "state": {
                "stage": "pr_promotion", "candidate": candidate,
                "primary_pr": "https://github.com/o/r/pull/7"}}}}
        pull = SimpleNamespace(head_sha=candidate, base_sha="c" * 40, draft=draft, node_id="PR_7", mergeable=True)
        github = SimpleNamespace(get_pull_request=lambda number: pull)
        runner = SimpleNamespace(config={"repositories": {"repo": {"base_branch": "main"}}}, root=self.root,
                                 api=SimpleNamespace(call=lambda path: timeline))
        promoted = []

        def promote(*_, **__):
            promoted.append(True)
            return Promotion("verified", "Verified the ready primary PR", pull, gates)

        def run():
            with patch.object(validation_stages, "_policy", lambda *_: SimpleNamespace(draft_checks=("lint",))), \
                    patch.object(validation_stages, "_github", lambda _: github), \
                    patch.object(validation_stages, "_candidate", lambda *_: None), \
                    patch.object(validation_stages, "_validation", lambda *_: {"draft": "evidence"}), \
                    patch.object(validation_stages, "promote", promote):
                return validation_stages._promotion(runner, snapshot)
        return candidate, promoted, run

    def test_ready_pr_without_draft_evidence_skips_draft_checks_and_advances(self):
        from phalanx.pr_notes import PRNotes
        from phalanx.validation import GateResult, GateState

        candidate, promoted, run = self._promotion_fixture(
            False, [], GateResult(GateState.SATISFIED, skipped=("lint (check_run)",)))
        result = run()
        self.assertEqual((result["route"], result["body"], promoted), ("pr-observation", {"draft": "evidence"}, [True]))
        self.assertEqual([note["note"] for note in PRNotes(self.root).flush(None)],
                         [f"draft-checks-skipped:{candidate}"])

    def test_draft_after_promotion_asks_the_owner_before_marking_ready(self):
        from phalanx import validation_stages
        from phalanx.validation import GateResult, GateState

        timeline = []
        _, promoted, run = self._promotion_fixture(True, timeline, GateResult(GateState.SATISFIED))
        self.assertEqual(run()["route"], "pr-observation")
        timeline.append({"event": "result", "details": {
            "pr_url": "https://github.com/o/r/pull/7", "pull": {"draft": False}}})
        asked = run()["body"]
        self.assertEqual((asked["outcome"], asked["question"]["prompt"]),
                         ("needs_input", validation_stages._REPROMOTE_PROMPT))
        question = {"prompt": validation_stages._REPROMOTE_PROMPT}
        timeline.append({"event": "reply", "details": {"answered_by": "captain", "question": question}})
        self.assertEqual(run()["body"]["outcome"], "needs_input")
        timeline.append({"event": "reply", "details": {"answered_by": "owner", "question": question}})
        self.assertEqual(run()["route"], "pr-observation")
        self.assertEqual(len(promoted), 2)
    def test_reviewed_update_retries_transient_errors_and_reworks_unexpected_heads(self):
        from phalanx import validation_stages
        from phalanx.git_publication import PublicationError

        remote = []
        pushes = []

        class Git:
            def __init__(self, *_):
                pass

            def read_head(self, **_):
                if not remote:
                    raise PublicationError("git ls-remote failed: 502")
                return remote[-1]

            def publish(self, candidate, *, expected, rewrite, **_):
                pushes.append(rewrite)
                return PublicationResult("conflict", "The update would not fast-forward")

        item_id = "item-1"
        snapshot = {"repositories": {"repo": object()}, "claim": {
            "attempt_id": "a", "item_id": item_id, "request": {"repository_id": "repo"},
            "execution": {"version": 3, "state": {"stage": "pr_promotion", "candidate": "b" * 40,
                                                  "primary_pr": "#7"}}}}
        local = SimpleNamespace(_inspect=lambda: SimpleNamespace(path="."), _authorize=lambda: None)
        pr = pull(head=_item_branch(item_id))
        pr["head"]["sha"] = "a" * 40
        pr = GitHubClient("owner", "repo", token="t", transport=lambda *_: response(pr)).get_pull_request(7)
        with patch("phalanx.validation_stages.GitPublication", Git):
            body = validation_stages._publish_update(snapshot, local, pr, "main", "a" * 40)["body"]
            self.assertEqual(body["outcome"], "failed")
            self.assertIn("502", body["failure_reason"])
            self.assertGreater(body["retry_after_seconds"], 0)
            remote.append("a" * 40)
            submission = validation_stages._publish_update(snapshot, local, pr, "main", "c" * 40)
        self.assertEqual(pushes, [False])
        self.assertEqual((submission["route"], submission["body"]["issue"]), ("pr-rework", "unexpected_head"))

    def test_validation_policy_gap_waits_for_owner_with_reason(self):
        self.api.stage, self.api.candidate = "pre_pr_validation", "a" * 40
        self.api.drop_claim = self.api.drop_result = False
        with patch("phalanx.validation_stages._candidate"):
            result = self.runner().prepare(stages=["pre_pr_validation"])
        body = self.api.posts[-1][0]
        self.assertEqual((result["status"], body["outcome"], body["candidate"]),
                         ("pre_pr_validation_submitted", "needs_input", "a" * 40))
        self.assertIn("validation_policy", body["question"]["prompt"])

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


class DraftTitleTest(unittest.TestCase):
    def test_title_is_the_goals_first_sentence_cut_at_a_word(self):
        self.assertEqual(draft_title("\n# Fix the login form. It drops the session cookie.\nMore."),
                         "Fix the login form")
        long = draft_title("Implement " + "the retry budget for every flaky integration step " * 3)
        self.assertEqual(long, "Implement the retry budget for every flaky integration step the retry…")


if __name__ == "__main__":
    unittest.main()
