from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from phalanx.store import connect, migrate, write_transaction


OWNER = "o" * 32
RUNNER = "r" * 32

# Let the real server exit before its Python launcher exits on Windows.
SERVER_PROCESS = """
import sys
from threading import Thread

import uvicorn

server = uvicorn.Server(uvicorn.Config("phalanx.api:app", port=int(sys.argv[1])))

def stop_on_eof():
    sys.stdin.buffer.read()
    server.should_exit = True

Thread(target=stop_on_eof, daemon=True).start()
server.run()
"""


class ServerFixture:
    def start_server(self, path):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, PHALANX_DB=str(path), PHALANX_OWNER_TOKEN=OWNER,
                   PHALANX_RUNNER_TOKEN=RUNNER)
        log = path.with_suffix(".log")
        with log.open("w") as output:
            server = subprocess.Popen(
                [sys.executable, "-c", SERVER_PROCESS, str(port)],
                env=env, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
            )
        self.addCleanup(self.stop_server, server)
        deadline = time.monotonic() + 10
        while server.poll() is None and time.monotonic() < deadline:
            try:
                if self.get("/health") == (200, {"status": "ok"}):
                    return server
            except (URLError, TimeoutError):
                pass
            time.sleep(0.05)
        self.fail(log.read_text())

    @staticmethod
    def stop_server(server):
        if server.stdin and not server.stdin.closed:
            server.stdin.close()
        if server.poll() is None:
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.terminate()
                server.wait(timeout=10)
                raise

    def get(self, path, token=None):
        return self.request(path, token=token)

    def request(self, path, token=None, body=None, headers=None):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = urlopen(Request(self.url + path, data=data, headers=headers), timeout=1)
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
            [sys.executable, "-m", "phalanx", command, str(argument)],
            env=dict(os.environ, PHALANX_DB=str(database)),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)

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
