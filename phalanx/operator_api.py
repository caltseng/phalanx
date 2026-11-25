"""Read-only, execution-scoped views for the operator board."""

from base64 import b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse, Response

from . import dispatch, pickup
from .auth import authenticate_board_read
from .captains import availability_reports
from .display_text import reason_text, short_title
from .flags import LAUNCH_UNCONFIRMED, launch_wait
from .intake import report as stored_report
from .intake_api import run
from .intake_models import LABEL_PATTERN, Identifier
from .operator_schema import ACTIVE_STARTED_AT, CLOSED, INPUT_RANK, OPEN
from .session_links import session_key
from .store import read_transaction
from .transitions import Stage, Status

router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate_board_read)])
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00")
ID = re.compile(r"[0-9a-f]{32}")
PR_NUMBER = re.compile(r"#?([0-9]{1,18})")
CLOSED_STATUSES = {Status.COMPLETED, Status.FAILED, Status.CANCELLED}
DELIVERED = ("report_stored", "draft_published", "ready_for_check_in", "checked_in")
Outcome = Literal["report_stored", "draft_published", "ready_for_check_in", "checked_in", "failed", "cancelled"]
# Filtered pages inspect at most this many indexed candidates before returning a continuation.
SCAN_LIMIT = 2000
REASON_LIMIT = 400
SHA = re.compile(r"[0-9a-f]{7,64}")

