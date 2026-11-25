import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from phalanx import captains, dependencies, intake, store, writers
from phalanx.intake_models import Intake, Result
from test_bootstrap import OWNER, RUNNER, SERVER_PROCESS, ServerFixture


CONTENTION_SERVER = """
import asyncio
from contextlib import asynccontextmanager
from threading import Event, Thread
import anyio.to_thread
from phalanx import api, store

store.ADMISSION_TIMEOUT = 0.2
original_lifespan = api.app.router.lifespan_context

@asynccontextmanager
async def lifespan(app):
    async with original_lifespan(app):
        anyio.to_thread.current_default_thread_limiter().total_tokens = 2
        yield

api.app.router.lifespan_context = lifespan
release, held = Event(), Event()

@api.app.post("/fixture/hold")
async def hold():
    release.clear()
    held.clear()
    def transaction():
        with store._gate.write():
            held.set()
            release.wait(300)
    Thread(target=transaction, daemon=True).start()
    while not held.is_set():
        await asyncio.sleep(0)
    return {"held": True}

@api.app.post("/fixture/release")
async def unlock():
    release.set()
    return {"released": True}
""" + SERVER_PROCESS


class ContentionTests(ServerFixture, unittest.TestCase):
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
        with TemporaryDirectory() as directory:
            path = Path(directory) / "refresh.sqlite3"
            store.migrate(path)
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
                    with store._gate.write():
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

    def test_http_writer_contention_is_retryable_and_recovers_without_restarting(self):
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            path = Path(directory) / "http.sqlite3"
            store.migrate(path)
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
            with patch("test_bootstrap.SERVER_PROCESS", CONTENTION_SERVER):
                server = self.start_server(path)
            cleanup.callback(self.stop_server, server)
            self.assertEqual(self.request("/fixture/hold", body={})[0], 200)
            try:
                operations = [
                    ("/v1/writer/progress", token, {"note": "Still moving"}),
                    ("/v1/writer/capacity", token, {"phase": "building", "expected_revision": 0}),
                    (result_path, token, result),
                    ("/v1/reconcilers/fixture/heartbeat", RUNNER, {}),
                ]
                with ThreadPoolExecutor(max_workers=20) as pool:
                    pending = [
                        pool.submit(self.request, route, credential, body, headers)
                        for route, credential, body in operations
                        for _ in range(5)
                    ]
                    self.assertEqual(pool.submit(self.get, "/v1/owner/status", OWNER).result(timeout=10),
                                     (200, {"role": "owner"}))
                    self.assertEqual(pool.submit(self.get, "/health").result(timeout=10),
                                     (200, {"status": "ok"}))
                    for future in pending:
                        code, body = future.result(timeout=10)
                        self.assertEqual(code, 503, body)
                        self.assertIn("retry the same request", body["detail"])
                request = Request(self.url + "/v1/writer",
                                  headers={"Authorization": f"Bearer {token}"})
                with self.assertRaises(HTTPError) as response:
                    urlopen(request, timeout=10)
                self.assertEqual(response.exception.code, 503)
                self.assertEqual(response.exception.headers["Retry-After"], "1")
                response.exception.close()
            finally:
                self.assertEqual(self.request("/fixture/release", body={})[0], 200)

            with ThreadPoolExecutor(max_workers=20) as pool:
                resumed = [
                    pool.submit(self.request, "/v1/writer/progress", f"fixture-writer-token-{index:032}",
                                {"note": f"Resumed {index}"})
                    for index in range(20)
                ]
                for future in resumed:
                    self.assertEqual(future.result(timeout=10)[0], 200)
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
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE ended_at IS NULL").fetchone()[0], 20)
