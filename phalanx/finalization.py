"""Server-owned report finalization, independent of worker claims."""

import asyncio
import json
import logging
import sqlite3
from time import time

from . import intake
from .store import connect, write_transaction
from .transitions import TERMINAL, Event, Evidence, Status, StoredReport, Wait


logger = logging.getLogger(__name__)
BACKOFF_SECONDS = (2, 4, 8, 16)
BATCH_SIZE = 32


def _closure_report(db, execution_id, state, details):
    attempts = db.execute(
        """SELECT stage, count(*) AS count FROM attempts WHERE execution_id=?
           GROUP BY stage ORDER BY stage""",
        (execution_id,),
    )
    attempted = ", ".join(f"{row['stage']} ({row['count']})" for row in attempts) or "No stages claimed"
    title = "Cancellation" if state.pending_output == Status.CANCELLED else "Failure"
    return (
        f"# {title} report\n\n"
        f"Last stage: {state.stage}\n\n"
        f"Reason: {details['reason']}\n\n"
        f"Stages attempted (attempt counts): {attempted}\n\n"
        f"Partial PR: {state.primary_pr or 'None recorded'}\n\n"
        f"Candidate: {state.candidate or 'None recorded'}\n\n"
        f"Report draft revision: {state.report_draft or 'None recorded'}\n\n"
        "Next action: Review the execution timeline and any partial artifacts. "
        "Resolve the reason for stopping before rerunning the item.\n"
    )


def _finish(db, row, state, job):
    revision = None
    if state.pending_output == Status.COMPLETED:
        revision = state.report_draft
        markdown = intake.draft(db, row["item_id"], row["id"], revision)
        if intake.digest(markdown) != revision:
            raise intake.Conflict("Reviewed report draft does not match its revision")
    else:
        intent = db.execute(
            "SELECT details_json FROM transition_history WHERE execution_id=? AND version=?",
            (row["id"], job["intent_version"]),
        ).fetchone()
        markdown = _closure_report(db, row["id"], state, json.loads(intent["details_json"]))
    existing = db.execute("SELECT markdown FROM reports WHERE execution_id=?", (row["id"],)).fetchone()
    if existing is None:
        db.execute("INSERT INTO reports VALUES (?, ?)", (row["id"], markdown))
    elif existing["markdown"] != markdown:
        raise intake.Conflict("Existing report differs from the intended final Markdown")
    output = StoredReport(f"/v1/items/{row['item_id']}/executions/{row['id']}/report", revision)
    updated = intake.apply(db, row, state, Event.FINALIZE, details={"report": output.reference},
                           evidence=Evidence(stored_report=output))
    if updated["state"]["status"] in TERMINAL:
        intake.end_writers(db, row["id"], updated["state"]["status"])
    db.execute("DELETE FROM report_finalizations WHERE execution_id=?", (row["id"],))
    return updated


def _record_failure(db, row, state, job, now, error):
    failures = job["failures"] + 1
    due = now + BACKOFF_SECONDS[failures - 1] if failures <= len(BACKOFF_SECONDS) else None
    reason = (getattr(error, "sqlite_errorname", "SQLite write error")
              if isinstance(error, sqlite3.Error) else str(error))
    db.execute("UPDATE report_finalizations SET failures=?, due_at=? WHERE execution_id=?",
               (failures, due, row["id"]))
    if due is None:
        wait = Wait(
            f"Final report storage stalled after {failures} failures ({reason}). "
            "Check database access and report integrity, then retry finalization.",
            "owner retry finalization",
        )
    else:
        wait = Wait(f"Final report storage failed ({reason}). Retrying automatically.",
                    "scheduled finalization retry")
    return intake.apply(db, row, state, Event.FINALIZE, wait=wait,
                        details={"failure": reason, "failures": failures, "retry_at_unix": due})


def attempt(path, execution_id):
    """Process a due intent once. Return its latest snapshot, if present."""
    try:
        with write_transaction(path) as db:
            row = db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None:
                return None
            now = int(time())
            job = db.execute(
                "SELECT * FROM report_finalizations WHERE execution_id=? AND due_at<=?",
                (execution_id, now),
            ).fetchone()
            if job is None:
                return intake.execution_view(row)
            state = intake.STATE.validate_json(row["state_json"])
            db.execute("SAVEPOINT finalize_report")
            try:
                return _finish(db, row, state, job)
            except (sqlite3.Error, intake.Missing, intake.Conflict) as error:
                db.execute("ROLLBACK TO finalize_report")
                db.execute("RELEASE finalize_report")
                return _record_failure(db, row, state, job, now, error)
    except sqlite3.Error:
        # The store may be too unavailable to record a failed retry. The
        # previously committed intent remains due when the store recovers.
        logger.exception("Finalization could not access the store for execution %s", execution_id)
        return None


def retry(db, item_id, execution_id, payload):
    row, state = intake.check_current(db, item_id, execution_id, payload.expected_version)
    updated = db.execute(
        """UPDATE report_finalizations SET failures=0, due_at=0
           WHERE execution_id=? AND due_at IS NULL""",
        (execution_id,),
    )
    if not updated.rowcount:
        raise intake.Conflict("Only stalled finalization can be manually retried")
    return intake.apply(db, row, state, Event.FINALIZE, details={"owner_retry": True},
                        wait=Wait("Store the final Markdown report", "scheduled finalization retry"))


def process_due(path):
    with connect(path, mode="ro") as db:
        due = db.execute(
            """SELECT execution_id FROM report_finalizations WHERE due_at<=?
               ORDER BY due_at, execution_id LIMIT ?""",
            (int(time()), BATCH_SIZE),
        ).fetchall()
    for row in due:
        attempt(path, row["execution_id"])


async def run(path):
    while True:
        try:
            await asyncio.to_thread(process_due, path)
        except sqlite3.Error:
            logger.exception("Could not read the report finalization queue")
        await asyncio.sleep(1)