SUMMARY = f"""
    SELECT execution.id, execution.item_id, execution.version, execution.created_at, execution.updated_at,
           execution.stage, execution.status, execution.state_json, execution.retry_at, execution.handoff_head,
           execution.ready_since, item.created_at AS item_created_at, item.label,
           item.repository_id, json_extract(item.request_json, '$.goal') AS goal,
           title.title AS display_title,
           execution.queue_affinity AS runner_affinity, execution.dependency_wait_json,
           json_extract(item.request_json, '$.target') AS target,
           progress.last_advanced_at, progress.wait_since, progress.revision_round,
           {ACTIVE_STARTED_AT} AS active_started_at,
           pr.url AS pr_url, pr.number AS pr_number, pr.state AS pr_state, pr.draft AS pr_draft,
           pr.merged AS pr_merged, pr.head_sha AS pr_head_sha, pr.additions AS pr_additions,
           pr.deletions AS pr_deletions, pr.ci AS pr_ci, pr.ci_head_sha AS pr_ci_head_sha,
           pr.duplicate_count,
           coalesce(json_array_length(execution.dependency_wait_json), 0) AS blocked_count,
           writer.id AS writer_id, writer.state AS writer_state, writer.runtime AS writer_runtime,
           writer.captain_id AS writer_captain_id, writer.session_id AS writer_session_id,
           writer.session_url AS writer_session_url, writer.last_progress_at AS writer_progress_at,
           writer.progress_note AS writer_progress_note, writer.ended_at AS writer_ended_at,
           writer.nonce AS writer_nonce, writer.created_at AS writer_created_at,
           writer.confirmed_at AS writer_confirmed_at, writer.abandoned_at AS writer_abandoned_at,
           writer.building AS writer_building, writer.building_requested_at,
           (SELECT json_extract(observation.observation_json, '$.deployment')
            FROM github_observations AS observation WHERE observation.execution_id = execution.id
            ORDER BY observation.last_observed_at DESC, observation.observed_at DESC LIMIT 1) AS deployment_json,
           (SELECT attempt.id FROM attempts AS attempt
            WHERE attempt.execution_id = execution.id AND attempt.ended_at IS NULL) AS stage_owner
    FROM executions AS execution
    JOIN work_items AS item ON item.id = execution.item_id
    LEFT JOIN item_titles AS title ON title.item_id = item.id AND title.revision = (
        SELECT max(revision) FROM item_titles WHERE item_id = item.id
    )
    JOIN operator_progress AS progress ON progress.execution_id = execution.id
    LEFT JOIN operator_pr AS pr ON pr.execution_id = execution.id
    LEFT JOIN (
        SELECT execution_id, max(rowid) AS latest_row FROM writers GROUP BY execution_id
    ) AS latest_writer ON latest_writer.execution_id = execution.id
    LEFT JOIN writers AS writer ON writer.rowid = latest_writer.latest_row
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


def applied_sha(details):
    for value in (details.get("candidate"), details.get("prevalidated_candidate"),
                  details.get("reviewed_candidate"), (details.get("pull") or {}).get("head_sha")):
        if isinstance(value, str) and SHA.fullmatch(value):
            return value
    return None


def result_reason(details):
    """Project only persisted cause fields, never task bodies or arbitrary result evidence."""
    parts = [reason_text(details[field], 40) for field in ("outcome", "issue") if details.get(field)]
    checks = []
    for check in details.get("local_checks") or ():
        if check.get("outcome") == "failed":
            cause = f": {reason_text(check['reason'], 160)}" if check.get("reason") else ""
            checks.append(f"local check {reason_text(check['name'], 80)} failed{cause}")
    if checks:
        parts.append(reason_text("; ".join(checks), 180))
    if details.get("failure_reason"):
        parts.append(reason_text(details["failure_reason"], 140))
    sha = applied_sha(details)
    suffix = ("; " if parts else "") + f"at {sha[:12]}" if sha else ""
    text = reason_text("; ".join(parts), REASON_LIMIT - len(suffix))
    return text + suffix if text or suffix else None


def timeline_event(row):
    event = dict(row)
    details = json.loads(event.pop("details") or "{}")
    event["candidate"] = applied_sha(details)
    event["pr_url"] = details.get("pr_url")
    if event["event"] == "result" and event["reason"] is None:
        event["reason"] = result_reason(details)
    return event


def stamp(moment):
    return moment.isoformat(timespec="microseconds")


def dispatch_context(db, now):
    return {"pickup": pickup.context(db, now), "session_reports": availability_reports(db)}


def conditional_read(request, response, db, now, context=None):
    # Weak validators ignore ticking display ages, but not deadlines that change row meaning.
    token = db.execute("SELECT token FROM operator_change").fetchone()[0]
    deadlines = tuple(db.execute(
        f"""SELECT
            (SELECT max(retry_at) FROM executions WHERE {OPEN} AND retry_at <= ?),
            (SELECT max(ready_since) FROM executions WHERE {OPEN} AND ready_since <= ?),
            (SELECT max(created_at) FROM writers
             WHERE ended_at IS NULL AND confirmed_at IS NULL AND created_at <= ?),
            (SELECT max(updated_at) FROM executions WHERE {CLOSED} AND updated_at < ?)""",
        (int(now.timestamp() * 1000), int(now.timestamp() * 1000),
         stamp(now - LAUNCH_UNCONFIRMED), stamp(now - timedelta(days=1))),
    ).fetchone())
    context = dispatch_context(db, now) if context is None else context
    holds = tuple(sorted(context["pickup"]["holds"]))
    sessions = tuple(sorted(context["session_reports"].items(), key=repr))
    live_pickup = context["pickup"]["captains"]
    key = f"{token}:{request.url.path}?{request.url.query}:{deadlines}:{holds}:{sessions}:{live_pickup}"
    etag = 'W/"' + sha256(key.encode()).hexdigest() + '"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    supplied = request.headers.get("if-none-match", "")
    tags = {tag.strip().removeprefix("W/") for tag in supplied.split(",")}
    if "*" in tags or etag.removeprefix("W/") in tags:
        return Response(status_code=304, headers=dict(response.headers))
    return None


def summary_wait(row, state, now, context):
    """Select pickup, Building, handoff, persisted, dependency, queue, then launch waits."""
    status = row["status"]
    wait_since = row["wait_since"]
    current = context["pickup"]
    if status == Status.QUEUED and (row["writer_id"] is None or row["writer_ended_at"] is not None):
        pickup_state = pickup.decision(row, now, current)
        wait = ({} if pickup_state["eligible"] else
                {"reason": pickup_state["reason"], "trigger": pickup_state["trigger"]})
        return wait, wait_since, pickup_state
    if (row["building_requested_at"] and not row["writer_building"] and not row["blocked_count"]
            and status in (Status.RUNNING, Status.QUEUED, Status.WAITING_FOR_EXTERNAL)):
        return {
            "reason": dispatch.building_wait(
                None, row["writer_id"], capacity=current["capacity"], rework=current["rework"])
            or "Building capacity available; retry acquisition",
            "trigger": "The same writer retries `work capacity building` before any rework",
        }, wait_since, None
    if row["stage"] == Stage.DELIVERY and status == Status.WAITING_FOR_EXTERNAL:
        if row["writer_building"]:
            return {
                "reason": "Writer is building or reviewing PR rework",
                "trigger": "The writer commits reviewed work and releases Building for publication",
            }, wait_since, None
        if row["handoff_head"] and row["pr_head_sha"] != row["handoff_head"]:
            return {
                "reason": f"Awaiting publication handoff {row['handoff_head']}",
                "trigger": "Publish the reviewed head, review a superseding head, or stop and withdraw the writer",
            }, wait_since, None
    if state.get("wait"):
        return state["wait"], wait_since, None
    if row["blocked_count"]:
        noun = "prerequisite" if row["blocked_count"] == 1 else "prerequisites"
        return {
            "reason": f"Waiting for {row['blocked_count']} {noun}", "trigger": "Prerequisite milestone",
        }, wait_since, None
    if status == Status.QUEUED:
        if current["capacity"]["paused"]:
            return {"reason": "Dispatch is paused", "trigger": "The owner resumes dispatch"}, wait_since, None
        retry_at = row["retry_at"]
        if retry_at is not None and retry_at > int(now.timestamp() * 1000):
            at = datetime.fromtimestamp(retry_at / 1000, UTC).isoformat()
            return {
                "reason": f"Retry scheduled for {at}", "trigger": "The retry delay elapses",
            }, wait_since, None
        hold = current["holds"].get(row["id"])
        if hold:
            return {"reason": hold, "trigger": "A Captain launches it or the hold expires"}, wait_since, None
    if (status == Status.RUNNING and not state.get("stopping") and row["writer_id"]
            and row["writer_ended_at"] is None and row["writer_confirmed_at"] is None):
        return (launch_wait(row["writer_id"], row["writer_nonce"], row["writer_created_at"], now),
                row["writer_created_at"], None)
    return {}, wait_since, None


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
    wait, wait_since, pickup_state = summary_wait(row, state, now, context)
    output = state["final_output"]
    output_revision = output.get("revision") if output is not None else None
    outcome = None
    delivery = None
    if output is not None:
        if row["status"] != "completed":
            outcome = row["status"]
        elif "reference" in output:
            outcome = "report_stored"
        else:
            outcome = ("checked_in" if output.get("merge_commit") else
                       "draft_published" if output.get("draft") else "ready_for_check_in")
        if output.get("merge_commit"):
            # What GitHub delivered, distinct from the Phalanx stage that recorded it.
            delivery = {"merged": True, "merge_commit": output["merge_commit"],
                        "merge_method": output.get("merge_method"), "merged_at": output.get("merged_at")}
        output = (
            {"kind": "report", "url": output["reference"]} if "reference" in output
            else {"kind": "pr", "url": output["url"]}
        )
    blocked_count = row["blocked_count"]
    status = row["status"]
    next_action = None
    if output is None and not wait:
        if status == Status.QUEUED:
            next_action = "Next to run: a Captain dispatches a writer"
        elif status == Status.RUNNING:
            next_action = ("Stopping the worker" if state.get("stopping")
                           else f"A worker is running the {row['stage']} stage")
        else:
            next_action = f"{status.replace('_', ' ').capitalize()}; the {row['stage']} stage resumes when it clears"
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "version": row["version"],
        "title": row["display_title"] or short_title(row["goal"]),
        "goal": row["goal"],
        "label": row["label"],
        "target": state["target"],
        "publication_mode": state.get("publication_mode", "ready"),
        "repository_id": row["repository_id"],
        "created_at": row["created_at"],
        "active_started_at": row["active_started_at"],
        "active_age_seconds": (
            max(0, int(((datetime.fromisoformat(row["updated_at"]) if output is not None else now)
                        - datetime.fromisoformat(row["active_started_at"])).total_seconds()))
            if row["active_started_at"] else None
        ),
        "stage_started_at": (
            max(row["last_advanced_at"], row["active_started_at"])
            if row["active_started_at"] else None
        ),
        "stage": row["stage"],
        "status": status,
        "revision_round": row["revision_round"],
        "last_advanced_at": row["last_advanced_at"],
        "last_progress_at": row["updated_at"],
        "wait_reason": wait.get("reason"),
        "pickup": pickup_state,
        "blocked_prerequisite_count": blocked_count,
        "resume_trigger": wait.get("trigger"),
        "next_action": next_action,
        "retry_at_ms": row["retry_at"],
        "stage_owner": row["stage_owner"],
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
        "output_revision": output_revision,
        "delivery": delivery,
        "deployment": json.loads(row["deployment_json"]) if row["deployment_json"] else None,
        # Ending a writer does not delete its host-app session.
        "writer": None if row["writer_id"] is None else {
            "id": row["writer_id"],
            "state": "ended" if row["writer_ended_at"] else row["writer_state"],
            "runtime": row["writer_runtime"], "ended_at": row["writer_ended_at"],
            "captain_id": row["writer_captain_id"], "session_id": row["writer_session_id"],
            "session_url": row["writer_session_url"], "last_progress_at": row["writer_progress_at"],
            "progress_note": row["writer_progress_note"],
            "nonce": row["writer_nonce"], "created_at": row["writer_created_at"],
            "confirmed_at": row["writer_confirmed_at"], "abandoned_at": row["writer_abandoned_at"],
            "building": bool(row["writer_building"]),
            "building_requested_at": row["building_requested_at"],
            "session_availability": context.get("session_reports", {}).get(
                session_key(row["writer_session_id"], row["writer_runtime"]), "unverified"),
        },
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
                OR instr(lower(title.title), lower(?)) > 0
                OR execution.item_id = ? OR execution.id = ? OR pr.url = ? OR pr.number = ?)"""
        )
        params.extend((query, query, query, query, query, int(number[1]) if number else None))
    return "".join(f" AND {clause}" for clause in clauses), params


