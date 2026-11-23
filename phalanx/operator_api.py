"""Read-only, execution-scoped views for the operator board."""

from base64 import b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse

from . import dispatch
from .auth import authenticate_board_read
from .intake import report as stored_report
from .intake_api import run
from .intake_models import LABEL_PATTERN, Identifier
from .operator_schema import CLOSED, INPUT_RANK, OPEN
from .store import read_transaction
from .transitions import Stage, Status

router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate_board_read)])
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00")
ID = re.compile(r"[0-9a-f]{32}")
RUNNER_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
PR_NUMBER = re.compile(r"#?([0-9]{1,18})")
CLOSED_STATUSES = {Status.COMPLETED, Status.FAILED, Status.CANCELLED}
DELIVERED = ("report_stored", "ready_for_check_in", "checked_in")
Outcome = Literal["report_stored", "ready_for_check_in", "checked_in", "failed", "cancelled"]
# A filtered closed page reads at most this many closures, so a rare match costs a bounded scan
# per request and may return a short page with a cursor to continue.
SCAN_LIMIT = 2000

SUMMARY = """
    SELECT execution.id, execution.item_id, execution.version, execution.created_at, execution.updated_at,
           execution.stage, execution.status, execution.state_json, execution.retry_at,
           execution.ready_since, item.created_at AS item_created_at, item.label,
           item.repository_id, json_extract(item.request_json, '$.goal') AS title,
           progress.last_advanced_at, progress.wait_since, progress.revision_round,
           pr.url AS pr_url, pr.number AS pr_number, pr.state AS pr_state, pr.draft AS pr_draft,
           pr.merged AS pr_merged, pr.head_sha AS pr_head_sha, pr.additions AS pr_additions,
           pr.deletions AS pr_deletions, pr.ci AS pr_ci, pr.ci_head_sha AS pr_ci_head_sha,
           coalesce(pr.duplicate_count, 0) AS duplicate_count,
           coalesce(json_array_length(execution.dependency_wait_json), 0) AS blocked_count,
           recheck.due_at_ms AS recheck_at_ms, worker.reference AS worker_reference, worker.url AS worker_url,
           (SELECT attempt.blocker FROM attempts AS attempt
            WHERE attempt.execution_id = execution.id AND attempt.ended_at IS NULL) AS runner_blocker,
           (SELECT attempt.lease_expires_at FROM attempts AS attempt
            WHERE attempt.execution_id = execution.id AND attempt.ended_at IS NULL) AS lease_expires_at
    FROM executions AS execution
    JOIN work_items AS item ON item.id = execution.item_id
    JOIN operator_progress AS progress ON progress.execution_id = execution.id
    LEFT JOIN operator_pr AS pr ON pr.execution_id = execution.id
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


def stamp(moment):
    return moment.isoformat(timespec="microseconds")


def dispatch_context(db, now):
    """Facts shared by every queued row's wait reason and by the overview."""
    cutoff = stamp(now - timedelta(seconds=dispatch.LEASE_SECONDS))
    default = dispatch.DEFAULT_CAPACITY
    runners = db.execute(
        """SELECT count(*) AS known,
                  coalesce(sum(runner.last_seen_at >= ?), 0) AS online,
                  coalesce(sum(CASE WHEN runner.last_seen_at >= ? THEN min(
                      coalesce(limits.capacity, ?), coalesce(runner.slots, limits.capacity, ?)) END), 0)
                      AS total
           FROM runners AS runner LEFT JOIN runner_limits AS limits ON limits.runner_id = runner.id""",
        (cutoff, cutoff, default, default),
    ).fetchone()
    return {
        "paused": bool(db.execute("SELECT paused FROM dispatch_settings WHERE id=1").fetchone()["paused"]),
        "known": runners["known"],
        "online": runners["online"],
        "slots_total": runners["total"],
        "slots_used": db.execute("SELECT count(*) FROM attempts WHERE ended_at IS NULL").fetchone()[0],
        "online_since": cutoff,
    }


