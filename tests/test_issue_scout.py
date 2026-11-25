from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx import intake
from phalanx.github import GitHubClient, HTTPResponse
from phalanx.issue_scout_client import scout
from phalanx.pr_models import github_issue_key
from phalanx.store import read_transaction, write_transaction
from phalanx.work_cli import request

from test_bootstrap import OWNER, RUNNER, ServerFixture


class IssueScoutTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        self.server = self.start_server(self.path)
        self.request("/v1/repositories", OWNER, {"id": "example", "remote": "https://github.com/example/project.git"})
        self.captain = self.register_captain("captain")
        self.configure()

    def register_captain(self, session):
        code, result = self.request("/v1/captains", RUNNER, {
            "session_ref": session, "charter_revision": "test",
            "pickup": {"repositories": {"example": None}}})
        self.assertEqual(code, 201, result)
        return result["id"]

    def api(self, route, **options):
        return request(self.url, RUNNER, route, **options)

    def configure(self, **changes):
        current = self.get("/v1/issue-scout", RUNNER)[1]["settings"]
        body = {"expected_revision": current["revision"], "enabled": True, "cadence_minutes": 60,
                "author_login": "configured-owner", "author_id": 42, "repositories": ["example"], **changes}
        code, result = self.request("/v1/issue-scout/settings", OWNER, body, method="PUT")
        self.assertEqual(code, 200, result)
        return result

    def claim(self, captain=None):
        return self.api("/v1/issue-scout/claim", body={"captain_id": captain or self.captain})["claim"]

    def issue(self, number=1, **changes):
        return {"number": number, "author_login": "configured-owner", "author_id": 42,
                "state": "open", "title": "Fix issue", "body": "UNTRUSTED PRIVATE BODY",
                "updated_at": "2026-01-01T00:00:00Z", **changes}

    def submit(self, claim, issues, *, has_more=False):
        body = {"claim_id": claim["claim_id"], "repository_id": claim["repository_id"], "page": claim["page"],
                "verified_login": "configured-owner", "verified_id": 42, "issues": issues, "has_more": has_more}
        return self.api(f"/v1/issue-scout/passes/{claim['id']}/page", body=body)

    def source(self, number=1):
        with read_transaction(self.path) as db:
            return dict(db.execute("SELECT * FROM issue_sources WHERE number=?", (number,)).fetchone())

    def items(self):
        with read_transaction(self.path) as db:
            return [intake.inspect(db, row[0]) for row in db.execute("SELECT id FROM work_items")]

    def due(self):
        with write_transaction(self.path) as db:
            db.execute("UPDATE issue_scout_settings SET due_at='2000-01-01T00:00:00.000000+00:00'")
            db.execute("UPDATE issue_scout_passes SET retry_at=NULL, lease_until='2000-01-01T00:00:00.000000+00:00'")

    def decide(self, decision, number=1):
        return self.request(f"/v1/issue-scout/issues/example/{number}/decision", OWNER, {
            "expected_fingerprint": self.source(number)["fingerprint"], "decision": decision})

    def test_configured_author_enters_real_intake_with_default_target_and_no_writer(self):
        self.request("/v1/dispatch/settings", OWNER, {"paused": True, "max_writers": 2,
                                                     "max_building_writers": 1}, method="PUT")
        result = self.submit(self.claim(), [self.issue()])
        item, = self.items()
        self.assertEqual(item["execution"]["state"]["target"], "checked_in")
        self.assertEqual(item["execution"]["state"]["status"], "queued")
        self.assertEqual(item["context_links"], ["https://github.com/example/project/issues/1"])
        self.assertIn("UNTRUSTED PRIVATE BODY", item["goal"])
        self.assertIn("untrusted", item["acceptance_criteria"][0])
        self.assertEqual(self.source()["state"], "imported")
        capacity = self.get("/v1/dispatch/settings", RUNNER)[1]
        self.assertEqual((capacity["paused"], capacity["active_writers"], capacity["building_writers"]), (True, 0, 0))
        self.assertEqual((capacity["max_writers"], capacity["max_building_writers"]), (2, 1))
        self.assertNotIn("UNTRUSTED PRIVATE BODY", json.dumps(result))
        self.assertIsNone(self.claim())

    def test_other_authors_require_owner_decision_and_fresh_observation(self):
        other = self.issue(author_login="another-author", author_id=99)
        self.submit(self.claim(), [other])
        self.assertEqual(self.items(), [])
        overview = self.get("/v1/operator/overview", RUNNER)[1]
        self.assertEqual(overview["sections"]["needs_you"], 1)
        pending, = overview["issue_scout_needs_owner"]
        self.assertEqual(pending["author_login"], "another-author")
        self.assertNotIn("UNTRUSTED PRIVATE BODY", json.dumps(overview))
        self.assertEqual(self.request("/v1/issue-scout/issues/example/1/decision", RUNNER, {
            "expected_fingerprint": pending["fingerprint"], "decision": "approved"})[0], 403)
        self.assertEqual(self.decide("approved")[0], 200)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.source()["decided_by"], "owner")
        self.submit(self.claim(), [other])
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.source()["state"], "imported")
        with read_transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT decision, decided_by FROM issue_scout_decisions").fetchone()[:],
                             ("approved", "owner"))

    def test_decline_is_durable_across_edits_reopening_and_restart(self):
        other = self.issue(author_login="another-author", author_id=99)
        self.submit(self.claim(), [other])
        self.assertEqual(self.decide("declined")[0], 200)
        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        self.due()
        self.submit(self.claim(), [{**other, "body": "Changed!"}])
        self.assertEqual(self.items(), [])
        self.assertEqual(self.source()["decision"], "declined")
        self.assertEqual(self.source()["state"], "skipped")

    def test_approval_is_revision_bound_and_closed_issues_are_not_imported(self):
        other = self.issue(author_login="another-author", author_id=99)
        self.submit(self.claim(), [other])
        self.decide("approved")
        self.submit(self.claim(), [{**other, "body": "Changed after approval"}])
        self.assertEqual(self.source()["state"], "needs_owner")
        self.assertIsNone(self.source()["decision"])
        self.decide("approved")
        self.submit(self.claim(), [{**other, "body": "Changed after approval", "state": "closed"}])
        self.assertEqual(self.source()["state"], "skipped")
        self.assertEqual(self.items(), [])

    def test_changed_closed_and_reopened_imports_never_create_an_execution(self):
        self.submit(self.claim(), [self.issue()])
        item = self.items()[0]
        for state in ("closed", "open"):
            self.due()
            self.submit(self.claim(), [self.issue(body="changed", state=state)])
        self.assertEqual(self.items(), [item])
        with read_transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM executions").fetchone()[0], 1)

    def test_historical_manual_context_and_completed_goal_imports_are_reconciled(self):
        for number, payload in (
                (5, {"goal": "Already delivered https://github.com/Example/Project/issues/5."}),
                (6, {"goal": "Existing manual work", "context_links": ["https://github.com/example/project/issues/6"]})):
            self.request("/v1/items", OWNER, {"repository_id": "example", **payload})
        with write_transaction(self.path) as db:
            db.execute("""UPDATE executions SET state_json=json_set(
                state_json, '$.status', 'completed', '$.final_output', json('{"reference":"fixture"}'))""")
        self.submit(self.claim(), [self.issue(5), self.issue(6), self.issue(50)])
        self.assertEqual(len(self.items()), 3)
        self.assertEqual(self.source(5)["state"], "imported")
        self.assertEqual(self.source(6)["state"], "imported")
        self.assertNotEqual(self.source(5)["item_id"], self.source(50)["item_id"])

    def test_issue_body_references_are_not_mistaken_for_imported_sources(self):
        self.submit(self.claim(), [self.issue(body="See https://github.com/example/project/issues/2"), self.issue(2)])
        self.assertEqual(len(self.items()), 2)
        self.assertNotEqual(self.source(1)["item_id"], self.source(2)["item_id"])

    def test_stable_manual_receipt_survives_edited_context_without_duplicate_intake(self):
        self.request("/v1/items", OWNER, {"repository_id": "example", "goal": "Manual import"},
                     {"Idempotency-Key": github_issue_key("example", "example/project", 1)})
        self.submit(self.claim(), [self.issue()])
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.source()["state"], "imported")

    def test_single_claim_page_replay_and_captain_handoff_preserve_cursor(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: self.claim(), range(4)))
        first, = [claim for claim in claims if claim]
        self.assertIsNone(self.claim())
        result = self.submit(first, [self.issue()], has_more=True)
        self.assertEqual(self.submit(first, [self.issue()], has_more=True), result)
        second = self.claim()
        self.assertEqual((second["id"], second["page"]), (first["id"], 2))
        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        successor = self.register_captain("successor")
        self.assertIsNone(self.claim(successor))
        self.due()
        resumed = self.claim(successor)
        self.assertEqual((resumed["id"], resumed["page"]), (first["id"], 2))
        self.assertNotEqual(second["claim_id"], resumed["claim_id"])
        with self.assertRaisesRegex(ValueError, "409"):
            self.submit(second, [self.issue(2)])
        self.submit(resumed, [self.issue(2)])
        self.assertEqual(len(self.items()), 2)

    def test_failure_retains_page_retries_and_redacts_provider_details(self):
        first = self.claim()
        self.api(f"/v1/issue-scout/passes/{first['id']}/failure",
                 body={"claim_id": first["claim_id"], "kind": "github"})
        failed = self.get("/v1/issue-scout", RUNNER)[1]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["last_pass"]["failures"], 1)
        self.assertIsNone(self.claim())
        self.due()
        retried = self.claim()
        self.assertEqual((retried["id"], retried["page"]), (first["id"], 1))
        self.submit(retried, [self.issue()])
        self.assertEqual(len(self.items()), 1)

    def test_configuration_scope_identity_and_generation_fence(self):
        code, _ = self.request("/v1/issue-scout/settings", RUNNER, {
            "expected_revision": 1, "author_login": "configured-owner", "author_id": 42,
            "repositories": ["example"]}, method="PUT")
        self.assertEqual(code, 403)
        first = self.claim()
        self.configure(enabled=False)
        with self.assertRaisesRegex(ValueError, "409"):
            self.submit(first, [self.issue()])
        self.assertIsNone(self.claim())
        self.configure()
        first = self.claim()
        body = {"claim_id": first["claim_id"], "repository_id": "example", "page": 1,
                "verified_login": "configured-owner", "verified_id": 99,
                "issues": [self.issue()], "has_more": False}
        self.assertEqual(self.request(f"/v1/issue-scout/passes/{first['id']}/page", RUNNER, body)[0], 409)
        self.assertEqual(self.items(), [])

    def test_local_github_client_verifies_identity_filters_prs_and_recovers_api_outage(self):
        calls = []

        def transport(req, timeout):
            calls.append(req.full_url)
            if req.full_url.endswith("/user"):
                return HTTPResponse(200, {}, b'{"id":42,"login":"configured-owner"}')
            raw = self.issue()
            return HTTPResponse(200, {}, json.dumps([
                {"pull_request": {}, "number": 2},
                {"number": 1, "html_url": "https://github.com/example/project/issues/1", "state": "open",
                 "title": raw["title"], "body": raw["body"], "updated_at": raw["updated_at"],
                 "user": {"id": 42, "login": "configured-owner"}},
                {"number": 3, "html_url": "https://github.com/example/project/issues/3", "state": "open",
                 "title": "Bot request", "body": "", "updated_at": raw["updated_at"],
                 "user": {"id": 99, "login": "dependabot[bot]"}}]).encode())

        def client(owner, name, *, token):
            return GitHubClient(owner, name, token=token, transport=transport)

        failed_post = True

        def unreliable_api(route, **options):
            nonlocal failed_post
            result = self.api(route, **options)
            if route.endswith("/page") and failed_post:
                failed_post = False
                raise ValueError("API response lost")
            return result

        with (patch.dict(os.environ, PHALANX_GITHUB_TOKEN="not-a-real-secret"),
              patch("phalanx.issue_scout_client.GitHubClient", side_effect=client)):
            result = scout(unreliable_api, self.captain)
            self.assertEqual(result["observation_error"], "api")
            self.assertEqual(result["counts"]["imported"], 1)
            self.assertEqual(len(self.items()), 1)
            self.due()
            scout(self.api, self.captain)
            self.assertEqual(len(self.items()), 1)
        self.assertTrue(calls[0].endswith("/user"))
        self.assertEqual(self.source()["state"], "imported")
        self.assertEqual(self.source(3)["state"], "needs_owner")

    def test_client_missing_credentials_github_failure_and_identity_mismatch_are_durable(self):
        with patch.dict(os.environ, PHALANX_GITHUB_TOKEN=""):
            result = scout(self.api, self.captain)
        self.assertEqual(result["last_pass"]["failure_kind"], "credential")
        self.due()
        with (patch.dict(os.environ, PHALANX_GITHUB_TOKEN="not-a-real-secret"),
              patch("phalanx.issue_scout_client.GitHubClient") as factory):
            factory.return_value.authenticated_user.return_value = {"id": 99, "login": "configured-owner"}
            result = scout(self.api, self.captain)
            self.assertEqual(result["last_pass"]["failure_kind"], "identity")
            factory.return_value.scout_issues_page.assert_not_called()
        self.assertEqual(self.items(), [])

    def test_api_unavailable_is_explicit_and_does_not_invalidate_the_captain(self):
        def unavailable(route, **options):
            raise OSError("Private host URL must not be echoed")
        result = scout(unavailable, self.captain)
        self.assertEqual(result["observation_error"], "api")
        self.assertNotIn("Private host", json.dumps(result))
        self.assertIn("do not replace", result["resume_trigger"])
        self.assertEqual(self.get(f"/v1/captains/{self.captain}", RUNNER)[1]["state"], "active")

    def test_github_rate_limit_is_recorded_and_retains_provider_backoff(self):
        def client(owner, name, *, token):
            return GitHubClient(owner, name, token=token, transport=lambda req, timeout:
                                HTTPResponse(429, {"Retry-After": "3600"}, b"PRIVATE PROVIDER ERROR"))
        with (patch.dict(os.environ, PHALANX_GITHUB_TOKEN="not-a-real-secret"),
              patch("phalanx.issue_scout_client.GitHubClient", side_effect=client)):
            result = scout(self.api, self.captain)
        latest = result["last_pass"]
        self.assertEqual(latest["failure_status"], 429)
        self.assertEqual(latest["failure_kind"], "github")
        self.assertNotIn("PRIVATE PROVIDER ERROR", json.dumps(result))
        self.assertIsNone(self.claim())
        self.assertEqual(latest["page"], 1)

    def test_local_needs_you_decision_is_same_origin_and_no_queue_receipt(self):
        self.stop_server(self.server)
        self.server = self.start_server(self.path, PHALANX_LOCAL_BOARD="1")
        self.submit(self.claim(), [self.issue(author_login="another-author", author_id=99)])
        payload = {"expected_fingerprint": self.source()["fingerprint"], "decision": "approved"}
        path = "/operator/api/issue-scout/example/1/decision"
        self.assertEqual(self.request(path, body=payload)[0], 403)
        self.assertEqual(self.request(path, body=payload, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request(path, body=payload, headers={"Origin": self.url})[0], 200)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.source()["state"], "approved")


if __name__ == "__main__":
    unittest.main()