def open_page(db, after, size, where="", params=(), *, select=SUMMARY, through=None):
    # Separate rank buckets let SQLite seek directly to the timestamp and ID.
    # Filtered callers also provide an indexed ceiling for their candidate window.
    first_rank = after[0] if after else 0
    rows = []
    for rank in range(first_rank, 2):
        if through and rank > through[0]:
            break
        boundary, bounds = "", ()
        if after and rank == first_rank:
            boundary = "AND (execution.created_at, execution.id) > (?, ?)"
            bounds = after[1:]
        ceiling, ceiling_bounds = "", ()
        if through and rank == through[0]:
            ceiling = "AND (execution.created_at, execution.id) <= (?, ?)"
            ceiling_bounds = through[1:]
        rows.extend(db.execute(
            f"""{select} WHERE {OPEN} AND ({INPUT_RANK}) = ? {boundary} {ceiling} {where}
                ORDER BY execution.created_at, execution.id LIMIT ?""",
            (rank, *bounds, *ceiling_bounds, *params, size - len(rows)),
        ))
        if len(rows) == size:
            break
    return rows


def filtered_open_page(db, after, size, where, params):
    keys = open_page(db, after, SCAN_LIMIT, select=(
        "SELECT id, created_at, status FROM executions AS execution"
    ))
    if not keys:
        return [], None
    last = open_key(keys[-1])
    rows = open_page(db, after, size + 1, where, params, through=last)
    if len(rows) > size:
        return rows[:size], open_key(rows[size - 1])
    return rows, last if len(keys) == SCAN_LIMIT else None


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
    request: Request, response: Response, kind: Literal["open", "closed"] = "open", limit: Limit = 25, cursor: Cursor = None,
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
    patterns = (is_rank, is_stamp, is_id) if kind == "open" else (is_stamp, is_id)
    after = decode_cursor(cursor, scope, patterns)
    where, params = filter_clause(filters)
    now = datetime.now(UTC)
    with read_transaction(request.app.state.database_path) as db:
        context = dispatch_context(db, now)
        unchanged = conditional_read(request, response, db, now, context)
        if unchanged is not None:
            return unchanged
        if kind == "open":
            if filters:
                rows, next_key = filtered_open_page(db, after, limit, where, params)
            else:
                rows = open_page(db, after, limit + 1)
                next_key = open_key(rows[limit - 1]) if len(rows) > limit else None
                rows = rows[:limit]
        else:
            if filters:
                rows, next_key = filtered_closed_page(db, after, limit, where, params, closed_after)
            else:
                rows = closed_page(db, after, limit + 1)
                next_key = closed_key(rows[limit - 1]) if len(rows) > limit else None
                rows = rows[:limit]
    return {
        "items": [summary(row, now, context) for row in rows],
        "next_cursor": encode_cursor(scope, next_key) if next_key else None,
    }


