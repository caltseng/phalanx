"""Retained production HTTP vectors from the independent Python retirement audit."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from phalanx.transitions import item_branch
from test_bootstrap import OWNER, RUNNER, ServerFixture


class RetirementContractTest(ServerFixture, unittest.TestCase):
    def test_completed_deployment_retirement_after_window_and_renamed_check(self):
        for configured in (None, "Replacement", "Deploy"):
            with self.subTest(configured=configured), TemporaryDirectory() as temporary:
                path = Path(temporary) / "fixture.sqlite3"
                server = self.start_server(path)
                try:
                    self.assertEqual(self.request("/v1/repositories", OWNER, {
                        "id": "fixture", "remote": "https://github.com/example/fixture.git",
                    })[0], 200)
                    code, item = self.request("/v1/items", OWNER, {
                        "repository_id": "fixture", "goal": "Retained G3 contract",
                    }, {"Idempotency-Key": "g3-contract"})
                    self.assertEqual(code, 201)
                    execution = item["execution"]
                    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
                    head, base, merge = "a" * 40, "b" * 40, "c" * 40
                    url = "https://github.com/example/fixture/pull/1"
                    state = dict(execution["state"], stage="delivery", status="waiting_for_external",
                                 candidate=head, primary_pr=url,
                                 wait={"reason": "Fixture delivery", "trigger": "observation"})
                    with closing(sqlite3.connect(path)) as db:
                        db.execute("UPDATE executions SET state_json=? WHERE id=?",
                                   (json.dumps(state), execution["id"]))
                        db.commit()
                    route = f"/v1/items/{item['id']}/executions/{execution['id']}/github-observation"
                    code, response = self.request(route, RUNNER, {
                        "observed_at": old, "target_base": "main",
                        "pr": {"number": 1, "state": "closed", "merged": True, "draft": False,
                               "head_ref": item_branch(item["id"]), "head_sha": head,
                               "base_ref": "main", "base_sha": base, "merge_commit": merge,
                               "merged_at": old},
                        "deployment": {"commit": merge, "check_name": "Deploy", "state": "pending"},
                    })
                    self.assertEqual(code, 200, response)
                    with closing(sqlite3.connect(path)) as db:
                        db.execute("UPDATE executions SET updated_at=? WHERE id=?", (old, execution["id"]))
                        db.commit()
                        persisted, = db.execute("SELECT state_json FROM executions WHERE id=?",
                                                (execution["id"],)).fetchone()
                    self.assertEqual(json.loads(persisted)["status"], "completed")
                    before = self.durable(path)
                    heartbeat = {"deployment_checks": {"fixture": configured}}
                    for _ in range(2):
                        code, response = self.request("/v1/reconcilers/contract/heartbeat", RUNNER, heartbeat)
                        self.assertEqual(code, 200, response)
                        after = self.durable(path)
                        for table in before:
                            if table not in ("github_observations", "reconciler_heartbeats", "operator_change"):
                                self.assertEqual(before[table], after[table], table)
                        with closing(sqlite3.connect(path)) as db:
                            latest, = db.execute(
                                "SELECT observation_json FROM github_observations "
                                "ORDER BY last_observed_at DESC,observed_at DESC LIMIT 1").fetchone()
                            heartbeat_count, = db.execute(
                                "SELECT count(*) FROM reconciler_heartbeats WHERE runner_id='contract'").fetchone()
                        self.assertEqual(heartbeat_count, 1)
                        self.assertEqual(json.loads(latest)["deployment"]["state"],
                                         "pending" if configured == "Deploy" else "disabled")
                        if configured != "Deploy":
                            code, flags = self.get("/v1/flags", OWNER)
                            self.assertEqual(code, 200)
                            self.assertFalse([flag for flag in flags["flags"]
                                              if flag["execution_id"] == execution["id"]])
                        before = after
                finally:
                    self.stop_server(server)

    @staticmethod
    def durable(path):
        with closing(sqlite3.connect(path)) as db:
            names = [name for name, in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: sorted(db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr)
                    for name in names}