def queued_wait(row, now_ms, context):
    retry_at = row["retry_at"]
    if context["paused"]:
        return {"reason": "Dispatch is paused", "trigger": "The owner resumes dispatch"}
    if retry_at is not None and retry_at > now_ms:
        at = datetime.fromtimestamp(retry_at / 1000, UTC).isoformat()
        return {"reason": f"Retry scheduled for {at}", "trigger": "The retry delay elapses"}
    if not context["online"]:
        return {"reason": "No runner online", "trigger": "A runner polls for work"}
    if context["slots_used"] >= context["slots_total"]:
        return {"reason": "Waiting for a free slot", "trigger": "A running stage finishes"}
    return {}


def pull_request(row):
    if row["pr_url"] is None:
        return None
    head = row["pr_head_sha"]
    return {
        "url": row["pr_url"],
        "number": row["pr_number"],
        "state": "merged" if row["pr_merged"] else row["pr_state"],
        "draft": None if row["pr_draft"] is None else bool(row["pr_draft"]),
        "head_sha": head,
        "head_ci": row["pr_ci"] if row["pr_ci_head_sha"] == head else None,
        "additions": row["pr_additions"],
        "deletions": row["pr_deletions"],
    }


def summary(row, now, context):
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
    status = row["status"]
    if status == Status.QUEUED and not wait:
        wait = queued_wait(row, int(now.timestamp() * 1000), context)
    next_action = None
    if output is None and not wait:
        if status == Status.QUEUED:
            next_action = "Next to run: a runner with a free slot claims it on its next poll"
        elif status == Status.RUNNING:
            next_action = ("Stopping the worker" if state.get("stopping")
                           else f"A worker is running the {row['stage']} stage")
        else:
            next_action = f"{status.replace('_', ' ').capitalize()}; the {row['stage']} stage resumes when it clears"
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "version": row["version"],
        "title": row["title"],
        "label": row["label"],
        "repository_id": row["repository_id"],
        "target": state.get("target"),
        "stage": row["stage"],
        "status": status,
        "revision_round": row["revision_round"],
        "last_advanced_at": row["last_advanced_at"],
        "last_progress_at": row["updated_at"],
        "wait_reason": wait.get("reason"),
        "blocked_prerequisite_count": blocked_count,
        "resume_trigger": wait.get("trigger"),
        "next_action": next_action,
        "retry_at_ms": row["retry_at"],
        "recheck_at_ms": row["recheck_at_ms"],
        "worker_reference": row["worker_reference"],
        "worker_url": row["worker_url"],
        "wait_since": wait_since,
        "wait_age_seconds": (
            max(0, int((now - datetime.fromisoformat(wait_since)).total_seconds()))
            if wait_since else None
        ),
        "primary_pr_url": state.get("primary_pr"),
        "pr": pull_request(row),
        "duplicate_count": row["duplicate_count"],
        "closed_at": row["updated_at"] if output is not None else None,
        "outcome": outcome,
        "output": output,
    }


def open_key(row):
    return [int(row["status"] != "waiting_for_input"), row["created_at"], row["id"]]


def closed_key(row):
    return [row["updated_at"], row["id"]]


def filter_clause(filters):
    clauses, params = [], []
    for name, column in (("repository", "item.repository_id"), ("label", "item.label"),
                         ("status", "execution.status"), ("stage", "execution.stage"),
                         ("outcome", "execution.outcome")):
        if name in filters:
            clauses.append(f"{column} = ?")
            params.append(filters[name])
    if "closed_after" in filters:
        clauses.append("execution.updated_at > ?")
        params.append(filters["closed_after"])
    if "q" in filters:
        query = filters["q"]
        number = PR_NUMBER.fullmatch(query)
        clauses.append(
            """(instr(lower(json_extract(item.request_json, '$.goal')), lower(?)) > 0
                OR execution.item_id = ? OR execution.id = ? OR pr.url = ? OR pr.number = ?)"""
        )
        params.extend((query, query, query, query, int(number[1]) if number else None))
    return "".join(f" AND {clause}" for clause in clauses), params


