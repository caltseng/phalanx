from dataclasses import replace
import json
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from phalanx import intake
from phalanx.draft_stage import DevelopmentCheckpoint, DraftAssignment, DraftStageRunner
from phalanx.git_publication import PublicationResult
from phalanx.github import GitHubClient
from phalanx.intake_models import Intake
from phalanx.store import migrate, write_transaction
from phalanx.workspaces import _item_branch

from test_bootstrap import OWNER, RUNNER, ServerFixture
from test_github import pull, response


CANDIDATE = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
REMOTE = "git@github.com:Owner/Repo.git"


class GitFixture:
    expected_remote = ("github", "owner/repo")

    def __init__(self, checkout, remote, item_id):
        assert remote == REMOTE
        self.ref = f"refs/heads/{_item_branch(item_id)}"
        self.head = None
        self.base = BASE
        self.pushes = []

    def verify_candidate(self, candidate, *, base, base_sha):
        assert (candidate, base, base_sha) == (CANDIDATE, "main", BASE)

    def read_branch(self, branch, *, authorize):
        authorize()
        assert branch == "main"
        return self.base

    def read_head(self, *, authorize):
        authorize()
        return self.head

    def publish(self, candidate, *, expected, authorize, rewrite=False):
        authorize()
        assert self.head == expected and rewrite
        self.pushes.append((candidate, expected))
        self.head = candidate
        return PublicationResult("success", "verified fixture push")


class DraftStageTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.db = self.directory / "queue.sqlite3"
        migrate(self.db)
        with write_transaction(self.db) as db:
            intake.register(db, "repo", REMOTE)
            item = intake.create(db, Intake(repository_id="repo", goal="Fix tests",
                                            target="ready_for_check_in", target_branch="main"))
        self.start_server(self.db)
        self.item = item["id"]
        self.execution = item["execution"]["id"]
        self.path = f"/items/{self.item}/executions/{self.execution}"
        self.branch = _item_branch(self.item)
        self.git = GitFixture(self.directory, REMOTE, self.item)
        self.github = GitHubClient("owner", "repo", token="fixture", transport=self.provider)
        self.calls = []
        self.remote_calls = []
        self.posts = []
        self.pull = None
        self.drop_response = False
        self.lose_create = False
        self.fail_lookups = False
        self.on_create = None
        self.revoke_during_lookup = False
        development = self.claim("development")
        self.call(f"{self.path}/result", {
            "attempt_id": development.attempt_id, "expected_version": development.state_version,
            "stage": "development", "outcome": "succeeded", "candidate": CANDIDATE,
        }, lease=development.lease)
        self.assignment = self.claim("pr_creation")
        self.checkpoint = DevelopmentCheckpoint(
            self.item, self.execution, self.directory, CANDIDATE, "main", BASE,
        )

    def claim(self, stage):
        lease = token_urlsafe(32)
        result = self.call("/dispatch/claim", {
            "runner_id": "local", "repository_ids": ["repo"],
        }, lease=lease)
        self.assertEqual(result["execution"]["state"]["stage"], stage)
        return DraftAssignment(self.item, self.execution, result["runner_id"],
                               result["attempt_id"], result["execution"]["version"], lease)

    def call(self, path, body, *, lease, key=None):
        self.calls.append((path, body, key))
        headers = {"Phalanx-Lease": lease}
        if key:
            headers["Idempotency-Key"] = key
        status, result = self.request("/v1" + path, RUNNER, body, headers)
        self.assertEqual(status, 200, result)
        if (path.endswith("/pr-observation") or path.endswith("/pr-publication-blocker")) and self.drop_response:
            self.drop_response = False
            raise TimeoutError("lost acknowledgement")
        return result

    def provider(self, request, _timeout):
        self.remote_calls.append(request.full_url)
        if self.revoke_during_lookup:
            self.revoke_during_lookup = False
            with write_transaction(self.db) as db:
                db.execute("UPDATE attempts SET lease_expires_at=0 WHERE id=?",
                           (self.assignment.attempt_id,))
        if request.method == "POST":
            self.posts.append(json.loads(request.data))
            self.pull = pull(head=self.branch)
            self.pull["head"]["repo"]["full_name"] = "Owner/Repo"
            if self.on_create:
                self.on_create()
            if self.lose_create:
                raise TimeoutError("lost create response")
            return response(self.pull, status=201)
        if self.fail_lookups:
            raise TimeoutError("lookup unavailable")
        if urlsplit(request.full_url).path.endswith("/pulls"):
            return response([self.pull] if self.pull else [])
        return response(self.pull)

    def run_stage(self, assignment=None, checkpoint=None):
        with patch("phalanx.draft_stage.GitPublication", return_value=self.git):
            return DraftStageRunner(self.directory, self, self.github).run(
                assignment or self.assignment, checkpoint or self.checkpoint,
                default_base="fallback", title="Fix tests", body="Description",
            )

    def test_claim_endpoint_checks_exact_runner_attempt_version_and_lease(self):
        body = {
            "attempt_id": self.assignment.attempt_id, "expected_version": self.assignment.state_version,
            "stage": "pr_creation", "runner_id": "local",
        }
        path = f"/v1{self.path}/pr-publication-claim"
        headers = {"Phalanx-Lease": self.assignment.lease}
        self.assertEqual(self.request(path, OWNER, body, headers)[0], 403)
        self.assertEqual(self.request(path, RUNNER, body)[0], 422)
        self.assertEqual(self.request(path, RUNNER, body, headers), (200, {
            "current": True, "candidate": CANDIDATE, "repository": "owner/repo",
            "remote": REMOTE, "head_ref": self.branch, "target_branch": "main",
        }))
        for field, value in (("runner_id", "other"), ("attempt_id", "unknown"),
                             ("expected_version", self.assignment.state_version - 1)):
            with self.subTest(field=field):
                self.assertEqual(self.request(path, RUNNER, dict(body, **{field: value}), headers),
                                 (200, {"current": False}))
        self.assertEqual(self.request(path, RUNNER, body, {"Phalanx-Lease": token_urlsafe(32)}),
                         (200, {"current": False}))

    def test_unknown_and_obsolete_claims_do_not_contact_providers(self):
        for assignment in (
            replace(self.assignment, attempt_id="unknown"),
            replace(self.assignment, state_version=self.assignment.state_version - 1),
            replace(self.assignment, execution_id="obsolete"),
        ):
            checkpoint = replace(self.checkpoint, execution_id=assignment.execution_id)
            self.assertEqual(self.run_stage(assignment, checkpoint).publication.status, "claim_lost")
        self.assertFalse(self.remote_calls or self.git.pushes)

    def test_revoked_claim_during_provider_read_stops_before_writes(self):
        self.revoke_during_lookup = True
        result = self.run_stage()
        self.assertEqual(result.publication.status, "claim_lost")
        self.assertFalse(self.posts or self.git.pushes)

    def test_preserves_development_base_and_blocks_other_refs_and_candidates(self):
        for changes in ({"base_ref": "release"}, {"candidate": OTHER}):
            result = self.run_stage(checkpoint=replace(self.checkpoint, **changes))
            self.assertEqual(result.publication.status, "conflict")
        self.assertFalse(self.remote_calls)
        self.git.base = OTHER
        self.assertEqual(self.run_stage().publication.status, "base_changed")
        self.assertFalse(self.posts or self.git.pushes)

    def test_restarts_after_lost_submission_response_with_identical_payload_and_key(self):
        self.drop_response = True
        with self.assertRaises(TimeoutError):
            self.run_stage()
        self.assertEqual(self.git.pushes, [(CANDIDATE, None)])
        self.assertEqual(len(self.posts), 1)
        previous_count = len(self.remote_calls)
        path, payload, key = self.calls[-1]
        self.assertEqual(payload["expected"]["base_sha"], BASE)
        self.assertEqual(payload["pull"]["head_sha"], CANDIDATE)
        self.assertEqual(payload["pull"]["head_ref"], self.branch)

        result = self.run_stage()
        self.assertEqual(result.execution["state"]["stage"], "self_review")
        self.assertEqual(result.execution["state"]["primary_pr"], "https://github.com/owner/repo/pull/7")
        self.assertEqual(self.calls[-1], (path, payload, key))
        self.assertEqual(len(self.remote_calls), previous_count)
        self.assertEqual(len(self.posts), 1)
        status, events = self.get(f"/v1{self.path}/timeline", RUNNER)
        self.assertEqual(status, 200)
        self.assertEqual(sum(event["details"].get("pr_url") is not None for event in events), 1)

    def state(self):
        status, item = self.get(f"/v1/items/{self.item}", RUNNER)
        self.assertEqual(status, 200)
        return item["execution"]["state"]

    def test_lost_create_response_retries_same_intent_without_development(self):
        self.lose_create, self.fail_lookups = True, True
        before = len(self.calls)
        self.assertEqual(self.run_stage().publication.status, "pending")
        self.assertEqual((self.state()["stage"], self.state()["status"]), ("pr_creation", "running"))
        self.lose_create, self.fail_lookups = False, False
        result = self.run_stage()
        self.assertEqual(result.execution["state"]["stage"], "self_review")
        self.assertEqual((len(self.posts), self.git.pushes), (1, [(CANDIDATE, None)]))
        self.assertFalse(any(path == "/dispatch/claim" for path, _, _ in self.calls[before:]))

    def test_base_move_after_lost_create_still_records_the_draft(self):
        def land_then_black_out():
            self.git.base = OTHER
            self.fail_lookups = True

        self.lose_create, self.on_create = True, land_then_black_out
        self.assertEqual(self.run_stage().publication.status, "pending")
        self.lose_create, self.on_create, self.fail_lookups = False, None, False
        result = self.run_stage()
        self.assertEqual(result.publication.status, "verified")
        self.assertEqual((result.execution["state"]["stage"], result.execution["state"]["primary_pr"]),
                         ("self_review", "https://github.com/owner/repo/pull/7"))
        self.assertEqual(len(self.posts), 1)

    def test_base_drift_routes_to_development_and_replays_lost_response(self):
        self.git.base = OTHER
        self.drop_response = True
        with self.assertRaises(TimeoutError):
            self.run_stage()
        path, payload, key = self.calls[-1]
        self.assertTrue(path.endswith("/pr-publication-blocker"))
        self.assertEqual(payload["observed_base_sha"], OTHER)
        remote = len(self.remote_calls)
        result = self.run_stage()
        self.assertEqual(self.calls[-1], (path, payload, key))
        self.assertEqual(len(self.remote_calls), remote)
        self.assertEqual((result.execution["state"]["stage"], result.execution["state"]["status"]),
                         ("development", "queued"))
        self.assertFalse(self.posts or self.git.pushes)

    def test_outside_pr_on_item_branch_routes_to_reconciliation(self):
        self.pull = pull(head=self.branch, draft=False)
        self.pull["head"]["repo"]["full_name"] = "Owner/Repo"
        result = self.run_stage()
        self.assertEqual(result.publication.status, "conflict")
        state = result.execution["state"]
        self.assertEqual((state["stage"], state["primary_pr"], state["publication_conflict"]),
                         ("pr_reconciliation", "https://github.com/owner/repo/pull/7", True))
        self.assertFalse(self.posts or self.git.pushes)

    def test_blocker_endpoint_rejects_matching_drafts_and_unmoved_bases(self):
        body = {
            "attempt_id": self.assignment.attempt_id, "expected_version": self.assignment.state_version,
            "stage": "pr_creation", "reason": "Blocked",
            "expected": {"head_ref": self.branch, "base_ref": "main", "base_sha": BASE},
        }
        matching = {
            "number": 7, "head_repository": "owner/repo", "base_repository": "owner/repo",
            "head_ref": self.branch, "base_ref": "main", "head_sha": CANDIDATE, "base_sha": BASE,
            "state": "open", "draft": True, "merged": False,
        }
        path = f"/v1{self.path}/pr-publication-blocker"
        headers = {"Phalanx-Lease": self.assignment.lease}
        for extra, status in (({}, 422), ({"observed_base_sha": BASE, "pull": matching}, 422),
                              ({"observed_base_sha": BASE}, 409), ({"pull": matching}, 409)):
            with self.subTest(extra=extra):
                self.assertEqual(self.request(path, RUNNER, {**body, **extra}, headers)[0], status)
        self.assertEqual((self.state()["stage"], self.state()["status"]), ("pr_creation", "running"))
