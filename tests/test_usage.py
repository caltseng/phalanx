from argparse import Namespace
from copy import deepcopy
from datetime import UTC, datetime
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx import intake, shift, work_cli
from phalanx.store import read_transaction

from test_bootstrap import OWNER, RUNNER, ServerFixture


class UsageTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "usage.sqlite3"
        self.start_server(self.path)
        self.ok("/v1/repositories", OWNER, {"id": "repo", "remote": "https://github.com/e/r.git"})
        self.captain = self.ok("/v1/captains", RUNNER, {
            "session_ref": "native-captain", "charter_revision": "c1",
            "pickup": {"repositories": {"repo": None}}}, code=201)["id"]
        self.item, self.execution, self.writer, self.token = self.register("native-writer")

    def ok(self, route, token=OWNER, body=None, code=200, **options):
        status, response = self.request(route, token, body, **options)
        self.assertEqual(status, code, response)
        return response

    def register(self, session, item=None, replaces=None):
        if item is None:
            data = self.ok("/v1/items", OWNER, {
                "repository_id": "repo", "goal": "Measure only this writer", "target": "report"}, code=201)
            item, execution = data["id"], data["execution"]["id"]
        else:
            data = self.ok(f"/v1/items/{item}")
            execution = data["execution"]["id"]
        version = self.ok(f"/v1/items/{item}")["execution"]["version"]
        token = token_urlsafe(32)
        binding = self.ok(f"/v1/items/{item}/executions/{execution}/writer", RUNNER, {
            "captain_id": self.captain, "runtime": "native-app",
            "expected_version": version, "nonce": token_urlsafe(16), "token_digest": intake.digest(token),
            **({"replaces": replaces, "replaced_stopped": True} if replaces else {})})
        writer = binding["writer"]["id"]
        self.ok(f"/v1/items/{item}/executions/{execution}/writer", RUNNER, {
            "captain_id": self.captain, "writer_id": writer, "session_id": session}, method="PATCH")
        return item, execution, writer, token

    def snapshot(self, **fields):
        binding = self.ok("/v1/writer", self.token)
        start = binding["writer"]["confirmed_at"]
        now = datetime.now(UTC).isoformat()
        return {
            "runtime": "native-app", "provider": "provider-a", "account": "authorized-account",
            "origin": "native-history-local", "session_id": "native-writer",
            "source": "app_session_metadata", "source_version": "fixture-v1", "epoch": "session-created",
            "model": None, "sequence": 1, "window_start": start, "cutoff": now, "observed_at": now,
            "scope": "item_exclusive", "status": "pending_final", "reason": "Last native turn not observed",
            "tokens": {"input": 10, "output": 0, "cache_read": None, "cache_write": None,
                       "reasoning": None, "total": None}, "credits": None, "money": None, **fields,
        }

    def detail(self, execution=None):
        return self.ok(f"/v1/operator/executions/{execution or self.execution}/timeline")

    def lifecycle(self):
        with read_transaction(self.path) as db:
            return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                    for table in ("executions", "attempts", "writers", "dispatch_settings",
                                  "transition_history", "flags")}

    def coordinator_route(self, writer=None, item=None, execution=None):
        return (f"/v1/items/{item or self.item}/executions/{execution or self.execution}"
                f"/writers/{writer or self.writer}/usage")

    def finish(self):
        for field in ({"report_markdown": "# Report\n"},
                      {"report_revision": intake.digest("# Report\n")},
                      {"report_revision": intake.digest("# Report\n")}):
            binding = self.ok("/v1/writer", self.token)
            self.ok(f"/v1/items/{self.item}/executions/{self.execution}/result", self.token, {
                "attempt_id": binding["writer"]["attempt_id"], "stage": binding["state"]["stage"],
                "expected_version": binding["version"], "outcome": "succeeded", **field})

    def test_cumulative_replay_zero_partial_and_no_lifecycle_side_effects(self):
        snapshot = self.snapshot()
        before = self.lifecycle()
        with read_transaction(self.path) as db:
            change = db.execute("SELECT token FROM operator_change").fetchone()[0]
        self.ok("/v1/writer/usage", self.token, snapshot)
        self.assertFalse(self.ok("/v1/writer/usage", self.token, snapshot)["accepted"])
        self.assertEqual(before, self.lifecycle())
        with read_transaction(self.path) as db:
            self.assertNotEqual(change, db.execute("SELECT token FROM operator_change").fetchone()[0])
        usage = self.detail()["usage"]
        self.assertEqual(usage["execution"]["tokens"]["input"], 10)
        self.assertEqual(usage["execution"]["tokens"]["output"], 0)
        self.assertIsNone(usage["execution"]["tokens"]["cache_write"])
        self.assertEqual(usage["execution"]["covered_sessions"], 1)
        self.assertEqual(usage["execution"]["status"], "pending_final")
        self.ok("/v1/writer/usage", self.token, dict(snapshot, sequence=0), code=409)
        self.ok("/v1/writer/usage", self.token, dict(snapshot, sequence=2, tokens={"input": 9}), code=409)
        newer = deepcopy(snapshot)
        newer.update(sequence=2, tokens={**snapshot["tokens"], "input": 20})
        self.ok("/v1/writer/usage", self.token, newer)
        self.assertEqual(self.detail()["usage"]["execution"]["tokens"]["input"], 20)
        self.assertEqual(before, self.lifecycle())

    def test_authorization_and_series_identity_are_fenced(self):
        snapshot = self.snapshot()
        _, _, _, other_token = self.register("other-native")
        self.ok("/v1/writer/usage", other_token, snapshot, code=409)
        self.ok(self.coordinator_route(), self.token, {
            "captain_id": self.captain, "snapshot": snapshot}, code=403)
        other_captain = self.ok("/v1/captains", RUNNER, {
            "session_ref": "other", "charter_revision": "c1"}, code=201)["id"]
        self.ok(self.coordinator_route(), RUNNER, {
            "captain_id": other_captain, "snapshot": snapshot}, code=403)
        self.ok(self.coordinator_route(item="wrong"), RUNNER, {
            "captain_id": self.captain, "snapshot": snapshot}, code=404)
        self.ok("/v1/writer/usage", self.token, snapshot)
        for name, value in (("origin", "mirror"), ("epoch", "reset"), ("model", "partition"),
                            ("account", "other"), ("scope", "session_lifetime")):
            self.ok("/v1/writer/usage", self.token, dict(snapshot, sequence=2, **{name: value}), code=409)
        for tokens in ({"input": -1}, {"input": True}, {"input": 1.5}, {"cost": 3}):
            self.ok("/v1/writer/usage", self.token, dict(snapshot, tokens=tokens), code=422)
        self.ok("/v1/writer/usage", self.token, dict(snapshot, prompt="private"), code=422)

    def test_acquisition_failure_retains_numbers_without_blocking_work(self):
        snapshot = self.snapshot()
        self.ok("/v1/writer/usage", self.token, snapshot)
        before = self.lifecycle()
        self.ok("/v1/writer/usage", self.token, dict(
            snapshot, sequence=2, status="unavailable", tokens={}, reason="Metadata access unavailable; retry next native pass"))
        row = self.detail()["usage"]
        self.assertEqual(row["execution"]["tokens"]["input"], 10)
        self.assertEqual(row["execution"]["status"], "partial")
        self.assertIn("Metadata access", row["sessions"][0]["reason"])
        self.assertEqual(before, self.lifecycle())

    def test_late_observation_does_not_revive_ended_writer_or_advance_execution(self):
        snapshot = self.snapshot()
        self.ok("/v1/writer/usage", self.token, snapshot)
        self.finish()
        with read_transaction(self.path) as db:
            ended_at = db.execute("SELECT ended_at FROM writers WHERE id=?", (self.writer,)).fetchone()[0]
        snapshot.update(sequence=2, cutoff=ended_at, observed_at=datetime.now(UTC).isoformat(),
                        status="observed", reason="Final bounded source refresh")
        before = self.lifecycle()
        self.ok("/v1/writer/usage", self.token, snapshot, code=401)
        successor = self.ok("/v1/captains", RUNNER, {
            "session_ref": "successor", "charter_revision": "c1",
            "pickup": {"repositories": {"repo": None}}}, code=201)["id"]
        before = self.lifecycle()
        self.ok(self.coordinator_route(), RUNNER, {"captain_id": successor, "snapshot": snapshot})
        self.assertEqual(before, self.lifecycle())
        self.assertEqual(self.detail()["usage"]["execution"]["status"], "observed")
        snapshot.update(sequence=3, cutoff=datetime.now(UTC).isoformat(),
                        observed_at=datetime.now(UTC).isoformat())
        self.ok(self.coordinator_route(), RUNNER, {"captain_id": self.captain, "snapshot": snapshot}, code=409)

    def test_rerun_rollup_separates_current_and_all_executions(self):
        snapshot = self.snapshot()
        self.ok("/v1/writer/usage", self.token, snapshot)
        self.finish()
        closed = self.ok(f"/v1/items/{self.item}")["execution"]
        self.ok(f"/v1/items/{self.item}/executions/{self.execution}/rerun", OWNER,
                {"expected_version": closed["version"], "target": "report"})
        _, execution, writer, token = self.register("new-native", self.item)
        current = self.ok("/v1/writer", token)
        newer = dict(snapshot, session_id="new-native", scope="item_exclusive",
                     window_start=current["writer"]["confirmed_at"], cutoff=datetime.now(UTC).isoformat(),
                     observed_at=datetime.now(UTC).isoformat())
        self.ok("/v1/writer/usage", token, newer)
        usage = self.detail(execution)["usage"]
        self.assertEqual(usage["execution"]["tokens"]["input"], 10)
        self.assertEqual(usage["item"]["tokens"]["input"], 20)
        self.assertEqual(usage["item"]["covered_sessions"], 2)
        self.assertEqual(usage["item"]["bound_sessions"], 2)
        self.assertEqual(usage["item"]["status"], "pending_final")

    def test_replacement_retains_prior_usage_once_and_revokes_old_transport(self):
        snapshot = self.snapshot()
        self.ok("/v1/writer/usage", self.token, snapshot)
        _, _, writer, token = self.register("replacement-native", self.item, self.writer)
        current = self.ok("/v1/writer", token)
        newer = dict(snapshot, session_id="replacement-native",
                     window_start=current["writer"]["confirmed_at"], cutoff=datetime.now(UTC).isoformat(),
                     observed_at=datetime.now(UTC).isoformat(), tokens={"input": 20})
        self.ok("/v1/writer/usage", token, newer)
        self.ok("/v1/writer/usage", token, newer)
        self.ok("/v1/writer/usage", self.token, snapshot, code=401)
        usage = self.detail()["usage"]
        self.assertEqual(usage["execution"]["tokens"]["input"], 30)
        self.assertEqual(usage["execution"]["covered_sessions"], 2)

    def test_shared_lifetime_without_baseline_is_visible_but_excluded(self):
        self.ok("/v1/writer/usage", self.token, self.snapshot(scope="session_lifetime"))
        usage = self.detail()["usage"]
        self.assertIsNone(usage["execution"]["tokens"]["input"])
        self.assertEqual(usage["sessions"][0]["snapshot"]["tokens"]["input"], 10)
        self.assertFalse(usage["sessions"][0]["included"])

    def test_missing_and_reused_native_sessions_are_not_zero_or_summed(self):
        usage = self.detail()["usage"]
        self.assertEqual(usage["execution"]["status"], "unavailable")
        self.assertIsNone(usage["execution"]["tokens"]["input"])
        snapshot = self.snapshot()
        self.ok("/v1/writer/usage", self.token, snapshot)
        self.register("native-writer")
        usage = self.detail()["usage"]
        self.assertIsNone(usage["execution"]["tokens"]["input"])
        self.assertIn("reused", usage["sessions"][0]["reason"])

    def test_quantities_keep_estimates_and_units_without_a_billing_sum(self):
        snapshot = self.snapshot(
            credits={"amount": "1.25", "unit": "provider-a credit", "basis": "estimated", "source": "native UI"},
            money={"amount": "0.01", "unit": "USD", "basis": "estimated", "source": "native UI"})
        self.ok("/v1/writer/usage", self.token, snapshot)
        usage = self.detail()["usage"]
        self.assertEqual(usage["sessions"][0]["snapshot"]["credits"]["basis"], "estimated")
        self.assertNotIn("credits", usage["item"])
        self.ok("/v1/writer/usage", self.token, dict(snapshot, sequence=2, credits={
            **snapshot["credits"], "unit": "other credit"}), code=409)

    def test_native_numeric_capture_transports_through_work_and_captain_clients(self):
        snapshot = self.snapshot(tokens={"input": 571954, "output": 3831,
                                         "cache_read": 508365, "cache_write": 63565})
        path = self.path.parent / "numeric-only.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with patch.object(work_cli, "credential", return_value=(self.url, self.token)), redirect_stdout(StringIO()):
            work_cli.run_work(Namespace(work_command="usage", file=path))
        snapshot["sequence"] = 2
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        config = {"base_url": self.url, "runtime": "native-app"}
        api = lambda route, **options: work_cli.request(self.url, RUNNER, route, **options)
        with (patch.object(shift, "load_config", return_value=config),
              patch.object(shift, "load_charter", return_value={}),
              patch.object(shift, "API", return_value=api), redirect_stdout(StringIO())):
            shift.run_shift(Namespace(shift_command="usage", config=path, home=None, captain=self.captain,
                                      item=self.item, execution=self.execution, writer=self.writer, file=path))
        observed = self.detail()["usage"]["execution"]["tokens"]
        self.assertEqual(observed["input"], 571954)
        self.assertEqual(observed["cache_write"], 63565)


if __name__ == "__main__":
    unittest.main()
