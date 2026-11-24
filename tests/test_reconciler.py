from dataclasses import asdict, replace
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

from phalanx import intake
from phalanx.intake_models import Intake
from phalanx.github import CommitChecks, GitHubError, PullRequest
from phalanx.github_checks import CheckCollection
from phalanx.pr_models import DEPLOYMENT_WINDOW
from phalanx.reconciler import API, Reconciler, ReconcilerError
from phalanx.reconciliation import heartbeat, observation_flags, reconciler_stale
from phalanx.validation import GateResult, GateState
from phalanx.workspaces import any_file_lock
from phalanx.store import MIGRATIONS, connect, migrate, read_transaction, write_transaction
from phalanx.transitions import Execution, PullRequest as OutputPR, Revision, Stage, Status, Target, item_branch

from test_bootstrap import OWNER, RUNNER, ServerFixture, claim_stage

HEAD, BASE, OTHER = "a" * 40, "b" * 40, "c" * 40
PRE_NORMALIZATION_VERSION = next(
    index for index, migration in enumerate(MIGRATIONS)
    if any("json_set(state_json, '$.stage', 'delivery')" in statement for statement in migration))


class ObservationRouteTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "git@github.com:Example/Repo.git")
            item = intake.create(db, Intake(repository_id="repo", goal="Update tests",
                                            target="ready_for_check_in", target_branch="main"))
        self.start_server(self.path)
        self.head_ref = item_branch(item["id"])
        self.item = f"/v1/items/{item['id']}"
        self.execution = f"{self.item}/executions/{item['execution']['id']}"
        assignment = claim_stage(self.path, item["id"], "w" * 32)
        claim = assignment["execution"]
        self.assertEqual(self.request(self.execution + "/result", "w" * 32, {
            "attempt_id": assignment["attempt_id"], "stage": "development",
            "expected_version": claim["version"], "outcome": "succeeded", "candidate": HEAD,
        })[0], 200)
        self.start = datetime.now(UTC) - timedelta(hours=1)
        self.post = f"{self.execution}/github-observation"

    def body(self, minute, **fields):
        if "pr" in fields:
            fields.setdefault("target_base", "main")
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
        self.request(self.post, RUNNER, self.body(
            1, pr=self.pull(mergeable=False), checks="failed", check_reasons=["build failed"],
            feedback=["alice: fix"]))
        self.assertEqual(set(self.flags()), {"merge_conflict", "checks_failed", "review_feedback"})
        status, body = self.request("/v1/flags", OWNER)
        self.assertEqual(status, 200)
        self.assertLessEqual({"merge_conflict", "observation_stale"},
                             {flag["kind"] for flag in body["flags"]})
        self.request(self.post, RUNNER, self.body(3, pr=self.pull(state="closed")))
        self.assertEqual(set(self.flags()), {"pr_closed"})
        self.request(self.post, RUNNER, self.body(4, pr=self.pull(state="closed", merged=True)))
        self.assertEqual(self.flags(), {})
        execution_id = self.execution.rsplit("/", 1)[1]
        status, timeline = self.request(f"/v1/operator/executions/{execution_id}/timeline", OWNER)
        self.assertEqual((status, timeline["item"]["pr"]["state"]), (200, "merged"))
        self.request(self.post, RUNNER, self.body(5, merged_branch=True))
        self.assertEqual(set(self.flags()), {"unpublished_branch_merged"})

    def test_blockers_on_a_superseded_head_do_not_outlive_the_recorded_candidate(self):
        # #154: GitHub was last seen at the old conflicting head while the writer had already
        # pushed and recorded its resolution, HEAD, as the candidate.
        self.request(self.post, RUNNER, self.body(
            1, pr=self.pull(head_sha=OTHER, mergeable=False), checks="failed", check_reasons=["build failed"],
            feedback=["alice: fix"]))
        self.request(f"{self.execution}/local-checks", RUNNER, {"head": OTHER, "failures": ["lint: failed"]})
        flags = self.flags()
        self.assertEqual(set(flags), {"head_drift", "review_feedback"})
        self.assertTrue(flags["head_drift"]["recovery"].startswith("Informational"))
        self.request(self.post, RUNNER, self.body(2, pr=self.pull(mergeable=False)))
        self.assertEqual(set(self.flags()), {"merge_conflict"})
        self.request(f"{self.execution}/local-checks", RUNNER,
                     {"head": HEAD, "failures": ["lint: Exited with status 1\n\ntail"]})
        flags = self.flags()
        self.assertEqual(flags["pre_pr_checks_failed"]["reason"],
                         f"Local checks failed on {HEAD}: lint: Exited with status 1")
        with read_transaction(self.path) as db:
            checked_at, = db.execute(
                "SELECT checked_at FROM local_check_results WHERE head_sha=?", (HEAD,)).fetchone()
        self.assertEqual(flags["pre_pr_checks_failed"]["first_seen"], checked_at)

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


