from argparse import Namespace
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from secrets import token_urlsafe
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx import captains, dispatch, finalization, flags, intake, shift, store, work_cli, writers
from phalanx.direct_schema import (
    CAPTAIN_COMPACTION_SCHEMA, CAPTAIN_SHIFT_SCHEMA, CLOSED_WRITER_MIGRATION, SESSION_ARCHIVE_SCHEMA,
)
from phalanx.dispatch_api import DispatchSettings
from phalanx.intake_models import Intake
from phalanx.pr_models import DEPLOYMENT_WINDOW
from phalanx.store import connect, migrate, write_transaction
from phalanx.transitions import item_branch

from cli_runner import run_cli
from test_bootstrap import OWNER, RUNNER, ServerFixture


STAGES = ["report_drafting", "report_review", "report_storage"]
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
        self.captain = self.ok("/v1/captains", RUNNER, {
            "session_ref": "chat-1", "charter_revision": "c1",
            "pickup": {"repositories": {"repo": None}}}, 201)

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

    def submit(self, token, binding, **fields):
        state = binding["state"]
        body = {"attempt_id": binding["writer"]["attempt_id"], "stage": state["stage"],
                "expected_version": binding["version"], "outcome": "succeeded", **fields}
        return self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/result", token, body)

    def test_overdue_age_survives_progress_stages_replacement_but_not_completion_or_rerun(self):
        item_id, execution_id = self.item()
        binding, token, _ = self.register(item_id, execution_id)
        started = datetime.now(UTC) - timedelta(minutes=91)
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET started_at=? WHERE execution_id=?",
                       (started.isoformat(timespec="microseconds"), execution_id))
        route = f"/v1/items/{item_id}/executions/{execution_id}/writer"
        self.ok(route, RUNNER, {"captain_id": self.captain["id"], "writer_id": binding["writer"]["id"],
                               "session_id": "original", "state": "busy"}, method="PATCH")
        self.ok("/v1/writer/progress", token, {"note": "Still drafting; no blocker"})
        first = next(flag for flag in self.ok("/v1/writer", token)["flags"]
                     if flag["kind"] == "active_overdue")
        self.assertIn("Still drafting; no blocker", first["reason"])
        self.assertIn("session original", first["recovery"])
        binding = self.submit(token, binding, report_markdown="# Report\n")
        reviewed = next(flag for flag in self.ok("/v1/writer", token)["flags"]
                        if flag["kind"] == "active_overdue")
        self.assertEqual(reviewed["first_seen"], first["first_seen"])
        self.assertIn("stage report_review", reviewed["reason"])
        replacement, replacement_token, _ = self.register(
            item_id, execution_id, replaces=binding["writer"]["id"], replaced_stopped=True)
        replaced = next(flag for flag in self.ok("/v1/writer", replacement_token)["flags"]
                        if flag["kind"] == "active_overdue")
        self.assertEqual(replaced["first_seen"], first["first_seen"])
        self.assertIn(replacement["writer"]["id"], replaced["recovery"])
        binding = self.submit(replacement_token, replacement, report_revision=intake.digest("# Report\n"))
        done = self.submit(replacement_token, binding, report_revision=intake.digest("# Report\n"))
        self.assertEqual(done["state"]["status"], "completed")
        self.assertNotIn("active_overdue", [flag["kind"] for flag in self.ok("/v1/flags", RUNNER)["flags"]])
        rerun = self.ok(f"/v1/items/{item_id}/executions/{execution_id}/rerun", OWNER,
                       {"expected_version": done["version"], "target": "report"})
        fresh, _, _ = self.register(item_id, rerun["id"])
        self.assertEqual(fresh["id"], rerun["id"])
        self.assertNotIn("active_overdue", [flag["kind"] for flag in self.ok("/v1/flags", RUNNER)["flags"]])

    def test_overdue_delivery_surfaces_known_blocker_actor_and_trigger_through_existing_reads(self):
        publication, token = self.publish("checked_in")
        delivery = self.submit(token, publication, pr_url=PR_URL)
        self.assertEqual(delivery["state"]["status"], "waiting_for_external")
        started = datetime.now(UTC) - timedelta(minutes=91)
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET started_at=? WHERE execution_id=?",
                       (started.isoformat(timespec="microseconds"), delivery["id"]))
        self.ok("/v1/flags", RUNNER, {
            "kind": "review_wait", "item_id": delivery["item_id"], "execution_id": delivery["id"],
            "reason": "Required independent final-head review pending for reviewer R",
            "recovery": "Reviewer R approves exact head " + HEAD + "; same writer resumes delivery",
        }, method="PUT")
        self.ok(f"/v1/items/{delivery['item_id']}/captain-actions", RUNNER, {
            "captain_id": self.captain["id"], "action": "review requested",
            "reason": "Reviewer R owns exact-head approval; resume on review; CI is green",
        }, code=201)
        current = self.ok("/v1/writer", token)
        warning = next(flag for flag in current["flags"] if flag["kind"] == "active_overdue")
        self.assertIn("stage delivery, status waiting_for_external", warning["reason"])
        self.assertIn("Required independent final-head review pending for reviewer R", warning["reason"])
        self.assertIn("CI is green", warning["reason"])
        self.assertIn("Reviewer R approves exact head " + HEAD, warning["recovery"])
        self.assertIn(delivery["writer"]["id"], warning["recovery"])
        briefing = shift.status(lambda route: self.ok(route, RUNNER), {"repositories": ["repo"]})
        self.assertIn("active_overdue", [flag["kind"] for flag in briefing["flags"]])
        self.assertEqual(current["version"], delivery["version"])
        self.assertEqual(current["state"], delivery["state"])
        self.assertEqual(current["writer"]["capacity_revision"], delivery["writer"]["capacity_revision"])

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
        self.assertEqual(self.ok(f"/v1/items/{head}", OWNER)["execution"]["state"]["status"], "queued")

        other, _ = self.item()
        self.ok(f"/v1/items/{other}", token, code=403)
        self.ok("/v1/flags", token, code=403)
        self.ok("/v1/writer", RUNNER, code=403)
        self.assertEqual(self.ok("/v1/writer", token)["writer"]["id"], binding["writer"]["id"])
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}/result", RUNNER, {
            "attempt_id": binding["writer"]["attempt_id"], "stage": "report_drafting",
            "expected_version": binding["version"], "outcome": "succeeded", "report_markdown": "# R\n",
        }, 403)
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
        native = "12345678-1234-1234-1234-123456789abc"
        route = f"/v1/items/{item_id}/executions/{execution_id}/writer"
        self.ok(route, RUNNER, {"captain_id": self.captain["id"], "writer_id": binding["writer"]["id"],
                               "session_id": native, "session_url": "codex://threads/" + native},
                method="PATCH")
        binding = self.submit(token, binding, report_markdown="# Report\n")
        binding = self.submit(token, binding, report_revision=intake.digest("# Report\n"))
        self.assertEqual(binding["state"]["stage"], "report_storage")
        finished = self.submit(token, binding, report_revision=intake.digest("# Report\n"))
        self.assertEqual(finished["state"]["status"], "completed")
        with connect(self.path, mode="ro") as db:
            writer = db.execute("SELECT * FROM writers WHERE id=?", (binding["writer"]["id"],)).fetchone()
        self.assertIsNotNone(writer["ended_at"])
        self.assertEqual((writer["end_reason"], writer["attempt_id"]), ("closed as completed", None))
        for url in (f"/v1/operator/items/{item_id}",
                    f"/v1/operator/executions/{execution_id}/timeline"):
            recorded = self.ok(url, OWNER)["item"]["writer"]
            self.assertEqual((recorded["state"], recorded["session_id"], recorded["session_url"],
                              recorded["session_availability"]),
                             ("ended", native, "codex://threads/" + native, "unverified"))
        history = self.ok("/v1/operator/executions?kind=closed", OWNER)["items"]
        self.assertEqual(history[0]["writer"]["session_id"], native)
        observer = self.ok("/v1/captains", RUNNER,
            {"session_ref": "observer", "charter_revision": "c1"}, 201)
        for availability in ("missing", "deleted"):
            self.ok(f"/v1/captains/{observer['id']}", RUNNER, {
                "charter_revision": "c1", "wake_count": 1, "state": "active",
                "sessions": [{"id": native, "name": "Ended writer [phx]", "availability": availability,
                              "runtime": "any-runtime"}],
            }, method="PUT")
            recorded = self.ok(f"/v1/operator/items/{item_id}", OWNER)["item"]["writer"]
            self.assertEqual(recorded["session_availability"], availability)

    def test_queued_writer_history_is_not_an_active_writer_or_a_replacement_session(self):
        item_id, execution_id = self.item()
        binding, _, _ = self.register(item_id, execution_id)
        route = f"/v1/items/{item_id}/executions/{execution_id}/writer"
        writer_id = binding["writer"]["id"]
        self.ok(route, RUNNER, {"captain_id": self.captain["id"], "writer_id": writer_id,
                               "session_id": "recorded-session"}, method="PATCH")
        self.ok(route + "/withdraw", RUNNER, {"captain_id": self.captain["id"],
                "writer_id": writer_id, "stopped": True, "reason": "Resume later"})
        recorded = self.ok(f"/v1/operator/items/{item_id}", OWNER)["item"]
        self.assertEqual((recorded["status"], recorded["writer"]["state"]), ("queued", "ended"))
        self.assertEqual(recorded["writer"]["session_id"], "recorded-session")
        self.assertIsNone(recorded["wait_reason"])
        self.assertIsNone(shift._summary(recorded)["writer"])
        builds = [self.register(*self.item())[0] for _ in range(5)]
        recorded = self.ok(f"/v1/operator/items/{item_id}", OWNER)["item"]
        self.assertIn("Building capacity exhausted", recorded["wait_reason"])
        self.assertEqual(recorded["writer"]["session_id"], "recorded-session")
        self.withdraw_binding(builds[0])
        replacement, _, _ = self.register(item_id, execution_id)
        recorded = self.ok(f"/v1/operator/items/{item_id}", OWNER)["item"]["writer"]
        self.assertEqual(recorded["id"], replacement["writer"]["id"])
        self.assertIsNone(recorded["session_id"])

    def test_concurrent_registrations_without_charter_limits_do_not_overfill(self):
        self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        items = [self.item() for _ in range(8)]
        bodies = [{"expected_version": self.version(item_id), "captain_id": self.captain["id"],
                   "runtime": "any-runtime", "nonce": token_urlsafe(16),
                   "token_digest": intake.digest(token_urlsafe(32))}
                  for item_id, _ in items]

        def register(index):
            item_id, execution_id = items[index]
            return self.request(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER, bodies[index])

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(register, range(8)))
        self.assertEqual(sorted(code for code, _ in responses), [200] + [409] * 7)
        winner = next(response for code, response in responses if code == 200)
        refused = next(response for code, response in responses if code == 409)
        self.assertIn("max_writers=1", refused["detail"])
        self.assertIn(winner["writer"]["id"], refused["detail"])
        winner_index = next(index for index, (code, _) in enumerate(responses) if code == 200)
        replay_code, replay = register(winner_index)
        self.assertEqual(replay_code, 200, replay)
        self.assertEqual(replay["writer"]["id"], winner["writer"]["id"])

    def test_initial_five_counts_sessions_not_unassigned_queued_items(self):
        queued = [self.item() for _ in range(10)]
        for index, (item_id, execution_id) in enumerate(queued[:5]):
            binding, _, _ = self.register(item_id, execution_id)
            self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER,
                    {"captain_id": self.captain["id"], "writer_id": binding["writer"]["id"],
                     "session_id": f"native-{index}",
                     "state": ("busy", "idle", "error", "stopped", "busy")[index]}, method="PATCH")
        status = self.ok("/v1/dispatch/settings", OWNER)
        self.assertEqual((status["max_writers"], status["active_writers"]), (10, 5))
        self.register(*queued[5], code=409)

    def test_failed_and_cancelled_writers_release_capacity(self):
        self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        for ending in ("failed", "cancelled"):
            with self.subTest(ending=ending):
                item_id, execution_id = self.item()
                binding, _, _ = self.register(item_id, execution_id)
                action = "close-failure" if ending == "failed" else "cancel"
                self.ok(f"/v1/items/{item_id}/executions/{execution_id}/{action}", OWNER,
                        {"expected_version": binding["version"], "reason": "Cannot continue"})
                self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer/withdraw", RUNNER,
                        {"captain_id": self.captain["id"], "writer_id": binding["writer"]["id"],
                         "reason": "stopped", "stopped": True})
                closed = finalization.attempt(self.path, execution_id)
                self.assertEqual(closed["state"]["status"], ending)
                following, following_execution = self.item()
                launched, _, _ = self.register(following, following_execution)
                self.ok(f"/v1/items/{following}/executions/{following_execution}/writer/withdraw", RUNNER,
                        {"captain_id": self.captain["id"], "writer_id": launched["writer"]["id"],
                         "reason": "never launched"})

    def test_capacity_limit_does_not_scan_unrelated_executions(self):
        from phalanx import writers

        self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        item_id, execution_id = self.item()
        binding, _, _ = self.register(item_id, execution_id)
        with write_transaction(self.path) as db:
            for index in range(1000):
                item = intake.create(db, Intake(repository_id="repo", goal=f"Work {index}", target="report"))
            payload = writers.Registration(
                expected_version=item["execution"]["version"], captain_id=self.captain["id"],
                runtime="any-runtime", nonce=token_urlsafe(16), token_digest=intake.digest(token_urlsafe(32)))
            steps = 0

            def budget():
                nonlocal steps
                steps += 100
                return steps > 1000

            db.set_progress_handler(budget, 100)
            try:
                with self.assertRaisesRegex(intake.Conflict, "max_writers=1") as refused:
                    writers.register(db, item["id"], item["execution"]["id"], payload)
                self.assertIn(binding["writer"]["id"], str(refused.exception))
            finally:
                db.set_progress_handler(None, 0)

    def test_raising_and_lowering_share_one_workspace_budget(self):
        self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        first_item, first_execution = self.item()
        first, token, _ = self.register(first_item, first_execution)
        self.ok("/v1/dispatch/settings", token, {"max_writers": 9}, 403, method="PUT")
        self.ok("/v1/dispatch/settings", token, code=403)
        self.ok("/v1/repositories", OWNER, {"id": "other", "remote": "https://github.com/e/other.git"})
        other = self.ok("/v1/items", OWNER, {"repository_id": "other", "goal": "Other repo",
                                           "target": "report"}, 201)
        # A different Captain or repository cannot bypass the saved budget.
        captain = self.ok("/v1/captains", RUNNER, {"session_ref": "second", "charter_revision": "old"}, 201)
        self.register(other["id"], other["execution"]["id"], captain=captain["id"], code=409)
        self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 2, "max_building_writers": 2}, method="PUT")
        second, _, _ = self.register(other["id"], other["execution"]["id"], captain=captain["id"])
        saved = self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        self.assertEqual((saved["active_writers"], saved["max_writers"]), (2, 1))
        queued, execution = self.item()
        self.register(queued, execution, code=409)
        # Existing work keeps its writer token and next stage when lowered below usage.
        advanced = self.submit(token, first, report_markdown="# Continue\n")
        self.assertEqual(advanced["writer"]["id"], first["writer"]["id"])
        replacement, _, _ = self.register(
            first_item, first_execution, replaces=first["writer"]["id"], replaced_stopped=True)
        self.assertEqual(replacement["writer"]["replaced_id"], first["writer"]["id"])
        for binding in (replacement, second):
            self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/writer/withdraw",
                    RUNNER, {"captain_id": binding["writer"]["captain_id"],
                             "writer_id": binding["writer"]["id"], "reason": "never launched"})
            if binding is replacement:
                self.register(queued, execution, code=409)
        self.register(queued, execution)

    def test_queued_binding_survives_steering_and_resumes_its_writer(self):
        item_id, execution_id = self.item()
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=? WHERE id=?", (2**42, execution_id))
        binding, token, _ = self.register(item_id, execution_id)
        self.assertEqual((binding["state"]["status"], binding["writer"]["attempt_id"]), ("queued", None))
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}", OWNER,
                {"expected_version": self.version(item_id), "priority": "urgent"}, method="PATCH")
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=0 WHERE id=?", (execution_id,))
            dispatch.set_dispatch(db, DispatchSettings(paused=True))
        attempt = self.ok("/v1/writer/attempt", token, method="POST")
        self.assertEqual(attempt["state"]["status"], "running")
        self.assertIsNotNone(attempt["writer"]["attempt_id"])
        following = self.submit(token, attempt, report_markdown="# Continue while paused\n")
        self.assertEqual((following["state"]["stage"], following["state"]["status"]), ("report_review", "running"))

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
            result = run_cli(["work", *arguments], cwd=worktree)
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
        self.assertIsNone(work("status")["last_reply"])
        self.assertIsNone(work("next")["last_reply"])
        route = f"/v1/items/{item_id}/executions/{execution_id}"
        for index, actor in enumerate(("owner", "captain")):
            with self.subTest(answered_by=actor):
                answer = f"Use the {actor}'s exact instructions.\n  Preserve this detail."
                body = {"expected_version": asked["version"], "answer": answer}
                if actor == "captain":
                    body["answered_by"] = actor
                headers = {"Idempotency-Key": f"reply-{index}"}
                queued = self.ok(route + "/reply", OWNER, body, headers=headers)
                self.assertEqual(self.ok(route + "/reply", OWNER, body, headers=headers), queued)
                self.ok(route + "/reply", token, body, 403,
                        headers={"Idempotency-Key": f"writer-reply-{index}"})
                self.ok(route + "/reply", OWNER, body, 409,
                        headers={"Idempotency-Key": f"stale-reply-{index}"})
                timeline = self.ok(route + "/timeline", OWNER)
                persisted = next(event for event in reversed(timeline) if event["event"] == "reply")
                expected = {"answer": answer, "answered_by": actor, "version": queued["version"],
                            "created_at": persisted["created_at"]}
                self.assertEqual(persisted["details"]["answer"], answer)
                self.assertEqual(self.ok(f"/v1/items/{item_id}", OWNER)["last_reply"], expected)
                status = work("status")
                self.assertEqual(status["state"]["status"], "queued")
                self.assertEqual(status["last_reply"], expected)
                resumed = work("next")
                self.assertEqual(resumed["last_reply"], expected)
                self.assertGreater(resumed["version"], expected["version"])
                self.assertEqual(resumed["state"]["stage"], asked["state"]["stage"])
                self.assertNotEqual(resumed["writer"]["attempt_id"], drafted["writer"]["attempt_id"])
                self.assertEqual(work("next"), resumed)
                self.assertEqual(work("status")["last_reply"], expected)
                stale = work("question", "Stale question", "--attempt", drafted["writer"]["attempt_id"],
                             "--handoff", "Old attempt", code=1)
                self.assertIn("is not open", stale)
                if actor == "owner":
                    asked = work("question", "Which detail?", "--attempt", resumed["writer"]["attempt_id"],
                                 "--handoff", "Audience chosen")

    def test_writer_replies_are_isolated_by_item_and_execution(self):
        item_id, execution_id = self.item()
        binding, token, _ = self.register(item_id, execution_id)
        waiting = self.submit(token, binding, outcome="needs_input",
                              question={"prompt": "Which scope?", "handoff": "Draft pending"})
        route = f"/v1/items/{item_id}/executions/{execution_id}"
        self.ok(route + "/reply", OWNER, {"expected_version": waiting["version"], "answer": "Original only"},
                headers={"Idempotency-Key": "original-reply"})
        original_reply = self.ok("/v1/writer", token)["last_reply"]
        other_id, other_execution = self.item()
        other, other_token, _ = self.register(other_id, other_execution)
        self.assertIsNone(other["last_reply"])
        self.assertIsNone(self.ok("/v1/writer", other_token)["last_reply"])
        self.assertIsNone(self.ok("/v1/writer/attempt", other_token, method="POST")["last_reply"])
        self.ok(route + "/timeline", other_token, code=403)
        self.ok(f"/v1/items/{other_id}/executions/{other_execution}/timeline", token, code=403)
        resumed = self.ok("/v1/writer/attempt", token, method="POST")
        self.assertEqual(resumed["last_reply"], original_reply)
        drafted = self.submit(token, resumed, report_markdown="# Report\n")
        reviewed = self.submit(token, drafted, report_revision=drafted["state"]["report_draft"])
        done = self.submit(token, reviewed, report_revision=reviewed["state"]["report_draft"])
        rerun = self.ok(route + "/rerun", OWNER, {"expected_version": done["version"], "target": "report"})
        replacement, replacement_token, _ = self.register(item_id, rerun["id"])
        self.assertIsNone(replacement["last_reply"])
        self.assertIsNone(self.ok(f"/v1/items/{item_id}", OWNER)["last_reply"])
        self.assertIsNone(self.ok("/v1/writer", replacement_token)["last_reply"])
        self.assertIsNone(self.ok("/v1/writer/attempt", replacement_token, method="POST")["last_reply"])
        self.ok(route + "/timeline", replacement_token, code=403)
        self.ok("/v1/writer", token, code=401)

    def pr_item(self, target, **fields):
        item = self.ok("/v1/items", OWNER, {"repository_id": "repo", "goal": "Fix it", "target": target,
                                             **fields}, 201)
        return item["id"], item["execution"]["id"]

    def capacity(self, token, phase, head=HEAD):
        current = self.ok("/v1/writer", token)
        return self.ok("/v1/writer/capacity", token, {
            "phase": phase, "expected_revision": current["writer"]["capacity_revision"],
            **({"handoff_head": head} if phase == "delivering" else {})})

    def withdraw_binding(self, binding):
        return self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/writer/withdraw",
                       RUNNER, {"captain_id": binding["writer"]["captain_id"],
                                "writer_id": binding["writer"]["id"], "reason": "never launched"})

    def test_five_building_ten_total_and_publication_handoff(self):
        delivering = []
        for _ in range(5):
            binding, token = self.publish("ready_for_check_in")
            self.assertFalse(binding["writer"]["building"])
            delivering.append((binding, token))
        builds = [self.register(*self.item())[0] for _ in range(5)]
        status = self.ok("/v1/dispatch/settings", OWNER)
        self.assertEqual((status["building_writers"], status["delivering_writers"],
                          status["active_writers"]), (5, 5, 10))
        refused, _, _ = self.register(*self.item(), code=409)
        self.assertIn("Total capacity exhausted", refused["detail"])
        self.withdraw_binding(delivering[0][0])
        refused, _, _ = self.register(*self.item(), code=409)
        self.assertIn("Building capacity exhausted", refused["detail"])
        self.withdraw_binding(builds[0])
        self.register(*self.item())

    def test_simultaneous_new_builds_obey_five_with_spare_total(self):
        items = [self.item() for _ in range(8)]
        bodies = [{"expected_version": self.version(item_id), "captain_id": self.captain["id"],
                   "runtime": "native", "nonce": token_urlsafe(16),
                   "token_digest": intake.digest(token_urlsafe(32))} for item_id, _ in items]
        def launch(index):
            item_id, execution_id = items[index]
            return self.request(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER, bodies[index])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(launch, range(8)))
        self.assertEqual(sorted(code for code, _ in results), [200] * 5 + [409] * 3)
        self.assertTrue(all("Building capacity exhausted" in row["detail"]
                            for code, row in results if code == 409))
        status = self.ok("/v1/dispatch/settings", OWNER)
        self.assertEqual((status["building_writers"], status["active_writers"]), (5, 5))

    def test_delivery_rework_waits_retains_total_and_has_priority_over_new_builds(self):
        publication, token = self.publish("ready_for_check_in")
        delivery = self.submit(token, publication, pr_url=PR_URL)
        builds = [self.register(*self.item())[0] for _ in range(5)]
        waiting = self.capacity(token, "building")
        self.assertFalse(waiting["writer"]["building"])
        self.assertIn("Building capacity exhausted", waiting["writer"]["capacity_wait"])
        self.assertEqual(waiting["state"]["stage"], "delivery")
        migrate(self.path)
        self.withdraw_binding(builds[0])
        refused, _, _ = self.register(*self.item(), code=409)
        self.assertIn("waiting PR rework", refused["detail"])
        acquired = self.capacity(token, "building")
        self.assertTrue(acquired["writer"]["building"])
        self.assertEqual(acquired["writer"]["id"], delivery["writer"]["id"])
        revision = acquired["writer"]["capacity_revision"]
        replay = self.capacity(token, "building")
        self.assertEqual(replay["writer"]["capacity_revision"], revision)
        released = self.capacity(token, "delivering")
        self.assertFalse(released["writer"]["building"])
        # A stale release cannot give away a subsequently reacquired allocation.
        self.capacity(token, "building")
        self.ok("/v1/writer/capacity", token,
                {"phase": "delivering", "expected_revision": released["writer"]["capacity_revision"]}, 409)
        self.assertTrue(self.ok("/v1/writer", token)["writer"]["building"])
        self.capacity(token, "delivering")
        self.register(*self.item())

    def test_rework_reacquisition_is_atomic_with_competing_admissions(self):
        publication, token = self.publish("ready_for_check_in")
        self.submit(token, publication, pr_url=PR_URL)
        builds = [self.register(*self.item())[0] for _ in range(5)]
        self.capacity(token, "building")
        self.withdraw_binding(builds[0])
        item_id, execution_id = self.item()
        body = {"expected_version": self.version(item_id), "captain_id": self.captain["id"],
                "runtime": "native", "nonce": token_urlsafe(16), "token_digest": intake.digest("new")}
        current = self.ok("/v1/writer", token)
        acquire = {"phase": "building", "expected_revision": current["writer"]["capacity_revision"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            rework = pool.submit(self.request, "/v1/writer/capacity", token, acquire)
            new = pool.submit(self.request, f"/v1/items/{item_id}/executions/{execution_id}/writer",
                              RUNNER, body)
        self.assertEqual(rework.result()[0], 200)
        self.assertTrue(rework.result()[1]["writer"]["building"])
        self.assertEqual(new.result()[0], 409)
        self.assertEqual(self.ok("/v1/dispatch/settings", OWNER)["building_writers"], 5)

    def test_rework_priority_is_fifo_and_ineligible_requests_do_not_starve_intake(self):
        first, first_token = self.publish("ready_for_check_in")
        first = self.submit(first_token, first, pr_url=PR_URL)
        second, second_token = self.publish("ready_for_check_in")
        self.submit(second_token, second, pr_url=PR_URL)
        builds = [self.register(*self.item())[0] for _ in range(5)]
        self.capacity(first_token, "building")
        self.capacity(second_token, "building")
        self.withdraw_binding(builds[0])
        self.assertFalse(self.capacity(second_token, "building")["writer"]["building"])
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET retry_at=? WHERE id=?", (2**42, first["id"]))
        current = self.ok("/v1/writer", first_token)
        self.ok("/v1/writer/capacity", first_token, {
            "phase": "building", "expected_revision": current["writer"]["capacity_revision"]}, 409)
        self.assertTrue(self.capacity(second_token, "building")["writer"]["building"])
        self.capacity(second_token, "delivering")
        self.register(*self.item())

    def test_delivery_observation_does_not_end_a_writer_with_unpublished_rework(self):
        publication, token = self.publish("ready_for_check_in")
        delivery = self.submit(token, publication, pr_url=PR_URL)
        self.assertTrue(self.capacity(token, "building")["writer"]["building"])
        observed = self.observe(delivery)
        self.assertEqual(observed["state"]["status"], "waiting_for_external")
        self.assertTrue(self.ok("/v1/writer", token)["writer"]["building"])
        current = self.ok("/v1/writer", token)
        self.ok("/v1/writer/capacity", token, {
            "phase": "delivering", "expected_revision": current["writer"]["capacity_revision"]}, 409)
        self.capacity(token, "delivering", head=OTHER)
        migrate(self.path)
        self.assertEqual(self.observe(delivery)["state"]["status"], "waiting_for_external")
        drift = next(flag for flag in self.ok("/v1/writer", token)["flags"] if flag["kind"] == "head_drift")
        self.assertIn(f"publication handoff {OTHER}", drift["reason"])
        board = self.ok("/v1/operator/executions?kind=open", OWNER)["items"]
        self.assertIn("publication handoff", board[0]["wait_reason"])
        self.assertEqual(self.observe(delivery, head_sha=OTHER)["state"]["status"], "completed")
        self.ok("/v1/writer", token, code=401)

    def test_merge_is_authoritative_during_building_or_mismatched_publication_handoff(self):
        for phase in ("building", "delivering"):
            with self.subTest(phase=phase):
                publication, token = self.publish("checked_in")
                delivery = self.submit(token, publication, pr_url=PR_URL)
                self.capacity(token, "building")
                if phase == "delivering":
                    self.capacity(token, "delivering", head=OTHER)
                merged = self.observe(delivery, state="closed", merged=True, merge_commit=MERGE)
                self.assertEqual(merged["state"]["status"], "completed")
                self.ok("/v1/writer", token, code=401)
                with connect(self.path) as db:
                    self.assertIsNone(db.execute("SELECT handoff_head FROM executions WHERE id=?",
                                                 (delivery["id"],)).fetchone()[0])

    def test_withdrawal_clears_superseded_handoff_and_recovers_delivery(self):
        publication, token = self.publish("ready_for_check_in")
        delivery = self.submit(token, publication, pr_url=PR_URL)
        self.capacity(token, "building")
        self.capacity(token, "delivering", head=OTHER)
        self.assertEqual(self.observe(delivery)["state"]["status"], "waiting_for_external")
        self.withdraw_binding(delivery)
        self.assertEqual(self.observe(delivery)["state"]["status"], "completed")
        with connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT handoff_head FROM executions WHERE id=?",
                                         (delivery["id"],)).fetchone()[0])

    def test_capacity_cli_fences_the_reviewed_head_at_publication_handoff(self):
        publication, token = self.publish("ready_for_check_in")
        self.submit(token, publication, pr_url=PR_URL)
        with patch.object(work_cli, "credential", return_value=(self.url, token)), patch("builtins.print"):
            work_cli.run_work(Namespace(work_command="capacity", phase="building"))
            with patch.object(work_cli.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, stdout=OTHER + "\n")) as git:
                work_cli.run_work(Namespace(work_command="capacity", phase="delivering"))
            git.assert_called_once()
        current = self.ok("/v1/writer", token)
        self.assertFalse(current["writer"]["building"])
        self.assertEqual(current["writer"]["handoff_head"], OTHER)

    def test_lowering_building_preserves_allocations_and_raising_wakes_rework(self):
        publication, token = self.publish("ready_for_check_in")
        self.submit(token, publication, pr_url=PR_URL)
        builds = [self.register(*self.item())[0] for _ in range(5)]
        self.ok("/v1/dispatch/settings", OWNER, {"max_building_writers": 3}, method="PUT")
        self.assertFalse(self.capacity(token, "building")["writer"]["building"])
        for build in builds[:2]:
            self.withdraw_binding(build)
        self.assertFalse(self.capacity(token, "building")["writer"]["building"])
        self.ok("/v1/dispatch/settings", OWNER, {"max_building_writers": 4}, method="PUT")
        self.assertTrue(self.capacity(token, "building")["writer"]["building"])
        self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 4}, method="PUT")
        self.capacity(token, "delivering")
        refused, _, _ = self.register(*self.item(), code=409)
        self.assertIn("Total capacity exhausted", refused["detail"])
        self.ok("/v1/dispatch/settings", OWNER, {"max_writers": 5}, method="PUT")
        self.register(*self.item())

    def test_replacement_transfers_waiting_rework_and_revokes_old_writer(self):
        publication, token = self.publish("ready_for_check_in")
        delivery = self.submit(token, publication, pr_url=PR_URL)
        builds = [self.register(*self.item())[0] for _ in range(5)]
        waiting = self.capacity(token, "building")
        replacement, replacement_token, _ = self.register(
            delivery["item_id"], delivery["id"], replaces=delivery["writer"]["id"], replaced_stopped=True)
        self.assertFalse(replacement["writer"]["building"])
        self.assertEqual(replacement["writer"]["building_requested_at"], waiting["writer"]["building_requested_at"])
        self.ok("/v1/writer/capacity", token, {"phase": "building", "expected_revision": 0}, 401)
        self.withdraw_binding(builds[0])
        self.assertTrue(self.capacity(replacement_token, "building")["writer"]["building"])
        self.withdraw_binding(replacement)
        self.assertEqual(self.ok("/v1/dispatch/settings", OWNER)["building_writers"], 4)

    def test_recovered_self_review_cannot_claim_without_building(self):
        publication, token = self.publish("ready_for_check_in", publication_mode="draft")
        delivery = self.submit(token, publication, pr_url=PR_URL)
        builds = [self.register(*self.item())[0] for _ in range(5)]
        self.observe(delivery, head_sha=OTHER, draft=True)
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=? WHERE id=?", (2**42, delivery["id"]))
        current = self.ok("/v1/writer", token)
        self.ok("/v1/writer/capacity", token, {
            "phase": "building", "expected_revision": current["writer"]["capacity_revision"]}, 409)
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=0 WHERE id=?", (delivery["id"],))
        waiting = self.ok("/v1/writer/attempt", token, method="POST")
        self.assertEqual(waiting["state"]["stage"], "self_review")
        self.assertIsNone(waiting["writer"]["attempt_id"])
        self.assertFalse(waiting["writer"]["building"])
        self.assertIn("Building capacity exhausted", waiting["writer"]["capacity_wait"])
        self.withdraw_binding(builds[0])
        reviewed = self.ok("/v1/writer/attempt", token, method="POST")
        self.assertTrue(reviewed["writer"]["building"])
        self.assertIsNotNone(reviewed["writer"]["attempt_id"])
        published = self.submit(token, reviewed, reviewed_candidate=OTHER)
        self.assertFalse(published["writer"]["building"])

    def test_upgrade_preserves_live_writer_and_charges_conservatively_until_protocol_handoff(self):
        publication, token = self.publish("ready_for_check_in")
        delivery = self.submit(token, publication, pr_url=PR_URL)
        with write_transaction(self.path) as db:
            db.execute("UPDATE dispatch_settings SET max_writers=5")
            db.execute("DROP TABLE item_titles")
            db.execute("ALTER TABLE dispatch_settings DROP COLUMN max_building_writers")
            db.execute("DROP INDEX waiting_writer_builds")
            db.execute("ALTER TABLE executions DROP COLUMN handoff_head")
            for column in ("building", "building_requested_at", "capacity_revision"):
                db.execute(f"ALTER TABLE writers DROP COLUMN {column}")
            db.execute(f"PRAGMA user_version={store.MIGRATIONS.index(store.WRITER_CAPACITY_SCHEMA)}")
        migrate(self.path)
        migrate(self.path)
        current = self.ok("/v1/writer", token)
        self.assertEqual(current["writer"]["id"], delivery["writer"]["id"])
        self.assertTrue(current["writer"]["building"])
        status = self.ok("/v1/dispatch/settings", OWNER)
        self.assertEqual((status["building_writers"], status["max_writers"]), (1, 5))
        self.capacity(token, "delivering")
        self.assertEqual(self.ok("/v1/dispatch/settings", OWNER)["building_writers"], 0)

    def publish(self, target, **fields):
        """Carry a writer-bound PR item through development and self-review to publication."""
        item_id, execution_id = self.pr_item(target, **fields)
        binding, token, _ = self.register(item_id, execution_id)
        binding = self.submit(token, binding, candidate=HEAD)
        self.assertEqual((binding["state"]["stage"], binding["state"]["status"]), ("self_review", "running"))
        binding = self.submit(token, binding, reviewed_candidate=HEAD)
        self.assertEqual((binding["state"]["stage"], binding["state"]["status"]), ("publication", "running"))
        return binding, token

    def observe(self, binding, code=200, *, checks="satisfied", **pull):
        pull = {"number": 7, "state": "open", "merged": False, "draft": False,
                "head_ref": item_branch(binding["item_id"]), "head_sha": HEAD, "base_ref": "main",
                "base_sha": BASE, **pull}
        body = {"observed_at": datetime.now(UTC).isoformat(), "pr": pull, "target_base": "main",
                **({"checks": checks} if pull["state"] == "open" else {})}
        self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/github-observation", RUNNER,
                body, code)
        return self.ok(f"/v1/items/{binding['item_id']}", OWNER)["execution"]

    def steer(self, binding, target, code=200, **fields):
        response = self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}", OWNER,
                           {"expected_version": binding["version"], "target": target, **fields},
                           code, method="PATCH")
        if code == 200:
            response["execution"]["item_id"] = binding["item_id"]
        return response

    def test_queued_target_steering_preserves_output_type_and_versions(self):
        item_id, execution_id = self.pr_item("ready_for_check_in")
        binding = dict(self.ok(f"/v1/items/{item_id}", OWNER)["execution"], item_id=item_id)
        self.steer(binding, "report", code=409)
        changed = self.steer(binding, "checked_in", steered_by="captain")
        self.assertEqual((changed["target"], changed["execution"]["state"]["target"]),
                         ("checked_in", "checked_in"))
        self.steer(binding, "ready_for_check_in", code=409)
        changed = self.steer(changed["execution"], "ready_for_check_in")
        self.assertEqual(changed["target"], "ready_for_check_in")
        report_id, _ = self.item()
        report = dict(self.ok(f"/v1/items/{report_id}", OWNER)["execution"], item_id=report_id)
        self.steer(report, "checked_in", code=409)
        self.steer(report, "ready_for_check_in", code=409)
        binding, token, _ = self.register(item_id, execution_id)
        self.steer(binding, "checked_in")
        binding = self.ok("/v1/writer", token)
        reviewed = self.submit(token, binding, candidate=HEAD)
        self.assertEqual((reviewed["state"]["stage"], reviewed["state"]["target"]),
                         ("self_review", "checked_in"))

    def test_delivery_target_steering_records_actor_and_completed_ready_requires_rerun(self):
        binding, token = self.publish("ready_for_check_in")
        binding = self.submit(token, binding, pr_url=PR_URL)
        self.steer(binding, "report", code=409)
        self.steer(binding, "checked_in", code=409, goal="Not a target-only edit")
        for target, actor in (("checked_in", "captain"), ("ready_for_check_in", "owner")):
            before = binding["state"]["target"]
            changed = self.steer(binding, target, steered_by=actor)
            binding = changed["execution"]
            self.assertEqual((changed["target"], binding["state"]["target"]), (target, target))
            self.assertEqual((binding["state"]["stage"], binding["state"]["status"]),
                             ("delivery", "waiting_for_external"))
            timeline = self.ok(f"/v1/items/{binding['item_id']}/executions/{binding['id']}/timeline", OWNER)
            self.assertEqual(timeline[-1]["event"], "steer")
            self.assertEqual(timeline[-1]["details"],
                             {"before": {"target": before}, "after": {"target": target}, "steered_by": actor})
        done = dict(self.observe(binding), item_id=binding["item_id"])
        self.assertEqual(done["state"]["status"], "completed")
        self.steer(done, "checked_in", code=409)
        rerun = self.ok(f"/v1/items/{done['item_id']}/executions/{done['id']}/rerun", OWNER,
                        {"expected_version": done["version"], "target": "checked_in"})
        self.assertNotEqual(rerun["id"], done["id"])
        self.assertEqual(rerun["state"]["target"], "checked_in")

    def test_steered_check_in_waits_for_required_checks_and_records_the_merge(self):
        binding, token = self.publish("ready_for_check_in")
        binding = self.submit(token, binding, pr_url=PR_URL)
        binding = self.steer(binding, "checked_in")["execution"]
        for checks in ("failed", "waiting"):
            waiting = self.observe(binding, checks=checks)
            self.assertEqual((waiting["state"]["stage"], waiting["state"]["status"]),
                             ("delivery", "waiting_for_external"))
            self.assertIsNone(waiting["state"]["ready_revision"])
            self.assertIsNone(waiting["state"]["final_output"])
        ready = self.observe(binding)
        self.assertEqual(ready["state"]["status"], "waiting_for_external")
        self.assertEqual(ready["state"]["ready_revision"], {"head": HEAD, "base": BASE})
        merged = self.observe(binding, state="closed", merged=True, merge_commit=MERGE)
        self.assertEqual((merged["state"]["status"], merged["state"]["target"]), ("completed", "checked_in"))
        self.assertEqual(merged["state"]["final_output"]["merge_commit"], MERGE)
        self.ok("/v1/writer", token, code=401)

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

    def test_delivery_stop_waits_for_the_registered_writer_without_an_attempt(self):
        for action, disposition in (("cancel", "cancelled"), ("close-failure", "failed")):
            with self.subTest(action=action):
                binding, token = self.publish("checked_in")
                binding = self.submit(token, binding, pr_url=PR_URL)
                route = f"/v1/items/{binding['item_id']}/executions/{binding['id']}"
                writer = binding["writer"]
                self.ok(route + "/writer", RUNNER, {
                    "captain_id": self.captain["id"], "writer_id": writer["id"],
                    "session_id": f"delivery-{action}",
                }, method="PATCH")
                stopping = self.ok(route + "/" + action, OWNER, {
                    "expected_version": binding["version"], "reason": "Stop delivery",
                })
                self.assertEqual((stopping["state"]["status"], stopping["state"]["stopping"]),
                                 ("waiting_for_external", disposition))
                self.assertIsNone(stopping["state"]["final_output"])
                self.assertIsNone(self.ok("/v1/writer/attempt", token, method="POST")["writer"]["attempt_id"])
                self.assertEqual(self.observe(binding)["version"], stopping["version"])
                self.assertEqual(self.observe(binding, state="closed", merged=True, base_ref="other",
                                              merge_commit=MERGE)["version"], stopping["version"])
                self.register(binding["item_id"], binding["id"], replaces=writer["id"],
                              replaced_stopped=True, code=409)
                withdrawal = {"captain_id": self.captain["id"], "writer_id": writer["id"],
                              "reason": "Session stopped; PR effects reconciled"}
                self.ok(route + "/writer/withdraw", RUNNER, withdrawal, 409)
                ended = self.ok(route + "/writer/withdraw", RUNNER, dict(withdrawal, stopped=True))
                self.assertEqual((ended["state"]["stopping"], ended["state"]["pending_output"],
                                  ended["writer"]["state"]), (None, disposition, "ended"))
                closed = finalization.attempt(self.path, binding["id"])
                self.assertEqual(closed["state"]["status"], disposition)
                with store.read_transaction(self.path) as db:
                    markdown = intake.report(db, binding["item_id"], binding["id"])
                self.assertIn("Stop delivery", markdown)
                self.assertIn(PR_URL, markdown)
                self.ok("/v1/writer", token, code=401)

    def test_a_verified_delivery_merge_wins_over_a_pending_stop(self):
        for action in ("cancel", "close-failure"):
            with self.subTest(action=action):
                binding, token = self.publish("checked_in")
                binding = self.submit(token, binding, pr_url=PR_URL)
                route = f"/v1/items/{binding['item_id']}/executions/{binding['id']}"
                self.ok(route + "/" + action, OWNER, {
                    "expected_version": binding["version"], "reason": "Stop raced with merge",
                })
                merged = self.observe(binding, state="closed", merged=True, merge_commit=MERGE,
                                      merge_method="merge", merged_at="2026-11-24T21:00:00Z")
                self.assertEqual((merged["state"]["status"], merged["state"]["stopping"],
                                  merged["state"]["pending_output"]), ("completed", None, None))
                self.assertEqual(merged["state"]["final_output"]["merge_commit"], MERGE)
                self.assertEqual(merged["state"]["final_output"]["url"], PR_URL)
                self.assertEqual(self.observe(binding, state="closed", merged=True,
                                              merge_commit=MERGE, code=409)["version"], merged["version"])
                self.ok("/v1/writer", token, code=401)

    def test_stopped_delivery_rework_releases_only_its_own_allocation(self):
        other, other_token, _ = self.register(*self.item())
        binding, token = self.publish("checked_in")
        binding = self.submit(token, binding, pr_url=PR_URL)
        self.capacity(token, "building")
        self.capacity(token, "delivering", head=OTHER)
        self.capacity(token, "building")
        route = f"/v1/items/{binding['item_id']}/executions/{binding['id']}"
        self.ok(route + "/cancel", OWNER, {
            "expected_version": binding["version"], "reason": "Stop local PR rework"})
        withdrawal = {"captain_id": self.captain["id"], "writer_id": binding["writer"]["id"],
                      "reason": "Stopped rework and reconciled effects", "stopped": True}
        ended = self.ok(route + "/writer/withdraw", RUNNER, withdrawal)
        self.assertEqual(ended["state"]["pending_output"], "cancelled")
        # Finalization may advance the execution between withdrawal responses.
        closed = finalization.attempt(self.path, binding["id"])
        self.assertEqual(closed["state"]["status"], "cancelled")
        replay = self.ok(route + "/writer/withdraw", RUNNER, withdrawal)
        self.assertEqual(replay["state"]["status"], "cancelled")
        self.assertIsNone(replay["state"]["pending_output"])
        for response in (ended, replay):
            self.assertEqual(response["writer"]["id"], binding["writer"]["id"])
            self.assertEqual(response["writer"]["state"], "ended")
            self.assertIsNotNone(response["writer"]["ended_at"])
            self.assertFalse(response["writer"]["building"])
            self.assertIsNone(response["writer"]["handoff_head"])
        capacity = self.ok("/v1/dispatch/settings", OWNER)
        self.assertEqual((capacity["building_writers"], capacity["active_writers"]), (1, 1))
        current = self.ok("/v1/writer", other_token)
        self.assertEqual(current["writer"]["id"], other["writer"]["id"])
        self.assertTrue(current["writer"]["building"])
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

    def test_deployment_observations_refresh_flags_without_reopening_delivery(self):
        binding, token = self.publish("checked_in", target_branch="main")
        self.submit(token, binding, pr_url=PR_URL)
        path = f"/v1/items/{binding['item_id']}/executions/{binding['id']}"
        pull = {"number": 7, "state": "closed", "merged": True, "draft": False,
                "head_ref": item_branch(binding["item_id"]), "head_sha": HEAD, "base_ref": "main",
                "base_sha": BASE, "merge_commit": MERGE}
        body = {"pr": pull, "target_base": "main",
                "deployment": {"commit": MERGE, "check_name": "deploy", "state": "pending"}}

        def observe(**fields):
            return self.ok(path + "/github-observation", RUNNER,
                           dict(body, observed_at=datetime.now(UTC).isoformat(), **fields))

        # A pending deployment doesn't delay completion or keep a writer alive.
        observe()
        completed = self.ok(f"/v1/items/{binding['item_id']}", OWNER)["execution"]
        self.assertEqual(completed["state"]["status"], "completed")
        self.ok("/v1/writer", token, code=401)
        for state, kind in (("pending", "deployment_pending"), ("failed", "deployment_failed"),
                            ("deployed", "deployed"), ("pending", "deployment_pending")):
            body["deployment"]["state"] = state
            observe()
            self.assertFalse(observe()["recorded"])
            found = [flag for flag in self.ok("/v1/flags", OWNER)["flags"]
                     if flag["execution_id"] == binding["id"]]
            self.assertEqual([flag["kind"] for flag in found], [kind])
            self.assertIn(MERGE, found[0]["reason"])
            self.assertEqual(self.ok(f"/v1/items/{binding['item_id']}", OWNER)["execution"], completed)
            page = self.ok("/v1/operator/executions?kind=closed", OWNER)
            self.assertEqual((page["items"][0]["outcome"], page["items"][0]["delivery"]["merge_commit"]),
                             ("checked_in", MERGE))
            self.assertEqual(page["items"][0]["deployment"]["state"], state)
            with connect(self.path, mode="ro") as db:
                later = flags.all_flags(db, datetime.now(UTC) + DEPLOYMENT_WINDOW)["flags"]
            kinds = {flag["kind"] for flag in later if flag["execution_id"] == binding["id"]}
            self.assertNotIn("observation_stale", kinds)
            self.assertEqual(kinds, {"deployment_missing"} if state == "pending"
                             else {"deployment_failed"} if state == "failed" else set())
            if state == "pending":
                missing, = [flag for flag in later if flag["kind"] == "deployment_missing"]
                self.assertTrue(missing["notify_owner"])
        # A delayed older snapshot cannot restore a previous failed state.
        stale = dict(body, deployment=dict(body["deployment"], state="failed"),
                     observed_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat())
        self.ok(path + "/github-observation", RUNNER, stale)
        self.assertIn("deployment_pending", {flag["kind"] for flag in self.ok("/v1/flags", OWNER)["flags"]})
        # A closed execution only accepts its own delivered PR, head and merge commit.
        for fields, code in (
            ({"deployment": None}, 409),
            ({"pr": dict(pull, head_sha=OTHER)}, 409),
            ({"pr": dict(pull, number=8)}, 409),
            ({"pr": dict(pull, merge_commit=OTHER),
              "deployment": dict(body["deployment"], commit=OTHER)}, 409),
            ({"pr": dict(pull, merged=False)}, 422),
            ({"pr": dict(pull, base_ref="dev"), "target_base": "dev"}, 409),
        ):
            self.ok(path + "/github-observation", RUNNER,
                    dict(body, observed_at=datetime.now(UTC).isoformat(), **fields), code)
        self.ok("/v1/reconcilers/local/heartbeat", RUNNER, {"deployment_checks": {"repo": None}})
        self.assertFalse([flag for flag in self.ok("/v1/flags", OWNER)["flags"]
                          if flag["execution_id"] == binding["id"]])
        self.assertEqual(self.ok(f"/v1/items/{binding['item_id']}", OWNER)["execution"], completed)
        self.ok("/v1/reconcilers/local/heartbeat", RUNNER, {"deployment_checks": {"repo": None}})
        self.assertEqual(self.ok("/v1/operator/executions?kind=closed", OWNER)["items"][0]["deployment"]["state"],
                         "disabled")
        with connect(self.path, mode="ro") as db:
            later = flags.all_flags(db, datetime.now(UTC) + DEPLOYMENT_WINDOW)["flags"]
        self.assertFalse([flag for flag in later if flag["execution_id"] == binding["id"]])

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

    def test_closed_pr_asks_the_owner(self):
        binding, token = self.publish("checked_in")
        binding = self.submit(token, binding, pr_url=PR_URL)
        closed = self.observe(binding, state="closed")
        self.assertEqual((closed["state"]["status"], closed["state"]["wait"]["trigger"]),
                         ("waiting_for_input", "owner reply"))


    def test_completed_ready_merge_advances_once_and_releases_check_in_dependency(self):
        for head, ancestry in ((HEAD, {}), (OTHER, {"merged_head_ancestor": HEAD})):
            with self.subTest(head=head):
                binding, token = self.publish("ready_for_check_in")
                self.submit(token, binding, pr_url=PR_URL)
                ready = self.observe(binding)
                self.assertEqual(ready["state"]["status"], "completed")
                dependent, execution_id = self.pr_item(
                    "report", dependencies=[{"upstream_id": binding["item_id"], "milestone": "checked_in"}])
                waiting, dependent_token, _ = self.register(dependent, execution_id)
                self.assertIsNone(waiting["writer"]["attempt_id"])
                self.assertIn("verified head or a descendant", waiting["unmet_dependencies"][0]["reason"])
                self.assertIsNone(self.ok("/v1/writer/attempt", dependent_token,
                                          method="POST")["writer"]["attempt_id"])
                merge = dict(state="closed", merged=True, head_sha=head, merge_commit=MERGE,
                             merged_at="2026-11-24T06:33:57Z", merge_method="squash_or_rebase", **ancestry)
                done = self.observe(binding, **merge)
                self.assertEqual((done["state"]["status"], done["version"]),
                                 ("completed", ready["version"] + 1))
                output = done["state"]["final_output"]
                self.assertEqual((output["merge_commit"], output["revision"]["head"]), (MERGE, head))
                self.assertTrue(output["merged_at"].startswith("2026-11-24T06:33:57"))
                self.assertEqual(self.observe(binding, **merge)["version"], done["version"])
                self.ok("/v1/writer", token, code=401)
                eligible = self.ok("/v1/writer/attempt", dependent_token, method="POST")
                self.assertEqual(eligible["state"]["status"], "running")
                with connect(self.path, mode="ro") as db:
                    transitions = db.execute(
                        "SELECT count(*) FROM transition_history WHERE execution_id=? AND event='pr_observed'",
                        (binding["id"],)).fetchone()[0]
                self.assertEqual(transitions, 2)  # Ready completion, then merged enrichment.
                page = self.ok("/v1/operator/executions?kind=closed&outcome=checked_in", OWNER)
                row = next(row for row in page["items"] if row["id"] == binding["id"])
                self.assertEqual(row["delivery"]["merge_commit"], MERGE)

    def test_completed_ready_rejects_other_pr_and_ignores_unverified_merges(self):
        binding, token = self.publish("ready_for_check_in")
        self.submit(token, binding, pr_url=PR_URL)
        ready = self.observe(binding)
        merged = dict(state="closed", merged=True, merge_commit=MERGE, merged_at="2026-11-24T06:33:57Z")
        self.observe(binding, number=8, code=409, **merged)
        for pull in (
                {"state": "closed"},
                dict(merged, head_sha=OTHER),
                dict(merged, head_sha=OTHER, merged_head_ancestor=BASE),
                dict(merged, base_ref="dev"),
                dict(merged, merged_at=None),
                dict(merged, draft=True),
        ):
            with self.subTest(pull=pull):
                unchanged = self.observe(binding, **pull)
                self.assertEqual((unchanged["state"], unchanged["version"]), (ready["state"], ready["version"]))
        draft, token = self.publish("ready_for_check_in", publication_mode="draft")
        self.submit(token, draft, pr_url=PR_URL)
        self.assertEqual(self.observe(draft, draft=True)["state"]["status"], "completed")
        self.observe(draft, code=409, **merged)

    def test_draft_publication_verifies_github_without_releasing_ready_dependencies(self):
        for target in ("ready_for_check_in", "checked_in"):
            with self.subTest(target=target):
                binding, token = self.publish(target, publication_mode="draft", target_branch="main")
                dependent, execution_id = self.pr_item(
                    "report", dependencies=[{"upstream_id": binding["item_id"], "milestone": "ready_for_check_in"}])
                waiting, dependent_token, _ = self.register(dependent, execution_id)
                self.assertIsNone(waiting["writer"]["attempt_id"])
                binding = self.submit(token, binding, pr_url=PR_URL)
                self.assertEqual(binding["state"]["publication_mode"], "draft")
                self.assertIn("open draft", binding["state"]["wait"]["reason"])
                for pull, flag in (({}, "draft_required"), ({"draft": True, "base_ref": "dev"}, "wrong_base")):
                    state = self.observe(binding, **pull)["state"]
                    self.assertEqual(state["status"], "waiting_for_external")
                    self.assertEqual(state["candidate"], HEAD)
                    self.assertIsNone(state["ready_revision"])
                    self.assertIn(flag, {f["kind"] for f in self.ok("/v1/flags", OWNER)["flags"]
                                         if f["item_id"] == binding["item_id"]})
                # Draft-only publication does not wait for ready-only CI or mergeability.
                body = {"observed_at": datetime.now(UTC).isoformat(), "target_base": "main",
                        "pr": {"number": 7, "state": "open", "merged": False, "draft": True,
                               "head_ref": item_branch(binding["item_id"]), "head_sha": HEAD,
                               "base_ref": "main", "base_sha": BASE}}
                route = f"/v1/items/{binding['item_id']}/executions/{binding['id']}"
                self.ok(route + "/github-observation", RUNNER, body)
                state = self.ok(f"/v1/items/{binding['item_id']}", OWNER)["execution"]["state"]
                self.assertEqual((state["status"], state["final_output"]["draft"]), ("completed", True))
                self.assertIsNone(state["ready_revision"])
                self.ok("/v1/writer", token, code=401)
                self.assertIsNone(self.ok("/v1/writer/attempt", dependent_token, method="POST")["writer"]["attempt_id"])
                board = self.ok(f"/v1/operator/executions/{binding['id']}/timeline", OWNER)["item"]
                self.assertEqual((board["publication_mode"], board["outcome"]), ("draft", "draft_published"))
                self.assertIsNone(board["delivery"])

    def test_a_draft_push_after_self_review_is_reviewed_before_delivery(self):
        binding, token = self.publish("ready_for_check_in", publication_mode="draft", target_branch="main")
        binding = self.submit(token, binding, pr_url=PR_URL)
        self.observe(binding)
        # The writer sees its own execution's flags, so a draft_required flag can't stall unseen.
        self.assertIn("draft_required", {flag["kind"] for flag in self.ok("/v1/writer", token)["flags"]})
        state = self.observe(binding, draft=True, head_sha=OTHER)["state"]
        self.assertEqual((state["stage"], state["status"], state["candidate"]), ("self_review", "queued", OTHER))
        self.assertEqual(self.ok("/v1/writer", token)["flags"], [])
        # A replacement writer can pick up the re-review of a draft that is already published.
        binding, token, _ = self.register(binding["item_id"], binding["id"], replaces=binding["writer"]["id"],
                                          replaced_stopped=True)
        binding = self.submit(token, binding, reviewed_candidate=OTHER)
        binding = self.submit(token, binding, pr_url=PR_URL)
        state = self.observe(binding, draft=True, head_sha=OTHER)["state"]
        self.assertEqual((state["status"], state["candidate"], state["final_output"]["draft"]),
                         ("completed", OTHER, True))

    def test_draft_intake_survives_rerun(self):
        item_id, execution_id = self.pr_item("checked_in", publication_mode="draft")
        binding, token, _ = self.register(item_id, execution_id)
        binding = self.submit(token, binding, candidate=HEAD)
        binding = self.submit(token, binding, reviewed_candidate=HEAD)
        binding = self.submit(token, binding, pr_url=PR_URL)
        self.observe(binding, draft=True)
        rerun = self.ok(f"/v1/items/{item_id}/executions/{execution_id}/rerun", OWNER,
                        {"expected_version": self.version(item_id), "target": "checked_in"})
        self.assertEqual(rerun["state"]["publication_mode"], "draft")
        self.ok("/v1/items", OWNER, {"repository_id": "repo", "goal": "No PR", "target": "report",
                                    "publication_mode": "draft"}, 422)

    def test_compaction_counter_validation_and_omitted_telemetry(self):
        route = f"/v1/captains/{self.captain['id']}"
        body = {"charter_revision": "new-charter", "wake_count": 8, "state": "active"}
        self.assertIsNone(self.captain["compaction_count"])
        for invalid in (-1, True, 1.5, "2", 9007199254740992):
            with self.subTest(invalid=invalid):
                self.ok(route, RUNNER, dict(body, compaction_count=invalid), 422, method="PUT")
        for count, expected in ((None, None), (0, 0), (3, 3), (3, 3), (1, 3), (None, 3)):
            beat = self.ok(route, RUNNER, dict(body, compaction_count=count), method="PUT")
            self.assertEqual(beat["compaction_count"], expected)
            self.assertEqual((beat["charter_revision"], beat["wake_count"]), ("new-charter", 8))
        self.assertEqual(self.ok(route, RUNNER, body, method="PUT")["compaction_count"], 3)

    def test_unconfirmed_launch_wait_is_visible_without_claiming_native_execution(self):
        from fastapi import Response
        from starlette.requests import Request
        from phalanx.operator_api import SUMMARY, conditional_read, dispatch_context, summary

        item_id, execution_id = self.item()
        binding, token, _ = self.register(item_id, execution_id)
        writer = binding["writer"]
        created = datetime.fromisoformat(writer["created_at"])
        request = Request({"type": "http", "path": "/v1/operator/executions", "query_string": b"", "headers": []})
        etags = []
        for age in (timedelta(seconds=30), flags.LAUNCH_UNCONFIRMED - timedelta(microseconds=1),
                    flags.LAUNCH_UNCONFIRMED, timedelta(minutes=30)):
            with self.subTest(age=age), connect(self.path) as db:
                now = created + age
                response = Response()
                conditional_read(request, response, db, now)
                etags.append(response.headers["ETag"])
                row = db.execute(SUMMARY + " WHERE execution.id=?", (execution_id,)).fetchone()
                projected = summary(row, now, dispatch_context(db, now))
                overdue = age >= flags.LAUNCH_UNCONFIRMED
                self.assertEqual(projected["status"], "running")
                self.assertEqual(projected["writer"]["state"], "launching")
                self.assertEqual(projected["writer"]["nonce"], writer["nonce"])
                self.assertEqual(projected["wait_since"], writer["created_at"])
                self.assertEqual(projected["wait_age_seconds"], int(age.total_seconds()))
                self.assertIsNone(projected["next_action"])
                self.assertIn("recovery overdue" if overdue else "handoff pending", projected["wait_reason"])
                self.assertIn("not prove a native writer is running", projected["wait_reason"])
                for text in (writer["nonce"], writer["id"], "fresh full native inventory",
                             "no create is in flight", "same nonce", "retain the reservation"):
                    self.assertIn(text, projected["resume_trigger"])
                alerts = [entry for entry in flags.writer_flags(db, now)
                          if entry["kind"] == "launch_unconfirmed"]
                self.assertEqual(len(alerts), int(overdue))
                if overdue:
                    self.assertTrue(alerts[0]["notify_owner"])
                    self.assertEqual(alerts[0]["first_seen"],
                                     (created + flags.LAUNCH_UNCONFIRMED).isoformat(timespec="microseconds"))
                    self.assertEqual(alerts[0]["recovery"], projected["resume_trigger"])
        self.assertEqual(etags[0], etags[1])
        self.assertNotEqual(etags[1], etags[2])
        # Both HTTP views expose the reservation, but never call it native execution.
        for projected in (
                self.ok("/v1/operator/executions?kind=open", OWNER)["items"][0],
                self.ok(f"/v1/operator/items/{item_id}", OWNER)["item"]):
            self.assertIn("Native writer launch unconfirmed", projected["wait_reason"])
            self.assertIsNone(projected["next_action"])
        confirmed = self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER, {
            "captain_id": self.captain["id"], "writer_id": writer["id"], "session_id": "native"},
            method="PATCH")
        projected = self.ok(f"/v1/operator/items/{item_id}", OWNER)["item"]
        self.assertIsNone(projected["wait_reason"])
        self.assertIn("A worker is running", projected["next_action"])
        self.assertEqual(confirmed["writer"]["attempt_id"], writer["attempt_id"])
        self.assertEqual(self.ok("/v1/writer", token)["writer"]["id"], writer["id"])
        with connect(self.path) as db:
            self.assertEqual(flags.writer_flags(db, created + timedelta(minutes=30)), [])
            self.assertEqual(db.execute("SELECT count(*) FROM writers").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT building FROM writers").fetchone()[0], 1)

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

    def test_task_first_inventory_uses_full_nonce_not_duplicate_titles(self):
        first_item, first_execution = self.item()
        second_item, second_execution = self.item()
        first, _, _ = self.register(first_item, first_execution, nonce="abcdef" + "1" * 16)
        second, _, _ = self.register(second_item, second_execution, nonce="abcdef" + "2" * 16)
        name = shift.writer_session_name("Fix recovery")
        sessions = [
            {"id": "first", "name": name, "writer_nonce": first["writer"]["nonce"]},
            {"id": "second", "name": name, "writer_nonce": second["writer"]["nonce"]},
            {"id": "unknown", "name": name},
            {"id": "title-only", "name": f"phx {first_item[:8]} {first['writer']['nonce']} [phx]"},
            {"id": "ended", "name": f"phx {first_item[:8]} {first['writer']['nonce']}",
             "writer_nonce": "abcdef" + "3" * 16},
        ]

        def beat():
            return self.ok(f"/v1/captains/{self.captain['id']}", RUNNER, {
                "charter_revision": "c1", "wake_count": 1, "state": "active", "sessions": sessions},
                method="PUT")

        def found():
            return {(entry["kind"], entry["reason"].split()[1]): entry
                    for entry in self.ok("/v1/flags", OWNER)["flags"]
                    if entry["kind"] in ("orphan_session", "duplicate_writer")}

        reported = beat()["sessions"]
        self.assertEqual(reported[0]["writer_nonce"], first["writer"]["nonce"])
        self.assertEqual(set(found()), {
            ("orphan_session", "unknown"), ("orphan_session", "ended"), ("orphan_session", "title-only")})
        self.ok(f"/v1/items/{first_item}/executions/{first_execution}/writer", RUNNER, {
            "captain_id": self.captain["id"], "writer_id": first["writer"]["id"], "session_id": "first"},
            method="PATCH")
        sessions[0]["name"] = "Renamed without any marker"
        sessions[1]["name"] = "Also renamed"
        sessions.append({"id": "duplicate", "name": "Different task words",
                         "writer_nonce": first["writer"]["nonce"]})
        beat()
        result = found()
        self.assertEqual(set(result), {
            ("orphan_session", "unknown"), ("orphan_session", "ended"),
            ("orphan_session", "title-only"), ("duplicate_writer", "duplicate")})
        self.assertEqual(result["duplicate_writer", "duplicate"]["item_id"], first_item)
        self.assertEqual(result["duplicate_writer", "duplicate"]["execution_id"], first_execution)
        self.assertEqual(beat()["sessions"][0]["first_seen"], reported[0]["first_seen"])

    def test_new_inventory_metadata_supersedes_an_older_captains_name_only_report(self):
        item_id, execution_id = self.item()
        binding, _, _ = self.register(item_id, execution_id)
        name = "Fix recovery [phx]"
        older = self.ok(f"/v1/captains/{self.captain['id']}", RUNNER, {
            "charter_revision": "c1", "wake_count": 1, "state": "active",
            "sessions": [{"id": "lost-launch", "name": name}]}, method="PUT")
        successor = self.ok("/v1/captains", RUNNER, {
            "session_ref": "successor", "charter_revision": "c1"}, 201)
        self.ok(f"/v1/captains/{successor['id']}", RUNNER, {
            "charter_revision": "c1", "wake_count": 1, "state": "active",
            "sessions": [{"id": "lost-launch", "name": name, "writer_nonce": binding["writer"]["nonce"]}]},
            method="PUT")
        self.assertFalse(any(entry["kind"] in ("orphan_session", "duplicate_writer")
                             for entry in self.ok("/v1/flags", OWNER)["flags"]))
        self.ok(f"/v1/captains/{self.captain['id']}", RUNNER, {
            "charter_revision": "c1", "wake_count": 2, "state": "active",
            "sessions": [{"id": "lost-launch", "name": name}]}, method="PUT")
        self.assertFalse(any(entry["kind"] in ("orphan_session", "duplicate_writer")
                             for entry in self.ok("/v1/flags", OWNER)["flags"]))
        self.ok(f"/v1/items/{item_id}/executions/{execution_id}/writer", RUNNER, {
            "captain_id": self.captain["id"], "writer_id": binding["writer"]["id"], "session_id": "actual"},
            method="PATCH")
        duplicate = next(entry for entry in self.ok("/v1/flags", OWNER)["flags"]
                         if entry["kind"] == "duplicate_writer")
        self.assertEqual(duplicate["first_seen"], older["sessions"][0]["first_seen"])

    def test_held_action_requires_reason_and_appears_as_board_and_item_wait(self):
        item_id, execution_id = self.item()
        action = {"captain_id": self.captain["id"], "action": "held"}
        route = f"/v1/items/{item_id}/captain-actions"
        self.ok(route, RUNNER, action, 422)
        self.ok(route, RUNNER, dict(action, reason="  "), 422)
        self.ok(route, None, dict(action, reason="Charter lane order"), 401)
        held = self.ok(route, RUNNER, dict(action, reason="Charter lane order"), 201)
        self.assertEqual(held["version"], self.version(item_id))
        board = "/v1/operator/executions"
        timeline = f"{board}/{execution_id}/timeline"
        for row in (self.ok(board, OWNER)["items"][0], self.ok(timeline, OWNER)["item"]):
            self.assertEqual(row["wait_reason"], "Charter lane order")
            self.assertIsNone(row["next_action"])
        self.ok(route, RUNNER, dict(action, reason="Charter max_writers=1"), 201)
        self.assertEqual(self.ok(timeline, OWNER)["item"]["wait_reason"], "Charter max_writers=1")
        self.ok(route, RUNNER, dict(action, action="released", reason="Capacity available"), 201)
        self.assertIsNone(self.ok(timeline, OWNER)["item"]["wait_reason"])


class CaptainFlagTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)

    def kinds(self, now):
        with connect(self.path) as db:
            return {(flag["kind"], flag["source"]) for flag in flags.all_flags(db, now)["flags"]}

    def backlog(self, db):
        intake.register(db, "repo", "https://github.com/e/r.git")
        captain = captains.register(db, captains.CaptainRegistration(
            session_ref="chat", charter_revision="c1", pickup=captains.Pickup(repositories={"repo": None})))
        active = intake.create(db, Intake(repository_id="repo", goal="Building", target="report"))
        binding = writers.register(db, active["id"], active["execution"]["id"], writers.Registration(
            expected_version=active["execution"]["version"], captain_id=captain["id"],
            runtime="test", nonce=token_urlsafe(16), token_digest=intake.digest(token_urlsafe(32))))
        queued = intake.create(db, Intake(repository_id="repo", goal="Backlog", target="report"))
        db.execute("UPDATE executions SET ready_since=0 WHERE id=?", (queued["execution"]["id"],))
        dispatch.set_dispatch(db, DispatchSettings(max_writers=1, max_building_writers=1))
        return captain, binding, queued

    def test_active_overdue_strict_boundary_never_claimed_and_terminal_exclusion(self):
        with write_transaction(self.path) as db:
            _, binding, queued = self.backlog(db)
            started = datetime(2026, 1, 1, tzinfo=UTC)
            db.execute("UPDATE attempts SET started_at=? WHERE execution_id=?",
                       (started.isoformat(timespec="microseconds"), binding["id"]))
            for delta in (-timedelta(microseconds=1), timedelta(), timedelta(microseconds=1)):
                now = started + timedelta(minutes=90) + delta
                with self.subTest(delta=delta):
                    warnings = flags.active_overdue(db, now, [])
                    self.assertEqual(len(warnings), int(delta > timedelta()))
                    if warnings:
                        self.assertEqual(warnings[0]["execution_id"], binding["id"])
                        self.assertNotEqual(warnings[0]["execution_id"], queued["execution"]["id"])
                        self.assertIn("5400", warnings[0]["reason"])
                        self.assertEqual(warnings[0]["first_seen"],
                                         (started + timedelta(minutes=90)).isoformat(timespec="microseconds"))
                        self.assertIn("No concrete blocker", warnings[0]["reason"])
            for status in ("queued", "running", "waiting_for_input", "waiting_for_external",
                           "completed", "failed", "cancelled"):
                with self.subTest(status=status):
                    state = dict(binding["state"], status=status)
                    if status in ("completed", "failed", "cancelled"):
                        state["final_output"] = {"reference": "phalanx://reports/test"}
                    db.execute("UPDATE executions SET state_json=? WHERE id=?",
                               (intake.encode(state), binding["id"]))
                    warnings = flags.active_overdue(db, started + timedelta(hours=5), [])
                    self.assertEqual(len(warnings), int(status not in ("completed", "failed", "cancelled")))

    def test_capacity_backlog_uses_assigned_writers_and_keeps_fresh_holds_visible(self):
        with write_transaction(self.path) as db:
            captain, binding, queued = self.backlog(db)
            captains.record_action(db, queued["id"], captains.Action(
                captain_id=captain["id"], action="held", reason="Waiting for writer capacity"))
            for state in ("launching", "busy", "idle", "stopped", "error"):
                with self.subTest(state=state):
                    db.execute("UPDATE writers SET state=?", (state,))
                    (backlog,) = flags.captain_idle_with_work(db, datetime.now(UTC))
                    self.assertEqual(backlog["kind"], "queue_backlogged")
                    self.assertEqual(backlog["execution_id"], queued["execution"]["id"])
                    self.assertEqual(backlog["item_id"], queued["id"])
                    self.assertIn("Total capacity exhausted", backlog["reason"])
                    self.assertIn("Captain hold: Waiting for writer capacity", backlog["reason"])
                    self.assertFalse(backlog["notify_owner"])
            # A lower limit never implies an ended writer or resets the waiting age.
            dispatch.set_dispatch(db, DispatchSettings(max_writers=2, max_building_writers=2))
            second = intake.create(db, Intake(repository_id="repo", goal="Second writer", target="report"))
            writers.register(db, second["id"], second["execution"]["id"], writers.Registration(
                expected_version=second["execution"]["version"], captain_id=captain["id"],
                runtime="test", nonce=token_urlsafe(16), token_digest=intake.digest(token_urlsafe(32))))
            dispatch.set_dispatch(db, DispatchSettings(max_writers=1, max_building_writers=1))
            (lowered,) = flags.captain_idle_with_work(db, datetime.now(UTC))
            self.assertIn("Total 2/1", lowered["reason"])
            self.assertEqual(lowered["first_seen"], backlog["first_seen"])

    def test_queue_age_threshold_applies_to_backlog_and_overdue(self):
        with write_transaction(self.path) as db:
            _, _, queued = self.backlog(db)
            now = datetime.now(UTC)
            cutoff = int((now - flags.IDLE_WITH_WORK).timestamp() * 1000)
            for maximum, kind in ((1, "queue_backlogged"), (2, "captain_idle_with_work")):
                with self.subTest(maximum=maximum):
                    dispatch.set_dispatch(db, DispatchSettings(max_writers=maximum, max_building_writers=maximum))
                    db.execute("UPDATE executions SET ready_since=? WHERE id=?",
                               (cutoff + 1, queued["execution"]["id"]))
                    self.assertEqual(flags.captain_idle_with_work(db, now), [])
                    db.execute("UPDATE executions SET ready_since=? WHERE id=?",
                               (cutoff, queued["execution"]["id"]))
                    self.assertEqual(flags.captain_idle_with_work(db, now)[0]["kind"], kind)

    def test_owner_action_pickup_blockers_remain_visible_after_the_existing_queue_budget(self):
        from phalanx.operator_api import SUMMARY, dispatch_context, summary

        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/e/r.git")
            captain = captains.register(db, captains.CaptainRegistration(
                session_ref="chat", charter_revision="c1"))
            item = intake.create(db, Intake(repository_id="repo", goal="Queued intake", target="report"))
            now = datetime.now(UTC)
            cutoff = int((now - flags.IDLE_WITH_WORK).timestamp() * 1000)
            cases = (
                ("uninitialized", {}, ""),
                ("out_of_scope", {"other": None}, ""),
                ("repository_blocked", {"repo": "Owner trust approval required"}, ""),
                ("affinity", {"repo": None}, "assigned-runner"),
            )
            for code, repositories, affinity in cases:
                with self.subTest(code=code):
                    captains.heartbeat(db, captain["id"], captains.Heartbeat(
                        charter_revision="c1", wake_count=1, state="active",
                        pickup=captains.Pickup(repositories=repositories)))
                    db.execute("UPDATE executions SET ready_since=?, queue_affinity=? WHERE id=?",
                               (cutoff + 1, affinity, item["execution"]["id"]))
                    self.assertEqual(flags.captain_idle_with_work(db, now), [])
                    db.execute("UPDATE executions SET ready_since=? WHERE id=?",
                               (cutoff, item["execution"]["id"]))
                    (blocked,) = flags.all_flags(db, now)["flags"]
                    row = db.execute(SUMMARY + " WHERE execution.id=?", (item["execution"]["id"],)).fetchone()
                    board = summary(row, now, dispatch_context(db, now))
                    self.assertEqual(board["pickup"]["code"], code)
                    self.assertEqual(blocked["kind"], "pickup_blocked")
                    self.assertEqual(blocked["execution_id"], item["execution"]["id"])
                    self.assertEqual(blocked["reason"], board["wait_reason"])
                    self.assertEqual(blocked["recovery"], board["resume_trigger"])
                    self.assertTrue(blocked["notify_owner"])

    def test_slot_release_changes_backlog_to_overdue_and_assignment_clears_it_over_http(self):
        with write_transaction(self.path) as db:
            captain, binding, queued = self.backlog(db)
        self.start_server(self.path)
        status, page = self.request("/v1/flags", OWNER)
        self.assertEqual(status, 200)
        (backlog,) = page["flags"]
        self.assertEqual(backlog["kind"], "queue_backlogged")
        with write_transaction(self.path) as db:
            writers.release(db, writers.writer_row(db, binding["writer"]["id"]), "Slot released")
            # Keep the released execution out of this old-backlog assertion.
            db.execute("UPDATE executions SET ready_since=NULL WHERE id=?", (binding["id"],))
        status, page = self.request("/v1/flags", OWNER)
        self.assertEqual(status, 200)
        (overdue,) = page["flags"]
        self.assertEqual(overdue["kind"], "captain_idle_with_work")
        self.assertIn("Dispatch overdue", overdue["reason"])
        self.assertIn("0 of 1", overdue["reason"])
        self.assertEqual(overdue["first_seen"], backlog["first_seen"])
        status, _ = self.request(
            f"/v1/items/{queued['id']}/executions/{queued['execution']['id']}/writer", RUNNER, {
                "expected_version": queued["execution"]["version"], "captain_id": captain["id"],
                "runtime": "test", "nonce": token_urlsafe(16),
                "token_digest": intake.digest(token_urlsafe(32)),
            })
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/v1/flags", OWNER)[1]["flags"], [])

    def test_dependency_holds_and_new_work_are_not_missed_launches_and_pause_is_informational(self):
        with write_transaction(self.path) as db:
            captain, binding, queued = self.backlog(db)
            now = datetime.now(UTC)
            intake.create(db, Intake(repository_id="repo", goal="Just arrived", target="report"))
            db.execute("UPDATE executions SET dependency_wait_json='[]' WHERE id=?",
                       (queued["execution"]["id"],))
            self.assertEqual(flags.captain_idle_with_work(db, now), [])
            db.execute("UPDATE executions SET dependency_wait_json=NULL WHERE id=?",
                       (queued["execution"]["id"],))
            dispatch.set_dispatch(db, DispatchSettings(max_writers=2, max_building_writers=2))
            captains.record_action(db, queued["id"], captains.Action(
                captain_id=captain["id"], action="held", reason="Charter sequence"))
            self.assertEqual(flags.captain_idle_with_work(db, now), [])
            captains.record_action(db, queued["id"], captains.Action(
                captain_id=captain["id"], action="released", reason="Sequence clear"))
            self.assertEqual(flags.captain_idle_with_work(db, now)[0]["kind"], "captain_idle_with_work")
            dispatch.set_dispatch(db, DispatchSettings(paused=True))
            (paused,) = flags.captain_idle_with_work(db, now)
            self.assertEqual(paused["kind"], "queue_backlogged")
            self.assertEqual(paused["reason"], "Dispatch is paused; Building 1/2, Delivering 0, Total 1/2")

    def test_building_backlog_does_not_infer_availability_from_spare_total(self):
        with write_transaction(self.path) as db:
            _, _, queued = self.backlog(db)
            dispatch.set_dispatch(db, DispatchSettings(max_writers=10))
            (backlog,) = flags.captain_idle_with_work(db, datetime.now(UTC))
            self.assertEqual(backlog["kind"], "queue_backlogged")
            self.assertIn("Building capacity exhausted", backlog["reason"])
            self.assertIn("Total 1/10", backlog["reason"])
            dispatch.set_dispatch(db, DispatchSettings(max_building_writers=2))
            (overdue,) = flags.captain_idle_with_work(db, datetime.now(UTC))
            self.assertEqual(overdue["kind"], "captain_idle_with_work")
            self.assertEqual(overdue["execution_id"], queued["execution"]["id"])

    def test_captain_liveness_drives_server_flags(self):
        now = datetime.now(UTC)
        self.assertEqual(self.kinds(now), set())
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/e/r.git")
            intake.create(db, Intake(repository_id="repo", goal="Work", target="report"))
        self.assertEqual(self.kinds(now), {("no_live_captain", "server")})
        with write_transaction(self.path) as db:
            captain = captains.register(db, captains.CaptainRegistration(
                session_ref="chat", charter_revision="c1", pickup=captains.Pickup(repositories={"repo": None})))
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

    def test_deliberate_hold_suppresses_idle_only_while_current_fresh_and_live(self):
        from phalanx.operator_api import SUMMARY, dispatch_context, summary

        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/e/r.git")
            # A second live Captain keeps idle detection enabled when the holder goes stale/ends.
            captains.register(db, captains.CaptainRegistration(
                session_ref="other", charter_revision="c1", pickup=captains.Pickup(repositories={"repo": None})))
        for lapse in ("none", "expired", "stale", "ended", "version", "superseded", "ending_overdue"):
            with self.subTest(lapse=lapse), write_transaction(self.path) as db:
                item = intake.create(db, Intake(repository_id="repo", goal="Work", target="report"))
                execution_id = item["execution"]["id"]
                captain = captains.register(db, captains.CaptainRegistration(
                    session_ref=f"holder-{lapse}", charter_revision="c1"))
                now = datetime.now(UTC)
                db.execute("UPDATE executions SET ready_since=0 WHERE id=?", (execution_id,))
                self.assertIn(execution_id, {entry["execution_id"] for entry in flags.captain_idle_with_work(db, now)})
                with patch("phalanx.intake.timestamp", return_value=now.isoformat(timespec="microseconds")):
                    captains.record_action(db, item["id"], captains.Action(
                        captain_id=captain["id"], action="held", reason="Charter mutual exclusion"))
                if lapse == "expired":
                    db.execute("UPDATE captain_actions SET created_at=? WHERE execution_id=?",
                               ((now - flags.IDLE_WITH_WORK).isoformat(timespec="microseconds"), execution_id))
                elif lapse == "stale":
                    db.execute("UPDATE captains SET heartbeat_at=? WHERE id=?",
                               ((now - captains.STALE).isoformat(timespec="microseconds"), captain["id"]))
                elif lapse == "ended":
                    captains.heartbeat(db, captain["id"], captains.Heartbeat(
                        charter_revision="c1", wake_count=1, state="ended"))
                elif lapse == "version":
                    db.execute("UPDATE executions SET version=version+1 WHERE id=?", (execution_id,))
                elif lapse == "superseded":
                    # Equal timestamps still honor recording order, not random action IDs.
                    with patch("phalanx.intake.timestamp", return_value=now.isoformat(timespec="microseconds")):
                        captains.record_action(db, item["id"], captains.Action(
                            captain_id=captain["id"], action="released", reason="Lane open"))
                elif lapse == "ending_overdue":
                    db.execute("UPDATE captains SET state='ending', ending_at=? WHERE id=?",
                               ((now - captains.ENDING_LIMIT).isoformat(timespec="microseconds"), captain["id"]))
                idle = {entry["execution_id"] for entry in flags.captain_idle_with_work(db, now)}
                self.assertEqual(execution_id in idle, lapse != "none")
                row = db.execute(SUMMARY + " WHERE execution.id=?", (execution_id,)).fetchone()
                wait = summary(row, now, dispatch_context(db, now))["wait_reason"]
                self.assertEqual(wait, "Charter mutual exclusion" if lapse == "none" else None)

    def test_latest_hold_lookup_does_not_scan_unrelated_executions(self):
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/e/r.git")
            for index in range(1000):
                item = intake.create(db, Intake(repository_id="repo", goal=f"Work {index}", target="report"))
            captain = captains.register(db, captains.CaptainRegistration(
                session_ref="holder", charter_revision="c1"))
            now = datetime.now(UTC)
            for action, reason in (("held", "First hold"), ("released", "Released"), ("held", "New hold")):
                with patch("phalanx.intake.timestamp", return_value=now.isoformat(timespec="microseconds")):
                    captains.record_action(db, item["id"], captains.Action(
                        captain_id=captain["id"], action=action, reason=reason))
                steps = 0

                def budget():
                    nonlocal steps
                    steps += 1000
                    return steps > 5000

                db.set_progress_handler(budget, 1000)
                try:
                    expected = {item["execution"]["id"]: reason} if action == "held" else {}
                    self.assertEqual(captains.active_holds(db, now), expected)
                finally:
                    db.set_progress_handler(None, 0)

    def test_expired_hold_invalidates_board_cache_without_a_write(self):
        from fastapi import Response
        from starlette.requests import Request
        from phalanx.operator_api import conditional_read

        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/e/r.git")
            item = intake.create(db, Intake(repository_id="repo", goal="Work", target="report"))
            captain = captains.register(db, captains.CaptainRegistration(session_ref="chat", charter_revision="c1"))
            now = datetime.now(UTC)
            held_at = now - flags.IDLE_WITH_WORK + timedelta(seconds=1)
            with patch("phalanx.intake.timestamp", return_value=held_at.isoformat(timespec="microseconds")):
                captains.record_action(db, item["id"], captains.Action(
                    captain_id=captain["id"], action="held", reason="Charter writer cap"))
            request = Request({"type": "http", "path": "/v1/operator/executions", "query_string": b"", "headers": []})
            first, second = Response(), Response()
            conditional_read(request, first, db, now)
            conditional_read(request, second, db, now + timedelta(seconds=1))
            self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def previous_store(self, migration):
        """A store upgraded up to, but not including, the given migration."""
        path = self.path.with_name("previous.sqlite3")
        with write_transaction(path, create=True) as db:
            for version, statements in enumerate(store.MIGRATIONS[:store.MIGRATIONS.index(migration)], start=1):
                for statement in statements:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {version}")
        return path

    def test_upgrade_keeps_historical_compaction_counts_unknown(self):
        path = self.previous_store(CAPTAIN_COMPACTION_SCHEMA)
        with write_transaction(path) as db:
            now = intake.timestamp()
            db.execute("""INSERT INTO captains (id, session_ref, charter_revision, state, wake_count,
                              created_at, heartbeat_at) VALUES ('c', 'chat', 'c1', 'ended', 12, ?, ?)""",
                       (now, now))
        migrate(path)
        with connect(path) as db:
            captain = captains.view(db, captains.captain_row(db, "c"))
        self.assertIsNone(captain["compaction_count"])
        self.assertEqual((captain["state"], captain["wake_count"], captain["charter_revision"]),
                         ("ended", 12, "c1"))

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
            db.execute("""INSERT INTO captains (id, session_ref, charter_revision, state, created_at, heartbeat_at)
                          VALUES ('c', 'chat', 'c1', 'active', ?, ?)""",
                       (intake.timestamp(), intake.timestamp()))
            for name in ("closed", "open"):
                item = intake.create(db, Intake(repository_id="repo", goal=name, target="report"))
                db.execute("""INSERT INTO writers (id, execution_id, item_id, captain_id, runtime, nonce,
                                  token_digest, state, created_at) VALUES (?, ?, ?, ?, 'r', ?, ?, 'busy', ?)""",
                           (name, item["execution"]["id"], item["id"], "c", name, intake.digest(name),
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

    def test_upgrade_from_version_17_supports_archive_flags_and_shift_status(self):
        path = self.previous_store(SESSION_ARCHIVE_SCHEMA)
        with write_transaction(path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 17)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='session_archives'").fetchone())
            now = intake.timestamp()
            db.execute("""INSERT INTO captains (id, session_ref, charter_revision, state, created_at, heartbeat_at)
                          VALUES ('c', 'chat', 'c1', 'active', ?, ?)""", (now, now))
        migrate(path)
        with connect(path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], len(store.MIGRATIONS))
            self.assertEqual(db.execute("SELECT count(*) FROM session_archives").fetchone()[0], 0)
        self.start_server(path)

        def api(route):
            status, response = self.request(route, RUNNER)
            self.assertEqual(status, 200, response)
            return response

        self.assertEqual(api("/v1/flags")["flags"], [])
        status = shift.status(api, {"repositories": ["repo"], "shift": shift.SHIFT_DEFAULTS}, "c")
        self.assertEqual(status["flags"], [])
        self.assertEqual(status["captain"]["cleanup_pending"], 0)
        self.assertEqual(status["captain"]["ended_writers"], [])

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