@router.get("/operator/overview")
def overview(request: Request, response: Response):
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    with read_transaction(request.app.state.database_path) as db:
        context = dispatch_context(db, now)
        unchanged = conditional_read(request, response, db, now, context)
        if unchanged is not None:
            return unchanged
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
        "dispatch": {"paused": context["pickup"]["capacity"]["paused"]},
        "oldest_queued_since": (
            stamp(datetime.fromtimestamp(oldest / 1000, UTC)) if oldest is not None else None
        ),
        "oldest_queued_age_seconds": max(0, (now_ms - oldest) // 1000) if oldest is not None else None,
    }


CURRENT = "NOT EXISTS (SELECT 1 FROM executions AS later WHERE later.previous_id = execution.id)"


def brief(db, item_id):
    row = db.execute(
        """SELECT json_extract(request_json, '$.acceptance_criteria') AS criteria,
                  json_extract(request_json, '$.context_links') AS context
           FROM work_items WHERE id=?""", (item_id,),
    ).fetchone()
    return {"acceptance_criteria": json.loads(row["criteria"] or "[]"),
            "context_links": json.loads(row["context"] or "[]")}


@router.get("/operator/items/{item_id}")
def current_item(request: Request, response: Response, item_id: Identifier):
    now = datetime.now(UTC)
    with read_transaction(request.app.state.database_path) as db:
        if db.execute("SELECT 1 FROM work_items WHERE id=?", (item_id,)).fetchone() is None:
            raise HTTPException(404, "Item not found")
        context = dispatch_context(db, now)
        unchanged = conditional_read(request, response, db, now, context)
        if unchanged is not None:
            return unchanged
        row = db.execute(f"{SUMMARY} WHERE execution.item_id = ? AND {CURRENT}", (item_id,)).fetchone()
        work_brief = brief(db, item_id)
    return {"item": {**summary(row, now, context), **work_brief}}


