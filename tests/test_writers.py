from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from phalanx import captains, dispatch, flags, intake
from phalanx.dispatch_api import Claim, DispatchSettings
from phalanx.intake_models import Intake
from phalanx.store import connect, migrate, write_transaction

from test_bootstrap import OWNER, RUNNER, ServerFixture


STAGES = ["report_drafting", "report_review", "report_storage"]
ROOT = Path(__file__).resolve().parents[1]


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
                                             "session_id": "s-1", "session_url": "https://example.test/s-1"},
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
        self.assertEqual(self.old_loop_claim()["item_id"], item_id)

    def test_work_cli_reads_the_worktree_credential_and_asks_a_question(self):
        item_id, execution_id = self.item()
        _, token, _ = self.register(item_id, execution_id)
        worktree = self.path.parent / "worktree"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        git_path = subprocess.run(["git", "rev-parse", "--git-path", "phalanx-writer.json"], cwd=worktree,
                                  check=True, capture_output=True, text=True).stdout.strip()
        (worktree / git_path).write_text(json.dumps({"token": token, "base_url": self.url}), encoding="utf-8")

        def work(*arguments):
            result = subprocess.run([sys.executable, "-m", "phalanx", "work", *arguments], cwd=worktree,
                                    env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual(work("progress", "--note", "Outlining")["writer"]["progress_note"], "Outlining")
        asked = work("question", "Which audience?", "--handoff", "Outline drafted", "--option", "Ops")
        self.assertEqual(asked["state"]["status"], "waiting_for_input")
        self.assertEqual(asked["state"]["wait"]["reason"], "Which audience?")


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
