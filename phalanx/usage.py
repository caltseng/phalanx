"""Optional cumulative writer observations, never lifecycle or admission evidence."""

from datetime import UTC, datetime
from decimal import Decimal
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from . import intake, writers
from .auth import require_coordinator, require_writer
from .intake_api import run
from .intake_models import Identifier, Payload
from .usage_models import Snapshot, TOKEN_FIELDS


class CaptainObservation(Payload):
    captain_id: Identifier
    snapshot: Snapshot


def observe(db, writer, snapshot):
    if not writer["confirmed_at"] or (
            writer["runtime"], writer["session_id"]) != (snapshot.runtime, snapshot.session_id):
        raise intake.Conflict("Usage requires this writer's confirmed runtime and native session")
    if snapshot.scope == "item_exclusive" and snapshot.window_start < datetime.fromisoformat(writer["created_at"]):
        raise intake.Conflict("Item-exclusive window cannot predate writer allocation")
    if writer["ended_at"] and snapshot.cutoff > datetime.fromisoformat(writer["ended_at"]):
        raise intake.Conflict("Late observation must retain a cutoff within ended writer ownership")
    encoded = snapshot.model_dump(mode="json")
    existing = db.execute("SELECT * FROM writer_usage WHERE writer_id=?", (writer["id"],)).fetchone()
    previous = json.loads(existing["snapshot_json"]) if existing and existing["snapshot_json"] else None
    failure = json.loads(existing["failure_json"]) if existing and existing["failure_json"] else None
    latest = failure or previous
    if latest:
        if snapshot.sequence <= existing["sequence"]:
            if encoded == latest:
                return {"accepted": False, "reason": "Identical cumulative snapshot already observed"}
            raise intake.Conflict("Stale or conflicting usage sequence; read the latest observation")
        if snapshot.observed_at < datetime.fromisoformat(existing["observed_at"]):
            raise intake.Conflict("Out-of-order usage observation")
        for field in ("runtime", "provider", "account", "origin", "session_id", "source",
                      "epoch", "model", "window_start", "scope"):
            if encoded[field] != latest[field]:
                raise intake.Conflict("Usage series changed; preserve its original attribution and epoch")
    if previous:
        if snapshot.cutoff < datetime.fromisoformat(previous["cutoff"]):
            raise intake.Conflict("Usage cutoff moved backwards")
        if snapshot.status != "unavailable":
            for name in TOKEN_FIELDS:
                old, new = previous["tokens"][name], encoded["tokens"][name]
                if old is not None and (new is None or new < old):
                    raise intake.Conflict("Cumulative counters decreased or disappeared; report a coverage gap")
            for name in ("credits", "money"):
                old, new = previous[name], encoded[name]
                if old is not None:
                    if (new is None or any(new[key] != old[key] for key in ("unit", "basis", "source"))
                            or Decimal(new["amount"]) < Decimal(old["amount"])):
                        raise intake.Conflict("Cumulative quantity changed units, basis or decreased")
    unavailable = snapshot.status == "unavailable"
    db.execute(
        """INSERT INTO writer_usage VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(writer_id) DO UPDATE SET sequence=excluded.sequence,
             observed_at=excluded.observed_at, snapshot_json=excluded.snapshot_json,
             failure_json=excluded.failure_json""",
        (writer["id"], snapshot.sequence, encoded["observed_at"],
         json.dumps(previous if unavailable else encoded) if previous or not unavailable else None,
         json.dumps(encoded) if unavailable else None),
    )
    return {"accepted": True, "status": snapshot.status}


def own_observation(db, writer_id, snapshot):
    writer = writers.writer_row(db, writer_id)
    if writer["ended_at"]:
        raise intake.Conflict("The writer has ended")
    return observe(db, writer, snapshot)


