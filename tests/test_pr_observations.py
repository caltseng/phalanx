from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from time import time
import unittest

from phalanx import intake
from phalanx.intake_models import Intake
from phalanx.store import migrate, write_transaction
from phalanx.workspaces import _item_branch

from test_bootstrap import OWNER, RUNNER, ServerFixture


HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40


class PullObservationFixture(ServerFixture):
    target = "ready_for_check_in"

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "git@github.com:Example/Repo.git")
            item = intake.create(db, Intake(repository_id="repo", goal="Update tests",
                                            target=self.target, target_branch="main"))
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

    def observation(self, claim, *, draft=False, head=HEAD, **fields):
        expected = {"head_ref": self.head_ref, "base_ref": "main", "base_sha": BASE}
        return dict(claim, expected=expected, pull={
            **expected, "number": 7, "head_sha": head, "draft": draft,
            "head_repository": "Example/Repo", "base_repository": "example/repo",
            "state": "open", "merged": False,
        }, **fields)

    def gates(self, phase, *, outcome="passed", head=HEAD):
        requirement = {"name": "build", "kind": "check_run", "app_id": 123}
        return {"phase": phase, "number": 7, "head_sha": head, "base_sha": BASE,
                "required": [requirement],
                "observations": [dict(requirement, tested_sha=head, outcome=outcome)]}

    def submit(self, body, **headers):
        return self.request(self.observe, RUNNER, body, {**self.lease, **headers})

    def publish(self):
        body = self.observation(self.claim("pr_creation"), draft=True)
        self.assertEqual(self.submit(body)[0], 200)
        self.assertEqual(self.result(self.claim("self_review"), reviewed_candidate=HEAD)[0], 200)
        self.assertEqual(self.result(self.claim("pre_pr_validation"), prevalidated_candidate=HEAD)[0], 200)

    def start_review(self):
        self.publish()
        self.assertEqual(self.submit(self.observation(
            self.claim("pr_promotion"), gates=self.gates("draft"),
        ))[0], 200)
        self.assertEqual(self.submit(self.observation(
            self.claim("pr_validation"), gates=self.gates("ready"),
        ))[0], 200)
        self.assertEqual(self.submit(self.reconciliation(self.claim("pr_reconciliation")))[0], 200)

    def review(self, claim):
        return self.observation(claim, review={
            "reviewer": "independent-review-chat", "summary": "Reviewed the diff",
            "findings": ["P2: Verify the edge case is covered by the integration test"],
        })

    def reconciliation(self, claim, *, head=HEAD, outcome="passed", **fields):
        return self.observation(claim, head=head, gates=self.gates("ready", head=head, outcome=outcome), **fields)

    def at_base(self, body, base):
        updated = dict(body, expected=dict(body["expected"], base_sha=base),
                       pull=dict(body["pull"], base_sha=base))
        if "gates" in body:
            updated["gates"] = dict(body["gates"], base_sha=base)
        return updated


