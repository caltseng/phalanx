"""Runner entry point for the ready PR's published-validation stage."""

from contextlib import closing
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sqlite3

from pydantic import TypeAdapter, ValidationError

from .draft_stage import RunnerAPI
from .github import GitHubClient, GitHubError
from .github_checks import PreviewReader, collect_github_checks
from .intake_models import Identifier
from .pr_observations import (
    ExpectedRefs, GateObservation, GitHubPull, PublishedValidationClaimCheck, Validation,
)
from .transitions import Outcome, Stage
from .validation import GateState, Phase, RequiredCheck

_IDENTIFIER = TypeAdapter(Identifier)


@dataclass(frozen=True)
class PublishedValidationAssignment:
    item_id: str
    execution_id: str
    runner_id: str
    attempt_id: str
    state_version: int
    lease: str = field(repr=False)


@dataclass(frozen=True)
class PublishedValidationPolicy:
    """Complete ready-phase policy from trusted runner configuration."""

    required: tuple[RequiredCheck, ...] | None
    read_preview: PreviewReader | None = None


class PublishedValidationRunner:
    def __init__(self, journal: Path, api: RunnerAPI, github: GitHubClient):
        self.journal = Path(journal).resolve()
        self.api = api
        self.github = github

    def run(self, assignment: PublishedValidationAssignment,
            policy: PublishedValidationPolicy) -> dict | None:
        """Return the committed execution view, or None if the claim was lost."""
        for value in (assignment.item_id, assignment.execution_id):
            _IDENTIFIER.validate_python(value)
        claim = PublishedValidationClaimCheck(
            expected_version=assignment.state_version, attempt_id=assignment.attempt_id,
            stage=Stage.PR_VALIDATION, runner_id=assignment.runner_id,
        )
        path = f"/items/{assignment.item_id}/executions/{assignment.execution_id}"
        key = f"published-validation:{assignment.attempt_id}:{assignment.state_version}"
        saved = self._receipt(assignment)
        if saved is None:
            authority = self.api.call(f"{path}/pr-validation-claim", claim.model_dump(mode="json"),
                                      lease=assignment.lease)
            if authority.get("current") is not True:
                return None
            event = claim.model_dump(mode="json", exclude={"runner_id"})
            saved = self._receipt(assignment, self._observe(authority, event, policy))
        route, body = saved
        return self.api.call(f"{path}/{route}", body, lease=assignment.lease, key=key)

    def _observe(self, authority, event, policy):
        promotion = authority["promotion"]
        url = authority["pr_url"]
        if promotion is None:
            return _block(event, "Restore the trusted promotion evidence for the primary PR.", url)
        expected = GitHubPull.model_validate(promotion)
        handoff = (f"{url}, target {expected.base_ref}, "
                   f"head {expected.head_sha}, base {expected.base_sha}")
        if policy.required is None:
            return _block(event, "Configure the complete ready-phase validation policy for this repository.",
                          handoff)
        if len(policy.required) > 100 or len({
            (gate.kind, gate.name, gate.app_id) for gate in policy.required
        }) != len(policy.required):
            return _block(event, "Resolve duplicate or oversized ready-phase policy.", handoff)
        if self.github.repository.casefold() != expected.base_repository.casefold():
            return _block(event, "Restore the runner's GitHub mapping for the registered repository.",
                          handoff)
        try:
            pull = self.github.get_pull_request(expected.number)
        except GitHubError as error:
            if error.retry_after_seconds is not None:
                return _retry(event, str(error), error.retry_after_seconds)
            return _block(event, f"Restore access to the primary PR. {error}", handoff)

        if (
            pull.url.casefold() != url.casefold() or pull.state != "open"
            or pull.merged is not False or pull.draft
            or (pull.head_repository or "").casefold() != expected.head_repository.casefold()
            or (pull.head_ref, pull.base_ref, pull.head_sha)
            != (expected.head_ref, expected.base_ref, expected.head_sha)
        ):
            return _block(event, "Reconcile the primary PR with the recorded branch and candidate.", handoff)

        checks = collect_github_checks(
            self.github, pull, Phase.READY, policy.required, read_preview=policy.read_preview,
        )
        if checks.retry_after_seconds is not None:
            return _retry(event, "; ".join(checks.gate.reasons), checks.retry_after_seconds)
        handoff = (f"{url}, target {pull.base_ref}, head {pull.head_sha}, base {pull.base_sha}")
        if checks.gate.state is GateState.BLOCKED or checks.candidate is None:
            return _block(event, "; ".join(checks.gate.reasons), handoff)
        if checks.candidate.draft:
            return _block(event, "Restore the ready state of the primary PR.", handoff)

        try:
            body = Validation(
                **event, schedule_recheck=True,
                expected=ExpectedRefs(
                    head_ref=expected.head_ref, base_ref=expected.base_ref, base_sha=pull.base_sha,
                ),
                pull=GitHubPull.model_validate({
                    field: getattr(pull, field) for field in GitHubPull.model_fields
                }),
                gates=GateObservation(
                    phase=Phase.READY, number=expected.number,
                    head_sha=expected.head_sha, base_sha=pull.base_sha,
                    test_merge_sha=checks.candidate.merge_sha,
                    required=[asdict(check) for check in policy.required],
                    observations=[
                        {"name": evidence.name, "kind": evidence.kind,
                         "tested_sha": evidence.tested_sha, "outcome": evidence.outcome,
                         "app_id": evidence.app_id}
                        for evidence in checks.observations
                        if any(
                            check.kind == evidence.kind and check.name == evidence.name
                            and (check.app_id is None or check.app_id == evidence.app_id)
                            for check in policy.required
                        )
                    ],
                ),
            )
        except ValidationError:
            return _block(event, "Resolve invalid or oversized ready-phase policy or evidence.", handoff)
        return "pr-observation", body.model_dump(mode="json")

    def _receipt(self, assignment, submission=None):
        identity = (assignment.item_id, assignment.execution_id, assignment.runner_id,
                    assignment.attempt_id, assignment.state_version)
        self.journal.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.journal / "published-validation.sqlite3")) as db, db:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    item_id TEXT, execution_id TEXT, runner_id TEXT, attempt_id TEXT,
                    version INTEGER, submission TEXT NOT NULL,
                    PRIMARY KEY (item_id, execution_id, runner_id, attempt_id, version)
                )
            """)
            if submission is not None:
                db.execute("""
                    INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
                """, (*identity, json.dumps(submission, sort_keys=True)))
            row = db.execute("""
                SELECT submission FROM submissions
                WHERE item_id=? AND execution_id=? AND runner_id=? AND attempt_id=? AND version=?
            """, identity).fetchone()
            return json.loads(row[0]) if row else None


def _block(event, reason, handoff):
    return "result", {
        **event, "outcome": Outcome.NEEDS_INPUT,
        "question": {"prompt": reason[:16000], "handoff": handoff or "Published validation"},
    }


def _retry(event, reason, delay):
    return "result", {
        **event, "outcome": Outcome.FAILED,
        "failure_reason": reason[:16000], "retry_after_seconds": delay,
    }
