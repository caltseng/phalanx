from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from phalanx import store
from phalanx.store import connect, migrate, read_transaction, write_transaction
from timing import WAIT_SECONDS


OWNER = "o" * 32
RUNNER = "r" * 32
# Loaded or hosted machines can stall a single fsync-heavy request or server start.
# Tests never assert on these timeouts.
REQUEST_TIMEOUT = STARTUP_TIMEOUT = WAIT_SECONDS

# Let the real server exit before its Python launcher exits on Windows.
SERVER_PROCESS = """
import sys
from threading import Thread

import uvicorn

server = uvicorn.Server(uvicorn.Config("phalanx.api:app", port=int(sys.argv[1]),
                                     timeout_graceful_shutdown=5))

def stop_on_eof():
    sys.stdin.buffer.read()
    server.should_exit = True

Thread(target=stop_on_eof, daemon=True).start()
server.run()
"""


class ServerFixture:
    def start_server(self, path, **environment):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, PHALANX_DB=str(path), PHALANX_OWNER_TOKEN=OWNER,
                   PHALANX_RUNNER_TOKEN=RUNNER, PHALANX_LOCAL_BOARD="0")
        env.update(environment)
        log = path.with_suffix(".log")
        with log.open("w") as output:
            server = subprocess.Popen(
                [sys.executable, "-c", SERVER_PROCESS, str(port)],
                env=env, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
            )
        self.addCleanup(self.stop_server, server)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while server.poll() is None and time.monotonic() < deadline:
            try:
                if self.request("/health", timeout=1) == (200, {"status": "ok"}):
                    return server
            except (URLError, TimeoutError):
                pass
            time.sleep(0.05)
        self.fail(log.read_text())

    @staticmethod
    def stop_server(server):
        if server.stdin and not server.stdin.closed:
            server.stdin.close()
        # Teardown only: a slow shutdown on a loaded host is not a test failure.
        for stop in (None, server.terminate, server.kill):
            if stop:
                stop()
            try:
                server.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                pass

    def get(self, path, token=None):
        return self.request(path, token=token)

    def request(self, path, token=None, body=None, headers=None, *, method=None,
                timeout=REQUEST_TIMEOUT):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = urlopen(Request(self.url + path, data=data, headers=headers, method=method),
                               timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            if response.code == 401:
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
            if response.headers.get_content_type() == "application/json":
                return response.code, json.load(response)
            return response.code, response.read().decode("utf-8")


class BootstrapTest(ServerFixture, unittest.TestCase):
    def test_http_restart_backup_and_restore(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live.sqlite3"
            saved = Path(directory) / "backup.sqlite3"
            restored = Path(directory) / "restored.sqlite3"
            with self.subTest("HTTP roles and live backup"):
                server = self.start_server(path)
                try:
                    for role, correct, wrong in (("owner", OWNER, RUNNER), ("runner", RUNNER, OWNER)):
                        route = f"/v1/{role}/status"
                        self.assertEqual(self.get(route, correct), (200, {"role": role}))
                        self.assertEqual(self.get(route, wrong)[0], 403)
                        self.assertEqual(self.get(route)[0], 401)
                        self.assertEqual(self.get(route, "invalid")[0], 401)
                    with connect(path) as db:
                        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                        db.execute("CREATE TABLE sample (value TEXT)")
                        db.execute("INSERT INTO sample VALUES ('durable')")
                    self.run_cli(path, "backup", saved)
                    self.run_cli(path, "backup", saved, success=False)
                    with connect(path) as db:
                        db.execute("INSERT INTO sample VALUES ('after snapshot')")
                finally:
                    self.stop_server(server)

            self.run_cli(restored, "restore", saved)
            self.run_cli(path, "restore", saved, success=False)
            for db_path, expected in ((path, ["durable", "after snapshot"]), (restored, ["durable"])):
                with self.subTest(path=db_path.name):
                    server = self.start_server(db_path)
                    try:
                        with connect(db_path) as db:
                            self.assertEqual([row[0] for row in db.execute("SELECT value FROM sample ORDER BY rowid")],
                                             expected)
                    finally:
                        self.stop_server(server)

    def run_cli(self, database, command, argument, *, success=True):
        result = subprocess.run(
            [sys.executable, "-m", "phalanx", "server", command, str(argument)],
            env=dict(os.environ, PHALANX_DB=str(database)),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)

    def test_lease_seconds_defaults_and_rejects_invalid_values(self):
        from phalanx.config import lease_seconds
        with patch.dict(os.environ):
            os.environ.pop("PHALANX_LEASE_SECONDS", None)
            self.assertEqual(lease_seconds(), 300)
            os.environ["PHALANX_LEASE_SECONDS"] = "45"
            self.assertEqual(lease_seconds(), 45)
            for value in ("0", "-5", "29", "3601", "1.5", "", "five"):
                os.environ["PHALANX_LEASE_SECONDS"] = value
                with self.subTest(value=value), self.assertRaises(ValueError):
                    lease_seconds()

    def test_migration_refuses_foreign_and_future_databases(self):
        with TemporaryDirectory() as directory:
            foreign = Path(directory) / "foreign.sqlite3"
            with connect(foreign, mode="rwc") as db:
                db.execute("CREATE TABLE other (value TEXT)")
            with self.assertRaises(ValueError):
                migrate(foreign)
            future = Path(directory) / "future.sqlite3"
            migrate(future)
            with connect(future) as db:
                db.execute("PRAGMA user_version = 999")
            with self.assertRaises(ValueError):
                migrate(future)

    def test_serialized_transactions_commit_and_roll_back(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "writes.sqlite3"
            migrate(path)
            with write_transaction(path) as db:
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 3)
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 2000)
                db.execute("CREATE TABLE counter (value INTEGER)")
                db.execute("INSERT INTO counter VALUES (0)")

            with self.assertRaisesRegex(RuntimeError, "cancel write"):
                with write_transaction(path) as db:
                    db.execute("UPDATE counter SET value = -1")
                    raise RuntimeError("cancel write")

            def increment(_):
                with write_transaction(path) as db:
                    value = db.execute("SELECT value FROM counter").fetchone()[0]
                    db.execute("UPDATE counter SET value = ?", (value + 1,))

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(increment, range(32)))
            with connect(path, mode="ro") as db:
                self.assertEqual(db.execute("SELECT value FROM counter").fetchone()[0], 32)

    def test_reads_start_between_back_to_back_writes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reads.sqlite3"
            migrate(path)
            stop = time.monotonic() + 3

            def write(_):
                while time.monotonic() < stop:
                    with write_transaction(path) as db:
                        # Hold the exclusive lock like a slow commit flush, then hand straight off.
                        db.execute("COMMIT")
                        db.execute("BEGIN EXCLUSIVE")
                        time.sleep(0.08)

            def read(_):
                slowest = 0.0
                while time.monotonic() < stop:
                    started = time.monotonic()
                    with read_transaction(path) as db:
                        db.execute("PRAGMA user_version").fetchone()
                    slowest = max(slowest, time.monotonic() - started)
                    time.sleep(0.02)
                return slowest

            with ThreadPoolExecutor(max_workers=8) as pool:
                writers = [pool.submit(write, index) for index in range(6)]
                readers = list(pool.map(read, range(2)))
                for writer in writers:
                    writer.result()
            self.assertLess(max(readers), 1.0)

    def test_reader_starts_before_writers_queued_past_the_timeout(self):
        gate, order, release = store._Gate(), [], threading.Event()

        def hold():
            with gate.write():
                release.wait(WAIT_SECONDS)

        def write():
            with gate.write():
                order.append("write")

        def read():
            with gate.read_start():
                order.append("read")

        with patch.object(store, "BUSY_TIMEOUT", 1.0), ThreadPoolExecutor(max_workers=8) as pool:
            running = pool.submit(hold)
            time.sleep(0.1)
            writers = [pool.submit(write) for _ in range(5)]
            # Under a deep write backlog, queued writers have already waited longer than the bound.
            time.sleep(1.2)
            reader = pool.submit(read)
            time.sleep(0.1)
            release.set()
            for future in (running, reader, *writers):
                future.result(timeout=WAIT_SECONDS)
        self.assertEqual(order, ["read"] + ["write"] * 5)

    def test_reader_waits_out_a_write_longer_than_the_busy_timeout(self):
        gate, release = store._Gate(), threading.Event()

        def hold():
            with gate.write():
                release.wait(WAIT_SECONDS)

        def read():
            with gate.read_start():
                return "read"

        # A write turn may wait up to the busy timeout to begin and again to commit.
        with patch.object(store, "BUSY_TIMEOUT", 0.2), ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(hold)
            time.sleep(0.1)
            reader = pool.submit(read)
            time.sleep(0.5)
            release.set()
            running.result(timeout=WAIT_SECONDS)
            self.assertEqual(reader.result(timeout=WAIT_SECONDS), "read")

    def test_readers_arriving_while_a_writer_waits_go_after_it(self):
        gate, order = store._Gate(), []

        def write():
            with gate.write():
                order.append("write")

        def read():
            with gate.read_start():
                order.append("read")

        def until(predicate):
            deadline = time.monotonic() + WAIT_SECONDS
            while not predicate():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

        with ThreadPoolExecutor(max_workers=2) as pool:
            with gate.read_start():
                writer = pool.submit(write)
                until(lambda: gate.writers == 1)
                reader = pool.submit(read)
                until(lambda: gate.queued == 1)
                self.assertEqual(order, [])
            writer.result(timeout=WAIT_SECONDS)
            reader.result(timeout=WAIT_SECONDS)
        self.assertEqual(order, ["write", "read"])