def pull_request(head, branch, *, state="open", draft=False, merged=False, mergeable=True):
    return PullRequest(7, "PR_7", "https://github.com/example/repo/pull/7", state, draft, merged, head,
                       branch, "example", "example/repo", BASE, "main", "example/repo", None, mergeable)


class FakeAPI:
    def __init__(self, executions, remotes, closed=()):
        self.executions = executions
        self.closed = closed
        self.remotes = remotes
        self.posts = []
        self.calls = []
        self.flags = []
        self.checked = False
        self.local_checks = []
        self.heartbeats = []

    def call(self, path, body=None):
        self.calls.append(path)
        if path == "/repositories":
            return [{"id": repo, "remote": remote} for repo, remote in self.remotes.items()]
        if path.startswith("/operator/executions?"):
            rows = self.closed if "kind=closed" in path else self.executions
            if "outcome=ready_for_check_in" in path:
                rows = [row for row in rows if row.get("outcome") == "ready_for_check_in"]
            return {"items": rows, "next_cursor": None}
        if path.endswith("/heartbeat"):
            self.heartbeats.append(body)
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
    def __init__(self, pulls=(), error=None, feedback_error=None, parents=(), on_branch=False, listed=None):
        self.pulls = pulls
        # What the list endpoint returns, if it differs from the exact PRs.
        self.listed = pulls if listed is None else listed
        self.error = error
        self.feedback_error = feedback_error
        self.parents = parents
        self.on_branch = on_branch

    def get_commit_parents(self, sha):
        return self.parents

    def is_on_branch(self, sha, branch):
        return self.on_branch and branch == "main"

    def get_pull_request(self, number):
        if self.error:
            raise self.error
        return next(pull for pull in self.pulls if pull.number == number)

    def list_matching_pull_requests(self, *, head_owner, head):
        return tuple(pull for pull in self.listed if pull.head_ref == head)

    def get_actionable_feedback(self, number):
        if self.feedback_error:
            raise self.feedback_error
        return (("THREAD", "alice on app.py: rename this"),)


def execution(item_id, repo="repo", primary=None):
    return {"id": uuid4().hex, "item_id": item_id, "repository_id": repo, "primary_pr_url": primary,
            "closed_at": datetime.now(UTC).isoformat()}


class ClosedReadyReconcilerTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "queue.sqlite3"
        migrate(self.path)
        self.start_server(self.path)

    def test_each_retired_closed_stage_migrates_loads_lists_and_reconciles(self):
        api = API(self.url, RUNNER)
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "repo", "remote": "https://github.com/example/repo"})[0], 200)
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Historical work", "target": "checked_in"})
        self.assertEqual(status, 201, item)
        primary = "https://github.com/example/repo/pull/7"
        merged_at = datetime.now(UTC).isoformat()
        revision = Revision(HEAD, BASE)
        closed = Execution(
            Target.MERGED, Stage.DELIVERY, Status.COMPLETED, candidate=HEAD,
            primary_pr=primary, ready_revision=revision,
            final_output=OutputPR(primary, revision, merge_commit=BASE,
                                  merge_method="merge", merged_at=merged_at))
        merged = replace(pull_request(HEAD, item_branch(item["id"]), state="closed"),
                         merged=True, merge_commit_sha=BASE, merged_at=merged_at)
        github = FakeGitHub([merged])
        config = {"reconciler_id": "local", "repositories": {
            "repo": {"base_branch": "main", "post_merge_check": "deploy"}}}
        reconciler = Reconciler(config, self.root / "state", api, github_token="token",
                                github_client=lambda *args, **kwargs: github)
        retired = ("pr_creation", "pre_pr_validation", "pr_promotion", "pr_validation",
                   "adversarial_review", "pr_reconciliation", "pr_revision", "check_in")
        for stage in retired:
            for status in ("completed", "failed", "cancelled"):
                with self.subTest(stage=stage, status=status):
                    raw = {**asdict(closed), "stage": stage, "status": status}
                    if status != "completed":
                        raw["final_output"] = {"reference": "/historical-report", "draft_revision": None}
                    with write_transaction(self.path) as db:
                        db.execute("UPDATE executions SET state_json=? WHERE id=?",
                                   (json.dumps(raw), item["execution"]["id"]))
                        db.execute("UPDATE transition_history SET state_json=? WHERE execution_id=?",
                                   (json.dumps(raw), item["execution"]["id"]))
                        # Undo later columns before replaying closed-stage normalization.
                        db.execute("ALTER TABLE captains DROP COLUMN compaction_count")
                        db.execute("ALTER TABLE dispatch_settings DROP COLUMN max_writers")
                        db.execute(f"PRAGMA user_version={PRE_NORMALIZATION_VERSION}")
                        saved = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                                 for table in ("work_items", "attempts", "transition_history", "reports")}
                        metadata = tuple(db.execute(
                            "SELECT version, created_at, updated_at, previous_id FROM executions WHERE id=?",
                            (item["execution"]["id"],)).fetchone())
                    migrate(self.path)
                    migrate(self.path)
                    with connect(self.path, mode="ro") as db:
                        row = intake.execution_row(db, item["id"], item["execution"]["id"])
                        self.assertEqual(intake.STATE.validate_json(row["state_json"]).stage, Stage.DELIVERY)
                        self.assertEqual(tuple(row[key] for key in (
                            "version", "created_at", "updated_at", "previous_id")), metadata)
                        for table, rows in saved.items():
                            self.assertEqual([tuple(row) for row in db.execute(f"SELECT * FROM {table}")], rows)
                    loaded = api.call(f"/items/{item['id']}")["execution"]["state"]
                    self.assertEqual(loaded, {**raw, "stage": "delivery"})
                    row, = api.call("/operator/executions?kind=closed")["items"]
                    self.assertEqual((row["stage"], row["status"]), ("delivery", status))
                    timeline = api.call(f"/operator/executions/{row['id']}/timeline")
                    self.assertEqual(timeline["item"]["id"], row["id"])
                    self.assertIn(stage, {event["stage"] for event in timeline["events"]})
                    with patch.object(github, "get_commit_checks", create=True,
                                      return_value=CommitChecks(BASE, (), ())):
                        result = reconciler.run_pass()
                    self.assertEqual((result["checked"], result["errors"]), (0, []))
                    self.assertEqual(api.call(f"/items/{item['id']}")["execution"]["state"], loaded)

    def test_real_operator_summary_advances_a_merged_ready_execution_to_checked_in(self):
        api = API(self.url, RUNNER)
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "repo", "remote": "https://github.com/example/repo"})[0], 200)
        config = {"reconciler_id": "local", "repositories": {"repo": {"base_branch": "main"}}}
        for merged_head in (HEAD, OTHER):
            with self.subTest(merged_head=merged_head):
                status, item = self.request("/v1/items", OWNER, {
                    "repository_id": "repo", "goal": "Ship it", "target": "ready_for_check_in"})
                self.assertEqual(status, 201, item)
                primary = "https://github.com/example/repo/pull/7"
                revision = Revision(HEAD, BASE)
                ready = Execution(Target.READY, Stage.DELIVERY, Status.COMPLETED,
                                  candidate=HEAD, primary_pr=primary, ready_revision=revision,
                                  final_output=OutputPR(primary, revision))
                with write_transaction(self.path) as db:
                    db.execute("UPDATE executions SET state_json=? WHERE id=?",
                               (json.dumps({**asdict(ready), "stage": "pr_reconciliation"}),
                                item["execution"]["id"]))
                    db.execute("ALTER TABLE captains DROP COLUMN compaction_count")
                    db.execute("ALTER TABLE dispatch_settings DROP COLUMN max_writers")
                    db.execute(f"PRAGMA user_version={PRE_NORMALIZATION_VERSION}")
                migrate(self.path)
                rows = api.call("/operator/executions?kind=closed&outcome=ready_for_check_in")["items"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["output"], {"kind": "pr", "url": primary})
                merged = replace(pull_request(merged_head, item_branch(item["id"]), state="closed"),
                                 merged=True, merge_commit_sha=BASE,
                                 merged_at="2026-11-24T06:33:57Z")
                github = FakeGitHub([merged], parents=(BASE, merged_head))
                reconciler = Reconciler(config, self.root / "state", api, github_token="token",
                                        github_client=lambda *args, **kwargs: github)
                with patch.object(github, "is_on_branch", return_value=True) as compare:
                    self.assertEqual(reconciler.run_pass(), {"observed": 1, "checked": 0, "errors": []})
                if merged_head != HEAD:
                    compare.assert_called_once_with(HEAD, merged_head)
                else:
                    compare.assert_not_called()
                done = api.call("/operator/executions?kind=closed&outcome=checked_in")["items"]
                row = next(row for row in done if row["id"] == item["execution"]["id"])
                self.assertEqual(row["delivery"]["merge_commit"], BASE)
                output = api.call(f"/items/{item['id']}")["execution"]["state"]["final_output"]
                self.assertEqual(output["revision"]["head"], merged_head)


class ReconcilerPassTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.github = {}
        self.config = {"reconciler_id": "local", "repositories": {}}

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
        self.config["repositories"] = {"repo": {"base_branch": "main"}, "other": {"base_branch": "main"}}
        first = replace(pull_request(HEAD, item_branch("one")), additions=17, deletions=3)
        self.github["repo"] = FakeGitHub([first, replace(first, number=8)])
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
        self.assertEqual((body["pr"]["additions"], body["pr"]["deletions"], body["duplicate_count"]),
                         (17, 3, 1))
        self.assertEqual(body["feedback"], ["alice on app.py: rename this"])
        self.assertNotIn("checks", body)

        api.calls.clear()
        self.assertEqual(reconciler.run_pass()["errors"], [])
        self.assertNotIn("/items/two", api.calls)
        self.now += 61
        self.assertEqual(len(reconciler.run_pass()["errors"]), 1)

    def test_draft_only_delivery_needs_no_check_policy_or_review_read(self):
        self.config["repositories"] = {"repo": {"base_branch": "main"}}
        self.config["validation_policy"] = "missing-policy.json"
        self.github["repo"] = FakeGitHub([pull_request(HEAD, item_branch("one"), draft=True)],
                                         feedback_error=GitHubError("graphql", "unavailable"))
        api = FakeAPI([execution("one", primary="https://github.com/example/repo/pull/7")],
                      {"repo": "https://github.com/example/repo"})
        original_call = api.call

        def call(path, body=None):
            if path == "/items/one":
                return {"target": "checked_in", "execution": {"state": {"publication_mode": "draft"}}}
            return original_call(path, body)

        with patch.object(api, "call", side_effect=call):
            result = self.reconciler(api).run_pass()
        self.assertEqual(result, {"observed": 1, "checked": 0, "errors": []})
        self.assertTrue(api.posts[0][1]["pr"]["draft"])
        self.assertNotIn("checks", api.posts[0][1])
        self.assertNotIn("feedback", api.posts[0][1])

    def test_an_incomplete_github_read_posts_nothing_and_backs_off(self):
        self.config["repositories"] = {"repo": {"base_branch": "main"}}
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

    def test_a_merge_into_the_target_is_posted_with_how_and_when_it_merged(self):
        self.config["repositories"] = {"repo": {"base_branch": "main", "post_merge_check": "deploy"}}
        merged = replace(pull_request(HEAD, item_branch("one"), state="closed"), merged=True,
                         merge_commit_sha=OTHER, merged_at="2026-11-24T06:33:57Z")
        api = FakeAPI([execution("one", primary="https://github.com/example/repo/pull/7")],
                      {"repo": "https://github.com/example/repo"})
        reconciler = self.reconciler(api)
        for parents, on_branch, method in (((BASE, HEAD), False, "merge"), ((BASE,), True, "squash_or_rebase"),
                                           ((BASE,), False, None)):
            with self.subTest(method=method):
                self.github["repo"] = FakeGitHub([merged], parents=parents, on_branch=on_branch)
                api.posts.clear()
                self.assertEqual(reconciler.run_pass()["errors"], [])
                (_, body), = api.posts
                self.assertEqual((body["pr"]["merge_method"], body["pr"]["merged_at"]),
                                 (method, "2026-11-24T06:33:57Z"))
                self.assertNotIn("deployment", body)
        # A merge into another base never delivers, so how it merged doesn't matter.
        self.github["repo"] = FakeGitHub([replace(merged, base_ref="dev")], parents=(BASE, HEAD))
        api.posts.clear()
        reconciler.run_pass()
        self.assertNotIn("merge_method", api.posts[0][1]["pr"])

    def test_deployment_is_opt_in_and_keeps_observing_checked_in_items(self):
        self.config["repositories"] = {"repo": {"base_branch": "main"}}
        primary = "https://github.com/example/repo/pull/7"
        merged = replace(pull_request(HEAD, item_branch("done"), state="closed"), merged=True,
                         merge_commit_sha=OTHER)
        done = dict(execution("done", primary=primary), outcome="checked_in")
        # A ready-only completion and an unmapped repository must not be deployment work.
        closed = [done, dict(execution("ready"), outcome="ready_for_check_in"),
                  dict(execution("elsewhere", repo="unmapped"), outcome="checked_in")]
        api = FakeAPI([], {"repo": "https://github.com/example/repo"}, closed=closed)
        self.github["repo"] = FakeGitHub([merged], parents=(BASE, HEAD))
        reconciler = self.reconciler(api)
        self.assertEqual(reconciler.run_pass()["observed"], 0)
        self.assertTrue(any("outcome=ready_for_check_in" in path for path in api.calls))
        self.config["repositories"]["repo"]["post_merge_check"] = "deploy"
        with patch.object(self.github["repo"], "get_commit_checks", create=True,
                          return_value=CommitChecks(OTHER, (), ())) as read:
            result = reconciler.run_pass()
        self.assertEqual((result["observed"], result["checked"], result["errors"]), (1, 0, []))
        read.assert_called_once_with(OTHER, include_statuses=False)
        self.assertEqual(api.posts[0][1]["deployment"],
                         {"commit": OTHER, "check_name": "deploy", "state": "pending"})
        # An incomplete provider read cannot turn the previous deployment into pending or success.
        with patch.object(self.github["repo"], "get_commit_checks", create=True,
                          side_effect=GitHubError("check runs", "rate limited", 429, retry_after_seconds=30)):
            self.assertEqual(len(reconciler.run_pass()["errors"]), 1)
        self.assertEqual(len(api.posts), 1)
        self.assertGreater(reconciler.backoff["repo"], self.now)

    def test_deployment_reads_the_latest_named_check_on_the_merge_commit(self):
        github = SimpleNamespace()
        run = {"id": 2, "name": "deploy", "head_sha": OTHER, "status": "completed", "conclusion": "success"}
        cases = [
            ([], "pending"), ([dict(run, name="CI")], "pending"),
            ([dict(run, status="in_progress", conclusion=None)], "pending"),
            ([run], "deployed"),
            *[([dict(run, conclusion=conclusion)], "failed")
              for conclusion in ("failure", "cancelled", "timed_out", "skipped", "neutral")],
            ([run, dict(run, id=3, status="queued", conclusion=None)], "pending"),
            ([dict(run, id=3), dict(run, conclusion="failure")], "deployed"),
        ]
        for runs, expected in cases:
            with self.subTest(runs=runs):
                github.get_commit_checks = lambda sha, **kw: CommitChecks(sha, tuple(runs), ())
                self.assertEqual(Reconciler.deployment(github, OTHER, "deploy")["state"], expected)
        for invalid in (dict(run, head_sha=HEAD), dict(run, conclusion=None), dict(run, id=None)):
            github.get_commit_checks = lambda sha, **kw: CommitChecks(sha, (invalid,), ())
            with self.assertRaises(GitHubError):
                Reconciler.deployment(github, OTHER, "deploy")

    def test_invalid_deployment_configuration_is_explicit(self):
        for value in ("", " ", 7, "x" * 256):
            self.config["repositories"] = {"repo": {"post_merge_check": value}}
            with self.subTest(value=value), self.assertRaisesRegex(ReconcilerError, "post_merge_check"):
                self.reconciler(FakeAPI([], {}))

    def test_a_merge_into_a_non_deploying_branch_has_no_deployment_signal(self):
        self.config["repositories"] = {"repo": {"base_branch": "main", "post_merge_check": "deploy"}}
        api = FakeAPI([], {"repo": "https://github.com/example/repo"}, closed=[
            dict(execution("done"), outcome="checked_in")])
        original_call = api.call

        def call(path, body=None):
            if path == "/items/done":
                return {"target": "checked_in", "target_branch": "release"}
            return original_call(path, body)

        api.call = call
        result = self.reconciler(api).run_pass()
        self.assertEqual((result["observed"], result["errors"], api.posts), (0, [], []))

    def test_deployed_and_expired_executions_stop_before_github_or_older_pages(self):
        self.config["repositories"] = {"repo": {"base_branch": "main", "post_merge_check": "deploy"}}
        deployed = dict(execution("deployed"), outcome="checked_in",
                        deployment={"state": "deployed", "check_name": "deploy"})
        expired = dict(execution("expired"), outcome="checked_in",
                       closed_at=(datetime.now(UTC) - DEPLOYMENT_WINDOW).isoformat())
        api = FakeAPI([], {"repo": "https://github.com/example/repo"}, closed=[deployed, expired])
        original_call = api.call

        def call(path, body=None):
            page = original_call(path, body)
            if "kind=closed" in path:
                if "outcome=ready_for_check_in" in path:
                    return page
                # Reading this cursor would walk history beyond the bounded window.
                self.assertNotIn("cursor=", path)
                page["next_cursor"] = "older"
            return page

        api.call = call
        self.assertEqual(self.reconciler(api).run_pass(), {"observed": 0, "checked": 0, "errors": []})
        self.assertNotIn("/items/deployed", api.calls)
        self.assertNotIn("/items/expired", api.calls)
        self.config["repositories"]["repo"].pop("post_merge_check")
        self.reconciler(api).run_pass()
        self.assertEqual(api.heartbeats[-1]["deployment_checks"], {"repo": None})

    def test_completed_ready_prs_are_observed_without_deployment_or_local_checks(self):
        self.config["repositories"] = {"repo": {"base_branch": "main"}}
        primary = "https://github.com/example/repo/pull/7"
        ready = dict(execution("ready", primary=primary), stage="delivery", outcome="ready_for_check_in",
                     output={"kind": "pr", "url": primary}, output_revision={"head": HEAD, "base": BASE},
                     closed_at=(datetime.now(UTC) - timedelta(days=30)).isoformat())
        api = FakeAPI([], {"repo": "https://github.com/example/repo"}, closed=[ready])
        merged = replace(pull_request(OTHER, item_branch("ready"), state="closed"), merged=True,
                         merge_commit_sha=BASE, merged_at="2026-11-24T06:33:57Z")
        github = FakeGitHub([merged], parents=(BASE, OTHER))
        self.github["repo"] = github
        reconciler = self.reconciler(api)
        for descendant in (True, False):
            with self.subTest(descendant=descendant), patch.object(
                    github, "is_on_branch", return_value=descendant) as compare:
                api.posts.clear()
                self.assertEqual(reconciler.run_pass(), {"observed": 1, "checked": 0, "errors": []})
                compare.assert_called_once_with(HEAD, OTHER)
                observed = api.posts[0][1]["pr"]
                self.assertEqual(observed.get("merged_head_ancestor"), HEAD if descendant else None)
        api.posts.clear()
        with patch.object(github, "is_on_branch",
                          side_effect=GitHubError("compare", "unavailable", 503, retry_after_seconds=1)):
            self.assertEqual(len(reconciler.run_pass()["errors"]), 1)
            self.assertEqual(api.posts, [])
        self.now += 120
        github.pulls = [replace(merged, head_sha=HEAD)]
        with patch.object(github, "is_on_branch", side_effect=AssertionError("No comparison needed")):
            self.assertEqual(reconciler.run_pass()["errors"], [])
        github.pulls = [pull_request(HEAD, item_branch("ready"))]
        with patch.object(reconciler, "policy", side_effect=AssertionError("No local checks after completion")):
            self.assertEqual(reconciler.run_pass(), {"observed": 1, "checked": 0, "errors": []})

    def test_unexpected_execution_errors_are_reported_without_aborting_the_pass(self):
        self.config["repositories"] = {"repo": {"base_branch": "main"}}
        self.github["repo"] = FakeGitHub([pull_request(HEAD, item_branch("good"))])
        for error in (KeyError("revision"), TypeError("invalid revision"), RuntimeError("unexpected")):
            with self.subTest(error=error):
                api = FakeAPI([execution("bad"), execution("good")],
                              {"repo": "https://github.com/example/repo"})
                reconciler = self.reconciler(api)
                observe = reconciler.observe

                def observe_one(row, *args):
                    if row["item_id"] == "bad":
                        raise error
                    return observe(row, *args)

                with patch.object(reconciler, "observe", side_effect=observe_one):
                    result = reconciler.run_pass()
                self.assertEqual((result["observed"], result["checked"]), (1, 0))
                self.assertEqual(len(result["errors"]), 1)
                self.assertIn(f"bad: {type(error).__name__}:", result["errors"][0])
                self.assertIn(str(error), result["errors"][0])
                self.assertEqual([item for item, _ in api.posts], ["good"])

    def test_completed_ready_discovery_keeps_its_filter_across_pages(self):
        primary = "https://github.com/example/repo/pull/7"
        ready = dict(execution("ready", primary=primary), stage="delivery", outcome="ready_for_check_in")
        api = FakeAPI([], {})
        original_call = api.call

        def call(path, body=None):
            if "outcome=ready_for_check_in" in path:
                api.calls.append(path)
                if "cursor=next" in path:
                    return {"items": [ready], "next_cursor": None}
                return {"items": [dict(ready, primary_pr_url=None)], "next_cursor": "next"}
            return original_call(path, body)

        api.call = call
        self.assertEqual(list(self.reconciler(api).executions()), [ready])
        self.assertTrue(any("outcome=ready_for_check_in&cursor=next" in path for path in api.calls))

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

    def test_a_merged_pr_found_by_branch_is_observed_exactly(self):
        self.config["repositories"] = {"repo": {"base_branch": "main"}}
        branch = item_branch("one")
        # The list endpoint leaves merged and mergeable unset.
        self.github["repo"] = FakeGitHub(
            [pull_request(HEAD, branch, state="closed", merged=True, mergeable=None)],
            listed=[pull_request(HEAD, branch, state="closed", merged=None, mergeable=None)])
        api = FakeAPI([execution("one")], {"repo": "https://github.com/example/repo"})
        self.assertEqual(self.reconciler(api).run_pass()["errors"], [])
        (_, body), = api.posts
        self.assertEqual((body["pr"]["state"], body["pr"]["merged"]), ("closed", True))

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
