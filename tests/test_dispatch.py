"""Direct dispatch settings and visible intake."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from phalanx.store import migrate
from test_bootstrap import OWNER, RUNNER, ServerFixture


class DispatchTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "queue.sqlite3"
        migrate(self.path)
        self.start_server(self.path)
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": "https://github.com/e/r.git"})

    def test_unpinned_intake_has_a_visible_direct_wait_and_is_launchable(self):
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Unpinned work", "target": "report",
        })
        self.assertEqual(status, 201)
        row = self.get("/v1/operator/executions?kind=open", OWNER)[1]["items"][0]
        self.assertEqual(row["wait_reason"], "Awaiting a direct Captain")
        status, captain = self.request("/v1/captains", RUNNER, {
            "session_ref": "captain", "charter_revision": "one",
        })
        self.assertEqual(status, 201)
        route = f"/v1/items/{item['id']}/executions/{item['execution']['id']}/writer"
        body = {"expected_version": item["execution"]["version"], "captain_id": captain["id"],
                "runtime": "native", "nonce": "n" * 16, "token_digest": "a" * 64}
        self.assertEqual(self.request("/v1/dispatch/settings", RUNNER, {"paused": True}, method="PUT")[0], 403)
        self.assertEqual(self.request("/v1/dispatch/settings", OWNER, {"paused": True}, method="PUT")[0], 200)
        self.assertEqual(self.request(route, RUNNER, body)[0], 409)
        self.request("/v1/dispatch/settings", OWNER, {"paused": False}, method="PUT")
        status, binding = self.request(route, RUNNER, body)
        self.assertEqual(status, 200, binding)
        self.assertEqual(binding["state"]["status"], "running")