def open_page(db, after, size, where="", params=()):
    # Separate rank buckets let SQLite seek directly to the timestamp and ID.
    # Filters narrow the open set, which stays bounded by capacity and intake.
    first_rank = after[0] if after else 0
    rows = []
    for rank in range(first_rank, 2):
        boundary, bounds = "", ()
        if after and rank == first_rank:
            boundary = "AND (execution.created_at, execution.id) > (?, ?)"
            bounds = after[1:]
        rows.extend(db.execute(
            f"""{SUMMARY} WHERE {OPEN} AND ({INPUT_RANK}) = ? {boundary} {where}
                ORDER BY execution.created_at, execution.id LIMIT ?""",
            (rank, *bounds, *params, size - len(rows)),
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


def filtered_closed_page(db, after, size, where, params, closed_after):
    """Match filters within the next SCAN_LIMIT closures; return rows and the next cursor key."""
    boundary = "AND (execution.updated_at, execution.id) < (?, ?)" if after else ""
    floor = "AND execution.updated_at > ?" if closed_after else ""
    bounds = (*(after or ()), *((closed_after,) if closed_after else ()))
    keys = db.execute(
        f"""SELECT execution.updated_at, execution.id FROM executions AS execution
            WHERE {CLOSED} {boundary} {floor}
            ORDER BY execution.updated_at DESC, execution.id DESC LIMIT ?""",
        (*bounds, SCAN_LIMIT),
    ).fetchall()
    if not keys:
        return [], None
    last = [keys[-1]["updated_at"], keys[-1]["id"]]
    rows = db.execute(
        f"""{SUMMARY} WHERE {CLOSED} {boundary} AND (execution.updated_at, execution.id) >= (?, ?) {where}
            ORDER BY execution.updated_at DESC, execution.id DESC LIMIT ?""",
        (*(after or ()), *last, *params, size + 1),
    ).fetchall()
    if len(rows) > size:
        return rows[:size], closed_key(rows[size - 1])
    return rows, last if len(keys) == SCAN_LIMIT else None


@router.get("/operator/executions")
def list_executions(
    request: Request, kind: Literal["open", "closed"] = "open", limit: Limit = 25, cursor: Cursor = None,
    repository: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_-]{1,100}$")] = None,
    label: Annotated[str | None, Query(pattern=LABEL_PATTERN)] = None,
    status: Status | None = None,
    stage: Stage | None = None,
    outcome: Outcome | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    closed_after: datetime | None = None,
):
    if kind == "open" and (outcome or closed_after):
        raise HTTPException(400, "outcome and closed_after apply only to closed executions")
    if status is not None and (status in CLOSED_STATUSES) != (kind == "closed"):
        raise HTTPException(400, f"Status {status} is not a {kind} status")
    if closed_after is not None:
        moment = closed_after if closed_after.tzinfo else closed_after.replace(tzinfo=UTC)
        closed_after = stamp(moment.astimezone(UTC))
    filters = {name: str(value) for name, value in (
        ("repository", repository), ("label", label), ("status", status), ("stage", stage),
        ("outcome", outcome), ("q", q), ("closed_after", closed_after),
    ) if value is not None}
    # Unfiltered cursors keep their original scope; filtered cursors are bound to their filters.
    scope = kind
    if filters:
        digest = sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:16]
        scope = f"{kind}:{digest}"
    where, params = filter_clause(filters)
    now = datetime.now(UTC)
    with read_transaction(request.app.state.database_path) as db:
        if kind == "open":
            after = decode_cursor(cursor, scope, (is_rank, is_stamp, is_id))
            rows = open_page(db, after, limit + 1, where, params)
            next_key = open_key(rows[limit - 1]) if len(rows) > limit else None
            rows = rows[:limit]
        else:
            after = decode_cursor(cursor, scope, (is_stamp, is_id))
            if filters:
                rows, next_key = filtered_closed_page(db, after, limit, where, params, closed_after)
            else:
                rows = closed_page(db, after, limit + 1)
                next_key = closed_key(rows[limit - 1]) if len(rows) > limit else None
                rows = rows[:limit]
        context = dispatch_context(db, now)
    return {
        "items": [summary(row, now, context) for row in rows],
        "next_cursor": encode_cursor(scope, next_key) if next_key else None,
    }


