"""Read-only, execution-scoped views for the operator board."""

from base64 import b64decode, urlsafe_b64encode
from datetime import UTC, datetime
import json
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .auth import authenticate_board_read
from .intake import report as stored_report
from .intake_api import run
from .intake_models import Identifier
from .operator_schema import CLOSED, INPUT_RANK, OPEN
from .store import read_transaction

router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate_board_read)])
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00")
ID = re.compile(r"[0-9a-f]{32}")

SUMMARY = """
    SELECT execution.id, execution.item_id, execution.version, execution.created_at, execution.updated_at,
           execution.stage, execution.status, execution.state_json, execution.retry_at,
           item.repository_id, json_extract(item.request_json, '$.goal') AS title,
           progress.last_advanced_at, progress.wait_since,
           coalesce(json_array_length(execution.dependency_wait_json), 0) AS blocked_count,
           recheck.due_at_ms AS recheck_at_ms, worker.reference AS worker_reference, worker.url AS worker_url,
           (SELECT attempt.blocker FROM attempts AS attempt
            WHERE attempt.execution_id = execution.id AND attempt.ended_at IS NULL) AS runner_blocker,
           (SELECT attempt.lease_expires_at FROM attempts AS attempt
            WHERE attempt.execution_id = execution.id AND attempt.ended_at IS NULL) AS lease_expires_at,
           (SELECT attempt.id FROM attempts AS attempt
            WHERE attempt.execution_id = execution.id AND attempt.ended_at IS NULL) AS stage_owner,
           (SELECT json_extract(history.details_json, '$.continued_from') IS NOT NULL
            FROM transition_history AS history
            WHERE history.execution_id = execution.id AND history.event = 'claim'
            ORDER BY history.version DESC LIMIT 1) AS slot_retained,
           (SELECT json_extract(history.details_json, '$.capacity_release')
            FROM transition_history AS history
            WHERE history.execution_id = execution.id AND history.attempt_id IS NOT NULL
              AND history.event != 'claim'
            ORDER BY history.version DESC LIMIT 1) AS capacity_release
    FROM executions AS execution
    JOIN work_items AS item ON item.id = execution.item_id
    JOIN operator_progress AS progress ON progress.execution_id = execution.id
    LEFT JOIN external_rechecks AS recheck
        ON recheck.execution_id = execution.id AND recheck.version = execution.version
    LEFT JOIN attempt_workers AS worker ON worker.attempt_id = (
        SELECT attempt.id FROM attempts AS attempt WHERE attempt.execution_id = execution.id
        ORDER BY attempt.started_at DESC, attempt.id DESC LIMIT 1
    )
"""


def encode_cursor(scope, key):
    data = json.dumps([1, scope, key], separators=(",", ":")).encode("ascii")
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def decode_cursor(cursor, scope, patterns):
    if cursor is None:
        return None
    try:
        version, actual_scope, key = json.loads(
            b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            type(version) is int and version == 1 and actual_scope == scope
            and isinstance(key, list) and len(key) == len(patterns)
            and all(pattern(value) for pattern, value in zip(patterns, key))
            and cursor == encode_cursor(scope, key)
        ):
            return key
    except (ValueError, TypeError):
        pass
    raise HTTPException(400, "Invalid cursor")


def is_stamp(value):
    return isinstance(value, str) and STAMP.fullmatch(value)


def is_id(value):
    return isinstance(value, str) and ID.fullmatch(value)


def is_rank(value):
    return type(value) is int and value in (0, 1)


def is_version(value):
    return type(value) is int and 1 <= value <= 2**63 - 1


def page_cursor(rows, limit, scope, key):
    return encode_cursor(scope, key(rows[limit - 1])) if len(rows) > limit else None


