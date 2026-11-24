from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from phalanx import captains, dispatch, flags, intake, store
from phalanx.direct_schema import CAPTAIN_SHIFT_SCHEMA, CLOSED_WRITER_MIGRATION
from phalanx.dispatch_api import Claim, DispatchSettings
from phalanx.intake_models import Intake
from phalanx.store import connect, migrate, write_transaction
from phalanx.transitions import item_branch

from test_bootstrap import OWNER, RUNNER, ServerFixture


STAGES = ["report_drafting", "report_review", "report_storage"]
ROOT = Path(__file__).resolve().parents[1]
HEAD, OTHER, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40, "d" * 40
PR_URL = "https://github.com/e/r/pull/7"


class WriterTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)
        self.start_server(self.path)
        self.assertEqual(self.request("/v1/repositories", OWNER, {"id": "repo", "remote": "https://github.com/e/r.git"})[0], 200)
        self.captain = self.ok("/v1/captains", RUNNER, {"session_ref": "chat-1", "charter_revision": "c1"}, 201)

    def ok(self, path, token, body=None, code=200, **options):
        status, response = self.request(path, token, body, **options)
        self.assertEqual(status, code, response)
        return response

    def item(self):
        item = self.ok("/v1/items", OWNER, {"repository_id": "repo", "goal": "Write it", "target": "report"}, 201)
        return item["id"], item["execution"]["id"]

    def version(self, item_id):
        return self.ok(f"/v1/items/{item_id}", OWNER)["execution"]["version"]

    def register(self, item_id, execution_id, *, token=None, nonce=None, captain=None, code=200, **fields):
        token = token or token_urlsafe(32)
        body = {"expected_version": self.version(item_id), "captain_id": captain or self.captain["id"],
                "runtime": "any-runtime", "nonce": nonce or token_urlsafe(16),
                "token_digest": intake.digest(token), **fields}
        response = self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER, body, code)
        return response, token, body

    def old_loop_claim(self):
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", 10)
            return dispatch.claim(db, Claim(runner_id="local", repository_ids=["repo"], stages=STAGES),
                                  intake.digest(token_urlsafe(32)))

    def submit(self, token, binding, **fields):
        state = binding["state"]
        body = {"attempt_id": binding["writer"]["attempt_id"], "stage": state["stage"],
                "expected_version": binding["version"], "outcome": "succeeded", **fields}
        return self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/result", token, body)

    def test_writer_claims_a_named_item_and_carries_it_through_every_stage(self):
        head, _ = self.item()
        item_id, execution_id = self.item()
        binding, token, body = self.register(item_id, execution_id)
        self.assertEqual((binding["state"]["status"], binding["writer"]["state"]), ("running", "launching"))
        self.assertIsNotNone(binding["writer"]["attempt_id"])
        replay = self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER, body)
        self.assertEqual(replay["writer"]["id"], binding["writer"]["id"])
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER,
                dict(body, token_digest=intake.digest("other")), 409)
        self.register(item_id, execution_id, code=409)
        self.assertEqual(self.old_loop_claim()["item_id"], head)
        self.assertEqual(self.old_loop_claim()["status"], "no_eligible_work")

        other, _ = self.item()
        self.ok(f"/v1/items/{other}", token, code=403)
        self.ok("/v1/flags", token, code=403)
        self.ok("/v1/writer", RUNNER, code=403)
        self.assertEqual(self.ok("/v1/writer", token)["writer"]["id"], binding["writer"]["id"])
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}/result", RUNNER, {
            "attempt_id": binding["writer"]["attempt_id"], "stage": "report_drafting",
            "expected_version": binding["version"], "outcome": "succeeded", "report_markdown": "# R\n",
        }, 422)
        self.ok("/v1/writer/progress", token, {"note": "Drafting"})

        binding = self.submit(token, binding, report_markdown="# Report\n")
        self.assertEqual((binding["state"]["stage"], binding["state"]["status"]), ("report_review", "running"))
        for _ in ("report_review", "report_storage"):
            binding = self.submit(token, binding, report_revision=intake.digest("# Report\n"))
        self.assertEqual(binding["state"]["status"], "completed")
        self.ok("/v1/writer", token, code=401)

    def test_report_finalization_ends_the_writer(self):
        item_id, execution_id = self.item()
        binding, token, _ = self.register(item_id, execution_id)
        binding = self.submit(token, binding, report_markdown="# Report\n")
        binding = self.submit(token, binding, report_revision=intake.digest("# Report\n"))
        self.assertEqual(binding["state"]["stage"], "report_storage")
        finished = self.submit(token, binding, report_revision=intake.digest("# Report\n"))
        self.assertEqual(finished["state"]["status"], "completed")
        with connect(self.path, mode="ro") as db:
            writer = db.execute("SELECT * FROM writers WHERE id=?", (binding["writer"]["id"],)).fetchone()
        self.assertIsNotNone(writer["ended_at"])
        self.assertEqual((writer["end_reason"], writer["attempt_id"]), ("closed as completed", None))

    def test_queued_binding_stays_out_of_the_old_loop_and_survives_steering(self):
        item_id, execution_id = self.item()
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=? WHERE id=?", (2**42, execution_id))
        binding, token, _ = self.register(item_id, execution_id)
        self.assertEqual((binding["state"]["status"], binding["writer"]["attempt_id"]), ("queued", None))
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}", OWNER,
                {"expected_version": self.version(item_id), "priority": "urgent"}, method="PATCH")
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=0 WHERE id=?", (execution_id,))
        self.assertEqual(self.old_loop_claim()["status"], "no_eligible_work")
        attempt = self.ok("/v1/writer/attempt", token, method="POST")
        self.assertEqual(attempt["state"]["status"], "running")
        self.assertIsNotNone(attempt["writer"]["attempt_id"])

    def test_pause_replacement_ownership_and_withdrawal(self):
        item_id, execution_id = self.item()
        with write_transaction(self.path) as db:
            dispatch.set_dispatch(db, DispatchSettings(paused=True))
        self.register(item_id, execution_id, code=409)
        with write_transaction(self.path) as db:
            dispatch.set_dispatch(db, DispatchSettings(paused=False))
        first, old_token, _ = self.register(item_id, execution_id)
        writer = f"/v1/items/{item_id}/executions/{execution_id}/writer"
        rival = self.ok("/v1/captains", RUNNER, {"session_ref": "chat-2", "charter_revision": "c1"}, 201)
        self.ok(writer, RUNNER, {"captain_id": rival["id"], "writer_id": first["writer"]["id"],
                                 "session_id": "s-1"}, 409, method="PATCH")
        confirmed = self.ok(writer, RUNNER, {"captain_id": self.captain["id"], "writer_id": first["writer"]["id"],
                                             "session_id": "s-1", "session_url": "ghapp://sessions/s-1"},
                            method="PATCH")
        self.assertEqual((confirmed["writer"]["state"], confirmed["writer"]["session_id"]), ("busy", "s-1"))

        self.register(item_id, execution_id, replaces=first["writer"]["id"], code=409)
        second, token, _ = self.register(item_id, execution_id, captain=rival["id"],
                                          replaces=first["writer"]["id"], replaced_stopped=True)
        self.assertNotEqual(second["writer"]["attempt_id"], first["writer"]["attempt_id"])
        self.ok("/v1/writer", old_token, code=401)
        self.assertEqual(self.ok("/v1/writer", token)["writer"]["replaced_id"], first["writer"]["id"])

        self.ok(writer, RUNNER, {"captain_id": rival["id"], "writer_id": second["writer"]["id"],
                                 "session_id": "s-2"}, method="PATCH")
        withdrawal = {"captain_id": rival["id"], "writer_id": second["writer"]["id"], "reason": "Reassigned"}
        self.ok(writer + "/withdraw", RUNNER, withdrawal, 409)
        ended = self.ok(writer + "/withdraw", RUNNER, dict(withdrawal, stopped=True))
        self.assertEqual((ended["state"]["status"], ended["writer"]["state"]), ("queued", "ended"))
        self.assertEqual(self.old_loop_claim()["status"], "no_eligible_work")

    def test_withdrawing_a_stopped_writer_confirms_a_pending_owner_stop(self):
        item_id, execution_id = self.item()
        first, _, _ = self.register(item_id, execution_id)
        writer = f"/v1/items/{item_id}/executions/{execution_id}/writer"
        self.ok(writer, RUNNER, {"captain_id": self.captain["id"], "writer_id": first["writer"]["id"],
                                 "session_id": "s-1"}, method="PATCH")
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}/cancel", OWNER,
                {"expected_version": self.version(item_id), "reason": "Not needed"})
        self.register(item_id, execution_id, replaces=first["writer"]["id"], replaced_stopped=True, code=409)
        ended = self.ok(writer + "/withdraw", RUNNER, {"captain_id": self.captain["id"], "reason": "Worker crashed",
                                                     "writer_id": first["writer"]["id"], "stopped": True})
        self.assertEqual((ended["state"]["stopping"], ended["state"]["pending_output"], ended["writer"]["state"]),
                         (None, "cancelled", "ended"))

    def test_work_cli_pins_the_attempt_and_asks_a_question(self):
        item_id, execution_id = self.item()
        binding, token, _ = self.register(item_id, execution_id)
        worktree = self.path.parent / "worktree"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        git_path = subprocess.run(["git", "rev-parse", "--git-path", "phalanx-writer.json"], cwd=worktree,
                                  check=True, capture_output=True, text=True).stdout.strip()
        (worktree / git_path).write_text(json.dumps({"token": token, "base_url": self.url}), encoding="utf-8")
        (worktree / "result.json").write_text(json.dumps({"outcome": "succeeded", "report_markdown": "# R\n"}),
                                              encoding="utf-8")

        def work(*arguments, code=0):
            result = subprocess.run([sys.executable, "-m", "phalanx", "work", *arguments], cwd=worktree,
                                    env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, code, result.stderr)
            return json.loads(result.stdout) if code == 0 else result.stderr

        self.assertEqual(work("progress", "--note", "Outlining")["writer"]["progress_note"], "Outlining")
        drafted = work("result", "--attempt", binding["writer"]["attempt_id"], "--file", "result.json")
        self.assertEqual(drafted["state"]["stage"], "report_review")
        resent = work("result", "--attempt", binding["writer"]["attempt_id"], "--file", "result.json", code=1)
        self.assertIn("is not open", resent)
        self.assertEqual(work("status")["version"], drafted["version"])
        asked = work("question", "Which audience?", "--attempt", drafted["writer"]["attempt_id"],
                     "--handoff", "Outline drafted", "--option", "Ops")
        self.assertEqual(asked["state"]["status"], "waiting_for_input")
        self.assertEqual(asked["state"]["wait"]["reason"], "Which audience?")

    def pr_item(self, target, **fields):
        item = self.ok("/v1/items", OWNER, {"repository_id": "repo", "goal": "Fix it", "target": target,
                                             **fields}, 201)
        return item["id"], item["execution"]["id"]

    def publish(self, target, **fields):
        """Carry a writer-bound PR item through development and self-review to publication."""
        item_id, execution_id = self.pr_item(target, **fields)
        binding, token, _ = self.register(item_id, execution_id)
        binding = self.submit(token, binding, candidate=HEAD)
        self.assertEqual((binding["state"]["stage"], binding["state"]["status"]), ("self_review", "running"))
        binding = self.submit(token, binding, reviewed_candidate=HEAD)
        self.assertEqual((binding["state"]["stage"], binding["state"]["status"]), ("publication", "running"))
        return binding, token

    def observe(self, binding, code=200, **pull):
        pull = {"number": 7, "state": "open", "merged": False, "draft": False,
                "head_ref": item_branch(binding["item_id"]), "head_sha": HEAD, "base_ref": "main",
                "base_sha": BASE, **pull}
        body = {"observed_at": datetime.now(UTC).isoformat(), "pr": pull, "target_base": "main",
                **({"checks": "satisfied"} if pull["state"] == "open" else {})}
        self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/github-observation", RUNNER,
                body, code)
        return self.ok(f"/v1/items/{binding['item_id']}", OWNER)["execution"]

    def test_writer_publishes_and_any_merge_delivers(self):
        binding, token = self.publish("checked_in")
        result = f"/v1/items/{binding['item_id']}/executions/{binding['id']}/result"
        body = {"attempt_id": binding["writer"]["attempt_id"], "stage": "publication",
                "expected_version": binding["version"], "outcome": "succeeded"}
        self.ok(result, token, body, 409)
        self.ok(result, token, dict(body, pr_url="https://github.com/other/r/pull/7"), 409)
        self.assertEqual(self.observe(binding)["version"], binding["version"])
        binding = self.submit(token, binding, pr_url=PR_URL)
        self.assertEqual((binding["state"]["stage"], binding["state"]["status"], binding["writer"]["attempt_id"]),
                         ("delivery", "waiting_for_external", None))
        self.assertEqual(self.observe(binding, draft=True)["state"]["primary_pr"], PR_URL)
        ready = self.observe(binding)
        self.assertEqual((ready["state"]["status"], ready["state"]["ready_revision"]),
                         ("waiting_for_external", {"head": HEAD, "base": BASE}))
        self.assertEqual(self.observe(binding)["version"], ready["version"])
        self.observe(binding, number=8, code=409)
        # A squash merge has a fresh commit and a pushed fix moved the head: GitHub wins.
        self.observe(binding, state="open", merge_method="merge", code=422)
        merged = self.observe(binding, state="closed", merged=True, head_sha=OTHER, merge_commit=MERGE,
                              merge_method="squash_or_rebase", merged_at="2026-11-24T06:33:57Z")
        self.assertEqual((merged["state"]["status"], merged["state"]["candidate"]), ("completed", OTHER))
        output = merged["state"]["final_output"]
        self.assertEqual((output["merge_commit"], output["merge_method"], output["revision"]["head"]),
                         (MERGE, "squash_or_rebase", OTHER))
        self.assertTrue(output["merged_at"].startswith("2026-11-24T06:33:57"))
        self.ok("/v1/writer", token, code=401)

    def test_a_rerun_is_delivered_only_by_the_pr_its_writer_published(self):
        binding, token = self.publish("checked_in")
        self.submit(token, binding, pr_url=PR_URL)
        first = self.observe(binding, state="closed", merged=True, merge_commit=MERGE)
        self.assertEqual(first["state"]["status"], "completed")
        rerun = self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/rerun", OWNER,
                        {"expected_version": first["version"], "target": "checked_in"})
        second, token, _ = self.register(binding["item_id"], rerun["id"])
        second = self.submit(token, second, candidate=OTHER)
        second = self.submit(token, second, reviewed_candidate=OTHER)
        # The reconciler still finds the earlier execution's merged PR on the shared item branch.
        before = self.observe(second, state="closed", merged=True, merge_commit=MERGE)
        self.assertEqual((before["state"]["stage"], before["state"]["status"], before["state"]["primary_pr"]),
                         ("publication", "running", None))
        result = f"/v1/items/{second['item_id']}/executions/{second['id']}/result"
        self.ok(result, token, {"attempt_id": second["writer"]["attempt_id"], "stage": "publication",
                                "expected_version": before["version"], "outcome": "succeeded",
                                "pr_url": PR_URL}, 409)
        second = self.submit(token, second, pr_url="https://github.com/e/r/pull/8")
        self.observe(second, state="closed", merged=True, merge_commit=MERGE, code=409)
        done = self.observe(second, number=8, state="closed", merged=True, head_sha=OTHER, merge_commit=OTHER)
        self.assertEqual((done["state"]["status"], done["state"]["final_output"]["url"]),
                         ("completed", "https://github.com/e/r/pull/8"))

    def test_a_pr_into_another_base_never_delivers(self):
        binding, token = self.publish("ready_for_check_in")
        binding = self.submit(token, binding, pr_url=PR_URL)
        wrong = self.observe(binding, base_ref="dev")
        self.assertEqual((wrong["state"]["status"], wrong["state"]["ready_revision"]), ("waiting_for_external", None))
        self.assertIn("wrong_base", {flag["kind"] for flag in self.ok("/v1/flags", OWNER)["flags"]})
        merged = self.observe(binding, base_ref="dev", state="closed", merged=True, merge_commit=MERGE)
        self.assertEqual((merged["state"]["status"], merged["state"]["final_output"]),
                         ("waiting_for_input", None))
        self.assertIn("merged into a branch other than", merged["state"]["wait"]["reason"])

    def test_ready_head_unblocks_dependents_and_closes_a_ready_target(self):
        binding, token = self.publish("checked_in")
        dependent, dependent_execution = self.pr_item(
            "report", dependencies=[{"upstream_id": binding["item_id"], "milestone": "ready_for_check_in"}])
        waiting, dependent_token, _ = self.register(dependent, dependent_execution)
        self.assertIsNone(waiting["writer"]["attempt_id"])
        self.submit(token, binding, pr_url=PR_URL)
        self.assertEqual(self.observe(binding, draft=True)["state"]["status"], "waiting_for_external")
        self.assertIsNone(self.ok("/v1/writer/attempt", dependent_token, method="POST")["writer"]["attempt_id"])
        self.assertEqual(self.observe(binding)["state"]["status"], "waiting_for_external")
        self.assertEqual(self.ok("/v1/writer/attempt", dependent_token, method="POST")["state"]["status"],
                         "running")

        ready, ready_token = self.publish("ready_for_check_in")
        self.submit(ready_token, ready, pr_url=PR_URL)
        self.assertEqual(self.observe(ready)["state"]["status"], "completed")
        self.ok("/v1/writer", ready_token, code=401)

    def test_closed_pr_asks_the_owner_and_old_pr_stages_refuse_writers(self):
        binding, token = self.publish("checked_in")
        binding = self.submit(token, binding, pr_url=PR_URL)
        closed = self.observe(binding, state="closed")
        self.assertEqual((closed["state"]["status"], closed["state"]["wait"]["trigger"]),
                         ("waiting_for_input", "owner reply"))

        item_id, execution_id = self.pr_item("checked_in")
        for stage, primary in (("pr_creation", None), ("development", PR_URL)):
            with write_transaction(self.path) as db:
                state = json.loads(db.execute("SELECT state_json FROM executions WHERE id=?",
                                              (execution_id,)).fetchone()[0])
                db.execute("UPDATE executions SET state_json=? WHERE id=?",
                           (json.dumps(dict(state, stage=stage, primary_pr=primary)), execution_id))
            self.register(item_id, execution_id, code=409)

    def test_captain_sessions_actions_handoff_and_reconciler_retirement(self):
        item_id, execution_id = self.item()
        binding, _, body = self.register(item_id, execution_id)
        captain = f"/v1/captains/{self.captain['id']}"
        launch = ("s-1", f"phx {item_id[:8]} {body['nonce']}")
        stray = ("s-stray", "phx deadbeef stray")

        def beat(*sessions, state="active", **fields):
            return self.ok(captain, RUNNER, {"charter_revision": "c1", "wake_count": 1, "state": state,
                                             "sessions": [{"id": s, "name": n} for s, n in sessions], **fields},
                           method="PUT")

        def session_flags():
            return {(entry["kind"], entry["reason"].split()[1]): entry for entry in self.ok("/v1/flags", OWNER)["flags"]
                    if entry["kind"] in ("orphan_session", "duplicate_writer")}

        beat(launch, stray, ("chat-1", "Captain chat"))
        found = session_flags()
        self.assertEqual(set(found), {("orphan_session", "s-stray")})
        first_seen = found["orphan_session", "s-stray"]["first_seen"]
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER, {
            "captain_id": self.captain["id"], "writer_id": binding["writer"]["id"], "session_id": "s-1"},
            method="PATCH")
        beat(launch, stray, ("s-old", f"phx {item_id[:8]} old-nonce"))
        found = session_flags()
        self.assertEqual(set(found), {("orphan_session", "s-stray"), ("duplicate_writer", "s-old")})
        self.assertEqual(found["orphan_session", "s-stray"]["first_seen"], first_seen)
        self.assertEqual(found["duplicate_writer", "s-old"]["item_id"], item_id)
        beat()
        self.assertEqual(session_flags(), {})

        self.ok(f"/v1/items/{item_id}/captain-actions", RUNNER, {
            "captain_id": self.captain["id"], "action": "launched", "reason": "Queued with no writer"}, 201)
        events = self.ok(f"/v1/operator/executions/{execution_id}/timeline", OWNER)["events"]
        self.assertTrue(any(event["event"] == "captain_action" and event["reason"].endswith(
            "launched: Queued with no writer") for event in events))
        (action,) = [entry for entry in self.ok(f"/v1/items/{item_id}/executions/{execution_id}/timeline", OWNER)
                     if entry["event"] == "captain_action"]
        self.assertEqual(action["details"]["action"], "launched")

        pending, _ = self.item()
        intent, _, _ = self.register(pending, self.ok(f"/v1/items/{pending}", OWNER)["execution"]["id"])
        ending = beat(state="ending", handoff_note="Watch the pending launch")
        self.assertIsNotNone(ending["ending_at"])
        self.assertEqual([entry["writer_id"] for entry in ending["open_intents"]], [intent["writer"]["id"]])
        self.assertEqual(beat(state="ending")["ending_at"], ending["ending_at"])
        beat(("s-next", f"phx captain {self.captain['id'][:8]}"), state="ending")
        self.assertEqual(session_flags(), {})
        self.ok(captain, RUNNER, {"charter_revision": "c1", "wake_count": 1, "state": "active"}, 409, method="PUT")
        successor = self.ok("/v1/captains", RUNNER, {"session_ref": "chat-2", "charter_revision": "c1",
                                                     "parent_id": self.captain["id"]}, 201)
        self.assertEqual(self.ok(captain, OWNER)["successors"], [successor["id"]])
        self.ok(f"/v1/items/{item_id}/captain-actions", RUNNER, {
            "captain_id": self.captain["id"], "action": "noted", "reason": "x"}, 201)
        beat(state="ended")
        self.ok(f"/v1/items/{item_id}/captain-actions", RUNNER, {
            "captain_id": self.captain["id"], "action": "noted", "reason": "x"}, 409)

        self.ok("/v1/reconcilers/r-1/heartbeat", RUNNER, method="POST")
        later = datetime.now(UTC) + timedelta(days=1)
        with connect(self.path) as db:
            self.assertIn("reconciler_stale", {entry["kind"] for entry in flags.all_flags(db, later)["flags"]})
        self.assertEqual(self.ok("/v1/reconcilers/r-1", RUNNER, method="DELETE"), {"runner_id": "r-1", "retired": True})
        self.assertEqual(self.ok("/v1/reconcilers/r-1", RUNNER, method="DELETE")["retired"], False)
        with connect(self.path) as db:
            self.assertNotIn("reconciler_stale", {entry["kind"] for entry in flags.all_flags(db, later)["flags"]})


class CaptainFlagTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)

    def kinds(self, now):
        with connect(self.path) as db:
            return {(flag["kind"], flag["source"]) for flag in flags.all_flags(db, now)["flags"]}

    def test_captain_liveness_drives_server_flags(self):
        now = datetime.now(UTC)
        self.assertEqual(self.kinds(now), set())
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/e/r.git")
            intake.create(db, Intake(repository_id="repo", goal="Work", target="report"))
        self.assertEqual(self.kinds(now), {("no_live_captain", "server")})
        with write_transaction(self.path) as db:
            captain = captains.register(db, captains.CaptainRegistration(session_ref="chat", charter_revision="c1"))
        now = datetime.now(UTC)  # after the item and heartbeat exist, so slow hosts can't make them look newer
        self.assertEqual(self.kinds(now), set())
        later = now + captains.STALE + timedelta(seconds=1)
        self.assertEqual(self.kinds(later), {("no_live_captain", "server"), ("captain_stale", "server")})
        idle = now + flags.IDLE_WITH_WORK + timedelta(seconds=1)
        with write_transaction(self.path) as db:
            db.execute("UPDATE captains SET heartbeat_at=?", (idle.isoformat(timespec="microseconds"),))
        self.assertEqual(self.kinds(idle), {("captain_idle_with_work", "server")})
        with write_transaction(self.path) as db:
            captains.heartbeat(db, captain["id"], captains.Heartbeat(
                charter_revision="c2", wake_count=3, state="ended", handoff_note="Item X needs review"))
            with self.assertRaises(intake.Conflict):
                captains.heartbeat(db, captain["id"], captains.Heartbeat(
                    charter_revision="c2", wake_count=4, state="active"))
        with connect(self.path) as db:
            (missing,) = flags.all_flags(db, now)["flags"]
        self.assertTrue(missing["notify_owner"])
        self.assertIn("Item X needs review", missing["reason"])

    def test_a_captain_ending_past_its_limit_is_stale(self):
        now = datetime.now(UTC)
        with write_transaction(self.path) as db:
            captain = captains.register(db, captains.CaptainRegistration(session_ref="chat", charter_revision="c1"))
            ending = captains.heartbeat(db, captain["id"], captains.Heartbeat(
                charter_revision="c1", wake_count=1, state="ending"))
        self.assertTrue(ending["live"])
        self.assertEqual(self.kinds(now), set())
        with write_transaction(self.path) as db:
            db.execute("UPDATE captains SET ending_at=?",
                       ((now - captains.ENDING_LIMIT - timedelta(seconds=1)).isoformat(timespec="microseconds"),))
        with connect(self.path) as db:
            (stale,) = flags.all_flags(db, now)["flags"]
        self.assertEqual(stale["kind"], "captain_stale")
        self.assertIn("without finishing its handoff", stale["reason"])

    def previous_store(self, migration):
        """A store upgraded up to, but not including, the given migration."""
        path = self.path.with_name("previous.sqlite3")
        with write_transaction(path, create=True) as db:
            for version, statements in enumerate(store.MIGRATIONS[:store.MIGRATIONS.index(migration)], start=1):
                for statement in statements:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {version}")
        return path

    def test_upgrade_resets_sessions_stored_as_bare_ids(self):
        path = self.previous_store(CAPTAIN_SHIFT_SCHEMA)
        with write_transaction(path) as db:
            now = intake.timestamp()
            db.execute("""INSERT INTO captains (id, session_ref, charter_revision, state, sessions_json, created_at,
                              heartbeat_at) VALUES ('c', 'chat', 'c1', 'active', '["s-1"]', ?, ?)""", (now, now))
        migrate(path)
        with write_transaction(path) as db:
            captain = captains.heartbeat(db, "c", captains.Heartbeat(
                charter_revision="c1", wake_count=1, state="active", sessions=[captains.Session(id="s-1", name="phx x")]))
            self.assertEqual([session["id"] for session in captain["sessions"]], ["s-1"])
            flags.all_flags(db)

    def test_upgrade_ends_writers_of_closed_executions(self):
        path = self.previous_store(CLOSED_WRITER_MIGRATION)
        with write_transaction(path) as db:
            intake.register(db, "repo", "https://github.com/e/r.git")
            captain = captains.register(db, captains.CaptainRegistration(session_ref="chat", charter_revision="c1"))
            for name in ("closed", "open"):
                item = intake.create(db, Intake(repository_id="repo", goal=name, target="report"))
                db.execute("""INSERT INTO writers (id, execution_id, item_id, captain_id, runtime, nonce,
                                  token_digest, state, created_at) VALUES (?, ?, ?, ?, 'r', ?, ?, 'busy', ?)""",
                           (name, item["execution"]["id"], item["id"], captain["id"], name, intake.digest(name),
                            intake.timestamp()))
            db.execute("""UPDATE executions SET state_json = json_set(state_json, '$.status', 'cancelled',
                              '$.final_output', json('{}'))
                          WHERE id = (SELECT execution_id FROM writers WHERE id = 'closed')""")
        migrate(path)
        with connect(path, mode="ro") as db:
            writers = {row["id"]: row for row in db.execute("SELECT * FROM writers")}
        self.assertIsNotNone(writers["closed"]["ended_at"])
        self.assertEqual(writers["closed"]["end_reason"], "closed as cancelled")
        self.assertEqual((writers["open"]["ended_at"], writers["open"]["end_reason"]), (None, None))

    def test_stored_flags_are_idempotent_and_cleared_by_their_raiser(self):
        self.start_server(self.path)
        flag = {"kind": "head_drift", "item_id": "i", "execution_id": "e", "reason": "Moved",
                "recovery": "Re-review"}
        status, first = self.request("/v1/flags", RUNNER, flag, method="PUT")
        self.assertEqual(status, 200, first)
        status, again = self.request("/v1/flags", OWNER, dict(flag, reason="Moved again"), method="PUT")
        self.assertEqual((again["first_seen"], again["reason"], again["source"]),
                         (first["first_seen"], "Moved again", "stored"))
        self.assertEqual(self.request("/v1/flags", OWNER)[1]["flags"], [again])
        key = {"kind": "head_drift", "item_id": "i", "execution_id": "e"}
        self.assertEqual(self.request("/v1/flags/clear", RUNNER, key)[1], {"cleared": True})
        self.assertEqual(self.request("/v1/flags/clear", RUNNER, key)[1], {"cleared": False})
        self.assertEqual(self.request("/v1/flags", RUNNER, dict(flag, kind="Bad"), method="PUT")[0], 422)


if __name__ == "__main__":
    unittest.main()
