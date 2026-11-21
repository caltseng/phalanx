"""Execute one resumable recovery step against the shared attended journal."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from time import time
from urllib.parse import quote
from uuid import UUID

from .attended import RunnerAPI, RunnerError
from .attended_recovery import (
    NativeObservation, StoppedEvidence, acknowledge_stop, reconcile, unmatched_leases,
)
from .handoff import WorkerHandoff, InvalidResult, WorkPacket
from .workspaces import WorkspaceError, WorkspaceManager


class RecoveryJournal:
    """Read and compare-and-swap the runner's numbered SQLite reservations."""

    def __init__(self, root: Path, slot: int):
        self.path = root / "attended.sqlite3"
        self.slot = slot

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS slot (id INTEGER PRIMARY KEY, record TEXT)")
            yield db

    def snapshot(self):
        with self._transaction() as db:
            records = {slot: json.loads(row) for slot, row in db.execute("SELECT id, record FROM slot")}
        owned_attempts = {row["claim"]["attempt_id"] for row in records.values() if "claim" in row}
        return records.get(self.slot, {}), owned_attempts

    def save(self, expected, updated, *, acknowledgment=None):
        with self._transaction() as db:
            row = db.execute("SELECT record FROM slot WHERE id=?", (self.slot,)).fetchone()
            current = json.loads(row[0]) if row else {}
            if current != expected:
                return False
            if acknowledgment is not None:
                request = current.get("recover_stop")
                if not request or not acknowledge_stop(current, request, acknowledgment):
                    return False
                archive = {key: value for key, value in current.items() if key != "lease"}
                archive["recovery_acknowledgment"] = acknowledgment
                db.execute("CREATE TABLE IF NOT EXISTS completed (attempt_id TEXT PRIMARY KEY, record TEXT)")
                db.execute("INSERT INTO completed VALUES (?, ?)",
                           (request["attempt_id"], json.dumps(archive)))
                db.execute("DELETE FROM slot WHERE id=?", (self.slot,))
            elif current != updated:
                db.execute("UPDATE slot SET record=? WHERE id=?", (json.dumps(updated), self.slot))
            return True


def _verify_local(root, record, packet, thread_id):
    handoff = WorkerHandoff(packet, root / "results")
    disposition = "unresolved"
    checked = None
    summary = "Restore the registered worktree and verify the result file"
    try:
        manager = WorkspaceManager(
            Path(record["checkout"]), record["repositories"][packet.repository], root / "worktrees",
        )
        workspace = manager.inspect(packet.item_id)
        if workspace.path != packet.worktree or workspace.branch != packet.branch:
            raise WorkspaceError("The registered workspace differs from the packet")
        checked = {**asdict(workspace), "path": str(workspace.path)}
        result = handoff.read_result()
        unchanged = workspace.head == packet.expected_revision and not workspace.dirty
        if os.path.lexists(handoff.temporary_path):
            summary = "Inspect the incomplete attempt result beside the final result path"
        elif result is None and unchanged:
            disposition = "retry_safe"
            summary = "Registered worktree is clean at the original revision, with no attempt result"
        elif result is None:
            summary = "Preserve and reconcile local changes before allowing a replacement"
        elif (
            (packet.stage in {"report_drafting", "report_review"} and unchanged
             and result.outcome in {"succeeded", "changes_required", "needs_input", "failed"})
            or (packet.stage == "development" and result.outcome == "succeeded"
                and not workspace.dirty and workspace.head != packet.expected_revision
                and result.details["evidence"].strip() == workspace.head)
            or (packet.stage == "development" and result.outcome in {"needs_input", "failed"} and unchanged)
        ):
            disposition = "verified_result"
            summary = "Verified attempt result and registered workspace checkpoint"
            identity = packet.result_identity() | {"outcome": result.outcome, "details": result.details}
            revision = sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
            checked.update(result_path=str(handoff.result_path), outcome=result.outcome,
                           result_revision=revision)
        else:
            summary = "Reconcile the attempt result with the current worktree before adoption"
    except (InvalidResult, WorkspaceError):
        pass
    return (
        StoppedEvidence(packet.attempt_id, thread_id, str(packet.worktree), disposition, summary),
        checked,
    )


