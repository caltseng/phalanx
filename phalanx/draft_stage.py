"""Run first-draft publication for an assigned server stage."""

from contextlib import closing
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
from typing import Protocol

from .draft_publication import DraftIntent, DraftOutcome, DraftPublisher, PublicationClaim
from .git_publication import GitPublication, PublicationError
from .github import GitHubClient
from .pr_observations import ExpectedRefs, GitHubPull, Publication
from .transitions import Stage


class RunnerAPI(Protocol):
    """The attended runner's authenticated API transport."""

    def call(self, path: str, body: dict, *, lease: str, key: str | None = None) -> dict: ...


@dataclass(frozen=True)
class DraftAssignment:
    item_id: str
    execution_id: str
    runner_id: str
    attempt_id: str
    state_version: int
    lease: str = field(repr=False)


@dataclass(frozen=True)
class DevelopmentCheckpoint:
    """Runner-owned workspace and base provenance recorded during development."""

    item_id: str
    execution_id: str
    checkout: Path
    candidate: str
    base_ref: str
    base_sha: str
    expected_head: str | None = None


@dataclass(frozen=True)
class DraftStageResult:
    publication: DraftOutcome | None = None
    execution: dict | None = None


class DraftStageRunner:
    """Publish with a live server claim and submit replayable PR evidence."""

    def __init__(self, journal: Path, api: RunnerAPI, github: GitHubClient):
        self.journal = Path(journal).resolve()
        self.api = api
        self.github = github

    def run(
        self, assignment: DraftAssignment, checkpoint: DevelopmentCheckpoint, *,
        default_base: str, title: str, body: str,
    ) -> DraftStageResult:
        if (checkpoint.item_id, checkpoint.execution_id) != (
            assignment.item_id, assignment.execution_id,
        ):
            raise ValueError("Development checkpoint must belong to the assigned execution")

        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) for value in (
            assignment.item_id, assignment.execution_id, assignment.runner_id, assignment.attempt_id,
        )):
            raise ValueError("Draft publication requires valid assignment identifiers")
        path = f"/items/{assignment.item_id}/executions/{assignment.execution_id}"
        event = {
            "expected_version": assignment.state_version,
            "attempt_id": assignment.attempt_id,
            "stage": Stage.PR_CREATION,
        }
        key = f"draft-observation:{assignment.attempt_id}:{assignment.state_version}"

        # Replay a previously sealed body before asking for a live claim. The
        # observation may have committed and ended the attempt before its reply.
        saved = self._receipt(assignment)
        if saved is not None:
            return DraftStageResult(execution=self.api.call(
                f"{path}/pr-observation", saved, lease=assignment.lease, key=key,
            ))

        def current():
            return self.api.call(
                f"{path}/pr-publication-claim", {**event, "runner_id": assignment.runner_id},
                lease=assignment.lease,
            )

        authority = current()
        if authority.get("current") is not True:
            return DraftStageResult(DraftOutcome("claim_lost", "The publication claim is no longer current"))

        base = authority["target_branch"] or default_base
        if checkpoint.candidate != authority["candidate"] or checkpoint.base_ref != base:
            return DraftStageResult(DraftOutcome(
                "conflict", "The assigned candidate or target differs from the development checkpoint",
            ))
        intent = DraftIntent(
            assignment.item_id, authority["repository"], authority["head_ref"], checkpoint.candidate,
            checkpoint.expected_head, base, checkpoint.base_sha, title, body,
        )
        claim = PublicationClaim(assignment.attempt_id, assignment.state_version)

        def check_claim(proposed, active):
            return proposed == intent and active == claim and current() == authority

        try:
            git = GitPublication(checkpoint.checkout, authority["remote"], assignment.item_id)
            git.verify_candidate(intent.candidate, base=intent.base, base_sha=intent.expected_base)
        except PublicationError as exc:
            return DraftStageResult(DraftOutcome("conflict", str(exc)))
        outcome = DraftPublisher(
            self.journal, git, self.github, check_claim=check_claim,
        ).publish(intent, claim)
        if outcome.status != "verified":
            return DraftStageResult(outcome)

        pull = outcome.primary
        assert pull is not None
        payload = Publication(
            **event,
            expected=ExpectedRefs(head_ref=intent.head, base_ref=intent.base, base_sha=intent.expected_base),
            pull=GitHubPull(
                number=pull.number, head_repository=pull.head_repository,
                base_repository=pull.base_repository, head_ref=pull.head_ref, base_ref=pull.base_ref,
                head_sha=pull.head_sha.lower(), base_sha=pull.base_sha.lower(), state=pull.state,
                draft=pull.draft, merged=pull.merged,
            ),
            duplicate_numbers=sorted(pr.number for pr in outcome.duplicates),
        ).model_dump(mode="json")
        saved = self._receipt(assignment, payload)
        assert saved is not None
        execution = self.api.call(f"{path}/pr-observation", saved, lease=assignment.lease, key=key)
        return DraftStageResult(outcome, execution)

    def _receipt(self, assignment: DraftAssignment, payload: dict | None = None) -> dict | None:
        identity = (assignment.item_id, assignment.execution_id, assignment.runner_id,
                    assignment.attempt_id, assignment.state_version)
        self.journal.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.journal / "draft-observations.sqlite3")) as db, db:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("""
                CREATE TABLE IF NOT EXISTS draft_observations (
                    item_id TEXT, execution_id TEXT, runner_id TEXT, attempt_id TEXT,
                    version INTEGER, payload TEXT NOT NULL,
                    PRIMARY KEY (item_id, execution_id, runner_id, attempt_id, version)
                )
            """)
            if payload is not None:
                db.execute("""
                    INSERT INTO draft_observations VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                """, (*identity, json.dumps(payload, sort_keys=True)))
            row = db.execute("""
                SELECT payload FROM draft_observations
                WHERE item_id=? AND execution_id=? AND runner_id=? AND attempt_id=? AND version=?
            """, identity).fetchone()
            return json.loads(row[0]) if row is not None else None
