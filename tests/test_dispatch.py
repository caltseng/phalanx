"""Direct dispatch settings and visible intake."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from phalanx.store import connect, initialize, write_transaction
from test_bootstrap import OWNER, RUNNER, ServerFixture


class DispatchTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "queue.sqlite3"
        initialize(self.path)
        self.start_server(self.path)
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": "https://github.com/e/r.git"})

    def test_capacity_is_owner_controlled_validated_and_persistent(self):
        route = "/v1/dispatch/settings"
        initial = self.get(route, RUNNER)
        self.assertEqual(initial, (200, {"paused": False, "aging_seconds": 300,
                                       "max_writers": 10, "active_writers": 0,
                                       "max_building_writers": 5, "building_writers": 0,
                                       "delivering_writers": 0}))
        for token, code in ((None, 401), (RUNNER, 403), ("x" * 32, 401)):
            self.assertEqual(self.request(route, token, {"max_writers": 9}, method="PUT")[0], code)
        for field in ("max_writers", "max_building_writers"):
            for value in (0, -1, True, 1.5, "5", None, 2**64):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.request(route, OWNER, {field: value}, method="PUT")[0], 422)
        self.assertEqual(self.request(route, OWNER, {"max_writers": 4}, method="PUT")[0], 409)
        self.assertEqual(self.request(route, OWNER, {"max_building_writers": 11}, method="PUT")[0], 409)
        self.assertEqual(self.get(route, OWNER), initial)
        self.assertEqual(self.request(route, OWNER, {"max_writers": 3, "max_building_writers": 2},
                                      method="PUT")[0], 200)
        for field in ("paused", "aging_seconds"):
            with self.subTest(field=field):
                self.assertEqual(self.request(route, OWNER, {field: None}, method="PUT")[0], 422)
        self.assertEqual(self.request(route, OWNER, {"max_writers": 9},
                                      {"Origin": "https://evil.example"}, method="PUT")[0], 403)
        self.assertEqual(self.request(route, OWNER, {"paused": True, "aging_seconds": 42,
                                                    "max_writers": 7}, method="PUT")[0], 200)
        status, saved = self.request(route, OWNER, {"max_writers": 6}, method="PUT")
        self.assertEqual((status, saved), (200, {"paused": True, "aging_seconds": 42,
                                               "max_writers": 6, "active_writers": 0,
                                               "max_building_writers": 2, "building_writers": 0,
                                               "delivering_writers": 0}))
        initialize(self.path)
        with connect(self.path, mode="ro") as db:
            row = db.execute("SELECT * FROM dispatch_settings").fetchone()
            self.assertEqual((row["paused"], row["aging_seconds"], row["max_writers"]), (1, 42, 6))
        # A fresh server process reads the same saved limit, not a charter or a default.
        self.start_server(self.path)
        self.assertEqual(self.get(route, OWNER), (200, saved))



    def test_unpinned_intake_has_a_visible_direct_wait_and_is_launchable(self):
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Unpinned work", "target": "report",
        })
        self.assertEqual(status, 201)
        row = self.get("/v1/operator/executions?kind=open", OWNER)[1]["items"][0]
        self.assertEqual(row["wait_reason"], "No live active Captain with pickup authority")
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