def summary(row, now):
    state = json.loads(row["state_json"])
    wait = state.get("wait") or {}
    output = state["final_output"]
    outcome = None
    if output is not None:
        if row["status"] != "completed":
            outcome = row["status"]
        elif "reference" in output:
            outcome = "report_stored"
        else:
            outcome = "checked_in" if output.get("merge_commit") else "ready_for_check_in"
        output = (
            {"kind": "report", "url": output["reference"]} if "reference" in output
            else {"kind": "pr", "url": output["url"]}
        )
    wait_since = row["wait_since"]
    blocked_count = row["blocked_count"]
    if blocked_count and not wait:
        noun = "prerequisite" if blocked_count == 1 else "prerequisites"
        wait = {"reason": f"Waiting for {blocked_count} {noun}", "trigger": "Prerequisite milestone"}
    if row["runner_blocker"] and not wait:
        wait = {"reason": row["runner_blocker"], "trigger": "Resolve the blocker; the runner clears it on its next successful pass"}
    expires = row["lease_expires_at"]
    if expires is not None and expires <= now.timestamp() and not wait:
        # Runners publish blockers only while renewing, so an expired lease needs its own reason.
        at = datetime.fromtimestamp(expires, UTC).isoformat()
        wait = {"reason": f"Lease expired at {at}; awaiting runner recovery",
                "trigger": "The runner recovers the expired attempt on its next pass"}
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "version": row["version"],
        "title": row["title"],
        "repository_id": row["repository_id"],
        "stage": row["stage"],
        "status": row["status"],
        "last_advanced_at": row["last_advanced_at"],
        "last_progress_at": row["updated_at"],
        "wait_reason": wait.get("reason"),
        "blocked_prerequisite_count": blocked_count,
        "resume_trigger": wait.get("trigger"),
        "retry_at_ms": row["retry_at"],
        "recheck_at_ms": row["recheck_at_ms"],
        "worker_reference": row["worker_reference"],
        "worker_url": row["worker_url"],
        # Who runs the current stage, whether it kept its slot from the previous
        # stage, and otherwise why the item last gave its runner slot back.
        "stage_owner": row["stage_owner"],
        "slot_retained": bool(row["stage_owner"] and row["slot_retained"]),
        "capacity_release": None if row["stage_owner"] else row["capacity_release"],
        "wait_since": wait_since,
        "wait_age_seconds": (
            max(0, int((now - datetime.fromisoformat(wait_since)).total_seconds()))
            if wait_since else None
        ),
        "primary_pr_url": state.get("primary_pr"),
        "closed_at": row["updated_at"] if output is not None else None,
        "outcome": outcome,
        "output": output,
    }


def open_key(row):
    return [int(row["status"] != "waiting_for_input"), row["created_at"], row["id"]]


def open_page(db, after, size):
    # Separate rank buckets let SQLite seek directly to the timestamp and ID.
    first_rank = after[0] if after else 0
    rows = []
    for rank in range(first_rank, 2):
        boundary, params = "", ()
        if after and rank == first_rank:
            boundary = "AND (execution.created_at, execution.id) > (?, ?)"
            params = after[1:]
        rows.extend(db.execute(
            f"""{SUMMARY} WHERE {OPEN} AND ({INPUT_RANK}) = ? {boundary}
                ORDER BY execution.created_at, execution.id LIMIT ?""",
            (rank, *params, size - len(rows)),
        ))
        if len(rows) == size:
            break
    return rows


def closed_page(db, after, size):
    boundary = "AND (execution.updated_at, execution.id) < (?, ?)" if after else ""
    return db.execute(
        f"""{SUMMARY} WHERE {CLOSED} {boundary}
            ORDER BY execution.updated_at DESC, execution.id DESC LIMIT ?""",
        (*(after or ()), size),
    ).fetchall()


@router.get("/operator/executions")
def list_executions(request: Request, kind: Literal["open", "closed"] = "open",
                    limit: Limit = 25, cursor: Cursor = None):
    with read_transaction(request.app.state.database_path) as db:
        if kind == "open":
            after = decode_cursor(cursor, kind, (is_rank, is_stamp, is_id))
            rows = open_page(db, after, limit + 1)
            key = open_key
        else:
            after = decode_cursor(cursor, kind, (is_stamp, is_id))
            rows = closed_page(db, after, limit + 1)
            key = lambda row: [row["updated_at"], row["id"]]
    now = datetime.now(UTC)
    return {
        "items": [summary(row, now) for row in rows[:limit]],
        "next_cursor": page_cursor(rows, limit, kind, key),
    }


