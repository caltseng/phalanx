from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
import unittest

from phalanx import intake
from phalanx.intake_models import Intake
from phalanx.store import migrate, write_transaction
from phalanx.workspaces import _item_branch

from test_bootstrap import OWNER, RUNNER, ServerFixture


HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40


class PullObservationTest(ServerFixture, unittest.TestCase):
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
        self.head_ref = _item_branch(item["id"])
        self.item = f"/v1/items/{item['id']}"
        self.execution = f"{self.item}/executions/{item['execution']['id']}"
        self.observe = f"{self.execution}/pr-observation"
        self.develop()

    def claim(self, stage):
        self.lease = {"Phalanx-Lease": token_urlsafe(32)}
        status, assignment = self.request("/v1/dispatch/claim", RUNNER, {
            "runner_id": "local", "repository_ids": ["repo"],
        }, self.lease)
        self.assertEqual(status, 200)
        self.assertEqual(assignment["status"], "claimed")
        self.assertEqual(assignment["execution"]["state"]["stage"], stage)
        return {
            "stage": stage, "attempt_id": assignment["attempt_id"],
            "expected_version": assignment["execution"]["version"],
        }

    def result(self, claim, **fields):
        return self.request(f"{self.execution}/result", RUNNER,
                            dict(claim, outcome="succeeded", **fields), self.lease)

    def develop(self):
        self.assertEqual(self.result(self.claim("development"), candidate=HEAD)[0], 200)

    def observation(self, claim, *, draft=False, **fields):
        expected = {"head_ref": self.head_ref, "base_ref": "main", "base_sha": BASE}
        return dict(claim, expected=expected, pull={
            **expected, "number": 7, "head_sha": HEAD, "draft": draft,
            "head_repository": "Example/Repo", "base_repository": "example/repo",
            "state": "open", "merged": False,
        }, **fields)

    def gates(self, phase, *, outcome="passed"):
        requirement = {"name": "build", "kind": "check_run", "app_id": 123}
        return {"phase": phase, "number": 7, "head_sha": HEAD, "base_sha": BASE,
                "required": [requirement],
                "observations": [dict(requirement, tested_sha=HEAD, outcome=outcome)]}

    def submit(self, body, **headers):
        return self.request(self.observe, RUNNER, body, {**self.lease, **headers})

    def publish(self):
        body = self.observation(self.claim("pr_creation"), draft=True)
        self.assertEqual(self.submit(body)[0], 200)
        self.assertEqual(self.result(self.claim("self_review"), reviewed_candidate=HEAD)[0], 200)
        self.assertEqual(self.result(self.claim("pre_pr_validation"), prevalidated_candidate=HEAD)[0], 200)

    def test_publication_requires_authority_identity_and_exact_candidate(self):
        claim = self.claim("pr_creation")
        body = self.observation(claim, draft=True, duplicate_numbers=[8])
        self.assertEqual(self.request(self.observe, OWNER, body, self.lease)[0], 403)
        self.assertEqual(self.request(self.observe, RUNNER, body)[0], 422)
        self.assertEqual(self.submit(body, **{"Phalanx-Lease": "y" * 32})[0], 409)
        self.assertEqual(self.submit(dict(body, expected_version=claim["expected_version"] - 1))[0], 409)
        with write_transaction(self.path) as db:
            attempt_id = claim["attempt_id"]
            expiry, = db.execute("SELECT lease_expires_at FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            db.execute("UPDATE attempts SET lease_expires_at=0 WHERE id=?", (attempt_id,))
        self.assertEqual(self.submit(body)[0], 409)
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET lease_expires_at=? WHERE id=?", (expiry, attempt_id))
        self.assertEqual(self.result(claim)[0], 409)
        self.assertEqual(self.result(claim, pull=body["pull"])[0], 422)

        for field, value in (("head_repository", "fork/repo"), ("base_repository", "other/repo"),
                             ("head_sha", OTHER), ("base_sha", OTHER), ("base_ref", "dev"),
                             ("draft", False), ("state", "closed"), ("merged", True)):
            with self.subTest(field=field):
                self.assertEqual(self.submit(dict(body, pull=dict(body["pull"], **{field: value})))[0], 409)
        different_branch = "codex/unrelated-item"
        self.assertEqual(self.submit(dict(
            body, expected=dict(body["expected"], head_ref=different_branch),
            pull=dict(body["pull"], head_ref=different_branch),
        ))[0], 409)
        headers = {"Idempotency-Key": "publication"}
        status, updated = self.submit(body, **headers)
        self.assertEqual(status, 200)
        self.assertEqual(updated["state"]["primary_pr"], "https://github.com/example/repo/pull/7")
        self.assertEqual(updated["state"]["stage"], "self_review")
        self.assertEqual(self.submit(body, **headers), (status, updated))
        self.assertEqual(self.submit(body)[0], 409)
        _, timeline = self.request(f"{self.execution}/timeline", OWNER)
        self.assertEqual(timeline[-1]["attempt_id"], claim["attempt_id"])
        self.assertEqual(timeline[-1]["details"]["duplicate_numbers"], [8])

    def test_promotion_and_validation_require_the_primary_pr_and_current_gates(self):
        self.publish()
        claim = self.claim("pr_promotion")
        body = self.observation(claim, gates=self.gates("draft"))
        self.assertEqual(self.result(claim)[0], 409)
        self.assertEqual(self.submit(dict(body, pull=dict(body["pull"], number=8)))[0], 409)
        self.assertEqual(self.submit(dict(body, pull=dict(body["pull"], draft=True)))[0], 409)
        for field, value in (("number", 8), ("head_sha", OTHER), ("base_sha", OTHER), ("phase", "ready"),
                             ("observations", []), ("required", None)):
            with self.subTest(field=field):
                response, _ = self.submit(dict(body, gates=dict(body["gates"], **{field: value})))
                self.assertEqual(response, 422 if value is None else 409)
        self.assertEqual(self.submit(body)[0], 200)
        body = self.observation(self.claim("pr_validation"), gates=self.gates("ready"))
        self.assertEqual(self.submit(dict(body, gates=self.gates("ready", outcome="pending")))[0], 409)
        status, updated = self.submit(body)
        self.assertEqual(status, 200)
        self.assertEqual(updated["state"]["stage"], "adversarial_review")
        self.assertIsNone(updated["state"]["final_output"])
        self.assertEqual(self.result(self.claim("adversarial_review"))[0], 409)

    def test_failed_ready_check_queues_reconciliation(self):
        self.publish()
        no_draft_requirements = dict(self.gates("draft"), required=[], observations=[])
        body = self.observation(self.claim("pr_promotion"), gates=no_draft_requirements)
        self.assertEqual(self.submit(body)[0], 200)
        body = self.observation(self.claim("pr_validation"), gates=self.gates("ready", outcome="failed"))
        status, updated = self.submit(body)
        self.assertEqual(status, 200)
        self.assertEqual(updated["state"]["stage"], "pr_reconciliation")
        self.assertIsNone(updated["state"]["final_output"])

    def test_cancelling_attempt_cannot_record_publication(self):
        claim = self.claim("pr_creation")
        status, updated = self.request(f"{self.execution}/cancel", OWNER, {
            "expected_version": claim["expected_version"], "reason": "Stop work",
        })
        self.assertEqual(status, 200)
        claim["expected_version"] = updated["version"]
        self.assertEqual(self.submit(self.observation(claim, draft=True))[0], 409)
        status, updated = self.request(f"{self.execution}/stop-confirmed", RUNNER, claim, self.lease)
        self.assertEqual(status, 200)
        self.assertEqual(updated["state"]["status"], "cancelled")
        self.assertIsNone(updated["state"]["primary_pr"])