@router.get("/operator/overview")
def overview(request: Request):
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    with read_transaction(request.app.state.database_path) as db:
        context = dispatch_context(db, now)
        open_counts = db.execute(
            f"""SELECT status, dependency_wait_json IS NOT NULL AS blocked, count(*) AS count
                FROM executions WHERE {OPEN} GROUP BY status, blocked"""
        ).fetchall()
        closed_counts = db.execute(
            f"""SELECT outcome, count(*) AS count FROM executions
                WHERE {CLOSED} AND updated_at >= ? GROUP BY outcome""",
            (stamp(now - timedelta(days=1)),),
        ).fetchall()
        oldest = db.execute(
            """SELECT min(ready_since) FROM executions
               WHERE status = 'queued' AND dependency_wait_json IS NULL AND ready_since <= ?""",
            (now_ms,),
        ).fetchone()[0]
    sections = {"needs_you": 0, "running": 0, "waiting_on_checks": 0, "queued": 0, "blocked": 0}
    for row in open_counts:
        if row["status"] == Status.QUEUED:
            sections["blocked" if row["blocked"] else "queued"] += row["count"]
        else:
            section = {Status.WAITING_FOR_INPUT: "needs_you", Status.RUNNING: "running",
                       Status.WAITING_FOR_EXTERNAL: "waiting_on_checks"}[row["status"]]
            sections[section] += row["count"]
    closed = {name: 0 for name in Outcome.__args__}
    closed.update({row["outcome"]: row["count"] for row in closed_counts})
    sections["delivered_24h"] = sum(closed[name] for name in DELIVERED)
    return {
        "generated_at": stamp(now),
        "sections": sections,
        "closed_24h": closed,
        "dispatch": {"paused": context["paused"]},
        "runners": {"online": context["online"], "known": context["known"]},
        "slots": {"used": context["slots_used"], "total": context["slots_total"]},
        "oldest_queued_since": (
            stamp(datetime.fromtimestamp(oldest / 1000, UTC)) if oldest is not None else None
        ),
        "oldest_queued_age_seconds": max(0, (now_ms - oldest) // 1000) if oldest is not None else None,
    }


def is_runner(value):
    return isinstance(value, str) and RUNNER_ID.fullmatch(value)


@router.get("/operator/runners")
def runners(request: Request, limit: Limit = 25, cursor: Cursor = None):
    after = decode_cursor(cursor, "runners", (is_runner,))
    now = datetime.now(UTC)
    with read_transaction(request.app.state.database_path) as db:
        context = dispatch_context(db, now)
        rows = db.execute(
            """WITH ids AS (
                   SELECT id FROM runners UNION SELECT runner_id FROM runner_limits
                   UNION SELECT runner_id FROM attempts WHERE ended_at IS NULL
               )
               SELECT ids.id, runner.slots, runner.last_seen_at, limits.capacity
               FROM ids LEFT JOIN runners AS runner ON runner.id = ids.id
               LEFT JOIN runner_limits AS limits ON limits.runner_id = ids.id
               WHERE ids.id > ? ORDER BY ids.id LIMIT ?""",
            (after[0] if after else "", limit + 1),
        ).fetchall()
        result = []
        for row in rows[:limit]:
            attempts = db.execute(
                """SELECT attempt.id AS attempt_id, execution.item_id, attempt.execution_id, attempt.stage,
                          attempt.lease_expires_at, attempt.started_at
                   FROM attempts AS attempt JOIN executions AS execution ON execution.id = attempt.execution_id
                   WHERE attempt.runner_id = ? AND attempt.ended_at IS NULL
                   ORDER BY attempt.started_at, attempt.id LIMIT 256""",
                (row["id"],),
            ).fetchall()
            limit_capacity = row["capacity"]
            capacity = limit_capacity if limit_capacity is not None else dispatch.DEFAULT_CAPACITY
            if row["slots"] is not None:
                capacity = min(capacity, row["slots"])
            result.append({
                "id": row["id"],
                "online": row["last_seen_at"] is not None and row["last_seen_at"] >= context["online_since"],
                "last_seen_at": row["last_seen_at"],
                "slots": row["slots"],
                "capacity_limit": limit_capacity,
                "capacity": capacity,
                "active": len(attempts),
                "attempts": [dict(attempt) for attempt in attempts],
            })
    return {"items": result, "next_cursor": page_cursor(rows, limit, "runners", lambda row: [row["id"]])}


CURRENT = "NOT EXISTS (SELECT 1 FROM executions AS later WHERE later.previous_id = execution.id)"


@router.get("/operator/groups/{label}")
def group(request: Request, label: Annotated[str, Path(pattern=LABEL_PATTERN)],
          limit: Limit = 25, cursor: Cursor = None):
    scope = f"group:{label}"
    after = decode_cursor(cursor, scope, (is_stamp, is_id))
    now = datetime.now(UTC)
    with read_transaction(request.app.state.database_path) as db:
        rollup = db.execute(
            f"""SELECT count(*) AS items,
                       coalesce(sum(NOT ({CLOSED})), 0) AS open,
                       sum(pr.additions) AS additions, sum(pr.deletions) AS deletions,
                       coalesce(sum(pr.execution_id IS NOT NULL
                                    AND (pr.additions IS NULL OR pr.deletions IS NULL)), 0)
                           AS size_unknown_count,
                       min(item.created_at) AS first_created_at,
                       max(CASE WHEN {CLOSED} THEN execution.updated_at END) AS last_closed_at,
                       min(CASE WHEN NOT ({CLOSED}) THEN execution.created_at END) AS oldest_open_since
                FROM work_items AS item JOIN executions AS execution ON execution.item_id = item.id
                LEFT JOIN operator_pr AS pr ON pr.execution_id = execution.id
                WHERE item.label = ? AND {CURRENT}""",
            (label,),
        ).fetchone()
        if not rollup["items"]:
            raise HTTPException(404, "No work items have this label")
        outcomes = db.execute(
            f"""SELECT execution.outcome, count(*) AS count
                FROM work_items AS item JOIN executions AS execution ON execution.item_id = item.id
                WHERE item.label = ? AND {CURRENT} AND execution.outcome IS NOT NULL
                GROUP BY execution.outcome""",
            (label,),
        ).fetchall()
        boundary = "AND (item.created_at, item.id) > (?, ?)" if after else ""
        rows = db.execute(
            f"""{SUMMARY} WHERE item.label = ? AND {CURRENT} {boundary}
                ORDER BY item.created_at, item.id LIMIT ?""",
            (label, *(after or ()), limit + 1),
        ).fetchall()
        context = dispatch_context(db, now)
    closed = {name: 0 for name in Outcome.__args__}
    closed.update({row["outcome"]: row["count"] for row in outcomes})
    return {
        "label": label,
        "rollup": {**dict(rollup), "outcomes": closed},
        "items": [summary(row, now, context) for row in rows[:limit]],
        "next_cursor": page_cursor(rows, limit, scope,
                                   lambda row: [row["item_created_at"], row["item_id"]]),
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
        now = datetime.now(UTC)
        context = dispatch_context(db, now)
    return {
        "item": summary(execution, now, context),
        "attempt": attempt["id"] if attempt else None,
        "prerequisites": [dict(row) for row in prerequisites],
        "question": question,
        "events": [dict(row) for row in rows[:limit]],
        "next_cursor": page_cursor(rows, limit, scope, lambda row: [row["version"], row["created_at"]]),
    }