class PullObservationTest(PullObservationFixture, unittest.TestCase):
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
        different_branch = "phalanx/unrelated-item"
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

    def test_candidate_local_evidence_is_persisted_for_the_current_stage_and_candidate(self):
        self.assertEqual(self.submit(self.observation(self.claim("pr_creation"), draft=True))[0], 200)
        review = self.claim("self_review")
        question = {"prompt": "Which scope?", "options": ["Current or expanded"],
                    "handoff": "Reviewed the current scope"}
        status, waiting = self.request(f"{self.execution}/result", RUNNER, dict(
            review, outcome="needs_input", candidate=HEAD, review_notes=question["prompt"],
            question=question,
        ), self.lease)
        self.assertEqual(status, 200)
        self.assertEqual(waiting["state"]["wait"]["trigger"], "owner reply")
        _, inbox = self.request("/v1/owner/inbox", OWNER)
        self.assertEqual(inbox[0]["question"]["handoff"], question["handoff"])
        self.assertEqual(self.request(f"{self.execution}/reply", OWNER, {
            "expected_version": waiting["version"], "answer": "Current scope",
        }, {"Idempotency-Key": "self-review-reply"})[0], 200)
        review = self.claim("self_review")
        notes = {"review_notes": "Reviewed the diff and acceptance criteria"}
        self.assertEqual(self.result(review, reviewed_candidate=OTHER, **notes)[0], 409)
        self.assertEqual(self.result(review, reviewed_candidate=HEAD, **notes)[0], 200)
        validate = self.claim("pre_pr_validation")
        status, waiting = self.request(f"{self.execution}/result", RUNNER, dict(
            validate, outcome="needs_input", candidate=HEAD, local_checks=[],
            question={"prompt": "Which local policy applies?", "handoff": "Policy is unverified"},
        ), self.lease)
        self.assertEqual(status, 200)
        self.assertEqual(waiting["state"]["wait"]["trigger"], "owner reply")
        self.assertEqual(self.request(f"{self.execution}/reply", OWNER, {
            "expected_version": waiting["version"], "answer": "Use section 2",
        }, {"Idempotency-Key": "local-policy-reply"})[0], 200)
        validate = self.claim("pre_pr_validation")
        bypass = {"name": "platform", "outcome": "skipped", "reason": "Platform unavailable",
                  "authorization": "Repository policy section 2"}
        self.assertEqual(self.result(validate, prevalidated_candidate=HEAD, **notes)[0], 409)
        self.assertEqual(self.result(validate, prevalidated_candidate=HEAD,
                                     local_checks=[dict(bypass, authorization=None)])[0], 409)
        self.assertEqual(self.result(validate, prevalidated_candidate=HEAD, local_checks=[bypass])[0],
                         200)
        _, timeline = self.request(f"{self.execution}/timeline", OWNER)
        self.assertIn(notes["review_notes"],
                      [entry["details"].get("review_notes") for entry in timeline])
        self.assertEqual(timeline[-1]["details"]["local_checks"], [bypass])
        self.assertEqual(timeline[-1]["details"]["prevalidated_candidate"], HEAD)

    def test_failed_local_check_returns_to_development_with_findings(self):
        self.assertEqual(self.submit(self.observation(self.claim("pr_creation"), draft=True))[0], 200)
        self.assertEqual(self.result(self.claim("self_review"), reviewed_candidate=HEAD)[0], 200)
        status, updated = self.request(f"{self.execution}/result", RUNNER, dict(
            self.claim("pre_pr_validation"), outcome="changes_required", candidate=HEAD,
            local_checks=[{"name": "lint", "outcome": "passed"},
                          {"name": "unit tests", "outcome": "failed", "reason": "Exited with status 1"}],
        ), self.lease)
        self.assertEqual(status, 200, updated)
        self.assertEqual((updated["state"]["stage"], updated["state"]["findings"]),
                         ("development", "Local check unit tests failed: Exited with status 1"))

    def test_unchanged_development_reruns_validation_until_the_repeat_limit(self):
        self.assertEqual(self.submit(self.observation(self.claim("pr_creation"), draft=True))[0], 200)
        failed = [{"name": "unit tests", "outcome": "failed", "reason": "Exited with status 1"}]
        for attempt in range(intake.REPEAT_LIMIT + 1):
            self.assertEqual(self.result(self.claim("self_review"), reviewed_candidate=HEAD)[0], 200)
            status, updated = self.request(f"{self.execution}/result", RUNNER, dict(
                self.claim("pre_pr_validation"), outcome="changes_required", candidate=HEAD,
                local_checks=failed,
            ), self.lease)
            self.assertEqual(status, 200, updated)
            if attempt < intake.REPEAT_LIMIT:
                self.assertEqual(updated["state"]["stage"], "development")
                self.develop()
        self.assertEqual((updated["state"]["stage"], updated["state"]["wait"]["trigger"]),
                         ("pre_pr_validation", "owner reply"))
        self.assertIn("same findings", updated["state"]["wait"]["reason"])

    def test_failed_draft_check_returns_to_development_with_findings(self):
        self.publish()
        status, updated = self.submit(self.observation(
            self.claim("pr_promotion"), draft=True, gates=self.gates("draft", outcome="failed"),
        ))
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["state"]["stage"], "development")
        self.assertIn("build", updated["state"]["findings"])

    def test_ready_pr_records_missing_draft_checks_as_skipped(self):
        self.publish()
        body = self.observation(self.claim("pr_promotion"), gates=dict(self.gates("draft"), observations=[]))
        status, updated = self.submit(body)
        self.assertEqual((status, updated["state"]["stage"]), (200, "pr_validation"))
        _, timeline = self.request(f"{self.execution}/timeline", OWNER)
        gate = timeline[-1]["details"]["gate_result"]
        self.assertEqual((gate["state"], gate["skipped"]), ("satisfied", ["build (check_run) from app 123"]))
        self.assertIn("first promotion", gate["skip_reason"])

    def test_promotion_and_validation_require_the_primary_pr_and_current_gates(self):
        self.publish()
        claim = self.claim("pr_promotion")
        body = self.observation(claim, gates=self.gates("draft"))
        self.assertEqual(self.result(claim)[0], 409)
        self.assertEqual(self.submit(dict(body, pull=dict(body["pull"], number=8)))[0], 409)
        self.assertEqual(self.submit(dict(body, pull=dict(body["pull"], draft=True)))[0], 409)
        for field, value in (("number", 8), ("head_sha", OTHER), ("base_sha", OTHER), ("phase", "ready"),
                             ("observations", self.gates("draft", outcome="pending")["observations"]),
                             ("required", None)):
            with self.subTest(field=field):
                response, _ = self.submit(dict(body, gates=dict(body["gates"], **{field: value})))
                self.assertEqual(response, 422 if value is None else 409)
        self.assertEqual(self.submit(body)[0], 200)
        body = self.observation(self.claim("pr_validation"), gates=self.gates("ready"))
        self.assertEqual(self.submit(dict(body, gates=self.gates("ready", outcome="pending")))[0], 409)
        status, updated = self.submit(body)
        self.assertEqual(status, 200)
        self.assertEqual(updated["state"]["stage"], "pr_reconciliation")
        self.assertIsNone(updated["state"]["final_output"])
        self.assertEqual(self.result(self.claim("pr_reconciliation"))[0], 409)

    def test_failed_ready_check_is_revised_on_the_ready_pr(self):
        self.publish()
        no_draft_requirements = dict(self.gates("draft"), required=[], observations=[])
        body = self.observation(self.claim("pr_promotion"), gates=no_draft_requirements)
        self.assertEqual(self.submit(body)[0], 200)
        body = self.observation(self.claim("pr_validation"), gates=self.gates("ready", outcome="failed"))
        status, updated = self.submit(body)
        self.assertEqual((status, updated["state"]["stage"]), (200, "pr_reconciliation"))
        status, updated = self.submit(self.reconciliation(self.claim("pr_reconciliation"), outcome="failed"))
        self.assertEqual(status, 200)
        self.assertEqual((updated["state"]["stage"], updated["state"]["status"]), ("pr_revision", "queued"))
        self.assertIn("Required checks did not pass", updated["state"]["findings"])
        self.assertIsNone(updated["state"]["final_output"])

    def test_published_validation_wait_replays_and_reclaims_exact_revision(self):
        self.publish()
        promotion = self.observation(self.claim("pr_promotion"), gates=self.gates("draft"))
        self.assertEqual(self.submit(promotion)[0], 200)
        claim = self.claim("pr_validation")
        authority = f"{self.execution}/pr-validation-claim"
        claim_body = dict(claim, runner_id="local")
        self.assertEqual(self.request(authority, OWNER, claim_body, self.lease)[0], 403)
        self.assertEqual(self.request(authority, RUNNER, dict(claim_body, runner_id="other"),
                                      self.lease), (200, {"current": False}))
        status, current = self.request(authority, RUNNER, claim_body, self.lease)
        self.assertEqual(status, 200)
        self.assertEqual(current["promotion"], promotion["pull"])

        def recheck(updated, cycle):
            with write_transaction(self.path) as db:
                due = db.execute("SELECT version FROM external_rechecks WHERE version=?",
                                 (updated["version"],)).fetchone()
                self.assertIsNotNone(due)
                db.execute("UPDATE external_rechecks SET due_at_ms=0 WHERE version=?",
                           (updated["version"],))
            status, due = self.request("/v1/runner/due-rechecks", RUNNER)
            self.assertEqual(status, 200)
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["revision"], {"head": HEAD, "base": BASE})
            self.assertEqual(due[0]["gate"], "pr_validation")
            status, queued = self.request(f"{self.execution}/recheck", RUNNER, {
                "expected_version": updated["version"],
            }, {"Idempotency-Key": f"recheck-ready-{cycle}"})
            self.assertEqual((status, queued["state"]["status"]), (200, "queued"))
            return self.claim("pr_validation")

        def wait_row(version):
            with write_transaction(self.path) as db:
                return db.execute("""SELECT due.due_at_ms, json_extract(h.details_json, '$.pending_since_ms')
                    FROM transition_history AS h JOIN external_rechecks AS due
                      ON due.execution_id=h.execution_id AND due.version=h.version
                    WHERE h.execution_id=? AND h.version=?""",
                                  (self.execution.rsplit("/", 1)[-1], version)).fetchone()

        # The runner's interval schedules each recheck; only elapsed time on one revision escalates.
        for cycle in range(3):
            body = self.observation(claim, gates=self.gates("ready", outcome="pending"),
                                    schedule_recheck=True, retry_after_seconds=7, wait_limit_seconds=60)
            if cycle == 0:
                changed_base = dict(
                    body, pull=dict(body["pull"], base_sha=OTHER),
                    expected=dict(body["expected"], base_sha=OTHER),
                )
                self.assertEqual(self.submit(changed_base)[0], 409)
                self.assertEqual(self.submit(dict(body, wait_limit_seconds=59))[0], 422)
            key = {"Idempotency-Key": f"pending-ready-{cycle}"}
            before = int(time() * 1000)
            status, updated = self.submit(body, **key)
            self.assertEqual(status, 200)
            self.assertEqual(self.submit(body, **key), (status, updated))
            if cycle == 2:
                self.assertEqual(updated["state"]["status"], "waiting_for_input")
                _, timeline = self.request(f"{self.execution}/timeline", OWNER)
                self.assertIn("stalled", timeline[-1]["details"]["question"]["prompt"])
                break
            self.assertEqual(updated["state"]["status"], "waiting_for_external")
            due_at, since = wait_row(updated["version"])
            self.assertGreaterEqual(due_at, before + 7_000)
            self.assertLess(due_at, before + 30_000)
            if cycle == 0:
                first = since
            else:
                self.assertEqual(since, first)
                with write_transaction(self.path) as db:
                    db.execute("""UPDATE transition_history SET details_json=json_set(
                        details_json, '$.pending_since_ms', ?) WHERE execution_id=? AND version=?""",
                               (first - 60_000, self.execution.rsplit("/", 1)[-1], updated["version"]))
            self.assertEqual(self.request(authority, RUNNER, dict(claim, runner_id="local"),
                                          self.lease), (200, {"current": False}))
            claim = recheck(updated, cycle)

        status, queued = self.request(f"{self.execution}/reply", OWNER, {
            "expected_version": updated["version"], "answer": "The CI provider recovered",
        }, {"Idempotency-Key": "ready-policy-reply"})
        self.assertEqual((status, queued["state"]["status"]), (200, "queued"))
        # A reply grants a fresh window on the same revision.
        claim = self.claim("pr_validation")
        pending = self.observation(claim, gates=self.gates("ready", outcome="pending"),
                                   schedule_recheck=True, wait_limit_seconds=60)
        status, updated = self.submit(pending, **{"Idempotency-Key": "pending-after-reply"})
        self.assertEqual((status, updated["state"]["status"]), (200, "waiting_for_external"))
        ready = self.observation(recheck(updated, "after-reply"), gates=self.gates("ready"),
                                 schedule_recheck=True)
        status, updated = self.submit(ready, **{"Idempotency-Key": "ready-passed"})
        self.assertEqual((status, updated["state"]["stage"]), (200, "pr_reconciliation"))

    def test_transient_validation_retry_honors_provider_delay(self):
        self.publish()
        promotion = self.observation(self.claim("pr_promotion"), gates=self.gates("draft"))
        self.assertEqual(self.submit(promotion)[0], 200)
        claim = self.claim("pr_validation")
        body = dict(claim, outcome="failed", failure_reason="GitHub returned HTTP 429",
                    retry_after_seconds=120)
        headers = {**self.lease, "Idempotency-Key": "validation-throttled"}
        before = int(time() * 1000)
        status, queued = self.request(f"{self.execution}/result", RUNNER, body, headers)
        self.assertEqual((status, queued["state"]["status"]), (200, "queued"))
        self.assertGreaterEqual(queued["retry_at_ms"], before + 120_000)
        self.assertEqual(self.request(f"{self.execution}/result", RUNNER, body, headers),
                         (status, queued))

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

    def test_ready_only_reuses_the_review_after_a_base_only_change(self):
        self.start_review()
        claim = self.claim("adversarial_review")
        review = self.review(claim)
        self.assertEqual(self.result(claim)[0], 409)
        self.assertEqual(self.submit(self.at_base(review, OTHER))[0], 409)
        self.assertEqual(self.submit(review)[0], 200)

        # The reviewed head is unchanged, so a new base needs current checks but no re-review.
        ready = self.at_base(self.reconciliation(self.claim("pr_reconciliation")), OTHER)
        self.assertEqual(self.submit(dict(ready, merge_intent={
            "operation_id": "unexpected", "authorization": "unexpected",
        }))[0], 409)
        headers = {"Idempotency-Key": "ready"}
        status, execution = self.submit(ready, **headers)
        self.assertEqual(status, 200)
        self.assertEqual(execution["state"]["status"], "completed")
        output = execution["state"]["final_output"]
        self.assertEqual(output["url"], "https://github.com/example/repo/pull/7")
        self.assertEqual(output["revision"], {"head": HEAD, "base": OTHER})
        self.assertIsNone(output["merge_commit"])
        self.assertEqual(self.submit(ready, **headers), (status, execution))

    def revise(self, head, **pull):
        body = self.observation(self.claim("pr_revision"), head=head, candidate=head)
        body["pull"].update(pull)
        return self.submit(body)

    def test_blocking_review_is_revised_and_a_lost_push_is_reconciled(self):
        self.start_review()
        review = self.review(self.claim("adversarial_review"))
        blocking = dict(review, outcome="changes_required")
        self.assertEqual(self.submit(dict(blocking, review=dict(blocking["review"], findings=[])))[0], 422)
        status, execution = self.submit(blocking)
        self.assertEqual(status, 200, execution)
        state = execution["state"]
        self.assertEqual((state["stage"], state["status"]), ("pr_revision", "queued"))
        self.assertIn("P2: Verify the edge case", state["findings"])
        claim = self.claim("pr_revision")
        self.assertEqual(self.submit(self.observation(claim, candidate=HEAD))[0], 409)
        self.assertEqual(self.submit(self.observation(claim, candidate=OTHER))[0], 409)
        lost = self.observation(claim, head="d" * 40, candidate=OTHER)
        status, execution = self.submit(lost)
        self.assertEqual(status, 200, execution)
        self.assertEqual((execution["state"]["stage"], execution["state"]["candidate"]),
                         ("pr_reconciliation", OTHER))
        self.assertIsNone(execution["state"]["findings"])

    def test_revision_escalates_new_scope_to_development(self):
        self.start_review()
        blocking = dict(self.review(self.claim("adversarial_review")), outcome="changes_required")
        self.assertEqual(self.submit(blocking)[0], 200)
        status, execution = self.request(f"{self.execution}/result", RUNNER, dict(
            self.claim("pr_revision"), outcome="changes_required", candidate=HEAD,
            review_notes="This needs a new storage design",
        ), self.lease)
        self.assertEqual(status, 200, execution)
        self.assertEqual((execution["state"]["stage"], execution["state"]["findings"]),
                         ("development", "This needs a new storage design"))

    def test_ready_pr_is_revised_through_conflict_failed_check_and_feedback(self):
        """Acceptance: the ready PR is fixed in place, never returning to draft or development."""
        self.start_review()
        self.assertEqual(self.submit(self.review(self.claim("adversarial_review")))[0], 200)
        head = HEAD
        rounds = [
            lambda claim: self.rework(claim, "merge_conflict", head=head),
            lambda claim: self.submit(self.reconciliation(claim, head=head, outcome="failed")),
            lambda claim: self.rework(claim, "feedback", head=head, feedback_ids=["PRRT_1"],
                                      findings=["@octo on app.py: Handle the empty list"]),
        ]
        for number, send in enumerate(rounds):
            status, execution = send(self.claim("pr_reconciliation"))
            self.assertEqual((status, execution["state"]["stage"]), (200, "pr_revision"), execution)
            head = f"{number + 1}" * 40
            status, execution = self.revise(head)
            self.assertEqual((status, execution["state"]["stage"]), (200, "pr_validation"), execution)
            self.assertEqual(self.submit(self.observation(
                self.claim("pr_validation"), head=head, gates=self.gates("ready", head=head)))[0], 200)
            status, execution = self.submit(self.reconciliation(self.claim("pr_reconciliation"), head=head))
            self.assertEqual(execution["state"]["stage"], "adversarial_review")
            review = self.observation(self.claim("adversarial_review"), head=head,
                                      review={"reviewer": "chat", "summary": "Reviewed the new diff", "findings": []})
            self.assertEqual(self.submit(review)[0], 200)
        status, execution = self.submit(self.reconciliation(self.claim("pr_reconciliation"), head=head))
        self.assertEqual((status, execution["state"]["status"]), (200, "completed"))
        self.assertEqual(execution["state"]["final_output"]["revision"]["head"], head)
        _, timeline = self.request(f"{self.execution}/timeline", OWNER)
        promoted = next(index for index, row in enumerate(timeline)
                        if row["details"].get("stage") == "pr_promotion")
        later = timeline[promoted + 1:]
        self.assertFalse({"development", "pr_promotion", "pr_creation"}
                         & {row["state"]["stage"] for row in later})
        self.assertFalse(any((row["details"].get("pull") or {}).get("draft") for row in later))

    def rework(self, claim, issue, **fields):
        if issue == "merge_conflict":
            fields.setdefault("base_tip", "c" * 40)
        return self.request(f"{self.execution}/pr-rework", RUNNER,
                            self.observation(claim, issue=issue, **fields), self.lease)

    def test_merge_conflict_is_revised_on_the_ready_pr(self):
        self.start_review()
        self.assertEqual(self.submit(self.review(self.claim("adversarial_review")))[0], 200)
        claim = self.claim("pr_reconciliation")
        self.assertEqual(self.rework(claim, "unexpected_head")[0], 409)
        self.assertEqual(self.rework(claim, "feedback")[0], 422)
        status, execution = self.rework(claim, "merge_conflict")
        self.assertEqual(status, 200, execution)
        state = execution["state"]
        self.assertEqual((state["stage"], state["status"]), ("pr_revision", "queued"))
        self.assertIn("integrate it into the candidate", state["findings"])
        self.assertIn(f"main at {'c' * 40}", state["findings"])

    def test_github_feedback_is_revised_on_the_ready_pr(self):
        self.start_review()
        self.assertEqual(self.submit(self.review(self.claim("adversarial_review")))[0], 200)
        status, execution = self.rework(self.claim("pr_reconciliation"), "feedback",
                                        findings=["@octo on app.py: Handle the empty list"],
                                        feedback_ids=["PRRT_1"])
        self.assertEqual(status, 200, execution)
        self.assertEqual(execution["state"]["stage"], "pr_revision")
        self.assertIn("Handle the empty list", execution["state"]["findings"])

    def test_draft_conflict_is_revised_on_the_draft_then_promoted(self):
        self.publish()
        status, execution = self.rework(self.claim("pr_promotion"), "merge_conflict", draft=True)
        self.assertEqual(status, 200, execution)
        self.assertEqual(execution["state"]["stage"], "pr_revision")
        self.assertIn(f"main at {'c' * 40}", execution["state"]["findings"])
        status, execution = self.revise(OTHER, draft=True)
        self.assertEqual(status, 200, execution)
        self.assertEqual((execution["state"]["stage"], execution["state"]["candidate"]), ("pr_promotion", OTHER))
        status, execution = self.submit(self.observation(
            self.claim("pr_promotion"), head=OTHER, gates=self.gates("draft", head=OTHER)))
        self.assertEqual((status, execution["state"]["stage"]), (200, "pr_validation"), execution)

    def test_repeated_rework_without_progress_asks_the_owner(self):
        self.publish()
        head = HEAD
        for attempt in range(3):
            status, execution = self.rework(self.claim("pr_promotion"), "merge_conflict", head=head, draft=True)
            self.assertEqual(status, 200, execution)
            if attempt == 2:
                break
            self.assertEqual(execution["state"]["stage"], "pr_revision")
            head = f"{attempt + 1}" * 40
            self.assertEqual(self.revise(head, draft=True)[1]["state"]["stage"], "pr_promotion")
        state = execution["state"]
        self.assertEqual((state["stage"], state["status"]), ("pr_promotion", "waiting_for_input"))
        self.assertIn("same findings", state["wait"]["reason"])