def run_recovery(root, runner_id, api, journal, *, observations=(), discovery_complete=False,
                 recheck=False, clock=time, no_launch_proof=None):
    """Return a Captain decision and execute at most one journaled stop request."""
    original, local_attempt_ids = journal.snapshot()
    route = f"/dispatch/runners/{quote(runner_id, safe='')}"
    expired = None
    if not original or original["runner_id"] == runner_id:
        try:
            expired = api.call(route + "/recovery")
        except RunnerError:
            pass
    orphaned = unmatched_leases(expired, local_attempt_ids) if expired is not None else []
    if not original:
        return {"status": "idle" if expired is not None else "retry",
                "reason": ("No local reservation in this slot" if expired is not None
                           else "Server recovery listing is unavailable"),
                "unmatched": orphaned}
    if original["runner_id"] != runner_id:
        return {"status": "owner_wait", "reason": "Use the original runner identity for this slot"}

    if no_launch_proof is not None and any(field in original for field in ("relay", "receipt", "submission")):
        raise RunnerError("No-launch proof cannot replace reconciliation of recorded launch effects")

    # Keep native observations, filesystem inspection and HTTP outside the lock.
    record = json.loads(json.dumps(original))
    saved = record.get("packet")
    packet = WorkPacket(**{
        **saved, "attempt_id": UUID(saved["attempt_id"]), "worktree": Path(saved["worktree"]),
        "dependencies": tuple(saved.get("dependencies", ())),
    }) if saved else None

    def decision(**kwargs):
        return reconcile(record, runner_id=runner_id, packet=packet, expired=expired,
                         observations=observations, discovery_complete=discovery_complete,
                         now=clock(), no_launch_proof=no_launch_proof, **kwargs)

    request = record.get("recover_stop")
    if (recheck and request and record.get("stop_rejected") and expired is not None
            and any(row["attempt_id"] == request["attempt_id"]
                    and row["version"] != request["body"]["expected_version"] for row in expired)):
        # A definite rejection allows a new intent for a changed version.
        # The core verifies the exact server and local identities again.
        del record["recover_stop"]
        del record["stop_rejected"]
        record.pop("stop_proof_stale", None)

    result = decision(recheck=recheck)
    if result["status"] == "verify_outputs":
        thread_id = UUID(record["receipt"]["thread_id"])
        evidence, checkpoint = _verify_local(root, record, packet, thread_id)
        result = decision(stopped=evidence, recheck=recheck and bool(record.get("stop_proof_stale")))
        record["recovery_checkpoint"] = {
            **asdict(evidence), "attempt_id": str(evidence.attempt_id),
            "thread_id": str(evidence.thread_id), "workspace": checkpoint,
        }
        if evidence.disposition == "verified_result":
            result["receipt"] = {"marker": packet.marker, "host_id": record["host_id"],
                                 "thread_id": str(thread_id)}
            if result["status"] == "reconcile_server":
                result["next_action"] = "Verify the lease, then use attended submit with this receipt"
    if result["status"] == "send_stop" and record["recover_stop"] != request:
        intent = record["recover_stop"]
        version = intent["body"]["expected_version"]
        intent["key"] = f"{intent['key']}-{version}"
    if not journal.save(original, record):
        return {"status": "retry", "reason": "Reservation changed during recovery", "unmatched": orphaned}
    if result["status"] != "send_stop":
        if "recovery_checkpoint" in record:
            result["checkpoint"] = record["recovery_checkpoint"]
        return {**result, "unmatched": orphaned}

    request = record["recover_stop"]
    endpoint = f"{route}/attempts/{quote(request['attempt_id'], safe='')}/recover-stopped"
    try:
        response = api.call(endpoint, request["body"], key=request["key"])
    except RunnerError as error:
        failure = "conflict" if str(error).startswith("Runner API returned HTTP 409 ") else "unavailable"
    else:
        if isinstance(response, dict) and acknowledge_stop(record, request, response):
            if journal.save(record, record, acknowledgment=response):
                return {"status": "recovered", "attempt_id": request["attempt_id"],
                        "execution": response, "unmatched": orphaned}
            return {"status": "retry", "reason": "Reservation changed before acknowledgment",
                    "unmatched": orphaned}
        failure = "unavailable"
    saved_intent = json.loads(json.dumps(record))
    if failure == "conflict":
        record["stop_rejected"] = True
    result = decision(stop_failure=failure)
    if not journal.save(saved_intent, record):
        return {"status": "retry", "reason": "Reservation changed during stop replay", "unmatched": orphaned}
    return {**result, "unmatched": orphaned}


def add_recovery_command(commands):
    command = commands.add_parser("attended-recover", help="reconcile one attended reservation")
    command.add_argument("--config", required=True, type=Path)
    command.add_argument("--state", required=True, type=Path)
    command.add_argument("--slot", type=int, default=1)
    command.add_argument("--observation", type=Path, help="fresh Captain observation JSON")
    command.add_argument("--recheck", action="store_true", help="resume an owner wait after inspection")


def run_recovery_command(args):
    token = os.environ.get("PHALANX_RUNNER_TOKEN")
    if not token:
        raise RunnerError("Set PHALANX_RUNNER_TOKEN in the runner environment")
    if not 1 <= args.slot <= 256:
        raise RunnerError("Slot must be an integer from 1 to 256")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    native = json.loads(args.observation.read_text(encoding="utf-8")) if args.observation else {}
    observations = []
    complete = False
    if native:
        observed_at = datetime.fromisoformat(native["observed_at"])
        if observed_at.utcoffset() is None or not 0 <= time() - observed_at.timestamp() <= 60:
            raise RunnerError("Supply a fresh Captain observation with a UTC offset")
        complete = native.get("discovery_complete") is True
        for row in native["threads"]:
            started_at = datetime.fromisoformat(row["started_at"])
            if started_at.utcoffset() is None or row["state"] not in {"active", "stopped", "unknown"}:
                raise RunnerError("Native rows need an offset-aware start and a recognized state")
            observations.append(NativeObservation(**{
                **row, "started_at": started_at, "thread_id": UUID(row["thread_id"]),
                "client_thread_id": UUID(row["client_thread_id"]) if row.get("client_thread_id") else None,
            }))
    root = args.state.resolve(strict=True)
    result = run_recovery(
        root, config["runner_id"], RunnerAPI(config["base_url"], token), RecoveryJournal(root, args.slot),
        observations=observations, discovery_complete=complete, recheck=args.recheck,
    )
    print(json.dumps(result, ensure_ascii=False))
