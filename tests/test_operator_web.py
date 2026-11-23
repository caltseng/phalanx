from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.request import urlopen

from test_bootstrap import OWNER, REQUEST_TIMEOUT, RUNNER, ServerFixture


class OperatorWebTest(ServerFixture, unittest.TestCase):
    def test_shell_and_assets_preserve_api_authentication(self):
        with TemporaryDirectory() as directory:
            server = self.start_server(Path(directory) / "operator.sqlite3")
            try:
                with urlopen(self.url + "/operator/", timeout=REQUEST_TIMEOUT) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                    self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertIn("img-src data:;", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                    shell = response.read().decode("utf-8")
                for asset, marker in (
                    ("operator-board.css", ".phalanx-board"),
                    ("operator-board.js", "PhalanxOperatorBoard"),
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
                for path in ("/v1/owner/status", "/v1/runner/status", "/v1/repositories",
                             f"/v1/items/{'0' * 32}"):
                    with self.subTest(path=path):
                        self.assertEqual(self.get(path)[0], 401)
                self.assertEqual(self.request("/v1/items", body={"goal": "x"})[0], 401)
                self.assertEqual(self.request("/v1/dispatch/claim", body={}, method="POST")[0], 401)
            finally:
                self.stop_server(server)