class VerifiedMergeTest(PullObservationFixture, unittest.TestCase):
    target = "checked_in"

    def ready(self):
        self.start_review()
        self.assertEqual(self.submit(self.review(self.claim("adversarial_review")))[0], 200)
        return self.reconciliation(self.claim("pr_reconciliation"))

    def test_without_merge_intent_the_ready_pr_is_delivered_for_the_owner_to_merge(self):
        status, execution = self.submit(self.ready())
        self.assertEqual(status, 200, execution)
        state = execution["state"]
        self.assertEqual((state["status"], state["target"]), ("completed", "ready_for_check_in"))
        self.assertIsNone(state["final_output"]["merge_commit"])

    def test_merge_closes_once_with_the_verified_pre_merge_revision(self):
        ready = self.ready()
        intent = {"operation_id": "merge-7", "authorization": "Owner requested check-in and repo policy permits it"}
        status, execution = self.submit(dict(ready, merge_intent=intent))
        self.assertEqual(status, 200)
        self.assertEqual(execution["state"]["stage"], "check_in")
        self.assertIsNone(execution["state"]["final_output"])

        claim = self.claim("check_in")
        # The base advanced before the merge, so only the head parent is the candidate.
        merged = self.observation(claim, operation_id="merge-7",
                                  merge_parent_shas=[OTHER, HEAD])
        merged["pull"].update(state="closed", merged=True, merge_commit=OTHER, base_sha=OTHER)
        self.assertEqual(self.result(claim)[0], 409)
        self.assertEqual(self.submit(dict(merged, operation_id="other-write"))[0], 409)
        for parents, expected_status in (([BASE, OTHER], 409), ([HEAD, BASE], 409), ([HEAD], 422)):
            with self.subTest(parents=parents):
                self.assertEqual(self.submit(dict(merged, merge_parent_shas=parents))[0],
                                 expected_status)
        missing_parents = {key: value for key, value in merged.items() if key != "merge_parent_shas"}
        self.assertEqual(self.submit(missing_parents)[0], 422)
        self.assertEqual(self.submit(dict(merged, expected=dict(merged["expected"], base_sha=OTHER)))[0], 409)
        self.assertEqual(self.submit(dict(merged, pull=dict(merged["pull"], number=8)))[0], 409)
        self.assertEqual(self.submit(dict(merged, pull=dict(merged["pull"], merged=False)))[0], 422)

        # A verified merge remains the output when a stop races with the provider write.
        status, stopping = self.request(f"{self.execution}/cancel", OWNER, {
            "expected_version": claim["expected_version"], "reason": "Stop check-in",
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.submit(merged)[0], 409)
        merged["expected_version"] = stopping["version"]
        headers = {"Idempotency-Key": "merge"}
        status, execution = self.submit(merged, **headers)
        self.assertEqual(status, 200)
        self.assertEqual(execution["state"]["status"], "completed")
        output = execution["state"]["final_output"]
        self.assertEqual(output["revision"], {"head": HEAD, "base": BASE})
        self.assertEqual(output["merge_commit"], OTHER)
        self.assertEqual(self.submit(merged, **headers), (status, execution))
        self.assertEqual(self.submit(merged)[0], 409)
        _, timeline = self.request(f"{self.execution}/timeline", OWNER)
        verified = [event for event in timeline if event["event"] == "merge_verified"]
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["details"]["merge_parent_shas"], [OTHER, HEAD])