def captain_observation(db, item_id, execution_id, writer_id, body):
    writer = writers.bound_writer(db, item_id, execution_id, writer_id)
    captain = writers.live_captain(db, body.captain_id)
    repository = db.execute("SELECT repository_id FROM work_items WHERE id=?", (item_id,)).fetchone()[0]
    if repository not in json.loads(captain["pickup_json"])["repositories"]:
        raise HTTPException(403, "Usage observation is outside this Captain's repository scope")
    return observe(db, writer, body.snapshot)


def projection(db, item_id, execution_id):
    rows = db.execute(
        """SELECT writer.id, writer.execution_id, writer.runtime, writer.session_id,
                  writer.ended_at, usage.snapshot_json, usage.failure_json,
                  (SELECT count(*) FROM writers AS other
                   WHERE other.session_id=writer.session_id) AS bindings
           FROM writers AS writer LEFT JOIN writer_usage AS usage ON usage.writer_id=writer.id
           WHERE writer.item_id=? ORDER BY writer.created_at, writer.id""", (item_id,),
    ).fetchall()
    sessions = []
    for row in rows:
        snapshot = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
        failure = json.loads(row["failure_json"]) if row["failure_json"] else None
        included = bool(snapshot and snapshot["scope"] == "item_exclusive" and row["bindings"] == 1)
        status = snapshot["status"] if snapshot else "unavailable"
        reason = snapshot["reason"] if snapshot else "No authorized native usage observation; retry at an ordinary runtime observation"
        if snapshot and status == "observed" and (
                not row["ended_at"] or datetime.fromisoformat(snapshot["cutoff"]) <
                datetime.fromisoformat(row["ended_at"])):
            status = "pending_final"
            reason += "; final ownership boundary not observed"
        if snapshot and (failure or not included):
            status = "partial"
            reason = (failure["reason"] if failure else
                      "Session lifetime or reused native session; excluded from attributable subtotal")
        sessions.append({
            "writer_id": row["id"], "execution_id": row["execution_id"],
            "runtime": row["runtime"], "session_id": row["session_id"],
            "ended_at": row["ended_at"], "included": included, "status": status, "reason": reason,
            "snapshot": snapshot, "acquisition_error": failure,
        })

    def subtotal(selected):
        included = [row for row in selected if row["included"]]
        tokens, covered = {}, {}
        for name in TOKEN_FIELDS:
            values = [row["snapshot"]["tokens"][name] for row in included
                      if row["snapshot"]["tokens"][name] is not None]
            tokens[name] = sum(values) if values else None
            covered[name] = len(values)
        statuses = {row["status"] for row in selected}
        status = ("unavailable" if not included else "pending_final" if statuses == {"pending_final"}
                  else "observed" if statuses == {"observed"} and len(included) == len(selected)
                  else "partial")
        return {"tokens": tokens, "field_sessions": covered, "covered_sessions": len(included),
                "bound_sessions": len(selected), "status": status}

    current = [row for row in sessions if row["execution_id"] == execution_id]
    return {
        "execution": subtotal(current), "item": subtotal(sessions),
        "sessions": sessions, "overhead": "Captain/shared reviewer usage unallocated; not included",
        "credits": "Unavailable unless explicitly supplied by an attributable provider source; never inferred from tokens",
        "capabilities": {
            "copilot-app": "Read-only native session metadata transport; no automatic CLI/SDK attachment or credit feed",
            "codex-app": "Automatic App-bound export unverified; authorized native observation required",
        },
    }


router = APIRouter(prefix="/v1")


@router.post("/writer/usage", dependencies=[Depends(require_writer)])
def post_own(request: Request, body: Snapshot):
    return run(request, own_observation, request.state.writer["id"], body, write=True)


@router.post("/items/{item_id}/executions/{execution_id}/writers/{writer_id}/usage",
             dependencies=[Depends(require_coordinator)])
def post_captain(request: Request, item_id: Identifier, execution_id: Identifier,
                 writer_id: Identifier, body: CaptainObservation):
    return run(request, captain_observation, item_id, execution_id, writer_id, body, write=True)
