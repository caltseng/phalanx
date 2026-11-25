"""Owner inbox and replies."""

import json

from . import intake
from .transitions import Event


def inbox(db, limit, after_time, after_id):
    rows = db.execute(
        """SELECT queued.*, history.details_json FROM (
               SELECT id, item_id, stage, version, updated_at, state_json
               FROM executions WHERE status = 'waiting_for_input' AND (updated_at, id) > (?, ?)
               ORDER BY updated_at, id LIMIT ?
           ) AS queued
           JOIN transition_history AS history
               ON history.execution_id=queued.id AND history.version=queued.version
           ORDER BY queued.updated_at, queued.id""",
        (after_time, after_id, limit),
    )
    return [{
        "item_id": row["item_id"], "execution_id": row["id"], "stage": row["stage"],
        "version": row["version"], "waiting_since": row["updated_at"],
        "wait": json.loads(row["state_json"])["wait"],
        "question": json.loads(row["details_json"]).get("question"),
    } for row in rows]


def reply(db, item_id, execution_id, payload):
    row, state = intake.check_current(db, item_id, execution_id, payload.expected_version)
    details = {"answer": payload.answer, "answered_by": payload.answered_by,
               "wait_version": row["version"]}
    waiting = db.execute(
        "SELECT details_json FROM transition_history WHERE execution_id=? AND version=?",
        (execution_id, row["version"]),
    ).fetchone()
    details["question"] = json.loads(waiting["details_json"]).get("question")
    return intake.apply(db, row, state, Event.REPLY, details=details)
