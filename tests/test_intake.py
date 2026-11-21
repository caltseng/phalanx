import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time
import unittest

from phalanx.finalization import BACKOFF_SECONDS, attempt
from phalanx.intake import apply, check_current, claim_attempt, digest
from phalanx.store import APPLICATION_ID, connect, write_transaction
from phalanx.transitions import Event, Wait

from test_bootstrap import OWNER, RUNNER, ServerFixture


LEASE = "x" * 32
REMOTE = "https://github.com/example/project.git"


class IntakeTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        # Start from a genuine migration-1 store.
        with connect(self.path, mode="rwc") as db:
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            db.execute("PRAGMA user_version = 1")
        self.server = self.start_server(self.path)

    def submit(self, path, body, *, token=OWNER, key=None, lease=None):
        headers = {}
        if key:
            headers["Idempotency-Key"] = key
        if lease:
            headers["Phalanx-Lease"] = lease
        return self.request(path, token, body, headers)

    def create(self, target="report"):
        self.assertEqual(self.submit("/v1/repositories", {"id": "example", "remote": REMOTE})[0], 200)
        body = {"repository_id": "example", "goal": "Review the project", "target": target}
        status, item = self.submit("/v1/items", body, key="first")
        self.assertEqual(status, 201, item)
        base = f"/v1/items/{item['id']}"
        execution = f"{base}/executions/{item['execution']['id']}"
        return item, body, base, execution

    def claim(self, item, execution, *, lease=LEASE):
        with write_transaction(self.path) as db:
            row, state = check_current(db, item["id"], execution["id"], execution["version"])
            claim = claim_attempt(
                db, row, state, runner_id="local", lease_digest=digest(lease),
                lease_expires_at=int(time()) + 3600,
            )
        updated = claim["execution"]
        return {"expected_version": updated["version"], "attempt_id": claim["attempt_id"],
                "stage": updated["state"]["stage"], "outcome": "succeeded"}

    def test_intake_restart_idempotency_auth_and_cli(self):
        self.assertEqual(self.submit("/v1/repositories", {"id": "example", "remote": REMOTE},
                                     token=RUNNER)[0], 403)
        item, body, base, execution = self.create()
        self.assertEqual(self.get(base)[0], 401)
        self.assertEqual(self.get(base, RUNNER), (200, item))
        self.assertEqual(self.submit("/v1/items", body, token=RUNNER, key="first")[0], 403)
        self.assertEqual(self.get(execution + "/timeline", OWNER)[1][0]["event"], "intake")

        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        self.assertEqual(self.submit("/v1/items", body, key="first"), (201, item))
        self.assertEqual(self.submit("/v1/items", dict(body, goal="Changed"), key="first")[0], 409)
        for command in (["inspect", item["id"]],
                        ["intake", "example", "Review the project", "--target", "report",
                         "--key", "first"]):
            cli = subprocess.run(
                [sys.executable, "-m", "phalanx", "--api-url", self.url, *command],
                env=dict(os.environ, PHALANX_OWNER_TOKEN=OWNER), text=True,
                capture_output=True, timeout=10,
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout), item)

    def test_report_review_loop_and_verified_output_survive_restart(self):
        item, _, base, route = self.create()
        path = route + "/result"
        claim = self.claim(item, item["execution"])
        self.assertEqual(self.submit(path, claim, token=RUNNER, lease=LEASE)[0], 409)
        body = dict(claim, report_markdown="# Draft 1\n")
        self.assertEqual(self.submit(path, body, lease=LEASE)[0], 403)
        self.assertEqual(self.submit(path, body, token=RUNNER, lease="z" * 32)[0], 409)
        self.assertEqual(self.submit(path, dict(body, stage="report_review"),
                                     token=RUNNER, lease=LEASE)[0], 409)
        status, reviewing = self.submit(path, body, token=RUNNER, lease=LEASE, key="draft")
        self.assertEqual(status, 200, reviewing)
        revision = reviewing["state"]["report_draft"]
        self.assertEqual(self.get(route + "/drafts/" + revision, RUNNER), (200, "# Draft 1\n"))
        self.assertEqual(self.submit(path, body, token=RUNNER, lease=LEASE)[0], 409)
        self.assertEqual(self.submit(path, body, token=RUNNER, lease=LEASE, key="draft"), (200, reviewing))

        claim = self.claim(item, reviewing)
        bad_review = dict(claim, report_revision="0" * 64)
        self.assertEqual(self.submit(path, bad_review, token=RUNNER, lease=LEASE)[0], 409)
        status, redraft = self.submit(path, dict(claim, outcome="changes_required", report_revision=revision),
                                     token=RUNNER, lease=LEASE)
        self.assertEqual(status, 200, redraft)
        self.assertEqual(redraft["state"]["stage"], "report_drafting")
        claim = self.claim(item, redraft)
        status, reviewing = self.submit(path, dict(claim, report_markdown="# Final\n"),
                                       token=RUNNER, lease=LEASE)
        self.assertEqual(status, 200, reviewing)
        revision = reviewing["state"]["report_draft"]
        claim = self.claim(item, reviewing)
        status, storage = self.submit(path, dict(claim, report_revision=revision), token=RUNNER, lease=LEASE)
        self.assertEqual(status, 200, storage)
        claim = self.claim(item, storage)
        self.assertEqual(self.submit(path, claim, token=RUNNER, lease=LEASE)[0], 409)
        self.assertEqual(self.get(route + "/report", OWNER)[0], 404)
        # Reconcile a matching artifact already present from an interrupted write.
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO reports VALUES (?, ?)", (storage["id"], "# Final\n"))
            db.execute("""CREATE TRIGGER no_duplicate_report BEFORE INSERT ON reports
                          BEGIN SELECT RAISE(ABORT, 'duplicate report write'); END""")
        status, complete = self.submit(path, dict(claim, report_revision=revision), token=RUNNER, lease=LEASE)
        self.assertEqual(status, 200, complete)
        self.assertEqual(complete["state"]["status"], "completed")
        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        self.assertEqual(self.get(base, OWNER)[1]["execution"], complete)
        self.assertEqual(self.get(complete["state"]["final_output"]["reference"], OWNER), (200, "# Final\n"))

    def test_pr_result_rejects_fabricated_provider_evidence_and_expired_lease(self):
        item, _, base, route = self.create("ready_for_check_in")
        path = route + "/result"
        claim = self.claim(item, item["execution"])
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET lease_expires_at=0 WHERE id=?", (claim["attempt_id"],))
        body = dict(claim, candidate="abc")
        self.assertEqual(self.submit(path, body, token=RUNNER, lease=LEASE)[0], 409)
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET lease_expires_at=? WHERE id=?",
                       (int(time()) + 3600, claim["attempt_id"]))
        status, publication = self.submit(path, body, token=RUNNER, lease=LEASE)
        self.assertEqual(status, 200, publication)
        claim = self.claim(item, publication)
        fake_pr = {"url": "https://github.com/example/project/pull/1", "draft": True,
                   "revision": {"head": "abc", "base": "def"}}
        self.assertEqual(self.submit(path, dict(claim, pr=fake_pr), token=RUNNER, lease=LEASE)[0], 422)
        self.assertEqual(self.submit(path, claim, token=RUNNER, lease=LEASE)[0], 409)
        active = self.get(base, OWNER)[1]["execution"]["state"]
        self.assertEqual(active["status"], "running")
        self.assertIsNone(active["primary_pr"])
        self.assertIsNone(active["final_output"])

    def test_cancel_active_rerun_and_preserve_history(self):
        item, _, base, route = self.create()
        claim = self.claim(item, item["execution"])
        cancellation = {"expected_version": claim["expected_version"], "reason": "Scope changed"}
        self.assertEqual(self.submit(route + "/cancel", cancellation, token=RUNNER)[0], 403)
        status, stopping = self.submit(route + "/cancel", cancellation, key="cancel")
        self.assertEqual(status, 200, stopping)
        self.assertEqual(stopping["state"]["stopping"], "cancelled")
        self.assertIsNone(stopping["state"]["final_output"])
        self.assertEqual(self.submit(route + "/cancel", cancellation, key="cancel"), (200, stopping))
        self.assertEqual(self.submit(route + "/result", dict(claim, report_markdown="# Late"),
                                     token=RUNNER, lease=LEASE)[0], 409)
        stop = {field: claim[field] for field in ("attempt_id", "stage")}
        stop["expected_version"] = stopping["version"]
        status, cancelled = self.submit(route + "/stop-confirmed", stop, token=RUNNER, lease=LEASE)
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["state"]["status"], "cancelled")
        report = self.get(route + "/report", RUNNER)
        self.assertEqual(report[0], 200)
        self.assertIn("Scope changed", report[1])
        rerun = {"expected_version": cancelled["version"], "target": "report"}
        status, replacement = self.submit(route + "/rerun", rerun, key="rerun")
        self.assertEqual(status, 200, replacement)
        self.assertEqual(self.submit(route + "/rerun", rerun, key="rerun"), (200, replacement))
        self.assertEqual(replacement["previous_id"], cancelled["id"])
        self.assertEqual(self.get(base + "/executions", OWNER)[1], [cancelled, replacement])
        self.assertEqual([event["event"] for event in self.get(route + "/timeline", OWNER)[1]],
                         ["intake", "claim", "cancel", "stop_confirmed", "finalize"])
        self.assertEqual(self.submit(route + "/rerun", rerun)[0], 409)

    def test_cancel_before_first_claim(self):
        _, _, _, route = self.create()
        status, closed = self.submit(route + "/cancel", {"expected_version": 1, "reason": "Withdrawn"})
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"]["status"], "cancelled")
        self.assertIn("No stages claimed", self.get(route + "/report", OWNER)[1])

    def test_cancel_passive_report_wait_without_an_open_attempt(self):
        item, _, _, route = self.create()
        claim = self.claim(item, item["execution"])
        with write_transaction(self.path) as db:
            row, state = check_current(db, item["id"], item["execution"]["id"],
                                       claim["expected_version"])
            waiting = apply(db, row, state, Event.EXTERNAL_WAIT, details={},
                            attempt_id=claim["attempt_id"], wait=Wait("Storage wait", "recheck"))
        status, closed = self.submit(
            route + "/cancel", {"expected_version": waiting["version"], "reason": "Withdrawn"},
        )
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"]["status"], "cancelled")
        self.assertIn("report_drafting", self.get(route + "/report", OWNER)[1])
        self.assertEqual([event["event"] for event in self.get(route + "/timeline", OWNER)[1]],
                         ["intake", "claim", "external_wait", "cancel", "stop_confirmed", "finalize"])

    def test_cancel_storage_error_rolls_back_and_recovers_after_restart(self):
        item, _, base, route = self.create()
        execution_id = item["execution"]["id"]
        with write_transaction(self.path) as db:
            # Fail after inserting the report, at the terminal timeline write.
            db.execute("""CREATE TRIGGER terminal_write_outage BEFORE INSERT ON transition_history
                          WHEN json_extract(NEW.state_json, '$.status') = 'cancelled'
                          BEGIN SELECT RAISE(ABORT, 'simulated outage'); END""")
        body = {"expected_version": 1, "reason": "Withdrawn during outage"}
        status, pending = self.submit(route + "/cancel", body, key="cancel")
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending["state"]["pending_output"], "cancelled")
        self.assertEqual(pending["state"]["status"], "waiting_for_external")
        self.assertIsNone(pending["state"]["final_output"])
        self.assertEqual(self.get(route + "/report", OWNER)[0], 404)

        self.stop_server(self.server)
        with write_transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
            db.execute("DROP TRIGGER terminal_write_outage")
            db.execute("UPDATE report_finalizations SET due_at=0 WHERE execution_id=?", (execution_id,))
        self.server = self.start_server(self.path)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            closed = self.get(base, OWNER)[1]["execution"]
            if closed["state"]["status"] == "cancelled":
                break
            sleep(0.05)
        self.assertEqual(closed["state"]["status"], "cancelled")
        self.assertEqual(self.submit(route + "/cancel", body, key="cancel"), (200, closed))
        self.assertIn("Withdrawn during outage", self.get(route + "/report", OWNER)[1])
        with connect(self.path, mode="ro") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reports").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM report_finalizations").fetchone()[0], 0)

    def test_failed_attempt_releases_slot_and_stalled_storage_can_be_retried(self):
        item, _, _, route = self.create("checked_in")
        claim = self.claim(item, item["execution"])
        result = dict(claim, outcome="failed", wait={"reason": "Build unavailable", "trigger": "owner reply"})
        status, waiting = self.submit(route + "/result", result, token=RUNNER, lease=LEASE)
        self.assertEqual(status, 200, waiting)
        body = {"expected_version": waiting["version"], "reason": "Build remains unavailable"}
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO reports VALUES (?, ?)", (waiting["id"], "# Conflicting report"))
        self.assertEqual(self.submit(route + "/close-failure", body, token=RUNNER)[0], 403)
        status, pending = self.submit(route + "/close-failure", body)
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending["state"]["pending_output"], "failed")
        self.stop_server(self.server)
        for _ in BACKOFF_SECONDS:
            with write_transaction(self.path) as db:
                db.execute("UPDATE report_finalizations SET due_at=0 WHERE execution_id=?", (pending["id"],))
            stalled = attempt(self.path, pending["id"])
        self.assertIn("stalled after 5 failures", stalled["state"]["wait"]["reason"])
        self.assertEqual(stalled["state"]["wait"]["trigger"], "owner retry finalization")
        self.assertIsNone(stalled["state"]["final_output"])
        self.assertEqual(attempt(self.path, pending["id"]), stalled)

        with write_transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT markdown FROM reports").fetchone()[0], "# Conflicting report")
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE ended_at IS NULL").fetchone()[0], 0)
            db.execute("DELETE FROM reports WHERE execution_id=?", (pending["id"],))
        self.server = self.start_server(self.path)
        retry = {"expected_version": stalled["version"]}
        self.assertEqual(self.submit(route + "/retry-finalization", retry, token=RUNNER)[0], 403)
        status, closed = self.submit(route + "/retry-finalization", retry)
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"]["status"], "failed")
        report = self.get(route + "/report", OWNER)[1]
        for expected in ("# Failure report", "development (1)", "Build remains unavailable", "Next action"):
            self.assertIn(expected, report)
