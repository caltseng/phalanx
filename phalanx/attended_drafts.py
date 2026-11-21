"""Attended runner bridge for initial draft assignments."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .github import GitHubClient


def github_repository(remote):
    from .pr_models import GITHUB_REMOTE

    match = GITHUB_REMOTE.fullmatch(remote)
    return match[1].lower() if match else None


def development_checkpoint(journal, claim):
    """Find accepted development evidence for this exact execution and candidate."""
    execution = claim["execution"]
    with closing(sqlite3.connect(journal)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='completed'").fetchone() is None:
            return None
        db.execute("""
            CREATE INDEX IF NOT EXISTS completed_development_candidate ON completed (
                json_extract(record, '$.claim.item_id'),
                json_extract(record, '$.claim.execution.id'),
                json_extract(record, '$.submission.body.candidate')
            )
        """)
        row = db.execute("""
            SELECT json_extract(record, '$.submission.checkpoint')
            FROM completed
            WHERE json_extract(record, '$.claim.item_id') = ?
              AND json_extract(record, '$.claim.execution.id') = ?
              AND json_extract(record, '$.submission.body.candidate') = ?
            ORDER BY rowid DESC LIMIT 1
        """, (claim["item_id"], execution["id"], execution["state"].get("candidate"))).fetchone()
    if row is None or row[0] is None:
        return None
    checkpoint = json.loads(row[0])
    provenance = checkpoint.get("base")
    if provenance is None:
        return None
    from .draft_stage import DevelopmentCheckpoint

    return DevelopmentCheckpoint(
        claim["item_id"], execution["id"], Path(checkpoint["path"]), checkpoint["head"],
        provenance["ref"], provenance["sha"],
    )


def publish_draft(root, api, record, mapping, checkpoint, token):
    from .draft_stage import DraftAssignment, DraftStageRunner

    claim = record["claim"]
    execution = claim["execution"]
    repository = github_repository(record["repositories"][claim["request"]["repository_id"]])
    owner, name = repository.split("/")
    assignment = DraftAssignment(
        claim["item_id"], execution["id"], record["runner_id"], claim["attempt_id"],
        execution["version"], record["lease"],
    )
    goal = claim["request"]["goal"]
    return DraftStageRunner(root / "publication", api, GitHubClient(owner, name, token=token)).run(
        assignment, checkpoint, default_base=mapping["base_branch"],
        title=goal.splitlines()[0][:256], body=goal,
    )
