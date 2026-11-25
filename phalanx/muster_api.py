"""Authenticated, read-only snapshot of the direct components."""

from datetime import UTC, datetime
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from .auth import require_coordinator
from .captains import live_captains
from .reconciliation import RECONCILER_STALE
from .store import read_transaction


router = APIRouter(prefix="/v1/muster", dependencies=[Depends(require_coordinator)])


@router.get("/status")
def status(request: Request):
    now = datetime.now(UTC)
    with read_transaction(request.app.state.database_path) as db:
        captains = [
            {key: row[key] for key in ("id", "session_ref", "state", "heartbeat_at")}
            for _, row in sorted(live_captains(db, now).items())
        ]
        writers = [dict(row) for row in db.execute(
            """SELECT writer.id, writer.item_id, writer.execution_id, writer.captain_id,
                      writer.state, writer.session_id, writer.session_url, writer.last_progress_at
               FROM writers AS writer JOIN executions AS execution ON execution.id=writer.execution_id
               WHERE writer.ended_at IS NULL AND writer.abandoned_at IS NULL
                 AND execution.status NOT IN ('completed', 'failed', 'cancelled')
               ORDER BY writer.id""")]
        reconcilers = []
        for row in db.execute("SELECT runner_id, last_seen FROM reconciler_heartbeats ORDER BY runner_id"):
            age = max(0, (now - datetime.fromisoformat(row["last_seen"])).total_seconds())
            reconcilers.append({**dict(row), "age_seconds": age,
                                "stale": age >= RECONCILER_STALE.total_seconds()})
        paused = bool(db.execute("SELECT paused FROM dispatch_settings WHERE id=1").fetchone()["paused"])
    return {"status": "ok", "identity": {"home": request.app.state.muster_home},
            "captains": captains, "writers": writers, "reconcilers": reconcilers,
            "dispatch_paused": paused, "observed_at": now.isoformat()}


def configured_home():
    home = os.environ.get("PHALANX_HOME")
    return str(Path(home).expanduser().resolve()) if home and Path(home).expanduser().is_absolute() else None
