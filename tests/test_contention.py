import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from phalanx import captains, dependencies, finalization, intake, store, writers
from phalanx.intake_models import Cancel, Intake, Result
from test_bootstrap import OWNER, RUNNER, ServerFixture


class ContentionTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        # Production timeout/load bounds belong to the separate envelope probe.
        self.fault_timeouts = self.enterContext(ExitStack())
        self.fault_timeouts.enter_context(patch.object(store, "BUSY_TIMEOUT", 0.05))
        self.fault_timeouts.enter_context(patch.object(store, "ADMISSION_TIMEOUT", 0.2))

    def test_interrupted_reader_does_not_leave_phantom_admission(self):
        for transferred in (False, True):
            with self.subTest(transferred=transferred):
                gate = store._Gate()

                def interrupt(*args, **kwargs):
                    if transferred:
                        gate.writes += 1
                        gate.admitted += gate.queued
                        gate.queued = 0
                    raise KeyboardInterrupt

                with gate.write():
                    with patch.object(gate.condition, "wait_for", side_effect=interrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            with gate.read_start():
                                self.fail("Interrupted read entered the gate")
                    self.assertEqual((gate.queued, gate.admitted), (0, 0))
                with patch.object(store, "ADMISSION_TIMEOUT", 0.05), gate.write():
                    self.assertTrue(gate.writing)

    def test_failed_commit_rolls_back_and_releases_admission(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "commit.sqlite3"
            store.initialize(path)
            with store.connect(path, mode="ro") as reader:
                reader.execute("BEGIN")
                original = reader.execute("SELECT * FROM dispatch_settings").fetchall()
                started = time.monotonic()
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    with store.write_transaction(path) as db:
                        db.execute("UPDATE dispatch_settings SET max_writers=100")
                self.assertLess(time.monotonic() - started, 5)
                reader.execute("ROLLBACK")
            with store.read_transaction(path) as db:
                self.assertEqual(db.execute("SELECT * FROM dispatch_settings").fetchall(), original)
            with store.write_transaction(path) as db:
                db.execute("UPDATE dispatch_settings SET max_writers=11")
            self.assertEqual((store._gate.writing, store._gate.admitted, store._gate.queued), (False, 0, 0))

    def test_canceled_transaction_rolls_back_before_same_process_resumes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.sqlite3"
            store.initialize(path)
            for error in (RuntimeError, asyncio.CancelledError):
                with self.subTest(error=error):
                    with store.read_transaction(path) as db:
                        original = db.execute("SELECT * FROM dispatch_settings").fetchall()
                    with self.assertRaises(error):
                        with store.write_transaction(path) as db:
                            db.execute("UPDATE dispatch_settings SET max_writers=100")
                            raise error
                    with store.write_transaction(path) as db:
                        self.assertEqual(db.execute("SELECT * FROM dispatch_settings").fetchall(), original)

    def test_sqlite_busy_response_does_not_need_a_free_request_worker(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "external.sqlite3"
            store.initialize(path)
            server = self.start_server(path)
            try:
                with store.connect(path) as lock:
                    lock.execute("BEGIN EXCLUSIVE")
                    try:
                        with ThreadPoolExecutor(max_workers=9) as pool:
                            responses = [pool.submit(self.get, "/v1/dispatch/settings", OWNER)
                                         for _ in range(8)]
                            self.assertEqual(pool.submit(self.get, "/v1/owner/status", OWNER).result(timeout=5),
                                             (200, {"role": "owner"}))
                            for response in responses:
                                code, body = response.result(timeout=10)
                                self.assertEqual(code, 503, body)
                                self.assertIn("database is locked", body["detail"])
                    finally:
                        lock.execute("ROLLBACK")
                self.assertEqual(self.get("/v1/dispatch/settings", OWNER)[0], 200)
            finally:
                self.stop_server(server)

    def test_admission_timeout_releases_request_threads_and_gate_counts(self):
        gate = store._Gate()

        def waiting(context):
            with context():
                self.fail("A blocked transaction entered the gate")

        with patch.object(store, "ADMISSION_TIMEOUT", 0.05):
            with ThreadPoolExecutor(max_workers=2) as pool:
                with gate.write():
                    pending = [pool.submit(waiting, context) for context in (gate.write, gate.read_start)]
                    for future in pending:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "busy"):
                            future.result(timeout=5)
                    self.assertEqual(pool.submit(lambda: "status").result(timeout=5), "status")
                    self.assertEqual((gate.writers, gate.queued), (0, 0))
                self.assertEqual(gate.admitted, 0)
                with gate.read_start():
                    writer = pool.submit(waiting, gate.write)
                    with self.assertRaisesRegex(sqlite3.OperationalError, "busy"):
                        writer.result(timeout=5)
                    self.assertEqual((gate.writers, gate.admitted), (0, 1))
                with gate.write():
                    self.assertTrue(gate.writing)
                with gate.read_start():
                    self.assertEqual(gate.admitted, 1)
                self.assertEqual(gate.admitted, 0)

    def test_refresh_retries_real_admission_failure_without_losing_its_audit(self):
        self.check_refresh_recovery(external=False)

    def test_refresh_retries_external_sqlite_lock_without_losing_its_audit(self):
        self.check_refresh_recovery(external=True)

    def check_refresh_recovery(self, *, external):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "refresh.sqlite3"
            store.initialize(path)
            with store.write_transaction(path) as db:
                db.execute("UPDATE dependency_audit SET cursor=''")
            refreshed = threading.Event()
            original = dependencies.process_refresh

            def process(path):
                worked = original(path)
                if worked:
                    refreshed.set()
                return worked

            async def run():
                with self.assertLogs("phalanx.dependencies", level="ERROR") as errors:
                    with ExitStack() as held:
                        if external:
                            lock = held.enter_context(store.connect(path))
                            lock.execute("BEGIN IMMEDIATE")
                        else:
                            held.enter_context(store._gate.write())
                        task = asyncio.create_task(dependencies.run_refresh(path))
                        try:
                            deadline = asyncio.get_running_loop().time() + 5
                            while not errors.output:
                                self.assertLess(asyncio.get_running_loop().time(), deadline)
                                await asyncio.sleep(0.01)
                            self.assertFalse(task.done())
                            with store.connect(path, mode="ro") as db:
                                self.assertEqual(db.execute(
                                    "SELECT cursor FROM dependency_audit").fetchone()[0], "")
                        except BaseException:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            raise
                    try:
                        self.assertTrue(await asyncio.to_thread(refreshed.wait, 5))
                        self.assertFalse(task.done())
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                with store.connect(path, mode="ro") as db:
                    self.assertIsNone(db.execute("SELECT cursor FROM dependency_audit").fetchone()[0])

            with patch.object(store, "ADMISSION_TIMEOUT", 0.05), patch.object(
                dependencies, "process_refresh", side_effect=process,
            ):
                asyncio.run(run())

    def test_background_finalization_keeps_intent_through_external_lock(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "finalization.sqlite3"
            store.initialize(path)
            with store.write_transaction(path) as db:
                intake.register(db, "repo", "https://github.com/e/r.git")
                item = intake.create(db, Intake(repository_id="repo", goal="Withdraw work"))
                execution = item["execution"]
                intake.cancel(db, item["id"], execution["id"],
                              Cancel(expected_version=execution["version"], reason="Canceled during contention"))

            async def run():
                with self.assertLogs("phalanx.finalization", level="ERROR") as errors:
                    with store.connect(path) as lock:
                        lock.execute("BEGIN IMMEDIATE")
                        task = asyncio.create_task(finalization.run(path))
                        try:
                            deadline = asyncio.get_running_loop().time() + 5
                            while not errors.output:
                                self.assertLess(asyncio.get_running_loop().time(), deadline)
                                await asyncio.sleep(0.01)
                            with store.connect(path, mode="ro") as db:
                                self.assertEqual(db.execute("SELECT failures FROM report_finalizations").fetchone()[0], 0)
                                self.assertEqual(db.execute("SELECT count(*) FROM reports").fetchone()[0], 0)
                        finally:
                            lock.execute("ROLLBACK")
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(finalization.process_due, path)

            asyncio.run(run())
            with store.read_transaction(path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM report_finalizations").fetchone()[0], 0)
                self.assertIn("Canceled during contention", db.execute("SELECT markdown FROM reports").fetchone()[0])
                self.assertEqual(intake.current_execution(db, item["id"])["status"], "cancelled")

    def test_http_writer_contention_is_retryable_and_recovers_without_restarting(self):
        self.check_http_writer_recovery(external=False)

    def test_http_external_sqlite_lock_recovers_without_restarting(self):
        self.check_http_writer_recovery(external=True)

    def check_http_writer_recovery(self, *, external):
        with TemporaryDirectory() as directory, ExitStack() as cleanup, ExitStack() as held:
            path = Path(directory) / "http.sqlite3"
            store.initialize(path)
            with store.write_transaction(path) as db:
                intake.register(db, "repo", "https://github.com/e/r.git")
                db.execute("UPDATE dispatch_settings SET max_building_writers=10, max_writers=20")
                captain = captains.register(db, captains.CaptainRegistration(
                    session_ref="fixture", charter_revision="test"))
                for index in range(20):
                    token = f"fixture-writer-token-{index:032}"
                    item = intake.create(db, Intake(repository_id="repo", goal="Keep writer moving"))
                    binding = writers.register(db, item["id"], item["execution"]["id"], writers.Registration(
                        captain_id=captain["id"], runtime="native", nonce=f"fixture-nonce-{index:032}",
                        token_digest=intake.digest(token), expected_version=item["execution"]["version"]))
                    if index < 10:
                        for fields in ({"candidate": "a" * 40}, {"reviewed_candidate": "a" * 40}):
                            payload = Result(
                                attempt_id=binding["writer"]["attempt_id"], stage=binding["state"]["stage"],
                                expected_version=binding["version"], outcome="succeeded", **fields)
                            view = intake.result(db, item["id"], binding["id"], payload, binding["writer"]["id"])
                            binding = writers.advance(db, binding["writer"]["id"], payload.attempt_id, view)
                self.assertEqual(tuple(db.execute(
                    "SELECT count(*), sum(building) FROM writers WHERE ended_at IS NULL").fetchone()), (20, 10))
                extra = intake.create(db, Intake(repository_id="repo", goal="Wait for capacity"))
                with self.assertRaises(intake.Conflict):
                    writers.register(db, extra["id"], extra["execution"]["id"], writers.Registration(
                        captain_id=captain["id"], runtime="native", nonce="extra-fixture-nonce",
                        token_digest=intake.digest("extra-token"), expected_version=extra["execution"]["version"]))
            result_path = f"/v1/items/{item['id']}/executions/{binding['id']}/result"
            result = {
                "attempt_id": binding["writer"]["attempt_id"], "stage": "development",
                "expected_version": binding["version"], "outcome": "succeeded", "candidate": "a" * 40,
            }
            headers = {"Idempotency-Key": "contention-result"}
            server = self.start_server(path)
            cleanup.callback(self.stop_server, server)
            with store.connect(path, mode="ro") as db:
                before = {
                    table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("writers", "attempts", "transition_history", "executions")
                }
            lock = cleanup.enter_context(store.connect(path))
            lock.execute("BEGIN IMMEDIATE" if external else "BEGIN EXCLUSIVE")
            try:
                operations = [
                    ("/v1/writer/progress", token, {"note": "Still moving"}),
                    ("/v1/writer/capacity", token, {"phase": "building", "expected_revision": 0}),
                    (result_path, token, result),
                    ("/v1/reconcilers/fixture/heartbeat", RUNNER, {}),
                ]
                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=22) as pool:
                    pending = [
                        pool.submit(self.request, route, credential, body, headers)
                        for route, credential, body in operations
                        for _ in range(5)
                    ]
                    self.assertEqual(pool.submit(self.get, "/v1/owner/status", OWNER).result(timeout=15),
                                     (200, {"role": "owner"}))
                    health = pool.submit(self.get, "/health").result(timeout=15)
                    self.assertEqual(health, (200, {"status": "ok"}) if external else
                                     (503, {"detail": "SQLite store unavailable"}))
                    for future in pending:
                        code, body = future.result(timeout=15)
                        self.assertEqual(code, 503, body)
                        self.assertIn("retry the same request", body["detail"])
                self.assertLess(time.monotonic() - started, 15)
                request = Request(self.url + ("/v1/writer/progress" if external else "/v1/writer"),
                                  data=b'{"note": "Still moving"}' if external else None,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as response:
                    urlopen(request, timeout=10)
                with response.exception:
                    self.assertEqual(response.exception.code, 503)
                    self.assertEqual(response.exception.headers["Retry-After"], "1")
            finally:
                lock.execute("ROLLBACK")

            with store.connect(path, mode="ro") as db:
                for table, rows in before.items():
                    self.assertEqual(
                        [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")],
                        rows, table,
                    )
            if external:
                lock.execute("BEGIN EXCLUSIVE")
                try:
                    started = time.monotonic()
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        health = pool.submit(self.get, "/health")
                        auth = pool.submit(self.get, "/v1/owner/status", OWNER)
                        writer = pool.submit(self.get, "/v1/writer", token)
                        self.assertEqual(auth.result(timeout=5), (200, {"role": "owner"}))
                        self.assertEqual(health.result(timeout=5)[0], 503)
                        self.assertEqual(writer.result(timeout=5)[0], 503)
                    self.assertLess(time.monotonic() - started, 5)
                finally:
                    lock.execute("ROLLBACK")
            self.fault_timeouts.close()
            with ThreadPoolExecutor(max_workers=20) as pool:
                resumed = [
                    pool.submit(self.request, "/v1/writer/progress", f"fixture-writer-token-{index:032}",
                                {"note": f"Resumed {index}"})
                    for index in range(20)
                ]
                for future in resumed:
                    response = future.result(timeout=10)
                    self.assertEqual(response[0], 200, response)
            self.assertEqual(self.request("/v1/reconcilers/fixture/heartbeat", RUNNER, {})[0], 200)
            self.assertEqual(self.request("/v1/writer/progress", token, {"note": "Recovered"})[0], 200)
            capacity = self.request("/v1/writer/capacity", token, {
                "phase": "building", "expected_revision": 0})
            self.assertEqual(capacity[0], 200, capacity)
            self.assertTrue(capacity[1]["writer"]["building"])
            accepted = self.request(result_path, token, result, headers)
            self.assertEqual(accepted[0], 200, accepted)
            self.assertEqual(accepted[1]["state"]["stage"], "self_review")
            self.assertEqual(self.request(result_path, token, result, headers), accepted)
            stale = self.request(result_path, token, dict(result, candidate="b" * 40), headers)
            self.assertEqual(stale[0], 409, stale)
            settings = self.get("/v1/dispatch/settings", RUNNER)[1]
            self.assertEqual((settings["max_building_writers"], settings["max_writers"]), (10, 20))
            with store.connect(path, mode="ro") as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], store.APPLICATION_ID)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE ended_at IS NULL").fetchone()[0], 20)