@router.get("/items/{item_id}/executions/{execution_id}/report", response_class=PlainTextResponse)
def report(request: Request, item_id: Identifier, execution_id: Identifier):
    return run(request, stored_report, item_id, execution_id)


@router.get("/operator/executions/{execution_id}/timeline")
def timeline(request: Request, execution_id: Identifier, limit: Limit = 25, cursor: Cursor = None):
    scope = f"timeline:{execution_id}"
    after = decode_cursor(cursor, scope, (is_version, is_stamp))
    with read_transaction(request.app.state.database_path) as db:
        execution = db.execute(
            f"{SUMMARY} WHERE execution.id = ?", (execution_id,)
        ).fetchone()
        if execution is None:
            raise HTTPException(404, "Execution not found")
        attempt = db.execute(
            "SELECT id FROM attempts WHERE execution_id = ? AND ended_at IS NULL",
            (execution_id,),
        ).fetchone()
        prerequisites = db.execute(
            """SELECT json_extract(block.value, '$.upstream_id') AS item_id,
                      json_extract(upstream.request_json, '$.goal') AS title,
                      json_extract(block.value, '$.milestone') AS milestone,
                      json_extract(block.value, '$.reason') AS reason
               FROM executions AS execution
               JOIN json_each(execution.dependency_wait_json) AS block
               JOIN work_items AS upstream ON upstream.id = json_extract(block.value, '$.upstream_id')
               WHERE execution.id = ?
               ORDER BY block.key""",
            (execution_id,),
        ).fetchall()
        boundary, params = ("WHERE (version, created_at) < (?, ?)", after) if after else ("", ())
        question = None
        if execution["status"] == "waiting_for_input":
            asked = db.execute(
                """SELECT json_extract(details_json, '$.question') AS question FROM transition_history
                   WHERE execution_id = ? AND version = ?""",
                (execution_id, execution["version"]),
            ).fetchone()
            question = json.loads(asked["question"]) if asked and asked["question"] else None
        rows = db.execute(
            f"""WITH events AS (
                SELECT event.version, event.event, event.created_at, event.attempt_id,
                       json_extract(event.state_json, '$.stage') AS stage,
                       json_extract(event.state_json, '$.status') AS status,
                       CASE WHEN json_extract(prior.state_json, '$.stage')
                                 <> json_extract(event.state_json, '$.stage')
                            THEN json_extract(prior.state_json, '$.stage') END AS from_stage,
                       coalesce(json_extract(event.details_json, '$.reason'),
                                CASE WHEN event.event = 'reply' THEN 'Answered by ' || coalesce(
                                    json_extract(event.details_json, '$.answered_by'), 'owner') END,
                                json_extract(event.state_json, '$.wait.reason')) AS reason,
                       CASE WHEN event.event = 'reply' THEN coalesce(
                           json_extract(event.details_json, '$.answered_by'), 'owner') END
                           AS answered_by
                FROM transition_history AS event
                LEFT JOIN transition_history AS prior
                    ON prior.execution_id = event.execution_id AND prior.version = event.version - 1
                WHERE event.execution_id = ?
                UNION ALL
                SELECT adoption.version, 'recover_claim', adoption.created_at, adoption.attempt_id,
                       json_extract(adopted.state_json, '$.stage'),
                       json_extract(adopted.state_json, '$.status'),
                       NULL, 'Adopted the attempt: ' || adoption.reconciliation, NULL
                FROM attempt_adoptions AS adoption
                JOIN transition_history AS adopted
                    ON adopted.execution_id = adoption.execution_id AND adopted.version = adoption.version
                WHERE adoption.execution_id = ?
            )
            SELECT * FROM events {boundary}
            ORDER BY version DESC, created_at DESC LIMIT ?""",
            (execution_id, execution_id, *params, limit + 1),
        ).fetchall()
    return {
        "item": summary(execution, datetime.now(UTC)),
        "attempt": attempt["id"] if attempt else None,
        "prerequisites": [dict(row) for row in prerequisites],
        "question": question,
        "events": [dict(row) for row in rows[:limit]],
        "next_cursor": page_cursor(rows, limit, scope, lambda row: [row["version"], row["created_at"]]),
    }
