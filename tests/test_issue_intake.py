from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx.__main__ import api_request, main
from phalanx.github import GitHubClient, GitHubError, HTTPResponse
from phalanx.issue_intake import ingest_issue
from phalanx.store import read_transaction

from test_bootstrap import OWNER, RUNNER, ServerFixture


ISSUE_URL = "https://github.com/Example/Project/issues/42"


class IssueIntakeTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        self.server = self.start_server(self.path)
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "example", "remote": "git@github.com:Example/Project.git",
        })[0], 200)
        self.issue = {"number": 42, "html_url": ISSUE_URL,
                      "title": "Fix the bug", "body": "Original context"}
        self.comments = [
            {"id": 1, "html_url": ISSUE_URL + "#issuecomment-1",
             "user": {"login": "reviewer"}, "body": "Acceptance criteria: preserve behavior"},
            {"id": 2, "html_url": ISSUE_URL + "#issuecomment-2",
             "user": {"login": "owner"}, "body": "Follow-up context"},
        ]
        self.requests = []
        environment = patch.dict(os.environ, PHALANX_OWNER_TOKEN=OWNER,
                                 PHALANX_GITHUB_TOKEN="local-github-token")
        environment.start()
        self.addCleanup(environment.stop)
        client = patch("phalanx.issue_intake.GitHubClient", side_effect=self.make_client)
        client.start()
        self.addCleanup(client.stop)

    def make_client(self, owner, name, *, token):
        return GitHubClient(owner, name, token=token, transport=self.github_request)

    def github_request(self, request, timeout):
        self.assertEqual(request.get_header("Authorization"), "Bearer local-github-token")
        self.assertEqual(request.method, "GET")
        self.requests.append(request)
        base = "https://api.github.com/repos/example/project/issues/42"
        headers = {}
        if request.full_url == base:
            data = self.issue
        elif request.full_url == base + "/comments?per_page=100":
            data = self.comments[:1]
            headers = {"Link": f'<{base}/comments?page=2>; rel="next"'}
        elif request.full_url == base + "/comments?page=2":
            data = self.comments[1:]
        else:
            self.fail(request.full_url)
        return HTTPResponse(200, headers, json.dumps(data).encode())

    def ingest(self, *, repository="example", target="ready_for_check_in", key=None):
        return ingest_issue(api_request, self.url, repository, 42, target, key=key)

    def item_count(self):
        with read_transaction(self.path) as db:
            return db.execute("SELECT count(*) FROM work_items").fetchone()[0]

    def file(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["phalanx", "--api-url", self.url, "intake", *args]):
            with redirect_stdout(output):
                main()
        return json.loads(output.getvalue())

    def test_cli_intake_sends_dependencies_priority_context_and_criteria(self):
        upstream = self.file("example", "First", "--target", "report")
        merged = self.file("example", "Merge", "--target", "checked_in")
        item = self.file("example", "Second", "--target", "checked_in", "--key", "second",
                         "--after", merged["id"], "--after", f"{upstream['id']}:report",
                         "--priority", "urgent", "--context", "https://example.test/a",
                         "--context", "note", "--accept", "tests pass", "--accept", "docs")
        self.assertEqual(item["priority"], "urgent")
        self.assertEqual(item["context_links"], ["https://example.test/a", "note"])
        self.assertEqual(item["acceptance_criteria"], ["tests pass", "docs"])
        self.assertEqual(
            sorted((d["upstream_id"], d["milestone"]) for d in item["dependencies"]),
            sorted([(merged["id"], "checked_in"), (upstream["id"], "report")]))
        self.assertEqual(upstream["priority"], "normal")
        self.assertEqual(upstream["dependencies"], [])
        with patch.object(sys, "argv", ["phalanx", "intake", "example", "x", "--target",
                                        "report", "--after", "abc:merged"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main()

    def test_cli_import_replay_restart_and_real_dispatch(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["phalanx", "--api-url", self.url, "ingest-issue",
                                       "example", "42", "--target", "ready_for_check_in"]):
            with redirect_stdout(output):
                main()
        item = json.loads(output.getvalue())
        self.assertEqual(item["context_links"], [ISSUE_URL])
        self.assertIn("Original context", item["goal"])
        self.assertIn("Acceptance criteria: preserve behavior", item["goal"])
        self.assertIn("Follow-up context", item["goal"])
        self.assertIn(ISSUE_URL + "#issuecomment-2", item["goal"])
        self.assertEqual(item["execution"]["state"]["stage"], "development")
        self.assertEqual(self.item_count(), 1)
        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        self.assertEqual(self.ingest(), item)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.ingest(), range(4)))
        self.assertEqual(results, [item] * 4)
        self.assertEqual(self.item_count(), 1)
        status, assignment = self.request("/v1/dispatch/claim", RUNNER, {
            "runner_id": "local", "repository_ids": ["example"], "stages": ["development"],
        }, {"Phalanx-Lease": "x" * 32, "Idempotency-Key": "claim-import"})
        self.assertEqual(status, 200, assignment)
        self.assertEqual(assignment["status"], "claimed")
        self.assertEqual(assignment["item_id"], item["id"])
        self.assertEqual(assignment["request"]["goal"], item["goal"])
        self.assertEqual(assignment["request"]["context_links"], [ISSUE_URL])
        self.assertNotIn("local-github-token", json.dumps(assignment))

    def test_cli_issue_target_defaults_to_merge_and_preserves_explicit_targets(self):
        for target in (None, "ready_for_check_in", "report"):
            with self.subTest(target=target):
                arguments = ["phalanx", "--api-url", self.url, "ingest-issue", "example", "42",
                             "--key", f"target-{target}"]
                if target:
                    arguments.extend(["--target", target])
                output = io.StringIO()
                with patch.object(sys, "argv", arguments), redirect_stdout(output):
                    main()
                item = json.loads(output.getvalue())
                self.assertEqual(item["execution"]["state"]["target"], target or "checked_in")

    def test_changed_snapshot_and_target_conflict_without_duplicate(self):
        original = self.ingest()
        self.issue["body"] = "Edited context"
        with self.assertRaisesRegex(ValueError, "HTTP 409"):
            self.ingest()
        self.issue["body"] = "Original context"
        with self.assertRaisesRegex(ValueError, "HTTP 409"):
            self.ingest(target="checked_in")
        self.assertEqual(self.item_count(), 1)
        self.issue["body"] = "Edited context"
        explicit = self.ingest(target="report", key="intentional-new-import")
        self.assertNotEqual(explicit["id"], original["id"])
        self.assertEqual(explicit["execution"]["state"]["stage"], "report_drafting")
        self.assertEqual(self.ingest(target="report", key="intentional-new-import"), explicit)

    def test_cli_intake_and_issue_intake_preserve_draft_publication(self):
        for command in (["intake", "example", "Review only"],
                        ["ingest-issue", "example", "42"]):
            output = io.StringIO()
            with patch.object(sys, "argv", ["phalanx", "--api-url", self.url, *command,
                                           "--target", "ready_for_check_in", "--publication-mode", "draft"]):
                with redirect_stdout(output):
                    main()
            item = json.loads(output.getvalue())
            self.assertEqual(item["publication_mode"], "draft")
            self.assertEqual(item["execution"]["state"]["publication_mode"], "draft")

    def test_unregistered_or_non_github_repository_and_credentials_cannot_import(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.ingest(repository="unknown")
        self.assertEqual(self.request("/v1/repositories", OWNER, {
            "id": "other", "remote": "https://other.example/owner/repo.git",
        })[0], 200)
        with self.assertRaisesRegex(ValueError, "github.com"):
            self.ingest(repository="other")
        for name in ("PHALANX_GITHUB_TOKEN", "PHALANX_OWNER_TOKEN"):
            with patch.dict(os.environ, {name: ""}):
                with self.assertRaisesRegex(ValueError, name):
                    self.ingest()
        self.assertEqual(self.requests, [])
        with patch.dict(os.environ, PHALANX_OWNER_TOKEN=RUNNER):
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                self.ingest()
        self.assertEqual(self.item_count(), 0)

    def test_provider_failure_pr_and_oversized_context_never_create_partial_work(self):
        with patch.object(self, "github_request", return_value=HTTPResponse(403, {}, b"{}")):
            with self.assertRaisesRegex(GitHubError, "HTTP 403"):
                self.ingest()
        self.issue["pull_request"] = {}
        with self.assertRaises(GitHubError):
            self.ingest()
        del self.issue["pull_request"]
        self.comments[1]["body"] = "x" * 16000
        with self.assertRaisesRegex(ValueError, "16000-character"):
            self.ingest()
        self.comments[1]["body"] = "Follow-up context"
        with patch.object(self, "github_request", side_effect=[
            HTTPResponse(200, {}, json.dumps(self.issue).encode()),
            HTTPResponse(200, {"Link": '<https://other.example/comments>; rel="next"'},
                         json.dumps(self.comments[:1]).encode()),
        ]):
            with self.assertRaisesRegex(GitHubError, "untrusted pagination"):
                self.ingest()
        self.assertEqual(self.item_count(), 0)

    def test_exact_intake_size_boundary_preserves_context(self):
        self.comments = []
        self.issue["body"] = "x"
        prefix = f"{self.issue['title']}\n\nGitHub issue: {ISSUE_URL}\n\n"
        self.issue["body"] = "x" * (16000 - len(prefix))
        item = self.ingest()
        self.assertEqual(len(item["goal"]), 16000)
        self.assertTrue(item["goal"].endswith(self.issue["body"]))
        self.issue["body"] += "x"
        with self.assertRaisesRegex(ValueError, "16000-character"):
            self.ingest()
        self.assertEqual(self.item_count(), 1)
