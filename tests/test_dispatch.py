from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from phalanx import dispatch, finalization, intake
from phalanx.attended_recovery import acknowledge_stop, reconcile
from phalanx.dispatch_api import Claim, DispatchSettings, ReconciledStop
from phalanx.intake_models import Cancel, Intake, Result
from phalanx.store import MIGRATIONS, connect, migrate, write_transaction

from test_bootstrap import OWNER, RUNNER, ServerFixture
from timing import WAIT_SECONDS


NOW = 1_800_000_000
LEASE = "x" * 32
CLAIM_PATH = "/v1/dispatch/claim"


class DispatchTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)

    def seed(self, repo, work):
        with write_transaction(self.path) as db:
            intake.register(db, repo, f"https://github.com/example/{repo}.git")
            ids = []
            for priority, seconds, affinity in work:
                item = intake.create(db, Intake(repository_id=repo, goal="Test queue",
                                                target="report", priority=priority,
                                                runner_affinity=affinity))
                db.execute("UPDATE executions SET ready_since=? WHERE item_id=?",
                           ((NOW + seconds) * 1000, item["id"]))
                ids.append(item["id"])
            return ids

    def claim(self, runner_id, repositories, *, lease=None, stages=None):
        body = Claim(runner_id=runner_id, repository_ids=repositories, stages=stages)
        with write_transaction(self.path) as db:
            return dispatch.claim(db, body, intake.digest(lease or token_urlsafe(32)))

    def test_aging_stable_fifo_and_capability_filters(self):
        low, normal, urgent_a, urgent_b, other, future, pinned = self.seed("repo", [
            ("low", -600, None), ("normal", -300, None), ("urgent", -10, None),
            ("urgent", -10, None), ("urgent", -700, "other"), ("urgent", 1, None),
            ("urgent", -20, "local"),
        ])
        self.seed("elsewhere", [("urgent", -800, None)])
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", 10)
        with patch.object(dispatch, "time", return_value=NOW):
            actual = [self.claim("local", ["repo"])["item_id"] for _ in range(5)]
            self.assertEqual(actual, [low, normal, pinned, *sorted((urgent_a, urgent_b))])
            self.assertEqual(self.claim("local", ["repo"])["status"], "no_eligible_work")
            self.assertEqual(self.claim("other", ["repo"])["item_id"], other)
        with patch.object(dispatch, "time", return_value=NOW + 1):
            self.assertEqual(self.claim("local", ["repo"])["item_id"], future)

    def test_attended_stage_filter_skips_later_work_and_preserves_it_for_other_runners(self):
        supported = ["development", "report_drafting"]
        first, second = self.seed("repo", [("normal", -10, None), ("normal", -1, None)])
        with patch.object(dispatch, "time", return_value=NOW), patch.object(intake, "time", return_value=NOW):
            with write_transaction(self.path) as db:
                pr = intake.create(db, Intake(repository_id="repo", goal="Update docs",
                                              target="ready_for_check_in", priority="urgent"))
                db.execute("UPDATE executions SET ready_since=? WHERE item_id=?", (NOW * 1000, pr["id"]))
            assignment = self.claim("local", ["repo"], lease=LEASE, stages=supported)
            self.assertEqual(assignment["item_id"], pr["id"])
            with write_transaction(self.path) as db:
                next_stage = intake.result(db, pr["id"], assignment["execution"]["id"], Result(
                    expected_version=assignment["execution"]["version"],
                    attempt_id=assignment["attempt_id"], stage="development", outcome="succeeded",
                    candidate="a" * 40,
                ), intake.digest(LEASE))
                self.assertEqual(next_stage["state"]["stage"], "pr_creation")
            for item_id in (first, second):
                lease = token_urlsafe(32)
                assignment = self.claim("local", ["repo"], stages=supported, lease=lease)
                self.assertEqual(assignment["item_id"], item_id)
                with write_transaction(self.path) as db:
                    report = intake.result(db, item_id, assignment["execution"]["id"], Result(
                        expected_version=assignment["execution"]["version"],
                        attempt_id=assignment["attempt_id"], stage="report_drafting",
                        outcome="succeeded", report_markdown="# Draft",
                    ), intake.digest(lease))
                    self.assertEqual(report["state"]["stage"], "report_review")
            self.assertEqual(self.claim("local", ["repo"], stages=supported)["status"], "no_eligible_work")
            with connect(self.path) as db:
                for item_id in (pr["id"], first, second):
                    self.assertEqual(intake.inspect(db, item_id)["execution"]["state"]["status"], "queued")
            self.assertEqual(self.claim("publisher", ["repo"], stages=["pr_creation"])["item_id"], pr["id"])

    def test_concurrent_claims_never_duplicate_or_exceed_capacity(self):
        self.seed("repo", [("normal", -1, None)] * 10)
        barrier = Barrier(16)

        def contender(number):
            barrier.wait(timeout=WAIT_SECONDS)
            return self.claim("even" if number % 2 == 0 else "odd", ["repo"])

        with patch.object(dispatch, "time", return_value=NOW):
            with ThreadPoolExecutor(16) as pool:
                results = list(pool.map(contender, range(16)))
        claimed = [result for result in results if result["status"] == "claimed"]
        self.assertEqual(len(claimed), 8)
        self.assertEqual(len({result["item_id"] for result in claimed}), 8)
        for runner_id in ("even", "odd"):
            self.assertEqual(sum(row["runner_id"] == runner_id for row in claimed), 4)
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM request_receipts").fetchone()[0], 8)

    def test_pause_preserves_claims_and_lower_capacity_takes_effect(self):
        self.seed("repo", [("normal", -1, None)] * 3)
        with patch.object(dispatch, "time", return_value=NOW):
            assignment = self.claim("local", ["repo"], lease=LEASE)
            with write_transaction(self.path) as db:
                dispatch.set_capacity(db, "local", 1)
                dispatch.set_dispatch(db, DispatchSettings(paused=True))
            self.assertEqual(self.claim("local", ["repo"])["status"], "paused")
            with write_transaction(self.path) as db:
                dispatch.renew(db, "local", assignment["attempt_id"], intake.digest(LEASE))
                dispatch.set_dispatch(db, DispatchSettings(paused=False))
            self.assertEqual(self.claim("local", ["repo"])["status"], "at_capacity")
            self.assertEqual(self.claim("other", ["repo"])["status"], "claimed")

    def test_expiry_preserves_reservation_and_reconciled_retry_ages_from_due(self):
        item_id, = self.seed("repo", [("low", -1000, None)])
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", 1)
        with patch.object(dispatch, "time", return_value=NOW):
            claimed = self.claim("local", ["repo"], lease=LEASE)
        attempt_id = claimed["attempt_id"]
        execution = claimed["execution"]
        body = ReconciledStop(item_id=item_id, expected_version=execution["version"],
                              reconciliation="Worker stopped and no pending outputs")
        expires = claimed["lease_expires_at"]
        with patch.object(dispatch, "time", return_value=expires - 1):
            with self.assertRaisesRegex(intake.Conflict, "still live"):
                with write_transaction(self.path) as db:
                    dispatch.recover_stopped(db, "local", attempt_id, body)
        with patch.object(dispatch, "time", return_value=expires):
            self.assertEqual(self.claim("local", ["repo"])["status"], "at_capacity")
            self.assertEqual(self.claim("other", ["repo"])["status"], "no_eligible_work")
            with write_transaction(self.path) as db:
                self.assertEqual(dispatch.recovery_candidates(db, "local")[0]["attempt_id"], attempt_id)
                self.assertEqual(dispatch.recovery_candidates(db, "other"), [])
                with self.assertRaisesRegex(intake.Conflict, "expired"):
                    dispatch.renew(db, "local", attempt_id, intake.digest(LEASE))
                with patch.object(intake, "time", return_value=expires):
                    with self.assertRaisesRegex(intake.Conflict, "no longer current"):
                        intake.result(db, item_id, execution["id"], Result(
                            expected_version=execution["version"], attempt_id=attempt_id,
                            stage=execution["state"]["stage"], outcome="succeeded",
                            report_markdown="# Stale\n",
                        ), intake.digest(LEASE))
                recovered = dispatch.recover_stopped(db, "local", attempt_id, body)
                self.assertEqual(recovered["retry_at_ms"], (expires + 5) * 1000)
                self.assertEqual(recovered["ready_since_ms"], recovered["retry_at_ms"])
            self.assertEqual(self.claim("local", ["repo"])["status"], "no_eligible_work")
        with patch.object(dispatch, "time", return_value=expires + 5):
            replacement = self.claim("local", ["repo"])
        self.assertEqual(replacement["item_id"], item_id)
        self.assertNotEqual(replacement["attempt_id"], attempt_id)

    def test_lease_and_result_http_replay_after_restart(self):
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/example/repo.git")
            item = intake.create(db, Intake(repository_id="repo", goal="Test HTTP", target="report"))
        server = self.start_server(self.path)
        body = {"runner_id": "local", "repository_ids": ["repo"],
                "stages": ["development", "report_drafting"]}
        headers = {"Phalanx-Lease": LEASE}
        self.assertEqual(self.request(CLAIM_PATH, None, body, headers)[0], 401)
        self.assertEqual(self.request(CLAIM_PATH, OWNER, body, headers)[0], 403)
        self.assertEqual(self.request(CLAIM_PATH, RUNNER, body)[0], 422)
        status, assigned = self.request(CLAIM_PATH, RUNNER, body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(assigned["status"], "claimed")
        self.stop_server(server)
        self.start_server(self.path)
        self.assertEqual(self.request(CLAIM_PATH, RUNNER, body, headers), (200, assigned))
        self.assertEqual(self.request(CLAIM_PATH, RUNNER, dict(body, slots=1), headers)[0], 409)
        self.assertEqual(self.request(CLAIM_PATH, RUNNER, dict(body, stages=["report_drafting"]), headers)[0], 409)
        reordered = dict(body, stages=list(reversed(body["stages"])))
        self.assertEqual(self.request(CLAIM_PATH, RUNNER, reordered, headers), (200, assigned))
        renew_path = f"/v1/dispatch/runners/local/attempts/{assigned['attempt_id']}/renew"
        self.assertEqual(self.request(renew_path, RUNNER, {}, {"Phalanx-Lease": "y" * 32})[0], 409)
        status, renewed = self.request(renew_path, RUNNER, {}, headers)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(renewed["lease_expires_at"], assigned["lease_expires_at"])
        for worker in ({"reference": "w", "url": "http://example.test/chat"},
                       {"reference": "w", "url": "https://user@example.test/chat"}, {"reference": ""}):
            self.assertEqual(self.request(renew_path, RUNNER, {"worker": worker}, headers)[0], 422, worker)
        worker = {"reference": "session-7", "url": "https://example.test/chat/7"}
        self.assertEqual(self.request(renew_path, RUNNER, {"worker": worker}, headers)[0], 200)
        board = self.get(f"/v1/operator/executions/{assigned['execution']['id']}/timeline", OWNER)[1]
        self.assertEqual((board["item"]["worker_reference"], board["item"]["worker_url"]),
                         ("session-7", "https://example.test/chat/7"))
        timeline = f"/v1/operator/executions/{assigned['execution']['id']}/timeline"
        blocker = "RunnerError: Native discovery has not established one exact writer; reconcile this reservation"
        self.assertEqual(self.request(renew_path, RUNNER, {"worker": worker, "blocker": blocker}, headers)[0], 200)
        self.assertEqual(self.get(timeline, OWNER)[1]["item"]["wait_reason"], blocker)
        self.assertEqual(self.request(renew_path, RUNNER, {"worker": worker}, headers)[0], 200)
        self.assertIsNone(self.get(timeline, OWNER)[1]["item"]["wait_reason"])
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET lease_expires_at = 0 WHERE id = ?", (assigned["attempt_id"],))
        expired = self.get(timeline, OWNER)[1]["item"]
        self.assertEqual(expired["wait_reason"], "Lease expired at 1970-01-01T00:00:00+00:00; awaiting runner recovery")
        self.assertIn("runner recovers", expired["resume_trigger"])
        wait_path = f"/v1/dispatch/runners/local/attempts/{assigned['attempt_id']}/recovery-wait"
        owner_wait = "Recovery is waiting on the owner: X. Then run `python -m phalanx attended-recover --slot 1 --recheck`"
        self.assertEqual(self.request(wait_path, RUNNER, {"blocker": owner_wait})[0], 200)
        self.assertEqual(self.get(timeline, OWNER)[1]["item"]["wait_reason"],
                         f"Lease expired at 1970-01-01T00:00:00+00:00; {owner_wait}")
        self.assertEqual(self.request(wait_path, RUNNER, {"blocker": None})[0], 200)
        self.assertIn("awaiting runner recovery", self.get(timeline, OWNER)[1]["item"]["wait_reason"])
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET lease_expires_at = ? WHERE id = ?",
                       (assigned["lease_expires_at"], assigned["attempt_id"]))
        self.assertEqual(self.request(wait_path, RUNNER, {"blocker": owner_wait})[0], 409)
        execution = assigned["execution"]
        result_path = f"/v1/items/{item['id']}/executions/{execution['id']}/result"
        result = {"expected_version": execution["version"], "attempt_id": assigned["attempt_id"],
                  "stage": execution["state"]["stage"], "outcome": "succeeded",
                  "report_markdown": "# Draft\n"}
        status, accepted = self.request(result_path, RUNNER, result, headers)
        self.assertEqual(status, 200)
        self.assertEqual(accepted["state"]["stage"], "report_review")
        self.assertIsNotNone(accepted["ready_since_ms"])
        self.assertEqual(self.request(renew_path, RUNNER, {}, headers)[0], 409)

    def test_recover_claim_http_adopts_expired_attempt_and_fences_old_lease(self):
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/example/repo.git")
            item = intake.create(db, Intake(repository_id="repo", goal="Test adoption", target="report"))
        self.start_server(self.path)
        old, new = {"Phalanx-Lease": LEASE}, {"Phalanx-Lease": "n" * 32}
        status, assigned = self.request(CLAIM_PATH, RUNNER, {"runner_id": "local", "repository_ids": ["repo"]}, old)
        self.assertEqual(status, 200)
        attempt, execution = assigned["attempt_id"], assigned["execution"]
        prefix = f"/v1/dispatch/runners/local/attempts/{attempt}"
        body = {"item_id": item["id"], "expected_version": execution["version"],
                "reconciliation": "Exact writer still active"}
        claim = new | {"Idempotency-Key": "adopt-1"}
        self.assertEqual(self.request(prefix + "/recover-claim", RUNNER, body, claim)[0], 409)
        with connect(self.path) as db:
            db.execute("UPDATE attempts SET lease_expires_at=0 WHERE id=?", (attempt,))
            db.commit()
        stale = body | {"expected_version": execution["version"] + 1}
        self.assertEqual(self.request(prefix + "/recover-claim", RUNNER, stale, new)[0], 409)
        status, adopted = self.request(prefix + "/recover-claim", RUNNER, body, claim)
        self.assertEqual(status, 200)
        self.assertEqual((adopted["attempt_id"], adopted["execution_id"], adopted["version"]),
                         (attempt, execution["id"], execution["version"]))
        self.assertEqual(self.request(prefix + "/recover-claim", RUNNER, body, claim), (200, adopted))
        timeline = self.get(f"/v1/items/{item['id']}/executions/{execution['id']}/timeline", OWNER)[1]
        adoptions = [event for event in timeline if event["event"] == "recover_claim"]
        self.assertEqual(len(adoptions), 1)
        self.assertEqual(adoptions[0]["details"], {"runner_id": "local",
                                                   "reconciliation": "Exact writer still active"})
        board = self.get(f"/v1/operator/executions/{execution['id']}/timeline", OWNER)[1]
        self.assertEqual((board["events"][0]["event"], board["events"][0]["reason"]),
                         ("recover_claim", "Adopted the attempt: Exact writer still active"))
        other = {"Phalanx-Lease": "o" * 32, "Idempotency-Key": "adopt-1"}
        self.assertEqual(self.request(prefix + "/recover-claim", RUNNER, body, other)[0], 409)
        self.assertEqual(self.request(prefix + "/recover-claim", RUNNER, body, new)[0], 409)
        self.assertEqual(self.request(prefix + "/renew", RUNNER, {}, old)[0], 409)
        self.assertEqual(self.request(prefix + "/renew", RUNNER, {}, new)[0], 200)
        with connect(self.path) as db:
            db.execute("UPDATE attempts SET lease_expires_at=1 WHERE id=?", (attempt,))
            db.commit()
        old, new = new, {"Phalanx-Lease": "t" * 32}
        self.assertEqual(self.request(prefix + "/recover-claim", RUNNER, body,
                                      new | {"Idempotency-Key": "adopt-2"})[0], 200)
        result_path = f"/v1/items/{item['id']}/executions/{execution['id']}/result"
        result = {"expected_version": execution["version"], "attempt_id": attempt,
                  "stage": execution["state"]["stage"], "outcome": "succeeded", "report_markdown": "# Draft\n"}
        self.assertEqual(self.request(result_path, RUNNER, result, old)[0], 409)
        status, accepted = self.request(result_path, RUNNER, result, new)
        self.assertEqual(status, 200)
        self.assertEqual(accepted["state"]["stage"], "report_review")
        self.assertEqual(self.request(prefix + "/recover-claim", RUNNER, body, new)[0], 409)

    def test_expired_cancellation_closes_only_after_reconciled_stop(self):
        item_id, = self.seed("repo", [("normal", -1, None)])
        with patch.object(dispatch, "time", return_value=NOW):
            claimed = self.claim("local", ["repo"])
        execution = claimed["execution"]
        attempt = claimed["attempt_id"]
        with write_transaction(self.path) as db:
            pending = intake.cancel(db, item_id, execution["id"], Cancel(
                expected_version=execution["version"], reason="Requested by owner",
            ))
        self.assertEqual(pending["state"]["status"], "waiting_for_external")
        with patch.object(dispatch, "time", return_value=claimed["lease_expires_at"]):
            with write_transaction(self.path) as db:
                expired = dispatch.recovery_candidates(db, "local")
                self.assertEqual(expired[0]["version"], pending["version"])
                reservation = {"runner_id": "local", "claim": claimed}
                reconcile(reservation, runner_id="local", packet=None, expired=expired, now=NOW,
                          no_launch_proof="No item worktree, owned branch or launch packet")
                stop = reservation["recover_stop"]
                pending = dispatch.recover_stopped(
                    db, "local", attempt, ReconciledStop(**stop["body"]),
                )
                self.assertEqual(pending["state"]["pending_output"], "cancelled")
                self.assertEqual(dispatch.recovery_candidates(db, "local"), [])
        closed = finalization.attempt(self.path, execution["id"])
        self.assertEqual(closed["state"]["status"], "cancelled")
        self.assertTrue(acknowledge_stop(reservation, stop, pending))
        self.assertTrue(acknowledge_stop(reservation, stop, closed))
        self.assertFalse(acknowledge_stop(reservation, stop, closed | {
            "state": closed["state"] | {"final_output": None},
        }))
        self.assertFalse(acknowledge_stop(reservation, stop, closed | {
            "state": closed["state"] | {"final_output": {"reference": "/other/report"}},
        }))
        self.assertFalse(acknowledge_stop(reservation, stop, closed | {
            "version": closed["version"] + 1,
        }))
        with connect(self.path, mode="ro") as db:
            self.assertIn("Requested by owner", intake.report(db, item_id, execution["id"]))

    def test_fifth_stopped_attempt_parks_stage(self):
        item_id, = self.seed("repo", [("normal", -1, None)])
        now = NOW
        for index in range(5):
            with patch.object(dispatch, "time", return_value=now):
                claimed = self.claim("local", ["repo"])
            execution = claimed["execution"]
            now = claimed["lease_expires_at"]
            body = ReconciledStop(item_id=item_id, expected_version=execution["version"],
                                  reconciliation="Stopped worker and verified effects")
            with patch.object(dispatch, "time", return_value=now):
                with write_transaction(self.path) as db:
                    outcome = dispatch.recover_stopped(db, "local", claimed["attempt_id"], body)
            if index < 4:
                now += 5 * 2 ** index
                self.assertEqual(outcome["ready_since_ms"], now * 1000)
        self.assertEqual(outcome["state"]["status"], "waiting_for_input")
        self.assertIsNone(outcome["ready_since_ms"])
        with patch.object(dispatch, "time", return_value=now + 9999):
            self.assertEqual(self.claim("local", ["repo"])["status"], "no_eligible_work")

    def test_stopped_technical_results_back_off_and_owner_questions_wait(self):
        technical, question = self.seed("repo", [("urgent", -5, None), ("normal", -1, None)])
        now = NOW
        for retry, delay in enumerate((5, 10, 20, 40, None)):
            with patch.object(dispatch, "time", return_value=now), patch.object(intake, "time", return_value=now):
                lease = token_urlsafe(32)
                assignment = self.claim("local", ["repo"], lease=lease)
                self.assertEqual(assignment["item_id"], technical)
                execution = assignment["execution"]
                failure = Result(
                    expected_version=execution["version"], attempt_id=assignment["attempt_id"],
                    stage="report_drafting", outcome="failed", failure_reason="Build resource unavailable",
                )
                with write_transaction(self.path) as db:
                    result = intake.result(db, technical, execution["id"], failure, intake.digest(lease))
                if delay is not None:
                    self.assertEqual(result["state"]["status"], "queued")
                    self.assertEqual(result["ready_since_ms"], (now + delay) * 1000)
                    self.assertEqual(result["retry_at_ms"], result["ready_since_ms"])
                    if retry == 0:
                        lease = token_urlsafe(32)
                        assignment = self.claim("local", ["repo"], lease=lease)
                        self.assertEqual(assignment["item_id"], question)
                        execution = assignment["execution"]
                        with write_transaction(self.path) as db:
                            waiting = intake.result(db, question, execution["id"], Result(
                                expected_version=execution["version"], attempt_id=assignment["attempt_id"],
                                stage="report_drafting", outcome="needs_input",
                                question={"prompt": "Which source should I use?", "handoff": "Two source reports are available."},
                            ), intake.digest(lease))
                        self.assertEqual(waiting["state"]["status"], "waiting_for_input")
                        self.assertIsNone(waiting["retry_at_ms"])
                    self.assertEqual(self.claim("local", ["repo"])["status"], "no_eligible_work")
                    now += delay
        self.assertEqual(result["state"]["status"], "waiting_for_input")
        self.assertIsNone(result["retry_at_ms"])
        self.assertIn("Build resource unavailable", result["state"]["wait"]["reason"])

    def test_schema_two_migrates_queue_and_preserves_running_attempt(self):
        path = self.path.with_name("legacy.sqlite3")
        with connect(path, mode="rwc") as db:
            for migration in MIGRATIONS[:2]:
                for statement in migration:
                    db.execute(statement)
            db.execute("PRAGMA user_version=2")
            stamp = "2026-01-02T00:00:00.123000+00:00"
            intake.register(db, "repo", "https://github.com/example/repo.git")
            for status in ("queued", "running"):
                db.execute("INSERT INTO work_items VALUES (?, 'repo', ?, ?)",
                           (status, '{"priority":"urgent"}', stamp))
                db.execute(
                    """INSERT INTO executions (id, item_id, version, state_json, created_at, updated_at)
                       VALUES (?, ?, 1, json_object('stage','report_drafting','status',?,
                                                    'final_output',NULL), ?, ?)""",
                    (status, status, status, stamp, stamp),
                )
            db.execute("INSERT INTO attempts VALUES ('attempt','running','report_drafting','local',"
                       "'digest',1,?,NULL)", (stamp,))
        migrate(path)
        with connect(path) as db:
            row = dispatch.queue_head(db, "local", ["repo"], NOW * 1000, 300_000)
            self.assertEqual(row["id"], "queued")
            self.assertEqual(row["ready_since"], 1767312000123)
            self.assertEqual(dispatch.recovery_candidates(db, "local")[0]["attempt_id"], "attempt")
            plan = db.execute("EXPLAIN QUERY PLAN " + dispatch.QUEUE_HEAD,
                              (NOW * 1000, "repo", "", 2)).fetchone()["detail"]
            self.assertIn("SEARCH executions USING INDEX queue_heads", plan)
            stage_plan = [row["detail"] for row in db.execute(
                "EXPLAIN QUERY PLAN " + dispatch.STAGE_QUEUE_HEAD,
                (NOW * 1000, "repo", "", 2, "report_drafting"),
            )]
            self.assertEqual(len(stage_plan), 1)
            self.assertIn("SEARCH executions USING INDEX queue_stage_heads", stage_plan[0])
            self.assertIn("stage=? AND ready_since", stage_plan[0])
