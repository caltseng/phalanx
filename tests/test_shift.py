import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from phalanx import shift
from phalanx.store import migrate
from phalanx.work_cli import credential, request

from test_bootstrap import OWNER, RUNNER, ServerFixture


ROOT = Path(__file__).resolve().parents[1]
CHARTER = """goal = "Deliver the pilot's queued work"
repositories = ["repo"]
allowed_actions = ["launch and replace writer sessions"]
escalate = ["anything outside the goal"]
stop_when = ["the queue is empty"]
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
        charter = self.charter(CHARTER + "[shift]\nmax_wakes = 2\n")
        self.assertEqual(charter["revision"], hashlib.sha256(Path(self.config["charter"]).read_bytes()).hexdigest())
        self.assertEqual(charter["shift"], {"max_hours": 4, "max_wakes": 2})
        for text in (CHARTER + 'extra = "x"\n', CHARTER.replace('["the queue is empty"]', "[]"),
                     CHARTER.replace('["repo"]', '["repo", "other"]'), CHARTER + "[shift]\nmax_hours = 0\n",
                     CHARTER + "[shift]\nmax_hours = true\n", "goal = [\n"):
            with self.subTest(text=text), self.assertRaises(shift.ShiftError):
                self.charter(text)

    def test_charter_must_live_outside_checkouts_and_worktrees(self):
        for path in (self.checkout / "c.toml", self.root / "worktrees" / "c.toml"):
            path.parent.mkdir(exist_ok=True)
            with self.subTest(path=path), self.assertRaisesRegex(shift.ShiftError, "outside every checkout"):
                self.charter(CHARTER, path)


class ShiftTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        database = root / "queue.sqlite3"
        migrate(database)
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
        charter.write_text(CHARTER + "[shift]\nmax_wakes = 2\n", encoding="utf-8")
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

    def item(self, repository="repo"):
        status, item = self.request("/v1/items", OWNER, {"repository_id": repository, "goal": "Write the report",
                                                         "target": "report", "acceptance_criteria": ["Cite files"]})
        self.assertEqual(status, 201, item)
        return item

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
        self.assertEqual(launched["session_name"], f"phx {item['id'][:8]} {writer['nonce']}")
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
        self.assertEqual(beat["shift"]["handoff_due"], ["wake count reached 2"])
        ended = shift.heartbeat(self.api, self.charter, captain, state="ended")["captain"]
        self.assertEqual((ended["state"], ended["handoff_note"], ended["wake_count"]), ("ended", "Pilot running", 2))

    def test_launch_refuses_out_of_charter_repositories(self):
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        with self.assertRaisesRegex(shift.ShiftError, "outside the charter"):
            shift.launch(self.api, self.config, self.charter, captain, self.item("other")["id"])

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
        successor = shift.start(self.api, self.charter, "chat-2", captain)["captain"]["id"]
        again = shift.handoff(self.api, self.charter, captain, "Item X waits on review", "phx-test shift")
        self.assertEqual([entry["id"] for entry in again["successors"]], [successor])
        self.assertEqual(again["captain"]["ending_at"], handoff["captain"]["ending_at"])
        with self.assertRaisesRegex(ValueError, "HTTP 409"):
            shift.heartbeat(self.api, self.charter, captain, state="active")

    def test_cli_prints_the_captain_prompt_and_validates_the_charter(self):
        def phx(*arguments):
            result = subprocess.run([sys.executable, "-m", "phalanx", "shift", *arguments],
                                    env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertIn("# Phalanx Captain", phx("prompt", "captain"))
        self.assertIn("# Phalanx keeper", phx("prompt", "keeper"))
        self.assertIn("# Phalanx work item $item_id", phx("prompt", "worker"))
        config = Path(self.config["charter"]).parent / "shift.json"
        config.write_text(json.dumps(self.config), encoding="utf-8")
        self.assertIn("# Phalanx Captain", phx("prompt", "captain", "--config", str(config)))
        for arguments in (("charter", "--config", str(config)), ("--config", str(config), "charter")):
            with self.subTest(arguments=arguments):
                self.assertEqual(json.loads(phx(*arguments))["revision"], self.charter["revision"])
        captain = shift.start(self.api, self.charter, "chat-1")["captain"]["id"]
        beat = json.loads(phx("--config", str(config), "heartbeat", "--captain", captain,
                              "--session", "s-1", "phx 1234abcd nonce", "--session", "s-2", "phx captain"))
        self.assertEqual([(entry["id"], entry["name"]) for entry in beat["captain"]["sessions"]],
                         [("s-1", "phx 1234abcd nonce"), ("s-2", "phx captain")])
        beat = json.loads(phx("--config", str(config), "heartbeat", "--captain", captain, "--no-sessions"))
        self.assertEqual(beat["captain"]["sessions"], [])
        ending = json.loads(phx("--config", str(config), "handoff", "--captain", captain, "--note", "Bye"))
        self.assertIn(f"`phx shift` means `python -m phalanx shift --config {config.resolve()}`", ending["kickoff"])


if __name__ == "__main__":
    unittest.main()
