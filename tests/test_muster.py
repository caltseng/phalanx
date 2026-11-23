from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from phalanx import muster
from phalanx.config import store_identity as server_store_identity
from phalanx.muster import MusterError
from phalanx.workspaces import _lock
from test_bootstrap import OWNER, RUNNER, ServerFixture


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Stub(BaseHTTPRequestHandler):
    """A process on the API port; `answer` decides what it says to /v1/runner/status."""

    def do_GET(self):
        if self.path == "/health":
            code, body = 200, {"status": "ok"}
        elif self.path == "/v1/runner/status":
            code, body = self.server.answer(self.headers.get("Authorization"))
        elif self.path.startswith("/v1/dispatch/runners/"):
            code, body = 200, {"runner_id": "desk", "paused": True, "aging_seconds": 60,
                               "capacity": 2, "reserved": 1}
        else:
            code, body = 404, {}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class FakeProcess:
    pid = 4242
    returncode = None

    def poll(self):
        return None


class MusterTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.port = free_port()
        (self.home / "runner").mkdir()
        (self.home / "private").mkdir()
        (self.home / "db").mkdir()
        (self.home / "db" / "phalanx.sqlite3").write_bytes(b"")
        self.write("runner.json", {"runner_id": "desk", "base_url": f"http://127.0.0.1:{self.port}",
                                   "capacity": 3})
        self.write("private/env.json", {"PHALANX_OWNER_TOKEN": OWNER, "PHALANX_RUNNER_TOKEN": RUNNER,
                                        "PHALANX_GITHUB_TOKEN": "g" * 20})
        self.launches = []
        self.held = ExitStack()
        self.addCleanup(self.held.close)

    def write(self, name, data):
        (self.home / name).write_text(json.dumps(data), encoding="utf-8")

    def files(self):
        return sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*"))

    def load(self):
        return muster.load(self.home, "--home")

    def serve(self, answer):
        server = ThreadingHTTPServer(("127.0.0.1", self.port), Stub)
        server.answer = answer
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.held.callback(server.server_close)
        self.held.callback(server.shutdown)

    def ours(self, authorization):
        if authorization != f"Bearer {RUNNER}":
            return 401, {"detail": "Unauthorized"}
        return 200, {"role": "runner", "store": muster.store_identity(self.home / "db" / "phalanx.sqlite3")}

    def hold_loop_lock(self):
        handle = self.held.enter_context((self.home / "runner" / "captain-loop.lock").open("a+b"))
        _lock(handle)

    def launch(self, argv, *, env, cwd, log):
        self.launches.append((argv, env))
        if "uvicorn" in argv:
            self.serve(self.ours)
        else:
            self.hold_loop_lock()
        return FakeProcess()

    def test_locator_resolution_order(self):
        locator = self.home / "muster.json"
        locator.write_text(json.dumps({"home": str(self.home / "file")}), encoding="utf-8")
        env = {"PHALANX_HOME": str(self.home / "env")}
        self.assertEqual(muster.locate(self.home / "flag", env, locator), (self.home / "flag", "--home"))
        self.assertEqual(muster.locate(None, env, locator), (self.home / "env", "PHALANX_HOME"))
        self.assertEqual(muster.locate(None, {"PHALANX_HOME": ""}, locator),
                         (self.home / "file", str(locator)))

    def test_missing_and_invalid_locators_fail_without_writing(self):
        locator = self.home / "locator" / "muster.json"
        with self.assertRaisesRegex(MusterError, "PHALANX_HOME.*muster use DIR"):
            muster.locate(None, {}, locator)
        locator.parent.mkdir()
        for text in ("not json", '{"home": "/x", "extra": 1}', '["/x"]', '{"home": 3}'):
            locator.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaisesRegex(MusterError, "is invalid"):
                muster.locate(None, {}, locator)
        locator.write_text('{"home": "relative/muster"}', encoding="utf-8")
        with self.assertRaisesRegex(MusterError, "absolute path"):
            muster.locate(None, {}, locator)
        with self.assertRaisesRegex(MusterError, "absolute path"):
            muster.locate(None, {"PHALANX_HOME": "relative"}, locator)

    def test_load_derives_paths_and_rejects_missing_or_ambiguous_state(self):
        loaded = self.load()
        self.assertEqual((loaded.config_path, loaded.state, loaded.database),
                         (self.home / "runner.json", self.home / "runner", self.home / "db" / "phalanx.sqlite3"))
        before = self.files()
        self.write("private/env.json", {"PHALANX_OWNER_TOKEN": OWNER, "PHALANX_RUNNER_TOKEN": RUNNER,
                                        "PHALANX_API_URL": "http://127.0.0.1:1"})
        with self.assertRaisesRegex(MusterError, "Ambiguous API"):
            self.load()
        self.write("private/env.json", {"PHALANX_OWNER_TOKEN": OWNER, "PHALANX_RUNNER_TOKEN": RUNNER,
                                        "PHALANX_DB": "db/other.sqlite3"})
        with self.assertRaisesRegex(MusterError, "never create a database"):
            self.load()
        self.write("private/env.json", {"PHALANX_RUNNER_TOKEN": RUNNER})
        with self.assertRaisesRegex(MusterError, "no PHALANX_OWNER_TOKEN"):
            self.load()
        (self.home / "runner").rmdir()
        with self.assertRaisesRegex(MusterError, "never create state"):
            self.load()
        with self.assertRaisesRegex(MusterError, "does not exist"):
            muster.load(self.home / "absent", "PHALANX_HOME")
        self.assertEqual(self.files(), [name for name in before if name != "runner"])

    def test_use_writes_the_locator_only_for_a_valid_muster(self):
        locator = self.home / "user" / "muster.json"
        with self.assertRaises(MusterError):
            muster.use(self.home / "absent", locator)
        self.assertFalse(locator.exists())
        muster.use(self.home, locator)
        self.assertEqual(muster.locate(None, {}, locator), (self.home, str(locator)))

    def test_repeated_start_launches_nothing_new(self):
        first = muster.start(self.load(), launch=self.launch, poll=0.01)
        self.assertEqual((first["api"]["status"], first["captain_loop"]["status"]), ("started", "started"))
        self.assertIn("standing relay chat", first["relay"])
        (api_argv, api_env), (loop_argv, loop_env) = self.launches
        self.assertEqual(api_env["PHALANX_DB"], str(self.home / "db" / "phalanx.sqlite3"))
        self.assertNotIn("PHALANX_GITHUB_TOKEN", api_env)
        self.assertNotIn("PHALANX_OWNER_TOKEN", loop_env)
        self.assertEqual(loop_argv[-4:], ["--config", str(self.home / "runner.json"),
                                          "--state", str(self.home / "runner")])
        self.assertNotIn(RUNNER, json.dumps(first))
        again = muster.start(self.load(), launch=self.launch, poll=0.01)
        self.assertEqual((again["api"]["status"], again["captain_loop"]["status"]),
                         ("already_running", "already_running"))
        self.assertEqual(len(self.launches), 2)

    def test_start_refuses_a_foreign_process_on_the_api_port(self):
        for answer in (lambda _: (401, {"detail": "Unauthorized"}),
                       lambda _: (200, {"role": "runner", "store": "0" * 16}),
                       lambda _: (200, {"role": "runner"})):
            with self.subTest(answer=answer), ExitStack() as stack:
                self.held = stack
                self.serve(answer)
                with self.assertRaisesRegex(MusterError, "Not starting anything"):
                    muster.start(self.load(), launch=self.launch, poll=0.01)
        self.assertEqual(self.launches, [])

    def test_a_concurrent_start_is_refused(self):
        handle = self.held.enter_context((self.home / "runner" / "muster-start.lock").open("a+b"))
        _lock(handle)
        with self.assertRaisesRegex(MusterError, "Another `phalanx muster start`"):
            muster.start(self.load(), launch=self.launch)
        self.assertEqual(self.launches, [])

    def relay_store(self, seen, requests):
        with sqlite3.connect(self.home / "runner" / "captain-relay.sqlite3") as db:
            db.execute("CREATE TABLE server (key TEXT PRIMARY KEY, value REAL)")
            db.execute("CREATE TABLE request (id TEXT PRIMARY KEY, created_at REAL)")
            if seen is not None:
                db.execute("INSERT INTO server VALUES ('seen', ?)", (seen,))
            db.executemany("INSERT INTO request VALUES (?, ?)", enumerate(requests))
        db.close()

    def test_status_reports_relay_down_while_api_and_loop_are_up(self):
        self.serve(self.ours)
        self.hold_loop_lock()
        now = time.time()
        self.relay_store(now - 600, [now - 300])
        with sqlite3.connect(self.home / "runner" / "attended.sqlite3") as db:
            db.execute("CREATE TABLE captain (key TEXT PRIMARY KEY, value TEXT)")
            for slot, state in (("0", "unmatched_attempts"), ("relay", "relay_stalled"),
                                ("mappings", "mappings_verified"), ("3", "renewal_blocked")):
                db.execute("INSERT INTO captain VALUES (?, ?)",
                           (f"status-{slot}", json.dumps({"slot": slot, "status": state})))
        db.close()
        report = muster.status(self.load(), now=now)
        self.assertEqual(report["api"]["state"], "verified")
        self.assertTrue(report["captain_loop"]["running"])
        self.assertEqual(report["relay"]["state"], "stalled")
        self.assertEqual(report["capacity"], {"local": 3, "server": 2, "reserved": 1, "effective": 2})
        self.assertEqual(report["dispatch"], {"paused": True, "aging_seconds": 60})
        self.assertEqual(sorted(event["status"] for event in report["blockers"]),
                         ["relay_stalled", "renewal_blocked", "unmatched_attempts"])
        self.assertNotIn(RUNNER, json.dumps(report))

    def test_relay_state_comes_only_from_relay_activity(self):
        now = time.time()
        self.assertEqual(muster.relay_status(self.home / "runner", now)["state"], "not_observed")
        self.relay_store(now - 10, [now - 5])
        self.assertEqual(muster.relay_status(self.home / "runner", now)["state"], "serving")
        self.assertEqual(muster.relay_status(self.home / "runner", now + 600)["state"], "stalled")

    def test_status_with_the_api_absent_reports_without_starting_anything(self):
        report = muster.status(self.load())
        self.assertEqual((report["api"]["state"], report["captain_loop"]["running"]), ("absent", False))
        self.assertEqual(report["dispatch"], {"reason": "API not verified"})
        self.assertFalse((self.home / "runner" / "captain-loop.lock").exists())

    def test_detach_options_per_os(self):
        self.assertEqual(muster.detach_options("nt"), {"creationflags": 0x8 | 0x200 | 0x01000000})
        self.assertEqual(muster.detach_options("posix"), {"start_new_session": True})


class MusterServerTest(ServerFixture, unittest.TestCase):
    def test_a_real_api_verifies_and_reports_dispatch(self):
        home = Path(self.enterContext(TemporaryDirectory())).resolve()
        (home / "runner").mkdir()
        (home / "private").mkdir()
        database = home / "phalanx.sqlite3"
        self.start_server(database)
        (home / "runner.json").write_text(json.dumps({"runner_id": "desk", "base_url": self.url}),
                                          encoding="utf-8")
        (home / "private" / "env.json").write_text(json.dumps(
            {"PHALANX_OWNER_TOKEN": OWNER, "PHALANX_RUNNER_TOKEN": RUNNER, "PHALANX_DB": str(database)}),
            encoding="utf-8")
        self.assertEqual(muster.store_identity(database), server_store_identity(database))
        report = muster.status(muster.load(home, "--home"))
        self.assertEqual(report["api"]["state"], "verified")
        self.assertEqual(report["capacity"], {"local": 1, "server": 4, "reserved": 0, "effective": 1})
        self.assertEqual(report["dispatch"]["paused"], False)


if __name__ == "__main__":
    unittest.main()
