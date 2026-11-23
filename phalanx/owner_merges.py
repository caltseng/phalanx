"""Owner merges of delivered ready PRs, attested by a runner so `checked_in` dependents can proceed.

A completed execution stays immutable. The runner observes the merge on GitHub and the
server appends it beside the execution, so the PR it verified is what dependents pin.
"""

import json
from time import time

from . import dependencies, intake
from .pr_models import GITHUB_REMOTE
from .transitions import PullRequest, Status, item_branch


def watches(db, limit):
    """Delivered ready PRs that a queued dependent needs merged."""
    rows = db.execute(
        """SELECT DISTINCT e.id, e.item_id, e.state_json, w.repository_id, w.request_json, r.remote
           FROM work_dependencies AS d
           JOIN executions AS e ON e.item_id = d.upstream_id AND e.status = 'completed'
                AND NOT EXISTS (SELECT 1 FROM executions WHERE previous_id = e.id)
           JOIN executions AS waiting ON waiting.item_id = d.item_id AND waiting.status = 'queued'
           JOIN work_items AS w ON w.id = e.item_id
           JOIN repositories AS r ON r.id = w.repository_id
           WHERE d.milestone = 'checked_in'
             AND json_type(e.state_json, '$.final_output.url') = 'text'
             AND json_extract(e.state_json, '$.final_output.merge_commit') IS NULL
             AND NOT EXISTS (SELECT 1 FROM owner_merges WHERE execution_id = e.id)
           ORDER BY e.id LIMIT ?""",
        (limit,),
    )
    result = []
    for row in rows:
        output = json.loads(row["state_json"])["final_output"]
        result.append({
            "item_id": row["item_id"], "execution_id": row["id"], "repository_id": row["repository_id"],
            "remote": row["remote"], "target_branch": json.loads(row["request_json"]).get("target_branch"),
            "head_ref": item_branch(row["item_id"]), "pr_url": output["url"], "revision": output["revision"],
        })
    return result


def record(db, item_id, execution_id, body):
    """Verify the merged PR is the execution's delivered PR at its verified head, then record it."""
    row = intake.execution_row(db, item_id, execution_id)
    state = intake.STATE.validate_json(row["state_json"])
    output = state.final_output
    if (state.status != Status.COMPLETED or not isinstance(output, PullRequest) or output.merge_commit
            or db.execute("SELECT 1 FROM executions WHERE previous_id=?", (execution_id,)).fetchone()):
        raise intake.Conflict("Only the latest completed execution with an unmerged ready PR takes an owner merge")
    remote, = db.execute("SELECT remote FROM repositories WHERE id=?",
                         (intake.item_row(db, item_id)["repository_id"],)).fetchone()
    match = GITHUB_REMOTE.fullmatch(remote)
    pull = body.pull
    repository = match[1].lower() if match else None
    url = f"https://github.com/{repository}/pull/{pull.number}"
    if (repository is None or pull.base_repository.lower() != repository
            or (pull.head_repository or "").lower() != repository
            or pull.head_ref != item_branch(item_id) or url != output.url
            or pull.head_sha != output.revision.head):
        raise intake.Conflict("The merged PR must be the delivered primary PR at its verified head")
    existing = db.execute("SELECT merge_commit FROM owner_merges WHERE execution_id=?",
                          (execution_id,)).fetchone()
    if existing is None:
        db.execute("INSERT INTO owner_merges VALUES (?, ?, ?, ?, ?, ?)",
                   (execution_id, url, pull.head_sha, pull.merge_commit, body.runner_id, intake.timestamp()))
        dependencies.refresh_dependents(db, item_id, int(time() * 1000))
    elif existing["merge_commit"] != pull.merge_commit:
        raise intake.Conflict("A different owner merge is already recorded")
    return {"execution_id": execution_id, "pr_url": url, "merge_commit": pull.merge_commit}
