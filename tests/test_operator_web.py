from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.request import urlopen

from test_bootstrap import OWNER, RUNNER, ServerFixture


class OperatorWebTest(ServerFixture, unittest.TestCase):
    def test_shell_and_assets_preserve_api_authentication(self):
        with TemporaryDirectory() as directory:
            server = self.start_server(Path(directory) / "operator.sqlite3")
            try:
                with urlopen(self.url + "/operator/", timeout=2) as response:
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                    self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                    shell = response.read().decode("utf-8")
                for asset, marker in (
                    ("operator-board.css", ".phalanx-board"),
                    ("operator-board.js", "PhalanxOperatorBoard"),
                    ("operator-http.js", "/v1/operator/executions"),
                ):
                    path = f"/operator/assets/{asset}"
                    self.assertIn(path, shell)
                    with urlopen(self.url + path, timeout=2) as response:
                        content = response.read().decode("utf-8")
                        self.assertIn(marker, content)
                        self.assertNotIn(OWNER, content)
                        self.assertNotIn(RUNNER, content)

                self.assertNotIn(OWNER, shell)
                self.assertNotIn(RUNNER, shell)
                self.assertEqual(self.get("/v1/owner/status")[0], 401)
                self.assertEqual(self.get("/v1/owner/status", RUNNER)[0], 403)
                self.assertEqual(self.get("/v1/owner/status", OWNER), (200, {"role": "owner"}))
            finally:
                self.stop_server(server)
