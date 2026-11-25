from argparse import Namespace
from contextlib import closing
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from phalanx import captains, shift
from phalanx.store import initialize, write_transaction
from phalanx.work_cli import credential, request

from cli_runner import run_cli
from test_bootstrap import OWNER, RUNNER, ServerFixture


CHARTER = """goal = "Deliver the pilot's queued work"
repositories = ["repo"]
allowed_actions = ["launch and replace writer sessions"]
escalate = ["anything outside the goal"]
stop_when = ["the owner ends the run"]
"""


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args], cwd=cwd,
                   check=True, capture_output=True, text=True)


class CharterTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q")
        (self.root / "charter").mkdir()
        self.config = {"base_url": "http://127.0.0.1:1", "runtime": "any-runtime",
                       "worktrees": str(self.root / "worktrees"), "charter": str(self.root / "charter" / "c.toml"),
                       "repositories": {"repo": {"checkout": str(self.checkout), "base_branch": "main"}}}

    def charter(self, text, path=None):
        path = Path(path or self.config["charter"])
        path.write_text(text, encoding="utf-8")
        return shift.load_charter(dict(self.config, charter=str(path)))

    def test_charter_is_validated_and_revisioned(self):
        charter = self.charter(CHARTER + "[shift]\nmax_hours = 2\n")
        self.assertEqual(charter["revision"], hashlib.sha256(Path(self.config["charter"]).read_bytes()).hexdigest())
        self.assertEqual(charter["shift"], {"max_hours": 2})
        for text in (CHARTER + 'extra = "x"\n', CHARTER.replace('["the owner ends the run"]', "[]"),
                     CHARTER + "[shift]\nmax_hours = 0\n",
                     CHARTER + "[shift]\nmax_hours = true\n", "goal = [\n"):
            with self.subTest(text=text), self.assertRaises(shift.ShiftError):
                self.charter(text)
        self.assertEqual(self.charter(CHARTER.replace('["repo"]', '["repo", "other"]'))["repositories"],
                         ["repo", "other"])

    def test_charter_rejects_writer_limits_with_dispatch_settings_guidance(self):
        for field in ("max_writers", "max_building_writers"):
            for value in ("5", "0", "true", '"4"'):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                        shift.ShiftError,
                        "concurrency moved to dispatch settings; remove it from the charter"):
                    self.charter(CHARTER + f"[shift]\n{field} = {value}\n")

    def test_charter_rejects_retired_count_limits_with_elapsed_time_guidance(self):
        for field in ("max_wakes", "max_compactions"):
            with self.subTest(field=field), self.assertRaisesRegex(
                    shift.ShiftError, "rotation is elapsed-time only; remove"):
                self.charter(CHARTER + f"[shift]\n{field} = 5\n")

    def test_rotation_is_due_at_24_elapsed_hours_regardless_of_counts_or_queue_stop_prose(self):
        charter = self.charter(CHARTER)
        old_charter = self.charter(CHARTER.replace("the owner ends the run", "the queue is empty"))
        now = datetime.now(UTC)
        captain = {"created_at": now.isoformat(), "wake_count": 0}
        for policy in (charter, old_charter):
            for count in (None, 0, 5, 100):
                for wakes in (0, 48, 1000):
                    for seconds in (21 * 60, 4 * 3600, 24 * 3600 - 1, 24 * 3600, 24 * 3600 + 1):
                        with self.subTest(count=count, wakes=wakes, seconds=seconds, policy=policy["stop_when"]):
                            current = dict(captain, wake_count=wakes, compaction_count=count)
                            bounds = shift.shift_bounds(current, policy, now + timedelta(seconds=seconds))
                            self.assertEqual(bounds["compaction_count"], count)
                            self.assertEqual(bounds["wakes"], wakes)
                            self.assertEqual(bounds["handoff_due"],
                                             ["shift age reached 24 h"] if seconds >= 24 * 3600 else [])
        self.assertIsNone(shift.shift_bounds(captain, charter, now)["compaction_count"])

    def test_charter_must_live_outside_checkouts_and_worktrees(self):
        for path in (self.checkout / "c.toml", self.root / "worktrees" / "c.toml"):
            path.parent.mkdir(exist_ok=True)
            with self.subTest(path=path), self.assertRaisesRegex(shift.ShiftError, "outside every checkout"):
                self.charter(CHARTER, path)

    def test_reinforcement_boundaries_and_clock_rollback(self):
        created = datetime(2026, 1, 1, tzinfo=UTC)
        row = {"created_at": created.isoformat(), "state": "active", "reinforced_interval": 0}
        for seconds, interval in ((-60, 0), (0, 0), (5399, 0), (5400, 1), (5401, 1),
                                  (10799, 1), (10800, 2), (21601, 4)):
            with self.subTest(seconds=seconds):
                result = captains.reinforcement(row, created + timedelta(seconds=seconds))
                self.assertEqual(result["interval"], interval)
                self.assertEqual(result["due"], interval > 0)
        acknowledged = dict(row, reinforced_interval=4)
        self.assertFalse(captains.reinforcement(acknowledged, created + timedelta(hours=3))["due"])
        for state in ("ending", "ended"):
            self.assertFalse(captains.reinforcement(dict(row, state=state), created + timedelta(days=2))["due"])

    def test_checks_environment_config_requires_non_empty_text_when_present(self):
        path = self.root / "shift.json"
        mapping = self.config["repositories"]["repo"]
        for value in ("", " \t", None, ["python"], 1):
            with self.subTest(value=value):
                mapping["checks_environment"] = value
                path.write_text(json.dumps(self.config), encoding="utf-8")
                with self.assertRaisesRegex(shift.ShiftError, "repo checks_environment must be non-empty text"):
                    shift.load_config(path)
        mapping["checks_environment"] = "uv run --no-sync python"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertEqual(shift.load_config(path), self.config)
        del mapping["checks_environment"]
        path.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertEqual(shift.load_config(path), self.config)

    def test_overdue_guidance_is_in_ordinary_wakes_and_canonical_reinforcement(self):
        for name in ("captain", "wake"):
            text = " ".join(shift.prompt(name).split())
            with self.subTest(prompt=name):
                for instruction in ("`active_overdue`", "90 elapsed minutes", "first persisted attempt",
                                    "green open PR is not checked-in completion",
                                    "responsible actor and resume trigger", "`shift action`",
                                    "original batch reason no longer applies"):
                    self.assertIn(instruction, text)
                self.assertIn("five-minute", text)
                self.assertIn("24-hour", text)
                self.assertIn("90-minute reinforcement", text)

    def test_kickoff_names_the_current_executions_target_not_the_original_request(self):
        item = {"id": "item", "repository_id": "repo", "target": "checked_in", "goal": "Rerun for review",
                "execution": {"id": "execution", "state": {"target": "ready_for_check_in"}}}
        prompt = shift.kickoff(item, "worktree", "nonce", "phx work", "main")
        self.assertIn("Target: ready_for_check_in", prompt)
        self.assertNotIn("Target: checked_in", prompt)

    def test_draft_kickoff_never_instructs_promotion_or_auto_merge(self):
        item = {"id": "item", "repository_id": "repo", "target": "checked_in", "goal": "Review only",
                "execution": {"id": "execution", "state": {"publication_mode": "draft", "target": "checked_in"}}}
        prompt = shift.kickoff(item, "worktree", "nonce", "phx work", "main")
        self.assertIn("gh pr create --draft --base main", prompt)
        self.assertIn("gh pr ready --undo", prompt)
        self.assertIn("gh pr merge --disable-auto", prompt)
        self.assertIn("Never promote, enable auto-merge or merge", prompt)
        self.assertNotIn("open one ready", prompt)
        self.assertNotIn("mark it ready", prompt)

    def test_ready_publication_retains_writer_before_merge_gates_pass(self):
        for original, current in (("checked_in", "checked_in"),
                                  ("checked_in", "ready_for_check_in"),
                                  ("ready_for_check_in", "checked_in")):
            with self.subTest(original=original, current=current):
                item = {"id": "item", "repository_id": "repo", "target": original, "goal": "Deliver",
                        "execution": {"id": "execution", "state": {"target": current}}}
                prompt = shift.kickoff(item, "worktree", "nonce", "phx work", "release")
                publication = " ".join(prompt.split("- `publication`:", 1)[1].split(
                    "- `delivery`:", 1)[0].split())
                delivery = " ".join(prompt.split("- `delivery`:", 1)[1].split())
                self.assertIn(f"Target: {current}", prompt)
                self.assertIn("gh pr create --base release", publication)
                self.assertIn("submit the PR URL and retain this writer in delivery", publication)
                self.assertIn('{"outcome": "succeeded", "pr_url":', publication)
                self.assertIn("satisfy the Merge decision section", publication)
                self.assertIn("Apply the Merge decision section on every pass", delivery)
                self.assertNotIn("enable GitHub auto-merge with any method", prompt)
                self.assertNotIn("enable GitHub auto-merge only for `checked_in`", delivery)

    def test_merge_decision_defines_review_sources_and_visible_wait_without_inventing_a_gate(self):
        item = {"id": "item", "repository_id": "repo", "goal": "Deliver",
                "execution": {"id": "execution", "state": {"target": "checked_in"}}}
        prompt = shift.kickoff(item, "worktree", "nonce", "phx work", "main")
        decision = " ".join(prompt.split("## Merge decision (publication and delivery)", 1)[1].split(
            "## PR title convention", 1)[0].split())
        self.assertIn("Before **either** merging or enabling auto-merge", decision)
        self.assertIn("target repository's instructions, the charter, or Captain/owner steering", decision)
        self.assertIn("If none requires independent review", decision)
        self.assertIn("current exact head has applicable independent final-head approval", decision)
        self.assertIn("record the missing approval/reviewer with `phx work progress`", decision)
        self.assertIn("seek another qualified independent reviewer", decision)
        self.assertIn("Captain assignment is not a prerequisite", decision)
        self.assertIn("Record reviewer identity, verdict, scope and exact final SHA", decision)
        self.assertIn("Any qualified reviewer independent of the author", decision)
        self.assertIn("no particular native session or person is a universal dependency", decision)
        self.assertIn("Reuse valid recorded approval for the current exact head", decision)
        self.assertIn("Genuine GitHub required reviewers, CODEOWNERS and branch protections", decision)
        self.assertIn("Use `phx work question` only when required review cannot be obtained", decision)

    def test_autonomy_policy_survives_kickoff_wake_and_keeper_handoff(self):
        worker = shift.kickoff(
            {"id": "item", "repository_id": "repo", "goal": "Deliver",
             "execution": {"id": "execution", "state": {"target": "checked_in"}}},
            "worktree", "nonce", "phx work", "main")
        for name, prompt in (("worker", worker), ("captain", shift.prompt("captain")),
                             ("wake", shift.prompt("wake")), ("keeper", shift.prompt("keeper"))):
            text = " ".join(prompt.split())
            with self.subTest(prompt=name):
                self.assertIn("qualified reviewer independent of the author", text)
                self.assertIn("two-round optimization budget", text)
                self.assertIn("safety/SLO/resource-contract gates stay hard", text)
                self.assertIn("Building", text)
                self.assertIn("Total", text)
                self.assertIn("sole-writer", text)
        for name in ("captain", "wake"):
            text = " ".join(shift.prompt(name).split())
            with self.subTest(prompt=name):
                self.assertIn("stage/result, Building release, PR merge and release/deployment events", text)
                self.assertIn("standing owner deployment authority", text)
                self.assertIn("Source merge is not a deployed result", text)

    def test_writer_names_put_task_words_before_a_bounded_discovery_marker(self):
        for goal, prefix in (
                ("Fix writer recovery", "Fix writer recovery"),
                ("# Fix writer recovery\nWith more context", "Fix writer recovery"),
                ("Fix writer recovery " * 100, "Fix writer recovery"),
                ("Give newly created Phalanx worker sessions concise human-readable task names",
                 "Phalanx worker sessions"),
                ("Implement unified concurrency configuration", "Implement unified concurrency"),
                ("x" * 100, "x" * 20),
                (None, "Phalanx task"), (" \n\t", "Phalanx task"),
                ("\x00Fix\u202ewriter\t recovery", "Fix writer recovery"),
                ("修复会话名称", "修复会话名称")):
            with self.subTest(goal=goal):
                name = shift.writer_session_name(goal)
                self.assertTrue(name.startswith(prefix), name)
                self.assertTrue(name.endswith(" [phx]"), name)
                self.assertLessEqual(len(name), 40)
        self.assertEqual(shift.writer_session_name("Fix recovery"), "Fix recovery [phx]")

    def test_writer_names_redact_secrets_before_truncating(self):
        for goal, secret in (
                ("Fix ghp_1234567890abcdef leakage", "ghp_1234567890abcdef"),
                ("Fix token=private-value exposure", "private-value"),
                ("Fix Bearer private-value exposure", "private-value"),
                ("Fix https://user:private-value@example.test/token leakage", "private-value")):
            with self.subTest(goal=goal):
                name = shift.writer_session_name(goal)
                self.assertTrue(name.startswith("Fix [redacted]"), name)
                self.assertNotIn(secret, name)

    def test_dashboard_link_preserves_configured_endpoint_instead_of_localhost_default(self):
        config = {"base_url": "https://muster.example.test:8443/team/"}
        with (mock.patch.object(shift, "load_config", return_value=config),
              mock.patch.object(shift, "load_charter", return_value={}),
              mock.patch.object(shift, "API"),
              mock.patch.object(shift, "status", return_value={"items": []}),
              mock.patch("builtins.print") as output):
            shift.run_shift(Namespace(shift_command="status", config="shift.json", captain=None))
        self.assertEqual(json.loads(output.call_args.args[0]),
                         {"items": [], "dashboard_url": "https://muster.example.test:8443/team/operator/"})

    def test_wake_uses_server_elapsed_age_not_the_client_clock(self):
        created = datetime(2026, 1, 1, tzinfo=UTC)
        charter = self.charter(CHARTER)
        captain = {"id": "captain", "created_at": created.isoformat(), "wake_count": 1,
                   "heartbeat_at": (created + timedelta(hours=24)).isoformat(),
                   "reinforcement": {"due": False}}
        api = mock.Mock(return_value=captain)
        with mock.patch.object(shift, "heartbeat", return_value={"captain": captain, "charter": charter}):
            result = shift.wake(api, charter, "captain", config=self.config)
        self.assertEqual(result["shift"]["handoff_due"], ["shift age reached 24 h"])


class ShiftTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        database = root / "queue.sqlite3"
        self.database = database
        initialize(database)
        self.start_server(database)
        bare, checkout = root / "origin.git", root / "checkout"
        git(root, "init", "-q", "--bare", str(bare))
        checkout.mkdir()
        git(checkout, "init", "-q", "-b", "main")
        (checkout / "README.md").write_text("pilot\n", encoding="utf-8")
        git(checkout, "add", "README.md")
        git(checkout, "commit", "-q", "-m", "Initial")
        git(checkout, "remote", "add", "origin", bare.as_uri())
        git(checkout, "push", "-q", "origin", "main")
        (root / "charter").mkdir()
        charter = root / "charter" / "charter.toml"
        charter.write_text(CHARTER + "[shift]\nmax_hours = 4\n", encoding="utf-8")
        self.config = {"base_url": self.url, "runtime": "any-runtime", "worktrees": str(root / "worktrees"),
                       "charter": str(charter), "session_workspace": "ws-1",
                       "repositories": {"repo": {"checkout": str(checkout), "base_branch": "main"}}}
        for repository, remote in (("repo", bare.as_uri()), ("other", "https://github.com/e/o.git")):
            self.assertEqual(self.request("/v1/repositories", OWNER, {"id": repository, "remote": remote})[0], 200)
        patch = mock.patch.dict(os.environ, {"PHALANX_RUNNER_TOKEN": RUNNER})
        patch.start()
        self.addCleanup(patch.stop)
        self.api = shift.API(self.config)
        self.charter = shift.load_charter(self.config)

    def item(self, repository="repo", **options):
        status, item = self.request("/v1/items", OWNER, {"repository_id": repository, "goal": "Write the report",
                                                         "target": "report", "acceptance_criteria": ["Cite files"],
                                                         **options})
        self.assertEqual(status, 201, item)
        return item

    def test_normal_intake_enters_real_captain_discovery_without_named_charter_ids(self):
        captain = shift.start(self.api, self.charter, "intake-captain", config=self.config)["captain"]["id"]
        item = self.item()
        excluded = self.item("other")
        status = shift.status(self.api, self.charter, captain)
        self.assertEqual(status["pickup_candidates"], [item["id"]])
        self.assertTrue(status["items"][0]["pickup"]["eligible"])
        self.assertEqual(status["excluded_items"][0]["pickup"]["code"], "out_of_scope")
        self.assertIn("other", status["excluded_items"][0]["wait_reason"])
        board = self.api(f"/v1/operator/items/{item['id']}")["item"]
        self.assertEqual(board["pickup"], status["items"][0]["pickup"])
        self.assertEqual(board["next_action"], "Next to run: a Captain dispatches a writer")
        code, dashboard = self.request("/operator/api/items", OWNER, {
            "repository_id": "repo", "goal": "Dashboard intake", "target": "report"},
            headers={"Origin": self.url, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(code, 201, dashboard)
        self.assertIn(dashboard["id"], shift.status(self.api, self.charter, captain)["pickup_candidates"])
        with write_transaction(self.database) as db:
            db.execute("UPDATE executions SET ready_since=0 WHERE item_id=?", (item["id"],))
        overdue = [flag for flag in self.api("/v1/flags")["flags"] if flag["item_id"] == item["id"]]
        self.assertEqual([flag["kind"] for flag in overdue], ["captain_idle_with_work"])
        launched = shift.launch(self.api, self.config, self.charter, captain, status["pickup_candidates"][0])
        self.assertEqual(launched["status"], "running")
        self.assertFalse(any(flag["item_id"] == item["id"] for flag in self.api("/v1/flags")["flags"]))
        with self.assertRaisesRegex(shift.ShiftError, "outside the charter"):
            shift.launch(self.api, self.config, self.charter, captain, excluded["id"])

    def test_pickup_reasons_preserve_pause_dependencies_capacity_and_lane_holds(self):
        captain = shift.start(self.api, self.charter, "policy-captain", config=self.config)["captain"]["id"]
        first = self.item()
        dependent = self.item(dependencies=[{"upstream_id": first["id"], "milestone": "report"}])

        def decision(item, code, reason):
            board = self.api(f"/v1/operator/items/{item['id']}")["item"]
            status = shift.status(self.api, self.charter, captain)
            discovery = next(row for row in status["items"] if row["item_id"] == item["id"])
            self.assertEqual(board["pickup"], discovery["pickup"])
            self.assertEqual(board["wait_reason"], discovery["wait_reason"])
            self.assertEqual(board["pickup"]["code"], code)
            self.assertIn(reason, board["wait_reason"])
            self.assertNotIn(item["id"], status["pickup_candidates"])
            with self.assertRaisesRegex(shift.ShiftError, reason):
                shift.launch(self.api, self.config, self.charter, captain, item["id"])

        decision(dependent, "dependency", first["id"])
        self.assertEqual(self.request("/v1/dispatch/settings", OWNER, {"paused": True}, method="PUT")[0], 200)
        decision(first, "paused", "Dispatch is paused")
        self.assertEqual(self.request("/v1/dispatch/settings", OWNER, {"paused": False}, method="PUT")[0], 200)
        reason = f"Lane serialized behind {dependent['id']}: owner requires ordered publication"
        self.api(f"/v1/items/{first['id']}/captain-actions",
                 body={"captain_id": captain, "action": "held", "reason": reason})
        decision(first, "held", "Lane serialized behind")
        self.api(f"/v1/items/{first['id']}/captain-actions",
                 body={"captain_id": captain, "action": "released", "reason": "Owner cleared lane order"})
        self.assertEqual(self.request("/v1/dispatch/settings", OWNER,
                                     {"max_building_writers": 1, "max_writers": 2}, method="PUT")[0], 200)
        shift.launch(self.api, self.config, self.charter, captain, first["id"])
        queued = self.item()
        decision(queued, "capacity", "Building capacity exhausted")
        self.assertEqual(self.request("/v1/dispatch/settings", OWNER, {"max_writers": 1}, method="PUT")[0], 200)
        decision(queued, "capacity", "Total capacity exhausted")

    def test_pickup_reports_mapping_affinity_and_persistent_runtime_trust_blocks(self):
        captain = shift.start(self.api, self.charter, "readiness-captain", config=self.config)["captain"]["id"]
        item = self.item()
        affinity = self.item(runner_affinity="another-runner")
        board = self.api(f"/v1/operator/items/{affinity['id']}")["item"]
        self.assertEqual(board["pickup"]["code"], "affinity")
        self.assertIn("another-runner", board["wait_reason"])
        shift.heartbeat(self.api, self.charter, captain, config=self.config,
                        repository_blocks=[("repo", "Owner trust approval required for shared workspace")])
        shift.heartbeat(self.api, self.charter, captain, config=self.config)
        board = self.api(f"/v1/operator/items/{item['id']}")["item"]
        self.assertEqual(board["pickup"]["code"], "repository_blocked")
        self.assertIn("Owner trust approval required", board["wait_reason"])
        shift.heartbeat(self.api, self.charter, captain, config=self.config, repository_ready=["repo"])
        self.assertIn(item["id"], shift.status(self.api, self.charter, captain)["pickup_candidates"])
        config = {**self.config, "repositories": {}}
        shift.heartbeat(self.api, self.charter, captain, config=config)
        board = self.api(f"/v1/operator/items/{item['id']}")["item"]
        self.assertEqual(board["pickup"]["code"], "repository_blocked")
        self.assertIn("no checkout mapping", board["wait_reason"])

    def test_pickup_scope_expires_with_authority_and_never_guesses_a_changed_charter(self):
        captain = shift.start(self.api, self.charter, "scope-captain", config=self.config)["captain"]["id"]
        item = self.item()
        route = f"/v1/captains/{captain}"
        self.api(route, body={"charter_revision": self.charter["revision"], "wake_count": 1,
                              "state": "active"}, method="PUT")
        self.assertIn(item["id"], shift.status(self.api, self.charter, captain)["pickup_candidates"])
        self.api(route, body={"charter_revision": "a-changed-charter-hash", "wake_count": 2,
                              "state": "active"}, method="PUT")
        board = self.api(f"/v1/operator/items/{item['id']}")["item"]
        self.assertEqual(board["pickup"]["code"], "uninitialized")
        shift.heartbeat(self.api, self.charter, captain, config=self.config)
        self.assertIn(item["id"], shift.status(self.api, self.charter, captain)["pickup_candidates"])
        with write_transaction(self.database) as db:
            db.execute("UPDATE captains SET heartbeat_at=? WHERE id=?",
                       ((datetime.now(UTC) - timedelta(minutes=16)).isoformat(), captain))
        board = self.api(f"/v1/operator/items/{item['id']}")["item"]
        self.assertEqual(board["pickup"]["code"], "no_captain")

    def test_captain_reference_records_host_without_rewriting_legacy_identities(self):
        native = "12345678-1234-1234-1234-123456789abc"
        for runtime, prefix in (("copilot-app", "ghapp://sessions/"), ("codex", "codex://threads/"),
                                ("unknown", "")):
            with self.subTest(runtime=runtime):
                captain = shift.start(self.api, self.charter, native, runtime=runtime)["captain"]
                self.assertEqual(captain["session_ref"], prefix + native)
                self.assertEqual(captain["session_availability"], "unverified")
                heartbeat = shift.heartbeat(self.api, self.charter, captain["id"],
                    sessions=[{"id": native, "name": "Captain [phx]", "availability": "present"}])
                self.assertEqual(heartbeat["captain"]["session_availability"], "present")
                flags = self.api("/v1/flags")["flags"]
                self.assertFalse(any(flag["kind"] == "orphan_session" for flag in flags))
                archived = self.api("/v1/session-archives?session_id=" + native)
                self.assertTrue(archived["active"])
        preserved = shift.start(self.api, self.charter, "legacy-opaque")["captain"]
        self.assertEqual(preserved["session_ref"], "legacy-opaque")

    def test_new_host_reference_does_not_reuse_an_ended_legacy_captain(self):
        native = "abcdefab-1234-1234-1234-123456789abc"
        ended = shift.start(self.api, self.charter, native)["captain"]
        shift.heartbeat(self.api, self.charter, ended["id"], state="ended")
        fresh = shift.start(self.api, self.charter, native, runtime="copilot-app")["captain"]
        self.assertNotEqual(fresh["id"], ended["id"])
        self.assertEqual(fresh["state"], "active")
        self.assertEqual(shift.heartbeat(self.api, self.charter, fresh["id"], wake=True)["captain"]["wake_count"], 1)

    def test_captain_launches_a_replayable_writer_and_counts_wakes(self):
        started = shift.start(self.api, self.charter, "chat-1")
        captain = started["captain"]["id"]
        self.assertEqual(shift.start(self.api, self.charter, "chat-1")["captain"]["id"], captain)
        self.assertEqual(started["captain"]["charter_revision"], self.charter["revision"])
        item = self.item()
        self.item("other")

        launched = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        writer = launched["writer"]
        self.assertEqual((launched["status"], writer["state"], writer["captain_id"]), ("running", "launching", captain))
        self.assertEqual(launched["session_name"], "Write the report [phx]")
        self.assertEqual(launched["session_identity"], {
            "item_id": item["id"], "writer_id": writer["id"], "writer_nonce": writer["nonce"]})
        self.assertLessEqual(len(launched["session_name"]), 40)
        self.assertEqual(launched["session_workspace"], "ws-1")
        worktree = Path(launched["worktree"])
        self.assertTrue(worktree.is_relative_to(Path(self.config["worktrees"]).resolve()))
        for text in (str(worktree), writer["nonce"], "Write the report", "- Cite files", "python -m phalanx work"):
            self.assertIn(text, launched["kickoff"])
        base_url, token = credential(worktree)
        self.assertEqual(request(base_url, token, "/v1/writer")["writer"]["id"], writer["id"])

        replay = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        self.assertEqual((replay["writer"]["id"], replay["writer"]["nonce"]), (writer["id"], writer["nonce"]))
        self.assertEqual(replay["session_name"], launched["session_name"])
        self.assertNotIn(token, launched["session_name"])
        self.assertNotIn(token, json.dumps(launched["session_identity"]))
        route = f"/v1/items/{item['id']}/executions/{launched['execution_id']}/writer"
        self.api(route + "/withdraw", body={"captain_id": captain, "writer_id": writer["id"],
                                            "reason": "never launched"})
        fresh = shift.launch(self.api, self.config, self.charter, captain, item["id"])["writer"]
        self.assertNotIn(fresh["nonce"], (writer["nonce"],))
        self.assertEqual(fresh["state"], "launching")
        self.api(route, body={"captain_id": captain, "writer_id": fresh["id"], "session_id": "s-1"}, method="PATCH")

        status = shift.status(self.api, self.charter)
        self.assertEqual([entry["item_id"] for entry in status["items"]], [item["id"]])
        self.assertEqual(status["items"][0]["writer"]["session_id"], "s-1")
        self.assertEqual(status["out_of_scope_open_items"], 1)

        beat = shift.heartbeat(self.api, self.charter, captain, wake=True, focus=[item["id"]],
                               sessions=[{"id": "s-1", "name": launched["session_name"]}])
        self.assertEqual((beat["captain"]["wake_count"], beat["shift"]["handoff_due"]), (1, []))
        first_seen = beat["captain"]["sessions"][0]["first_seen"]
        beat = shift.heartbeat(self.api, self.charter, captain, wake=True, handoff_note="Pilot running")
        self.assertEqual((beat["captain"]["wake_count"], beat["captain"]["focus"], beat["captain"]["sessions"]),
                         (2, [item["id"]], [{"id": "s-1", "name": launched["session_name"], "first_seen": first_seen}]))
        self.assertEqual(beat["shift"]["handoff_due"], [])
        ended = shift.heartbeat(self.api, self.charter, captain, state="ended")["captain"]
        self.assertEqual((ended["state"], ended["handoff_note"], ended["wake_count"]), ("ended", "Pilot running", 2))

    def test_native_compaction_counter_is_optional_cumulative_and_independent_of_wakes(self):
        started = shift.start(self.api, self.charter, "native-session")["captain"]
        captain = started["id"]
        self.assertIsNone(started["compaction_count"])
        beat = shift.heartbeat(self.api, self.charter, captain, wake=True)
        self.assertIsNone(beat["captain"]["compaction_count"])
        beat = shift.heartbeat(self.api, self.charter, captain, compaction_count=0)
        self.assertEqual(beat["captain"]["compaction_count"], 0)
        self.assertEqual(beat["shift"]["handoff_due"], [])
        for count in (4, 4, 1, None):
            with self.subTest(count=count):
                beat = shift.heartbeat(self.api, self.charter, captain, compaction_count=count)
                self.assertEqual(beat["captain"]["compaction_count"], 4)
                self.assertEqual(beat["captain"]["wake_count"], 1)
                self.assertEqual(beat["shift"]["handoff_due"], [])
        for count, expected in ((5, 5), (5, 5), (None, 5), (6, 6), (4, 6), (None, 6)):
            with self.subTest(count=count, expected=expected):
                beat = shift.heartbeat(self.api, self.charter, captain, compaction_count=count)
                self.assertEqual(beat["captain"]["compaction_count"], expected)
                self.assertEqual(beat["shift"]["handoff_due"], [])
        ended = shift.heartbeat(self.api, self.charter, captain, state="ended")["captain"]
        self.assertEqual(ended["compaction_count"], 6)
        successor = shift.start(self.api, self.charter, "new-native-session", captain)["captain"]
        self.assertIsNone(successor["compaction_count"])
        self.assertEqual(shift.status(self.api, self.charter, captain)["captain"]["compaction_count"], 6)

    def test_heartbeat_rereads_elapsed_time_bound_without_resetting_registration_or_telemetry(self):
        captain = shift.start(self.api, self.charter, "native-session")["captain"]["id"]
        beat = shift.heartbeat(self.api, self.charter, captain, wake=True, compaction_count=100)
        self.assertEqual(beat["shift"]["handoff_due"], [])
        created_at = beat["captain"]["created_at"]
        Path(self.config["charter"]).write_text(CHARTER + "[shift]\nmax_hours = 1\n", encoding="utf-8")
        changed = shift.load_charter(self.config)
        with mock.patch.object(shift, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(created_at) + timedelta(hours=1)
            beat = shift.heartbeat(self.api, changed, captain)
        self.assertEqual(beat["captain"]["compaction_count"], 100)
        self.assertEqual(beat["captain"]["wake_count"], 1)
        self.assertEqual(beat["captain"]["created_at"], created_at)
        self.assertEqual(beat["captain"]["charter_revision"], changed["revision"])
        self.assertEqual(beat["shift"]["handoff_due"], ["shift age reached 1 h"])

    def test_wake_reinforces_at_90_minutes_until_acknowledged_and_coalesces_missed_intervals(self):
        captain = shift.start(self.api, self.charter, "reinforced-session", config=self.config)["captain"]
        captain_id = captain["id"]
        first = shift.wake(self.api, self.charter, captain_id, config=self.config)
        self.assertFalse(first["reinforcement"]["due"])
        self.assertNotIn("workflow", first["reinforcement"])
        ordinary = " ".join(first["instructions"].split())
        for requirement in (
                "progress and outcome accountability",
                "every ordinary scheduled wake and material work event",
                "Is work progressing as expected? What is stalled or at risk?",
                "Take the next useful authorized action and verify its effect",
                "stage/progress, dependency, validation, CI/review, integration and merge evidence",
                "not by themselves delivery progress",
                "waiting-fix preference", "retaining the same writer",
                "concrete reason, responsible actor and next action or resume trigger",
                "Queued wake prompts do not refresh liveness during a lengthy turn",
                "between bounded coordination batches before the heartbeat becomes stale",
                "Heartbeat freshness is liveness, not delivery progress",
                "Preserve explicit report/ready-only targets and draft ceilings",
                "keep auto-merge off until independent final exact-head approval"):
            with self.subTest(delivery="ordinary", requirement=requirement):
                self.assertIn(requirement, ordinary)
        with write_transaction(self.database) as db:
            db.execute("UPDATE captains SET created_at=? WHERE id=?",
                       ((datetime.now(UTC) - timedelta(minutes=91)).isoformat(), captain_id))
        # A non-wake heartbeat must not consume the reminder.
        shift.heartbeat(self.api, self.charter, captain_id, config=self.config)
        due = shift.wake(self.api, self.charter, captain_id, config=self.config)
        self.assertTrue(due["reinforcement"]["due"])
        self.assertEqual(due["reinforcement"]["workflow"], shift.prompt("captain"))
        reinforced = " ".join(due["reinforcement"]["workflow"].split())
        for requirement in (
                "## Own progress and unblock delivery",
                "every ordinary scheduled wake and material work event",
                "Take the next useful authorized action and verify its effect",
                "stage's expectations", "last meaningful evidence",
                "stale holds and reconciliation", "both capacity limits",
                "Do not churn, interrupt or replace healthy sessions",
                "concrete reason, responsible actor, and next action or resume trigger",
                "current charter revision", "retained sessions need explicit adoption"):
            with self.subTest(delivery="reinforcement", requirement=requirement):
                self.assertIn(requirement, reinforced)
        self.assertIn("run the existing non-wake", reinforced)
        self.assertIn("before the current heartbeat becomes stale", reinforced)
        self.assertIn("not delivery progress", reinforced)
        self.assertEqual(due["charter"], shift.load_charter(self.config))
        self.assertEqual(due["reinforcement"]["acknowledgement_arguments"],
                         ["shift", "ack-reinforcement", "--captain", captain_id, "--interval", "1"])
        # Output lost after the heartbeat: replay still contains the workflow.
        again = shift.wake(self.api, self.charter, captain_id, config=self.config)
        self.assertEqual(again["reinforcement"], due["reinforcement"])
        route = f"/v1/captains/{captain_id}/reinforcement"
        self.assertEqual(self.request(route, RUNNER, {"interval": 2}, method="PUT")[0], 409)
        self.assertEqual(self.request(route, body={"interval": 1}, method="PUT")[0], 401)
        for invalid in (0, -1, True):
            self.assertEqual(self.request(route, RUNNER, {"interval": invalid}, method="PUT")[0], 422)
        ack = self.api(route, method="PUT", body={"interval": 1})
        self.assertFalse(ack["due"])
        self.assertEqual(self.api(route, method="PUT", body={"interval": 1}), ack)
        self.assertNotIn("workflow", shift.wake(self.api, self.charter, captain_id,
                                               config=self.config)["reinforcement"])
        with write_transaction(self.database) as db:
            db.execute("UPDATE captains SET created_at=? WHERE id=?",
                       ((datetime.now(UTC) - timedelta(minutes=361)).isoformat(), captain_id))
        pending = shift.wake(self.api, self.charter, captain_id, config=self.config)["reinforcement"]
        self.assertEqual((pending["interval"], pending["acknowledged_interval"], pending["due"]), (4, 1, True))
        self.assertTrue(self.api(route, method="PUT", body={"interval": 1})["due"])
        self.assertFalse(self.api(route, method="PUT", body={"interval": 4})["due"])
        # A late acknowledgement remains idempotent even if the server clock rolls back.
        with write_transaction(self.database) as db:
            row = captains.captain_row(db, captain_id)
            self.assertEqual(row["reinforced_interval"], 4)
            with mock.patch.object(captains, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(row["created_at"]) + timedelta(minutes=180)
                result = captains.acknowledge_reinforcement(
                    db, captain_id, captains.ReinforcementAcknowledgement(interval=4))
                self.assertEqual(result["acknowledged_interval"], 4)
                self.assertFalse(result["due"])

    def test_wake_fails_without_consuming_reinforcement_when_prerequisites_fail(self):
        api = mock.Mock(return_value={"id": "captain"})
        with self.assertRaisesRegex(shift.ShiftError, "API needs"):
            shift.wake(api, self.charter, "captain", config=self.config)
        api.assert_called_once_with("/v1/captains/captain")
        with mock.patch.object(shift, "prompt", side_effect=OSError("cannot read workflow")):
            api.reset_mock()
            with self.assertRaisesRegex(OSError, "cannot read workflow"):
                shift.wake(api, self.charter, "captain", config=self.config)
            api.assert_not_called()

    def test_prompts_keep_checkpoints_and_degradation_independent_of_compaction_telemetry(self):
        captain = " ".join(shift.prompt("captain").split())
        keeper = " ".join(shift.prompt("keeper").split())
        for prompt in (captain, keeper):
            self.assertIn("rotation is elapsed-time only", prompt)
            self.assertIn("[shift].max_hours", prompt)
            self.assertIn("checkpoint, not a mandatory handoff", prompt)
            self.assertIn("first-compaction fallback", prompt)
            self.assertNotIn("max_compactions", prompt)
        self.assertIn("five-minute wake/reconciliation schedule", captain)
        self.assertIn("Stop for good on an explicit owner stop, not queue completion", captain)
        self.assertIn("dependency/external waits", captain)
        for signal in ("three failed actions in a row", "self-raised deviation flag",
                       "missed heartbeat noticed on waking"):
            self.assertIn(signal, captain)

    def test_keeper_preserves_accountability_and_adoption_without_replacing_live_work(self):
        keeper = " ".join(shift.prompt("keeper").split())
        for requirement in (
                "current canonical Captain workflow and fresh charter revision",
                "progress and outcome accountability",
                "taking useful authorized unblocking action and verifying the effect",
                "blockers' concrete reasons/responsible actors/next actions or resume triggers",
                "explicit report/ready/draft ceilings and existing writer ownership",
                "Do not replace healthy sessions or add a watchdog/schedule",
                "adoption occurs when the current instructions are actually read",
                "90-minute reinforcement",
                "keeper neither grants new access nor takes over a live Captain's work"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, keeper)

    def test_interrupted_registration_reply_replays_the_pending_reservation(self):
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        item = self.item()
        committed = []

        def lose_reply(route, **options):
            response = self.api(route, **options)
            if route.endswith("/writer") and "body" in options:
                committed.append(response)
                raise ConnectionError("Registration committed, reply lost")
            return response

        with self.assertRaisesRegex(ConnectionError, "reply lost"):
            shift.launch(lose_reply, self.config, self.charter, captain, item["id"])
        original = committed[0]["writer"]
        pending = shift.status(self.api, self.charter)["items"][0]
        self.assertEqual(pending["writer"]["state"], "launching")
        self.assertEqual(pending["writer"]["nonce"], original["nonce"])
        self.assertIn("handoff pending", pending["wait_reason"])
        self.assertIn("fresh full native inventory", pending["resume_trigger"])
        self.assertEqual((pending["writer"]["created_at"], pending["writer"]["confirmed_at"]),
                         (original["created_at"], None))
        replay = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        self.assertEqual(replay["writer"], original)
        worktree = Path(replay["worktree"])
        self.assertFalse(shift.credential_path(worktree).with_name(shift.PENDING).exists())
        base_url, token = credential(worktree)
        self.assertEqual(request(base_url, token, "/v1/writer")["writer"], original)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writers").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def test_native_creation_before_confirmation_recovers_the_same_full_nonce_at_capacity(self):
        self.request("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        item = self.item()
        launched = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        original = launched["writer"]
        # Native creation succeeded but the Captain was interrupted before confirmation.
        shift.heartbeat(self.api, self.charter, captain, sessions=[
            {"id": "unrelated", "name": launched["session_name"], "writer_nonce": "other_nonce_123456"},
            {"id": "created", "name": "Renamed task", "writer_nonce": original["nonce"]},
        ])
        self.request("/v1/dispatch/settings", OWNER, {"paused": True}, method="PUT")
        replay = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        self.assertEqual(replay["writer"], original)
        inventory = self.api(f"/v1/captains/{captain}")["sessions"]
        (native,) = [session for session in inventory if session["writer_nonce"] == original["nonce"]]
        route = f"/v1/items/{item['id']}/executions/{launched['execution_id']}/writer"
        body = {"captain_id": captain, "writer_id": original["id"], "session_id": native["id"]}

        def lose_confirmation_reply():
            self.api(route, body=body, method="PATCH")
            raise ConnectionError("Confirmation committed, reply lost")

        with self.assertRaisesRegex(ConnectionError, "reply lost"):
            lose_confirmation_reply()
        recovered = self.api(route, body=body, method="PATCH")["writer"]
        self.assertEqual((recovered["id"], recovered["nonce"], recovered["attempt_id"]),
                         (original["id"], original["nonce"], original["attempt_id"]))
        self.assertEqual(recovered["session_id"], "created")
        self.assertIsNotNone(recovered["confirmed_at"])
        with self.assertRaisesRegex(ValueError, "already confirmed"):
            self.api(route, body=dict(body, session_id="unrelated"), method="PATCH")
        status = shift.status(self.api, self.charter)
        self.assertEqual((status["active_writers"], status["building_writers"]), (1, 1))
        self.assertIsNone(status["items"][0]["wait_reason"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writers").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def test_writer_cap_counts_intents_and_confirmed_writers_across_captains(self):
        self.request("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        other = shift.start(self.api, self.charter, "chat-2")["captain"]["id"]
        item, queued = self.item(), self.item()
        launched = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        writer = launched["writer"]
        route = f"/v1/items/{item['id']}/executions/{launched['execution_id']}/writer"
        flags = self.api("/v1/flags")["flags"]
        for session in (None, "s-1"):
            with self.subTest(session=session):
                if session:
                    self.api(route, body={"captain_id": captain, "writer_id": writer["id"],
                                          "session_id": session}, method="PATCH")
                with self.assertRaisesRegex(ValueError, "Total capacity exhausted: max_writers=1"):
                    shift.launch(self.api, self.config, self.charter, other, queued["id"])
                status = shift.status(self.api, self.charter, other)
                self.assertEqual((status["active_writers"], status["max_writers"]), (1, 1))
                replay = shift.launch(self.api, self.config, self.charter, captain, item["id"])
                self.assertEqual(replay["writer"]["id"], writer["id"])
        self.assertEqual(self.api("/v1/flags")["flags"], flags)
        self.api(route + "/withdraw", body={"captain_id": captain, "writer_id": writer["id"],
                                            "reason": "stopped", "stopped": True})
        self.assertEqual(shift.status(self.api, self.charter)["active_writers"], 0)
        shift.launch(self.api, self.config, self.charter, other, queued["id"])

    def test_replacement_at_writer_cap_releases_only_the_replaced_writer(self):
        self.request("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        item = self.item()
        launched = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        replacement = shift.launch(self.api, self.config, self.charter, captain, item["id"],
                                   replaces=launched["writer"]["id"])
        self.assertEqual(replacement["writer"]["replaced_id"], launched["writer"]["id"])
        self.assertEqual(shift.status(self.api, self.charter)["active_writers"], 1)
        replay = shift.launch(self.api, self.config, self.charter, captain, item["id"],
                              replaces=launched["writer"]["id"])
        self.assertEqual(replay["writer"]["id"], replacement["writer"]["id"])
        with self.assertRaisesRegex(ValueError, "max_writers=1"):
            shift.launch(self.api, self.config, self.charter, captain, self.item()["id"])

    def test_unmapped_replacement_preserves_writer_and_capacity(self):
        self.request("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        item = self.item()
        launched = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        route = f"/v1/items/{item['id']}/executions/{launched['execution_id']}/writer"
        self.api(route, body={"captain_id": captain, "writer_id": launched["writer"]["id"],
                              "session_id": "original-session"}, method="PATCH")
        worktree = Path(launched["worktree"])
        path = shift.credential_path(worktree)
        saved = path.read_bytes()
        base_url, token = credential(worktree)
        binding = request(base_url, token, "/v1/writer")
        status = shift.status(self.api, self.charter)
        with closing(sqlite3.connect(self.database)) as db:
            writers = db.execute("SELECT * FROM writers").fetchall()
            attempts = db.execute("SELECT * FROM attempts").fetchall()

        config = {**self.config, "repositories": {}}
        with (mock.patch.object(shift, "WorkspaceManager") as manager,
              self.assertRaisesRegex(shift.ShiftError, "Repository repo has no checkout mapping")):
            shift.launch(self.api, config, self.charter, captain, item["id"],
                         replaces=launched["writer"]["id"])
        manager.assert_not_called()
        self.assertEqual(request(base_url, token, "/v1/writer"), binding)
        after = shift.status(self.api, self.charter)
        for field in ("active_writers", "building_writers"):
            self.assertEqual(after[field], status[field])
            self.assertEqual(after[field], 1)
        self.assertEqual(path.read_bytes(), saved)
        self.assertFalse(path.with_name(shift.PENDING).exists())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM writers").fetchall(), writers)
            self.assertEqual(db.execute("SELECT * FROM attempts").fetchall(), attempts)

    def test_completed_writer_releases_a_place(self):
        self.request("/v1/dispatch/settings", OWNER, {"max_writers": 1, "max_building_writers": 1}, method="PUT")
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        launched = shift.launch(self.api, self.config, self.charter, captain, self.item()["id"])
        base_url, token = credential(Path(launched["worktree"]))
        binding = request(base_url, token, "/v1/writer")
        for evidence in ({"report_markdown": "# Report\n"},
                         {"report_revision": hashlib.sha256(b"# Report\n").hexdigest()},
                         {"report_revision": hashlib.sha256(b"# Report\n").hexdigest()}):
            binding = request(base_url, token,
                              f"/v1/items/{binding['item_id']}/executions/{binding['id']}/result",
                              body={"attempt_id": binding["writer"]["attempt_id"],
                                    "stage": binding["state"]["stage"], "expected_version": binding["version"],
                                    "outcome": "succeeded", **evidence})
        self.assertEqual(binding["state"]["status"], "completed")
        self.assertEqual(shift.status(self.api, self.charter)["active_writers"], 0)
        shift.launch(self.api, self.config, self.charter, captain, self.item()["id"])

    def test_charter_and_heartbeats_do_not_restore_a_stale_limit(self):
        charter = self.charter
        captain = shift.start(self.api, charter, "chat")["captain"]["id"]
        self.assertEqual(shift.status(self.api, charter)["max_writers"], 10)
        self.request("/v1/dispatch/settings", OWNER, {"max_writers": 2, "max_building_writers": 2}, method="PUT")
        shift.heartbeat(self.api, charter, captain, wake=True)
        shift.launch(self.api, self.config, charter, captain, self.item()["id"])
        shift.launch(self.api, self.config, charter, captain, self.item()["id"])
        with self.assertRaisesRegex(ValueError, "max_writers=2"):
            shift.launch(self.api, self.config, charter, captain, self.item()["id"])
        status = shift.status(self.api, charter)
        self.assertEqual((status["max_writers"], status["active_writers"]), (2, 2))

    def test_launch_kickoff_names_the_repository_checks_environment_or_its_absence(self):
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        item = self.item()
        mapping = self.config["repositories"]["repo"]
        for environment in (None, r'& "C:\checks env\Scripts\python.exe"', "uv run --no-sync python"):
            with self.subTest(environment=environment):
                if environment is not None:
                    mapping["checks_environment"] = environment
                prompt = shift.launch(self.api, self.config, self.charter, captain, item["id"])["kickoff"]
                expected = (f"`{environment}`" if environment is not None
                            else "Not configured. Do not guess an interpreter or command prefix.")
                self.assertIn(f"Checks environment (interpreter or command prefix): {expected}", prompt)
                self.assertIn("Never borrow another checkout's environment or install packages.", prompt)
                self.assertIn("python -m phalanx work question", prompt)
                self.assertIn("It is separate from the Phalanx command.", prompt)

    def test_refused_launch_leaves_the_live_credential_untouched(self):
        first = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        second = shift.start(self.api, self.charter, "chat-2")["captain"]["id"]
        item = self.item()
        launched = shift.launch(self.api, self.config, self.charter, first, item["id"])
        worktree = Path(launched["worktree"])
        live = shift.credential_path(worktree)
        before = live.read_bytes()
        for captain, replaces in ((second, None), (first, "0" * 32)):
            with self.subTest(captain=captain), self.assertRaisesRegex(ValueError, "HTTP 409"):
                shift.launch(self.api, self.config, self.charter, captain, item["id"], replaces=replaces)
            self.assertEqual(live.read_bytes(), before)
        base_url, token = credential(worktree)
        self.assertEqual(request(base_url, token, "/v1/writer")["writer"]["id"], launched["writer"]["id"])
        replay = shift.launch(self.api, self.config, self.charter, first, item["id"])
        self.assertEqual(replay["writer"]["id"], launched["writer"]["id"])
        self.assertEqual(live.read_bytes(), before)

    def test_handoff_marks_ending_and_names_a_findable_successor(self):
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        launched = shift.launch(self.api, self.config, self.charter, captain, self.item()["id"])
        handoff = shift.handoff(self.api, self.charter, captain, "Item X waits on review", "phx-test shift")
        self.assertEqual((handoff["captain"]["state"], handoff["captain"]["handoff_note"]),
                         ("ending", "Item X waits on review"))
        self.assertEqual([intent["writer_id"] for intent in handoff["open_intents"]], [launched["writer"]["id"]])
        self.assertEqual((handoff["session_name"], handoff["successors"]), (f"phx captain {captain[:8]}", []))
        self.assertIn(f"--parent {captain}", handoff["kickoff"])
        self.assertIn("`phx shift` means `phx-test shift`", handoff["kickoff"])
        self.assertIn("# Phalanx Captain", handoff["kickoff"])
        self.assertIn("not your Captain ID", handoff["kickoff"])
        self.assertIn("predecessor", handoff["session_name_identity"])
        self.assertEqual(handoff["off_shift_instructions"], shift.prompt("forwarding"))
        self.assertIn(handoff["off_shift_instructions"], handoff["kickoff"])
        self.assertIn("browser canvas by default", handoff["kickoff"])
        successor = shift.start(self.api, self.charter, "chat-2", captain)["captain"]["id"]
        again = shift.handoff(self.api, self.charter, captain, "Item X waits on review", "phx-test shift")
        self.assertEqual([entry["id"] for entry in again["successors"]], [successor])
        self.assertEqual(again["captain"]["ending_at"], handoff["captain"]["ending_at"])
        with self.assertRaisesRegex(ValueError, "HTTP 409"):
            shift.heartbeat(self.api, self.charter, captain, state="active")

    def test_current_captain_follows_successor_chain_not_parent_or_latest_registration(self):
        first = shift.start(self.api, self.charter, "first-chat")["captain"]["id"]
        shift.heartbeat(self.api, self.charter, first, state="ended")
        second = shift.start(self.api, self.charter, "second-chat", first)["captain"]["id"]
        shift.heartbeat(self.api, self.charter, second, state="ended")
        third = shift.start(self.api, self.charter, "third-chat", second)["captain"]["id"]
        stale = shift.start(self.api, self.charter, "newer-but-stale-chat", third)["captain"]["id"]
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE captains SET heartbeat_at=? WHERE id=?",
                       ("2000-01-01T00:00:00+00:00", stale))
        current = self.api("/v1/captains/current")
        self.assertEqual(current["captain"], {"id": third, "session_ref": "third-chat"})
        self.assertEqual(current["candidates"], [current["captain"]])
        self.assertNotIn("session_url", current["captain"])
        self.assertEqual(self.api(f"/v1/captains/{first}")["successors"], [second])
        shift.heartbeat(self.api, self.charter, third, state="ended")
        fourth = shift.start(self.api, self.charter, "fourth-chat", third)["captain"]["id"]
        self.assertEqual(self.api("/v1/captains/current")["captain"],
                         {"id": fourth, "session_ref": "fourth-chat"})

    def test_current_captain_refuses_missing_ambiguous_and_ending_destinations(self):
        self.assertIsNone(self.api("/v1/captains/current")["captain"])
        self.assertIn("No live active Captain", self.api("/v1/captains/current")["reason"])
        first = shift.start(self.api, self.charter, "first-chat")["captain"]["id"]
        second = shift.start(self.api, self.charter, "second-chat", first)["captain"]["id"]
        ambiguous = self.api("/v1/captains/current")
        self.assertIsNone(ambiguous["captain"])
        self.assertIn("2 live active Captains", ambiguous["reason"])
        self.assertEqual({entry["id"] for entry in ambiguous["candidates"]}, {first, second})
        shift.heartbeat(self.api, self.charter, first, state="ending")
        self.assertEqual(self.api("/v1/captains/current")["captain"]["id"], second)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE captains SET session_ref='' WHERE id=?", (second,))
        missing = self.api("/v1/captains/current")
        self.assertIsNone(missing["captain"])
        self.assertIn("no native session binding", missing["reason"])
        shift.heartbeat(self.api, self.charter, second, state="ended")
        self.assertIsNone(self.api("/v1/captains/current")["captain"])
        self.assertEqual(self.request("/v1/captains/current")[0], 401)

    def test_current_captain_is_not_limited_to_recent_history(self):
        current = shift.start(self.api, self.charter, "active-chat")["captain"]["id"]
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executemany(
                """INSERT INTO captains
                   (id, session_ref, charter_revision, state, created_at, heartbeat_at, ending_at)
                   VALUES (?, ?, ?, 'ending', ?, ?, ?)""",
                [(f"{index:032x}", f"ending-chat-{index}", self.charter["revision"], now, now, now)
                 for index in range(100)])
        self.assertNotIn(current, {entry["id"] for entry in self.api("/v1/captains?limit=100")["captains"]})
        self.assertEqual(self.api("/v1/captains/current")["captain"],
                         {"id": current, "session_ref": "active-chat"})

    def test_generated_prompts_preserve_off_shift_routing_and_provenance(self):
        forwarding = shift.prompt("forwarding")
        for name in ("captain", "keeper"):
            self.assertIn(forwarding, shift.prompt(name))
        self.assertIn("browser canvas", shift.prompt("captain"))
        self.assertIn("dashboard_url", shift.prompt("captain"))
        for instruction in (
                "I am no longer on shift", "phx shift current", "session_ref",
                "Immediately before forwarding", "current source reference",
                "Do not reconstruct provenance", "not execution", "uncertain send",
                "exact runtime-returned conversation URL", "Do not change runtime nesting",
                "creator-owned archive authority"):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, " ".join(forwarding.split()))
        self.assertNotIn(forwarding, shift.prompt("worker"))

    def test_handoff_prompts_require_completed_handoff_before_retirement(self):
        captain = shift.start(self.api, self.charter, "outgoing-chat")["captain"]["id"]
        handoff = shift.handoff(self.api, self.charter, captain, "Transfer work", "phx shift")
        for prompt in (handoff["off_shift_instructions"], handoff["kickoff"],
                       shift.prompt("captain"), shift.prompt("keeper")):
            text = " ".join(prompt.split())
            for guard in ("be `ended`, with no `open_intents`",
                          "successor whose `parent_id` is your Captain ID",
                          "failed/uncertain handoff or missing successor",
                          "leave the title unchanged"):
                with self.subTest(guard=guard):
                    self.assertIn(guard, text)
        self.assertIn("Ending the shift alone is not a completed handoff",
                      handoff["kickoff"])
        self.assertIn("notify your predecessor's verified native `session_ref` once",
                      handoff["kickoff"])
        self.assertIn("never rename the predecessor yourself", handoff["kickoff"])

    def test_retirement_prompt_preserves_title_and_makes_retries_idempotent(self):
        text = " ".join(shift.prompt("forwarding").split())
        for instruction in ("exactly one literal `[retired]` prefix followed by one space",
                            "Repeatedly remove only leading literal `[retired]` tags",
                            "preserving it exactly", "If the exact title already matches, do not rename",
                            "On every retry re-read identity, state and title",
                            "abbreviate, truncate or remove other tags",
                            "Verify the resulting title through native inspection"):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, text)
        self.assertNotIn("an off-shift chat `Off-shift", text)

    def test_retirement_prompt_protects_incoming_and_unrelated_sessions(self):
        text = " ".join(shift.prompt("forwarding").split())
        for instruction in ("Verify it is this outgoing conversation",
                            "a result naming your registration/session prevents renaming",
                            "Never target the incoming/current Captain",
                            "an unrelated chat", "must not rename another conversation",
                            "never a title, determine authority", "within its permissions and title limits",
                            "report the exact limitation", "never bulk-rename historical sessions"):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, text)

    def test_cli_prints_the_captain_prompt_and_validates_the_charter(self):
        def phx(*arguments):
            result = run_cli(["shift", *arguments])
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertIn("# Phalanx Captain", phx("prompt", "captain"))
        self.assertIn("# Phalanx keeper", phx("prompt", "keeper"))
        self.assertEqual(phx("prompt", "forwarding").strip(), shift.prompt("forwarding").strip())
        self.assertIn("# Phalanx work item $item_id", phx("prompt", "worker"))
        config = Path(self.config["charter"]).parent / "shift.json"
        config.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertIn("# Phalanx Captain", phx("prompt", "captain", "--config", str(config)))
        for arguments in (("charter", "--config", str(config)), ("--config", str(config), "charter")):
            with self.subTest(arguments=arguments):
                self.assertEqual(json.loads(phx(*arguments))["revision"], self.charter["revision"])
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        self.assertEqual(json.loads(phx("--config", str(config), "current"))["captain"],
                         {"id": captain, "session_ref": "chat-1"})
        beat = json.loads(phx("--config", str(config), "heartbeat", "--captain", captain,
                              "--session", "s-1", "phx 1234abcd nonce", "--session", "s-2", "phx captain",
                              "--compaction-count", "1"))
        self.assertEqual(beat["captain"]["compaction_count"], 1)
        self.assertEqual([(entry["id"], entry["name"]) for entry in beat["captain"]["sessions"]],
                         [("s-1", "phx 1234abcd nonce"), ("s-2", "phx captain")])
        beat = json.loads(phx("--config", str(config), "heartbeat", "--captain", captain,
                              "--session", "s-task", "Fix recovery [phx]",
                              "--session-writer", "s-task", "abcdef0123456789",
                              "--session-availability", "s-task", "missing"))
        session = beat["captain"]["sessions"][0]
        self.assertEqual(session["writer_nonce"], "abcdef0123456789")
        self.assertEqual(session["availability"], "missing")
        repeated = json.loads(phx("--config", str(config), "heartbeat", "--captain", captain))
        self.assertEqual(repeated["captain"]["sessions"], [session])
        for arguments in (("--session-writer", "missing", "nonce"),
                          ("--no-sessions", "--session-writer", "s-task", "nonce"),
                          ("--session-availability", "missing", "deleted"),
                          ("--session", "s-task", "Task", "--session-availability", "s-task", "unknown")):
            result = run_cli(
                ["shift", "--config", str(config),
                 "heartbeat", "--captain", captain, *arguments])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--session", result.stderr)
        beat = json.loads(phx("--config", str(config), "heartbeat", "--captain", captain, "--no-sessions"))
        self.assertEqual(beat["captain"]["sessions"], [])
        self.assertEqual(beat["captain"]["compaction_count"], 1)
        ending = json.loads(phx("--config", str(config), "handoff", "--captain", captain, "--note", "Bye"))
        self.assertIn(f"`phx shift` means `python -m phalanx shift --config {config.resolve()}`", ending["kickoff"])
        checked = json.loads(phx("--config", str(config), "archive-check", "--session", "helper",
                                 "--by", "chat-1", "--no-worktree"))
        self.assertTrue(checked["can_archive"])
        refused = json.loads(phx("--config", str(config), "archive-result", "--session", "helper",
                                 "--by", "chat-1", "--outcome", "refused", "--reason", "Creator only"))
        self.assertEqual(refused["reason"], "Creator only")
        checked = json.loads(phx("--config", str(config), "archive-check", "--session", "helper",
                                 "--by", "chat-1", "--no-worktree"))
        self.assertFalse(checked["can_archive"])
        exact = json.loads(phx("--config", str(config), "status", "--captain", captain))
        self.assertEqual(exact["captain"]["id"], captain)
        self.assertEqual(exact["dashboard_url"], self.url + "/operator/")

    def test_captain_prompt_recovers_task_first_and_renamed_sessions_by_full_nonce(self):
        prompt = shift.prompt("captain")
        for text in ("not just names starting with `phx `", "exact full writer nonce",
                     "session_identity.writer_nonce", "--session-writer",
                     "If inspection is unavailable or inconclusive", "even if renamed"):
            self.assertIn(text, prompt)

    def ended_writer(self, *, push=True, **options):
        captain = shift.start(self.api, self.charter, "creator")["captain"]["id"]
        item = self.item(**options)
        launched = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        route = f"/v1/items/{item['id']}/executions/{launched['execution_id']}/writer"
        self.api(route, method="PATCH", body={
            "captain_id": captain, "writer_id": launched["writer"]["id"], "session_id": "worker"})
        worktree = Path(launched["worktree"])
        if push:
            git(worktree, "push", "-q", "origin", "HEAD")
        base_url, token = credential(worktree)
        binding = request(base_url, token, "/v1/writer")
        report = "# Finished\n"
        while binding["state"]["status"] not in ("completed", "failed", "cancelled"):
            body = {"attempt_id": binding["writer"]["attempt_id"], "stage": binding["state"]["stage"],
                    "expected_version": binding["version"], "outcome": "succeeded"}
            body.update({"report_markdown": report} if body["stage"] == "report_drafting"
                        else {"report_revision": hashlib.sha256(report.encode()).hexdigest()})
            binding = request(base_url, token, f"/v1/items/{item['id']}/executions/{binding['id']}/result",
                              body=body)
        return captain, item["id"], worktree

    def check_archive(self, item):
        return shift.archive_check(self.api, self.config, self.charter, "worker", "creator", item_id=item)

    def test_ended_writer_is_discovered_after_item_closes_and_archived_by_creator(self):
        captain, item, _ = self.ended_writer()
        status = shift.status(self.api, self.charter)
        self.assertEqual(status["items"], [])
        owned = next(entry for entry in status["captains"] if entry["id"] == captain)
        self.assertEqual([writer["session_id"] for writer in owned["ended_writers"]], ["worker"])
        self.assertEqual(owned["cleanup_pending"], 1)
        checked = self.check_archive(item)
        self.assertTrue(checked["can_archive"], checked)
        self.assertEqual(checked["session_id"], "worker")
        shift.archive_result(self.api, "worker", "archived", "Archived worker", "creator")
        owned = self.api(f"/v1/captains/{captain}")
        self.assertEqual((owned["ended_writers"], owned["cleanup_pending"]), ([], 0))
        self.assertFalse(self.check_archive(item)["can_archive"])

    def test_ignored_build_output_does_not_block_archiving(self):
        _, item, worktree = self.ended_writer()
        (worktree / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        git(worktree, "add", ".gitignore")
        git(worktree, "commit", "-q", "-m", "Ignore build output")
        git(worktree, "push", "-q", "origin", "HEAD")
        cache = worktree / "__pycache__"
        cache.mkdir()
        (cache / "module.pyc").write_bytes(b"build output")
        self.assertTrue(self.check_archive(item)["can_archive"])
        self.assertTrue((cache / "module.pyc").exists())

    def test_local_changes_and_unpushed_commits_block_archiving(self):
        _, item, worktree = self.ended_writer()
        readme = worktree / "README.md"
        readme.write_text("changed\n", encoding="utf-8")
        checked = self.check_archive(item)
        self.assertFalse(checked["can_archive"])
        self.assertIn("local changes", checked["reason"])
        git(worktree, "add", "README.md")
        git(worktree, "commit", "-q", "-m", "Unpushed work")
        checked = self.check_archive(item)
        self.assertFalse(checked["can_archive"])
        self.assertIn("not pushed", checked["reason"])
        flags = self.api("/v1/flags")["flags"]
        self.assertTrue(any(flag["kind"] == "orphan_session" and "not pushed" in flag["reason"]
                            for flag in flags))
        git(worktree, "push", "-q", "origin", "HEAD")
        self.assertTrue(self.check_archive(item)["can_archive"])
        (worktree / "notes.txt").write_text("preserve me\n", encoding="utf-8")
        self.assertFalse(self.check_archive(item)["can_archive"])
        self.assertEqual((worktree / "notes.txt").read_text(encoding="utf-8"), "preserve me\n")

    def test_report_only_clean_worktree_without_remote_branch_can_archive(self):
        _, item, worktree = self.ended_writer(push=False)
        checked = self.check_archive(item)
        self.assertTrue(checked["can_archive"], checked)
        (worktree / "report-notes.md").write_text("Keep these notes\n", encoding="utf-8")
        checked = self.check_archive(item)
        self.assertFalse(checked["can_archive"])
        self.assertIn("local changes", checked["reason"])

    def test_never_pushed_local_commit_blocks_then_fresh_base_allows_retry(self):
        _, item, worktree = self.ended_writer(push=False)
        (worktree / "README.md").write_text("Unpublished work\n", encoding="utf-8")
        git(worktree, "commit", "-qam", "Local work")
        checked = self.check_archive(item)
        self.assertFalse(checked["can_archive"])
        self.assertIn("not pushed to origin/main", checked["reason"])
        self.assertEqual(self.api("/v1/session-archives?session_id=worker")["record"]["outcome"], "blocked")
        checkout = Path(self.config["repositories"]["repo"]["checkout"])
        stale_base = shift._git(worktree, "rev-parse", "refs/remotes/origin/main").strip()
        git(checkout, "push", "-q", "origin", f"{shift._git(worktree, 'rev-parse', 'HEAD').strip()}:main")
        git(worktree, "update-ref", "refs/remotes/origin/main", stale_base)
        self.assertTrue(shift._git(worktree, "rev-list", "HEAD", "--not", "refs/remotes/origin/main").strip())
        checked = self.check_archive(item)
        self.assertTrue(checked["can_archive"], checked)
        shift.archive_result(self.api, "worker", "archived", "Creator archived after retry", "creator")
        self.assertEqual(self.api("/v1/session-archives?session_id=worker")["record"]["outcome"], "archived")

    def test_missing_topic_uses_item_target_branch_not_mapping_default(self):
        checkout = Path(self.config["repositories"]["repo"]["checkout"])
        git(checkout, "checkout", "-qb", "release")
        (checkout / "README.md").write_text("Release-only work\n", encoding="utf-8")
        git(checkout, "commit", "-qam", "Release base")
        git(checkout, "push", "-q", "origin", "release")
        _, item, _ = self.ended_writer(push=False, target_branch="release")
        checked = self.check_archive(item)
        self.assertTrue(checked["can_archive"], checked)
        git(checkout, "push", "-q", "origin", "--delete", "release")
        checked = self.check_archive(item)
        self.assertFalse(checked["can_archive"])
        self.assertIn("fetch", checked["reason"])

    def test_missing_remote_branch_cannot_use_stale_tracking_ref(self):
        _, item, worktree = self.ended_writer()
        (worktree / "README.md").write_text("Only on the deleted topic\n", encoding="utf-8")
        git(worktree, "commit", "-qam", "Published topic work")
        git(worktree, "push", "-q", "origin", "HEAD")
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=worktree, text=True).strip()
        git(Path(self.config["repositories"]["repo"]["checkout"]), "push", "-q", "origin", "--delete", branch)
        checked = self.check_archive(item)
        self.assertFalse(checked["can_archive"])
        self.assertIn("not pushed to origin/main", checked["reason"])

    def test_archive_remote_lookup_and_fetch_failures_block(self):
        _, item, _ = self.ended_writer(push=False)
        original_git = shift._git
        for operation in ("ls-remote", "fetch"):
            with self.subTest(operation=operation):
                def failing_git(path, *args):
                    if args[0] == operation:
                        raise shift.WorkspaceError(f"Git {operation} failed: temporary network failure")
                    return original_git(path, *args)

                with mock.patch.object(shift, "_git", side_effect=failing_git):
                    checked = self.check_archive(item)
                self.assertFalse(checked["can_archive"])
                self.assertIn(f"Git {operation} failed: temporary network failure", checked["reason"])
        self.assertTrue(self.check_archive(item)["can_archive"])

    def test_refused_archive_persists_reason_and_flag_without_retry(self):
        captain, item, _ = self.ended_writer()
        self.assertTrue(self.check_archive(item)["can_archive"])
        shift.archive_result(self.api, "worker", "refused", "Only the creator may archive", "creator")
        shift.heartbeat(self.api, self.charter, captain, state="ended", sessions=[])
        with mock.patch.object(shift, "_git", side_effect=AssertionError("Must not retry")):
            checked = self.check_archive(item)
        self.assertFalse(checked["can_archive"])
        self.assertEqual((checked["outcome"], checked["reason"]),
                         ("refused", "Only the creator may archive"))
        flags = self.api("/v1/flags")["flags"]
        self.assertTrue(any(flag["kind"] == "orphan_session"
                            and "Only the creator may archive" in flag["reason"] for flag in flags))
        recorded = shift.archive_result(self.api, "worker", "blocked", "Do not replace refusal", "other")
        self.assertEqual(recorded["outcome"], "refused")
        shift.archive_result(self.api, "worker", "archived", "Owner confirmed archived", "owner")
        self.assertFalse(any(flag["kind"] == "orphan_session" for flag in self.api("/v1/flags")["flags"]))

    def test_keeper_archives_its_previous_captain_only_after_clean_handoff(self):
        captain = shift.start(self.api, self.charter, "previous-captain")["captain"]["id"]

        def check():
            return shift.archive_check(self.api, self.config, self.charter,
                                       "previous-captain", "keeper", no_worktree=True)

        self.assertFalse(check()["can_archive"])
        with self.assertRaisesRegex(ValueError, "HTTP 409"):
            shift.archive_result(self.api, "previous-captain", "archived", "Too early", "keeper")
        shift.handoff(self.api, self.charter, captain, "Clean handoff", "phx shift")
        shift.heartbeat(self.api, self.charter, captain, state="ended")
        self.assertFalse(check()["can_archive"])
        shift.start(self.api, self.charter, "successor", captain)
        self.assertTrue(check()["can_archive"])
        shift.archive_result(self.api, "previous-captain", "archived",
                             "Archived previous Captain", "keeper")
        self.assertFalse(check()["can_archive"])
        keeper = shift.prompt("keeper")
        self.assertLess(keeper.index("Before any early exit"), keeper.index('If any Captain has'))
        self.assertIn("A fresh keeper run is not this child's creator", keeper)

    def test_archive_check_cannot_bypass_writer_worktree_or_end_state(self):
        captain = shift.start(self.api, self.charter, "creator")["captain"]["id"]
        item = self.item()
        launched = shift.launch(self.api, self.config, self.charter, captain, item["id"])
        route = f"/v1/items/{item['id']}/executions/{launched['execution_id']}/writer"
        self.api(route, method="PATCH", body={
            "captain_id": captain, "writer_id": launched["writer"]["id"], "session_id": "worker"})
        self.assertFalse(self.check_archive(item["id"])["can_archive"])
        with self.assertRaisesRegex(ValueError, "HTTP 409"):
            shift.archive_result(self.api, "worker", "archived", "Still active", "creator")
        self.api(route + "/withdraw", body={
            "captain_id": captain, "writer_id": launched["writer"]["id"],
            "reason": "Abandoned and stopped", "stopped": True})
        checked = shift.archive_check(self.api, self.config, self.charter,
                                      "worker", "creator", no_worktree=True)
        self.assertFalse(checked["can_archive"])
        self.assertIn("registered writer", checked["reason"])
        shift.launch(self.api, self.config, self.charter, captain, item["id"])
        self.assertFalse(self.check_archive(item["id"])["can_archive"])

    def test_prompts_assign_creator_cleanup_and_preservation_before_archival(self):
        captain = shift.prompt("captain")
        keeper = shift.prompt("keeper")
        self.assertIn("ended_writers", captain)
        self.assertIn("completed`, `failed` or", captain)
        self.assertIn("all ended/stopped helper sessions you created", captain)
        for prompt in (captain, keeper):
            self.assertLess(prompt.index("archive-check"), prompt.index("archive-session tool"))
            self.assertIn("--outcome refused", prompt)
            self.assertIn("orphan_session", prompt)
            self.assertIn("Never retry a refused archive in a loop", " ".join(prompt.split()))


if __name__ == "__main__":
    unittest.main()
