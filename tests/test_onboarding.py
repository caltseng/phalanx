import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx.github import CommitChecks, GitHubClient
from phalanx.onboarding import discover_ready_checks, onboard
from test_bootstrap import OWNER, RUNNER, ServerFixture


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class OnboardingTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.start_server(self.directory / "server.sqlite3")
        self.remote = self.directory / "remote.git"
        source = self.directory / "source"
        git("init", "--bare", "--initial-branch=trunk", str(self.remote))
        git("init", "--initial-branch=trunk", str(source))
        for key, value in (("user.name", "Phalanx tests"), ("user.email", "tests@example.invalid"),
                           ("commit.gpgSign", "false"), ("core.hooksPath", str(self.directory / "no-hooks"))):
            git("config", key, value, cwd=source)
        (source / "README").write_text("hello\n")
        git("add", ".", cwd=source)
        git("commit", "-m", "base", cwd=source)
        git("push", str(self.remote), "trunk", cwd=source)
        self.config = self.directory / "runner.json"
        self.policy = self.directory / "validation.json"
        self.config.write_text(json.dumps({
            "base_url": self.url, "runner_id": "desk", "validation_policy": str(self.policy),
            "repositories": {"other": {"checkout": "x", "base_branch": "main",
                                       "host_id": "local", "project_id": "p0"}},
        }), encoding="utf-8")
        self.checkout = self.directory / "clones" / "repo"

    def onboard(self, **options):
        found = []

        def discover(remote, base, token):
            found.append((remote, base, token))
            return [{"name": "test", "kind": "check_run", "app_id": 15368}], "1 found"
        return onboard(self.config, self.directory / "state", "repo", discover=discover, **options), found

    def statuses(self, report):
        return {step["step"]: step["status"] for step in report["steps"]}

    def test_onboards_from_nothing_then_verifies_without_rewriting(self):
        report, found = self.onboard(remote=str(self.remote), checkout=str(self.checkout), project_id="p1",
                                     owner_token=OWNER, runner_token=RUNNER, github_token="gh")
        self.assertEqual(self.statuses(report), {
            "registration": "done", "checkout": "done", "runtime_project": "ok", "mapping": "done",
            "validation": "missing", "owner_trust": "manual"})
        self.assertFalse(report["complete"])
        self.assertIn("set local_checks", report["steps"][4]["detail"])
        self.assertEqual(found, [(str(self.remote), "trunk", "gh")])
        self.assertEqual(self.get("/v1/repositories", RUNNER)[1][0]["id"], "repo")
        self.assertTrue((self.checkout / "README").exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["repositories"]["repo"], {
            "checkout": str(self.checkout), "base_branch": "trunk", "host_id": "local", "project_id": "p1"})
        self.assertIn("other", config["repositories"])
        policy = json.loads(self.policy.read_text(encoding="utf-8"))
        self.assertEqual(policy["repositories"]["repo"], {
            "local_checks": None, "draft_checks": [],
            "ready_checks": [{"name": "test", "kind": "check_run", "app_id": 15368}]})

        policy["repositories"]["repo"]["local_checks"] = []
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        report, found = self.onboard(runner_token=RUNNER)
        self.assertTrue(report["complete"])
        self.assertEqual(found, [])
        self.assertEqual(set(self.statuses(report).values()), {"ok", "manual"})

    def test_reports_each_missing_step_without_partial_mapping(self):
        report, _ = self.onboard(remote=str(self.remote), checkout=str(self.checkout), host_id="",
                                 runner_token=RUNNER)
        self.assertFalse(report["complete"])
        details = {step["step"]: step for step in report["steps"]}
        self.assertIn("python -m phalanx register repo", details["registration"]["detail"])
        self.assertEqual(details["checkout"]["status"], "missing")
        self.assertIn("--project-id", details["runtime_project"]["detail"])
        self.assertEqual(details["mapping"]["status"], "missing")
        self.assertNotIn("repo", json.loads(self.config.read_text(encoding="utf-8"))["repositories"])
        self.assertFalse(self.checkout.exists())

        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": "https://github.com/o/r"})
        report, _ = self.onboard(remote=str(self.remote), runner_token=RUNNER)
        self.assertIn("is registered with remote https://github.com/o/r", report["steps"][0]["detail"])


class DiscoveryTests(unittest.TestCase):
    def test_proposes_deduplicated_ready_checks_from_base_tip(self):
        tip = "a" * 40
        runs = ({"name": "test", "app": {"id": 15368}}, {"name": "test", "app": {"id": 15368}},
                {"name": "lint", "app": {"id": 7}})
        with patch.object(GitHubClient, "get_branch_tip", return_value=tip), \
                patch.object(GitHubClient, "get_commit_checks",
                             return_value=CommitChecks(tip, runs, ({"context": "ci/legacy"},))):
            checks, detail = discover_ready_checks("https://github.com/o/r.git", "main", "token")
        self.assertEqual(checks, [{"name": "test", "kind": "check_run", "app_id": 15368},
                                  {"name": "lint", "kind": "check_run", "app_id": 7},
                                  {"name": "ci/legacy", "kind": "status"}])
        self.assertIn("3 found on main", detail)
        self.assertEqual(discover_ready_checks("https://github.com/o/r", "main", None)[0], None)


if __name__ == "__main__":
    unittest.main()