@router.get("/operator/groups/{label}")
def group(request: Request, response: Response, label: Annotated[str, Path(pattern=LABEL_PATTERN)],
          limit: Limit = 25, cursor: Cursor = None):
    scope = f"group:{label}"
    after = decode_cursor(cursor, scope, (is_stamp, is_id))
    now = datetime.now(UTC)
    with read_transaction(request.app.state.database_path) as db:
        if db.execute("SELECT 1 FROM work_items WHERE label=? LIMIT 1", (label,)).fetchone() is None:
            raise HTTPException(404, "No work items have this label")
        context = dispatch_context(db, now)
        unchanged = conditional_read(request, response, db, now, context)
        if unchanged is not None:
            return unchanged
        boundary = "AND (item.created_at, item.id) > (?, ?)" if after else ""
        rows = db.execute(
            f"""WITH group_page AS MATERIALIZED (
                    SELECT item.id FROM work_items AS item WHERE item.label = ? {boundary}
                    ORDER BY item.created_at, item.id LIMIT ?
                )
                {SUMMARY} WHERE item.id IN (SELECT id FROM group_page) AND {CURRENT}
                ORDER BY item.created_at, item.id""",
            (label, *(after or ()), limit + 1),
        ).fetchall()
    closed = {name: 0 for name in Outcome.__args__}
    page = rows[:limit]
    items = [summary(row, now, context) for row in page]
    for item in items:
        if item["outcome"]:
            closed[item["outcome"]] += 1

    def known_sum(column):
        values = [row[column] for row in page if row[column] is not None]
        return sum(values) if values else None

    rollup = {
        "scope": "page",
        "items": len(page),
        "open": sum(row["status"] not in CLOSED_STATUSES for row in page),
        "additions": known_sum("pr_additions"),
        "deletions": known_sum("pr_deletions"),
        "size_unknown_count": sum(row["pr_url"] is not None and
                                  (row["pr_additions"] is None or row["pr_deletions"] is None)
                                  for row in page),
        "first_created_at": min((row["item_created_at"] for row in page), default=None),
        "last_closed_at": max((row["updated_at"] for row in page
                               if row["status"] in CLOSED_STATUSES), default=None),
        "oldest_open_since": min((row["created_at"] for row in page
                                 if row["status"] not in CLOSED_STATUSES), default=None),
        "outcomes": closed,
    }
    return {
        "label": label,
        "rollup": rollup,
        "items": items,
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
        relatives = db.execute(
            """SELECT previous_id,
                      (SELECT id FROM executions AS later WHERE later.previous_id = execution.id) AS next_id
               FROM executions AS execution WHERE id = ?""",
            (execution_id,),
        ).fetchone()
        prerequisites = db.execute(
            """SELECT json_extract(block.value, '$.upstream_id') AS item_id,
                      coalesce((SELECT title FROM item_titles WHERE item_id = upstream.id
                                ORDER BY revision DESC LIMIT 1),
                               json_extract(upstream.request_json, '$.goal')) AS title,
                      json_extract(block.value, '$.milestone') AS milestone,
                      json_extract(block.value, '$.reason') AS reason
               FROM executions AS execution
               JOIN json_each(execution.dependency_wait_json) AS block
               JOIN work_items AS upstream ON upstream.id = json_extract(block.value, '$.upstream_id')
               WHERE execution.id = ?
               ORDER BY block.key""",
            (execution_id,),
        ).fetchall()
        boundary, params = ("WHERE (events.version, events.created_at) < (?, ?)", after) if after else ("", ())
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
                           AS answered_by,
                       event.details_json AS details
                FROM transition_history AS event
                LEFT JOIN transition_history AS prior
                    ON prior.execution_id = event.execution_id AND prior.version = event.version - 1
                WHERE event.execution_id = ?
                UNION ALL
                SELECT observation.version, 'github_observation', observation.observed_at, NULL,
                       json_extract(observed.state_json, '$.stage'),
                       json_extract(observed.state_json, '$.status'),
                       NULL, coalesce(
                           'Observed ' || json_extract(observation.observation_json, '$.pr.url')
                               || ' ' || json_extract(observation.observation_json, '$.pr.state')
                               || ' at ' || observation.head_sha,
                           'Observed no PR for the item branch'), NULL, NULL
                FROM github_observations AS observation
                JOIN transition_history AS observed
                    ON observed.execution_id = observation.execution_id
                   AND observed.version = observation.version
                WHERE observation.execution_id = ?
                UNION ALL
                SELECT result.version, 'local_checks', result.checked_at, NULL,
                       json_extract(checked.state_json, '$.stage'),
                       json_extract(checked.state_json, '$.status'),
                       NULL, CASE WHEN json_array_length(result.failures_json) = 0
                                  THEN 'Local checks passed on ' ELSE 'Local checks failed on ' END
                           || result.head_sha, NULL, NULL
                FROM local_check_results AS result
                JOIN transition_history AS checked
                    ON checked.execution_id = result.execution_id AND checked.version = result.version
                WHERE result.execution_id = ?
                UNION ALL
                SELECT action.version, 'captain_action', action.created_at, NULL,
                       json_extract(acted.state_json, '$.stage'),
                       json_extract(acted.state_json, '$.status'),
                       NULL, 'Captain ' || substr(action.captain_id, 1, 8) || ' ' || action.action
                           || ': ' || action.reason, NULL, NULL
                FROM captain_actions AS action
                JOIN transition_history AS acted
                    ON acted.execution_id = action.execution_id AND acted.version = action.version
                WHERE action.execution_id = ?
            )
            SELECT events.* FROM events
            {boundary}
            ORDER BY events.version DESC, events.created_at DESC LIMIT ?""",
            (execution_id, execution_id, execution_id, execution_id, *params, limit + 1),
        ).fetchall()
        from .usage import projection
        usage = projection(db, execution["item_id"], execution_id)
        now = datetime.now(UTC)
        context = dispatch_context(db, now)
        work_brief = brief(db, execution["item_id"])
    return {
        "item": {**summary(execution, now, context), **work_brief},
        "usage": usage,
        "attempt": attempt["id"] if attempt else None,
        "previous_execution_id": relatives["previous_id"],
        "next_execution_id": relatives["next_id"],
        "prerequisites": [{**row, "title": short_title(row["title"])} for row in map(dict, prerequisites)],
        "question": question,
        "events": [timeline_event(row) for row in rows[:limit]],
        "next_cursor": page_cursor(rows, limit, scope, lambda row: [row["version"], row["created_at"]]),
    }
