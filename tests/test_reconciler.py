from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead, RemoteDisconnected
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from phalanx.github import GitHubError, PullRequest
from phalanx.github_checks import CheckCollection
from phalanx.reconciler import API, Reconciler, ReconcilerError
from phalanx.reconciliation import heartbeat, observation_flags, reconciler_stale
from phalanx.validation import GateResult, GateState
from phalanx.workspaces import any_file_lock
from phalanx.store import migrate, read_transaction, write_transaction
from phalanx.transitions import item_branch

from test_bootstrap import OWNER, RUNNER
from test_pr_observations import BASE, HEAD, OTHER, PullObservationFixture


class ObservationRouteTest(PullObservationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.start = datetime.now(UTC) - timedelta(hours=1)
        self.post = f"{self.execution}/github-observation"

    def body(self, minute, **fields):
        return {"observed_at": (self.start + timedelta(minutes=minute)).isoformat(), **fields}

    def pull(self, **fields):
        return {"number": 7, "state": "open", "merged": False, "draft": False, "head_ref": self.head_ref,
                "head_sha": HEAD, "base_ref": "main", "base_sha": BASE, **fields}

    def flags(self, minute=10):
        with read_transaction(self.path) as db:
            return {flag["kind"]: flag
                    for flag in observation_flags(db, self.start + timedelta(minutes=minute))}

    def stamp(self, minute):
        return (self.start + timedelta(minutes=minute)).isoformat(timespec="microseconds")

    def test_records_each_fact_once_and_shows_it_in_the_timelines(self):
        body = self.body(1, pr=self.pull())
        self.assertEqual(self.request(self.post, OWNER, body)[0], 403)
        self.assertEqual(self.request(self.post, RUNNER, body),
                         (200, {"recorded": True, "local_checks_recorded": False}))
        repeat = self.body(2, pr=self.pull())
        self.assertEqual(self.request(self.post, RUNNER, repeat)[1]["recorded"], False)
        for invalid, status in (
            (self.body(3, pr=self.pull(head_ref="feature")), 409),
            (self.body(120, pr=self.pull()), 409),
            (self.body(3, pr=self.pull(), merged_branch=True), 422),
            (self.body(3, feedback=["review"]), 422),
        ):
            self.assertEqual(self.request(self.post, RUNNER, invalid)[0], status, invalid)
        checks = f"{self.execution}/local-checks"
        self.assertEqual(self.request(checks, OWNER, {"head": HEAD})[0], 403)
        self.assertEqual(self.request(checks, RUNNER, {"head": OTHER})[0], 409)
        self.assertEqual(self.request(checks, RUNNER, {"head": HEAD}), (200, {"recorded": True}))
        self.assertEqual(self.request(checks, RUNNER, {"head": HEAD, "failures": ["late"]}),
                         (200, {"recorded": False}))
        self.assertTrue(self.request(self.post, RUNNER, repeat)[1]["local_checks_recorded"])

        status, events = self.request(f"{self.execution}/timeline", RUNNER)
        self.assertEqual(status, 200)
        observed = [event for event in events if event["event"] == "github_observation"]
        self.assertEqual([event["details"]["pr"]["url"] for event in observed],
                         ["https://github.com/Example/Repo/pull/7"])
        checked, = [event for event in events if event["event"] == "local_checks"]
        self.assertEqual(checked["details"], {"head": HEAD, "failures": []})
        self.assertEqual(observed[0]["details"]["last_observed_at"], datetime.fromisoformat(
            repeat["observed_at"]).isoformat(timespec="microseconds"))
        execution_id = self.execution.rsplit("/", 1)[1]
        status, page = self.request(f"/v1/operator/executions/{execution_id}/timeline", OWNER)
        self.assertEqual(status, 200)
        reasons = [event["reason"] for event in page["events"]]
        self.assertIn(f"Observed https://github.com/Example/Repo/pull/7 open at {HEAD}", reasons)
        self.assertIn(f"Local checks passed on {HEAD}", reasons)

    def test_flags_follow_the_latest_observation(self):
        drifted = self.pull(head_sha=OTHER, mergeable=False)
        self.request(self.post, RUNNER, self.body(
            1, pr=drifted, checks="failed", check_reasons=["build failed"], feedback=["alice: fix"]))
        self.assertEqual(set(self.flags()), {"head_drift", "merge_conflict", "checks_failed", "review_feedback"})
        status, body = self.request("/v1/flags", OWNER)
        self.assertEqual(status, 200)
        self.assertLessEqual({"head_drift", "merge_conflict", "observation_stale"},
                             {flag["kind"] for flag in body["flags"]})
        self.request(f"{self.execution}/local-checks", RUNNER,
                     {"head": OTHER, "failures": ["lint: Exited with status 1\n\ntail"]})
        flags = self.flags()
        self.assertEqual(flags["pre_pr_checks_failed"]["reason"],
                         f"Local checks failed on {OTHER}: lint: Exited with status 1")
        with read_transaction(self.path) as db:
            checked_at, = db.execute("SELECT checked_at FROM local_check_results").fetchone()
        self.assertEqual(flags["pre_pr_checks_failed"]["first_seen"], checked_at)
        self.request(self.post, RUNNER, self.body(3, pr=self.pull(state="closed")))
        self.assertEqual(set(self.flags()), {"pr_closed"})
        self.request(self.post, RUNNER, self.body(4, pr=self.pull(state="closed", merged=True)))
        self.assertEqual(self.flags(), {})
        self.request(self.post, RUNNER, self.body(5, merged_branch=True))
        self.assertEqual(set(self.flags()), {"unpublished_branch_merged"})

    def test_late_local_checks_never_revive_an_older_snapshot(self):
        self.request(self.post, RUNNER, self.body(1, pr=self.pull(head_sha=OTHER, mergeable=False)))
        self.request(self.post, RUNNER, self.body(2, pr=self.pull(state="closed")))
        self.request(f"{self.execution}/local-checks", RUNNER, {"head": OTHER, "failures": ["lint: failed"]})
        self.assertEqual(set(self.flags()), {"pr_closed"})

    def test_an_old_observation_is_flagged_stale(self):
        self.request(self.post, RUNNER, self.body(1, pr=self.pull(mergeable=False)))
        self.assertEqual(set(self.flags(15)), {"merge_conflict"})
        flags = self.flags(16)
        self.assertEqual(set(flags), {"merge_conflict", "observation_stale"})
        self.assertTrue(flags["merge_conflict"]["reason"].endswith(f"(last observed {self.stamp(1)})"))
        self.assertEqual(flags["observation_stale"]["first_seen"], self.stamp(16))
        self.request(self.post, RUNNER, self.body(20, pr=self.pull(mergeable=False)))
        self.assertEqual(set(self.flags(21)), {"merge_conflict"})

    def test_heartbeat_is_runner_only(self):
        self.assertEqual(self.request("/v1/reconcilers/local/heartbeat", OWNER, {})[0], 403)
        status, body = self.request("/v1/reconcilers/local/heartbeat", RUNNER, {})
        self.assertEqual((status, body["runner_id"]), (200, "local"))


class ReconcilerStaleTest(unittest.TestCase):
    def test_flags_each_reconciler_that_goes_quiet(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "queue.sqlite3"
            migrate(path)
            with write_transaction(path) as db:
                self.assertEqual(reconciler_stale(db, datetime.now(UTC)), [])
                seen = datetime.fromisoformat(heartbeat(db, "dead")["last_seen"])
                self.assertEqual(reconciler_stale(db, seen + timedelta(minutes=14)), [])
                later = seen + timedelta(minutes=16)
                db.execute("INSERT INTO reconciler_heartbeats VALUES ('live', ?)",
                           (later.isoformat(timespec="microseconds"),))
                flag, = reconciler_stale(db, later)
        self.assertEqual((flag["kind"], flag["first_seen"]),
                         ("reconciler_stale", (seen + timedelta(minutes=15)).isoformat(timespec="microseconds")))
        self.assertIn("Reconciler dead ", flag["reason"])


class APITest(unittest.TestCase):
    def test_a_dropped_connection_is_a_reconciler_error(self):
        api = API("http://localhost:1", "token")
        for error in (ConnectionResetError("reset"), RemoteDisconnected("closed"), IncompleteRead(b"")):
            with self.subTest(error=error), patch.object(api.opener, "open", side_effect=error), \
                    self.assertRaisesRegex(ReconcilerError, "Phalanx API unavailable"):
                api.call("/flags")


def git(path, *args):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          cwd=path, env=env, check=True, capture_output=True, text=True).stdout.strip()


def pull_request(head, branch, *, state="open", draft=False):
    return PullRequest(7, "PR_7", "https://github.com/example/repo/pull/7", state, draft, False, head,
                       branch, "example", "example/repo", BASE, "main", "example/repo", None, True)


class FakeAPI:
    def __init__(self, executions, remotes):
        self.executions = executions
        self.remotes = remotes
        self.posts = []
        self.calls = []
        self.flags = []
        self.checked = False
        self.local_checks = []

    def call(self, path, body=None):
        self.calls.append(path)
        if path == "/repositories":
            return [{"id": repo, "remote": remote} for repo, remote in self.remotes.items()]
        if path.startswith("/operator/executions?"):
            return {"items": self.executions, "next_cursor": None}
        if path.endswith("/heartbeat"):
            return {}
        if path == "/flags":
            return {"flags": self.flags}
        if path.endswith("/github-observation"):
            self.posts.append((path.split("/")[2], body))
            return {"recorded": True, "local_checks_recorded": self.checked}
        if path.endswith("/local-checks"):
            self.local_checks.append(body)
            return {"recorded": True}
        return {"target": "ready_for_check_in", "target_branch": None}


class FakeGitHub:
    def __init__(self, pulls=(), error=None, feedback_error=None):
        self.pulls = pulls
        self.error = error
        self.feedback_error = feedback_error

    def get_pull_request(self, number):
        if self.error:
            raise self.error
        return self.pulls[0]

    def list_matching_pull_requests(self, *, head_owner, head):
        return tuple(pull for pull in self.pulls if pull.head_ref == head)

    def get_actionable_feedback(self, number):
        if self.feedback_error:
            raise self.feedback_error
        return (("THREAD", "alice on app.py: rename this"),)


def execution(item_id, repo="repo", primary=None):
    return {"id": uuid4().hex, "item_id": item_id, "repository_id": repo, "primary_pr_url": primary}


class ReconcilerPassTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.github = {}
        self.config = {"runner_id": "local", "repositories": {}}

    def reconciler(self, api, **options):
        return Reconciler(self.config, self.root / "state", api, github_token="token",
                          clock=lambda: self.now,
                          github_client=lambda owner, name, token: self.github[name.lower()],
                          **options)

    def repository(self, branches):
        """A checkout whose origin names github.com but fetches from a local bare repository."""
        bare, source, checkout = self.root / "remote.git", self.root / "source", self.root / "checkout"
        git(self.root, "init", "--bare", "-b", "main", str(bare))
        git(self.root, "init", "-b", "main", str(source))
        (source / "app.py").write_text("print('hello')\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "start")
        git(source, "push", str(bare), "main")
        heads = {}
        for branch, ahead in branches.items():
            git(source, "checkout", "-b", branch, "main")
            if ahead:
                (source / "feature.py").write_text("x = 1\n")
                git(source, "add", ".")
                git(source, "commit", "-m", "feature")
            git(source, "push", str(bare), branch)
            heads[branch] = git(source, "rev-parse", "HEAD")
        git(self.root, "clone", bare.as_uri(), str(checkout))
        git(checkout, "remote", "set-url", "origin", "https://github.com/example/repo.git")
        git(checkout, "config", f"url.{bare.as_uri()}.insteadOf", "https://github.com/example/repo.git")
        self.config["repositories"]["repo"] = {"checkout": str(checkout), "base_branch": "main"}
        return checkout, heads

    def test_a_rate_limited_repository_waits_while_others_are_observed(self):
        self.config["repositories"] = {"repo": {}, "other": {}}
        self.github["repo"] = FakeGitHub([pull_request(HEAD, item_branch("one"))])
        self.github["other"] = FakeGitHub(error=GitHubError("get pull request", "rate limited", 403,
                                                            retry_after_seconds=30))
        api = FakeAPI([execution("one", primary="https://github.com/example/repo/pull/7"),
                       execution("two", "other", "https://github.com/example/other/pull/3")],
                      {"repo": "https://github.com/example/repo", "other": "https://github.com/example/other"})
        reconciler = self.reconciler(api)
        result = reconciler.run_pass()
        self.assertEqual((result["observed"], len(result["errors"])), (1, 1))
        (item, body), = api.posts
        self.assertEqual(item, "one")
        self.assertEqual(body["pr"]["head_sha"], HEAD)
        self.assertEqual(body["feedback"], ["alice on app.py: rename this"])
        self.assertNotIn("checks", body)

        api.calls.clear()
        self.assertEqual(reconciler.run_pass()["errors"], [])
        self.assertNotIn("/items/two", api.calls)
        self.now += 61
        self.assertEqual(len(reconciler.run_pass()["errors"]), 1)

    def test_an_incomplete_github_read_posts_nothing_and_backs_off(self):
        self.config["repositories"] = {"repo": {}}
        self.config["validation_policy"] = "policy.json"
        primary = "https://github.com/example/repo/pull/7"
        api = FakeAPI([execution("one", primary=primary), execution("two", primary=primary)],
                      {"repo": "https://github.com/example/repo"})
        policy = SimpleNamespace(draft_checks=("ci",), local_checks=())
        retry = CheckCollection(GateResult(GateState.BLOCKED, ("rate limited",)), retry_after_seconds=30)
        for collected, github in (
            (retry, FakeGitHub([pull_request(HEAD, "branch", draft=True)])),
            (CheckCollection(GateResult(GateState.SATISFIED, ())), FakeGitHub(
                [pull_request(HEAD, "branch", draft=True)], feedback_error=GitHubError("graphql", "errors"))),
        ):
            self.github["repo"] = github
            reconciler = self.reconciler(api)
            with self.subTest(collected=collected), patch.object(Reconciler, "policy", return_value=policy), \
                    patch("phalanx.reconciler.collect_github_checks", return_value=collected):
                api.calls.clear()
                self.assertEqual(len(reconciler.run_pass()["errors"]), 1)
                self.assertEqual(api.posts, [])
                self.assertNotIn("/items/two", api.calls)
                self.assertGreater(reconciler.backoff["repo"], self.now)

    def test_a_second_reconciler_cannot_share_the_state_directory(self):
        state = self.root / "state"
        state.mkdir()
        with any_file_lock([state / "reconciler.lock"]):
            with self.assertRaisesRegex(ReconcilerError, "Another reconciler"):
                self.reconciler(FakeAPI([], {})).run(Event(), once=True)
        self.assertEqual(self.reconciler(FakeAPI([], {})).run(Event(), once=True)["errors"], [])

    def test_the_owner_hears_about_each_flag_once(self):
        api = FakeAPI([], {})
        flag = {"kind": "no_live_captain", "item_id": "", "execution_id": "", "captain_id": "",
                "reason": "No Captain", "recovery": "Start one", "first_seen": "t", "notify_owner": True,
                "source": "server"}
        api.flags = [flag, dict(flag, kind="head_drift", notify_owner=False)]
        sent, failing = [], [True]

        def notify(event):
            if failing.pop():
                raise OSError("unavailable")
            sent.append(event)
        reconciler = self.reconciler(api, notify=notify)
        self.assertEqual(len(reconciler.run_pass()["errors"]), 1)
        failing.append(False)
        reconciler.run_pass()
        reconciler.run_pass()
        self.assertEqual(sent, [{"event": "flag", **flag}])

    def test_an_unpublished_branch_is_checked_against_its_base(self):
        merged, ahead, missing = "one", "two", "three"
        self.repository({item_branch(merged): False, item_branch(ahead): True})
        self.github["repo"] = FakeGitHub()
        api = FakeAPI([execution(merged), execution(ahead), execution(missing)],
                      {"repo": "git@github.com:Example/Repo.git"})
        result = self.reconciler(api).run_pass()
        self.assertEqual(result["errors"], [])
        self.assertEqual([(item, body.get("merged_branch", False), "pr" in body) for item, body in api.posts],
                         [(merged, True, False), (ahead, False, False), (missing, False, False)])

    def test_a_checkout_of_another_repository_is_refused(self):
        checkout, _ = self.repository({})
        git(checkout, "remote", "set-url", "origin", "https://github.com/example/other.git")
        self.github["repo"] = FakeGitHub()
        api = FakeAPI([execution("one")], {"repo": "https://github.com/example/repo"})
        result = self.reconciler(api).run_pass()
        self.assertEqual(api.posts, [])
        self.assertIn("is not a checkout of the registered remote", result["errors"][0])

    def test_local_checks_run_once_per_head_and_report_failures(self):
        branch = item_branch("one")
        checkout, heads = self.repository({branch: True})
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"version": 1, "repositories": {"repo": {
            "local_checks": [
                {"name": "slow", "argv": [sys.executable, "-c", "import time; time.sleep(1)"]},
                {"name": "fail", "argv": [sys.executable, "-c", "import sys; print('broken'); sys.exit(3)"]},
                {"name": "skipped", "allow_skip": True,
                 "bypass": {"outcome": "skipped", "reason": "slow", "authorization": "owner"}},
            ],
            "draft_checks": None, "ready_checks": None,
        }}}))
        self.config["validation_policy"] = str(policy)
        self.github["repo"] = FakeGitHub([pull_request(heads[branch], branch, draft=True)])
        api = FakeAPI([execution("one")], {"repo": "https://github.com/example/repo"})
        reconciler = self.reconciler(api)
        with patch("phalanx.reconciler.HEARTBEAT_SECONDS", 0.1):
            self.assertEqual(reconciler.run_pass()["checked"], 1)
        # The pass heartbeats once up front; the rest came while the slow check ran.
        self.assertGreater(sum(call.endswith("/heartbeat") for call in api.calls), 3)
        (_, observed), = api.posts
        self.assertEqual(observed["pr"]["head_sha"], heads[branch])
        checked, = api.local_checks
        self.assertEqual(checked["head"], heads[branch])
        failure, = checked["failures"]
        self.assertTrue(failure.startswith("fail: Exited with status 3"))
        self.assertIn("broken", failure)
        self.assertFalse((self.root / "state" / "checks" / heads[branch][:16]).exists())
        self.assertEqual(git(checkout, "worktree", "list", "--porcelain").count("worktree "), 1)

        api.checked = True
        self.assertEqual(reconciler.run_pass()["checked"], 0)
        self.assertEqual((len(api.posts), len(api.local_checks)), (2, 1))


if __name__ == "__main__":
    unittest.main()
