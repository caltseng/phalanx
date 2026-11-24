from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.request import urlopen

from phalanx import captains
from phalanx.store import write_transaction

from test_bootstrap import OWNER, REQUEST_TIMEOUT, RUNNER, ServerFixture


class OperatorWebTest(ServerFixture, unittest.TestCase):
    def test_owner_intake_from_board_is_validated_idempotent_and_same_origin(self):
        with TemporaryDirectory() as directory:
            server = self.start_server(Path(directory) / "create.sqlite3", PHALANX_LOCAL_BOARD="1")
            try:
                self.assertEqual(self.request("/v1/repositories", OWNER, {
                    "id": "example", "remote": "https://github.com/example/project.git",
                })[0], 200)
                self.assertEqual(self.get("/v1/repositories", OWNER)[1][0]["id"], "example")
                body = {"repository_id": "example", "goal": "**Queued from Scribe**",
                        "acceptance_criteria": ["Visible on board"], "label": "PH-29b"}
                headers = {"Origin": self.url, "Sec-Fetch-Site": "same-origin",
                           "Idempotency-Key": "scribe-create"}
                for token, status in ((None, 401), (RUNNER, 403), ("x" * 32, 401)):
                    self.assertEqual(self.request("/v1/items", token, body, headers)[0], status)
                for origin, site in (("https://evil.example", "cross-site"), ("null", "same-origin"),
                                     (self.url + ".evil.example", "same-origin"),
                                     (self.url, "same-site"), (None, "same-origin")):
                    cross = dict(headers, **{"Sec-Fetch-Site": site})
                    if origin is None:
                        cross.pop("Origin")
                    else:
                        cross["Origin"] = origin
                    self.assertEqual(self.request("/v1/items", OWNER, body, cross)[0], 403)
                self.assertEqual(self.request("/v1/items", OWNER, body, {
                    "Origin": "https://evil.example",
                })[0], 403)
                status, error = self.request("/v1/items", OWNER, dict(body, goal=" "), headers)
                self.assertEqual(status, 422)
                self.assertEqual(error["detail"][0]["loc"], ["body", "goal"])
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(
                        lambda _: self.request("/v1/items", OWNER, body, headers), range(2)))
                self.assertEqual(results[0][0], 201)
                self.assertEqual(results[0], results[1])
                item = results[0][1]
                self.assertEqual(item["execution"]["state"]["status"], "queued")
                self.assertEqual(item["execution"]["state"]["target"], "checked_in")
                self.assertEqual(self.request("/v1/items", OWNER, body, headers), results[0])
                page = self.get("/v1/operator/executions")[1]
                self.assertEqual([row["item_id"] for row in page["items"]], [item["id"]])
            finally:
                self.stop_server(server)

    def test_board_captain_read_uses_server_liveness(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "captains.sqlite3"
            server = self.start_server(path, PHALANX_LOCAL_BOARD="1")
            try:
                now = datetime.now(UTC)
                ids = {}
                with write_transaction(path) as db:
                    for kind in ("live", "stale", "ended"):
                        row = captains.register(db, captains.CaptainRegistration(
                            session_ref=kind, charter_revision="test"))
                        ids[kind] = row["id"]
                    db.execute("UPDATE captains SET heartbeat_at=? WHERE id=?",
                               ((now - captains.STALE - timedelta(seconds=1)).isoformat(), ids["stale"]))
                    db.execute("UPDATE captains SET state='ended' WHERE id=?", (ids["ended"],))
                    expected = {row["id"]: captains.is_live(row, now)
                                for row in db.execute("SELECT * FROM captains")}
                status, page = self.get("/v1/captains?limit=25")
                self.assertEqual(status, 200)
                rows = {row["id"]: row for row in page["captains"]}
                self.assertEqual({key: row["live"] for key, row in rows.items()}, expected)
                self.assertEqual([rows[ids[kind]]["stale"] for kind in ids], [False, True, False])
                self.assertEqual([rows[ids[kind]]["live"] for kind in ids], [True, False, False])
            finally:
                self.stop_server(server)

    def test_shell_and_assets_preserve_api_authentication(self):
        with TemporaryDirectory() as directory:
            server = self.start_server(Path(directory) / "operator.sqlite3")
            try:
                for route in ("/operator/", "/operator/queue?repository=example", "/operator/history",
                              f"/operator/items/{'1' * 32}", f"/operator/executions/{'2' * 32}"):
                    with self.subTest(route=route), urlopen(self.url + route, timeout=REQUEST_TIMEOUT) as response:
                        self.assertEqual(response.headers.get_content_type(), "text/html")
                        self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
                        self.assertIn("img-src data:;", response.headers["Content-Security-Policy"])
                        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                        shell = response.read().decode("utf-8")
                self.assertEqual(self.get("/operator/items/invalid")[0], 422)
                self.assertEqual(self.get("/operator/executions/invalid")[0], 422)
                self.assertEqual(self.get("/operator/runners")[0], 404)
                for asset, marker in (
                    ("operator-board.css", ".phalanx-board"),
                    ("operator-board.js", "PhalanxOperatorBoard"),
                    ("operator-markdown.js", "PhalanxMarkdown"),
                    ("operator-http.js", "/v1/operator/executions"),
                ):
                    path = f"/operator/assets/{asset}"
                    self.assertIn(path, shell)
                    with urlopen(self.url + path, timeout=REQUEST_TIMEOUT) as response:
                        content = response.read().decode("utf-8")
                        self.assertIn(marker, content)
                        self.assertNotIn(OWNER, content)
                        self.assertNotIn(RUNNER, content)

                self.assertNotIn(OWNER, shell)
                self.assertNotIn(RUNNER, shell)
                self.assertEqual(self.get("/v1/owner/status")[0], 401)
                self.assertEqual(self.get("/v1/owner/status", RUNNER)[0], 403)
                self.assertEqual(self.get("/v1/owner/status", OWNER), (200, {"role": "owner"}))
                self.assertEqual(self.get("/v1/operator/executions")[0], 401)
            finally:
                self.stop_server(server)

    def test_board_compaction_counts_survive_a_server_restart_and_legacy_heartbeat(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "counts.sqlite3"
            server = self.start_server(path, PHALANX_LOCAL_BOARD="1")
            try:
                status, captain = self.request("/v1/captains", RUNNER, {
                    "session_ref": "native-session", "charter_revision": "charter",
                })
                self.assertEqual(status, 201)
                route = f"/v1/captains/{captain['id']}"
                beat = {"charter_revision": "charter", "wake_count": 4, "state": "active"}
                self.assertEqual(self.request(route, RUNNER, dict(beat, compaction_count=2),
                                              method="PUT")[0], 200)
            finally:
                self.stop_server(server)
            server = self.start_server(path, PHALANX_LOCAL_BOARD="1")
            try:
                self.assertEqual(self.request(route, RUNNER, beat, method="PUT")[0], 200)
                for row in (self.get(route)[1], self.get("/v1/captains")[1]["captains"][0]):
                    self.assertEqual(row["compaction_count"], 2)
                    self.assertTrue(row["live"])
                    self.assertEqual(row["wake_count"], 4)
            finally:
                self.stop_server(server)

    def test_local_board_reads_without_token_only_for_local_hosts(self):
        board = "/v1/operator/executions"
        report = f"/v1/items/{'0' * 32}/executions/{'1' * 32}/report"
        with TemporaryDirectory() as directory:
            server = self.start_server(Path(directory) / "local.sqlite3", PHALANX_LOCAL_BOARD="1")
            try:
                port = self.url.rpartition(":")[2]
                for host in ("127.0.0.1", f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
                    with self.subTest(host=host):
                        status, page = self.request(board, headers={"Host": host})
                        self.assertEqual((status, page["items"]), (200, []))
                self.assertEqual(self.get(f"{board}/{'1' * 32}/timeline")[0], 404)
                self.assertEqual(self.get(report)[0], 404)
                for host in ("evil.example", f"evil.example:{port}", "localhost.evil.example", "127.0.0.1.nip.io"):
                    with self.subTest(host=host):
                        self.assertEqual(self.request(board, headers={"Host": host})[0], 401)
                        self.assertEqual(self.request(report, headers={"Host": host})[0], 401)
                self.assertEqual(self.get(board, "x" * 32)[0], 401)
                item_route = f"/v1/operator/items/{'0' * 32}"
                self.assertEqual(self.get(item_route)[0], 404)
                self.assertEqual(self.request(item_route, headers={"Host": "evil.example"})[0], 401)
                for path in ("/v1/owner/status", "/v1/runner/status", "/v1/repositories",
                             f"/v1/items/{'0' * 32}"):
                    with self.subTest(path=path):
                        self.assertEqual(self.get(path)[0], 401)
                self.assertEqual(self.request("/v1/items", body={"goal": "x"})[0], 401)
                self.assertEqual(self.request("/v1/writer/attempt", body={}, method="POST")[0], 401)
                self.assertEqual(self.get("/v1/dispatch/settings")[1]["max_writers"], 5)
                self.assertEqual(self.request("/v1/dispatch/settings",
                                              headers={"Host": "evil.example"})[0], 401)
                self.assertEqual(self.request("/v1/dispatch/settings", body={"max_writers": 9},
                                              method="PUT")[0], 401)
            finally:
                self.stop_server(server)
