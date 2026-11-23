"""Execute one resumable recovery step against the shared attended journal."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import time
from urllib.parse import quote
from uuid import UUID

from .attended import AttendedRunner, RunnerAPI, RunnerError, development_candidate
from .attended_recovery import (
    StoppedEvidence, acknowledge_claim, acknowledge_stop, native_observation,
    native_timestamp, reconcile, unmatched_leases,
)
from .handoff import WorkerHandoff, InvalidResult, WorkPacket
from .workspaces import WorkspaceError, WorkspaceManager, _file_lock, _git


# Any of these means a launch or recovery effect may exist, so a no-launch
# proof must not stand in for reconciling it.
LAUNCH_EVIDENCE = ("relay", "receipt", "submission", "recover_stop", "recover_claim", "cancel_stop")


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
            # Development alone commits to the checkout; every other worker stage leaves it unchanged.
            (unchanged and (packet.stage != "development" or result.outcome in {"needs_input", "failed"}))
            or (packet.stage == "development" and result.outcome == "succeeded"
                and development_candidate(record, packet, workspace, result.details["evidence"].strip()))
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

    if no_launch_proof is not None and any(field in original for field in ("relay", "receipt", "submission",
                                                                           "recover_claim")):
        raise RunnerError("No-launch proof cannot replace reconciliation of recorded launch effects")

    # Keep native observations, filesystem inspection and HTTP outside the lock.
    record = json.loads(json.dumps(original))
    saved = record.get("packet")
    packet = WorkPacket(**{
        **saved, "attempt_id": UUID(saved["attempt_id"]), "worktree": Path(saved["worktree"]),
        "dependencies": tuple(saved.get("dependencies", ())),
        "checks_command": tuple(saved.get("checks_command", ())),
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
    if result["status"] == "send_claim":
        return {**_send_claim(api, route, journal, record, decision), "unmatched": orphaned}
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


def _send_claim(api, route, journal, record, decision):
    # The intent and its fresh secret are durable before the request, so a
    # lost reply replays the same key and lease.
    request = record["recover_claim"]
    endpoint = f"{route}/attempts/{quote(request['attempt_id'], safe='')}/recover-claim"
    try:
        response = api.call(endpoint, request["body"], lease=request["lease"], key=request["key"])
    except RunnerError as error:
        failure = "conflict" if str(error).startswith("Runner API returned HTTP 409 ") else "unavailable"
    else:
        if acknowledge_claim(record, request, response):
            adopted = {key: value for key, value in record.items() if key not in {"recover_claim", "recovery"}}
            adopted.update(lease=request["lease"], lease_expires_at=response["lease_expires_at"])
            if journal.save(record, adopted):
                return {"status": "adopted", "attempt_id": request["attempt_id"],
                        "reason": "Adopted the exact attempt under a fresh lease",
                        "lease_expires_at": response["lease_expires_at"]}
            return {"status": "retry", "reason": "Reservation changed before adoption was saved"}
        failure = "unavailable"
    saved_intent = json.loads(json.dumps(record))
    result = decision(claim_failure=failure)
    if not journal.save(saved_intent, record):
        return {"status": "retry", "reason": "Reservation changed during adoption replay"}
    return result


def unlaunched_proof(runner, snapshot):
    """Return a no-launch proof for a reservation, or raise if evidence of a launch exists."""
    claim = snapshot["claim"]
    attempt = UUID(claim["attempt_id"])
    for identity in (str(attempt), attempt.hex):
        result = runner.results / f"phalanx-attempt-{identity}.json"
        if os.path.lexists(result) or os.path.lexists(result.with_suffix(".json.tmp")):
            raise RunnerError("Attempt evidence exists; reconcile it before confirming stop or launch")
    repo = claim["request"]["repository_id"]
    mapping = runner.config["repositories"][repo]
    manager = runner._manager(mapping, snapshot["repositories"][repo])
    common = manager._verify_checkout()
    with _file_lock(common / "phalanx-workspaces.lock"):
        path, branch, owner = manager._location(common, claim["item_id"])
        if manager._registered(path, branch):
            workspace = manager._snapshot(path, branch, common)
            base = claim["request"].get("target_branch") or mapping["base_branch"]
            packet = runner._handoff(snapshot).packet if "packet" in snapshot else None
            candidate = claim["execution"]["state"].get("candidate")
            expected = packet.expected_revision if packet else candidate or manager.resolve_origin_base(base)
            if packet and (packet.worktree != workspace.path or packet.branch != workspace.branch):
                raise RunnerError("Prepared packet does not match the owned worktree; reconcile it")
            accounted = workspace.head == expected
            if not accounted and not packet and not candidate and not workspace.dirty:
                accounted = _git(manager.checkout, "merge-base", workspace.head, expected).strip() == workspace.head
            if workspace.dirty or not accounted:
                raise RunnerError("Unlaunched workspace has unaccounted changes; preserve it and reconcile stop")
            return f"Unlaunched clean workspace {path} at accounted revision {workspace.head}"
        refs = _git(manager.checkout, "for-each-ref", "--format=%(refname)", owner,
                    f"refs/heads/{branch}").strip()
        if refs:
            raise RunnerError("Partial workspace refs exist; reconcile them before confirming stop")
        return "No item worktree, owned branch, launch packet, relay, receipt or result"


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
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    native = json.loads(args.observation.read_text(encoding="utf-8")) if args.observation else {}
    observations = []
    complete = False
    if native:
        try:
            observed_at = native_timestamp(native["observed_at"])
            if not 0 <= time() - observed_at.timestamp() <= 60:
                raise ValueError("stale")
        except (KeyError, ValueError, TypeError) as error:
            raise RunnerError("Supply a fresh Captain observation with a UTC offset") from error
        complete = native.get("discovery_complete") is True
        for row in native["threads"]:
            try:
                observations.append(native_observation(row, observed_at))
            except (KeyError, ValueError, TypeError, AttributeError) as error:
                raise RunnerError(f"Malformed native observation row: {error}") from error
    root = args.state.resolve(strict=True)
    api = RunnerAPI(config["base_url"], token)
    journal = RecoveryJournal(root, args.slot)
    snapshot, _ = journal.snapshot()
    proof = None
    if ("claim" in snapshot and snapshot.get("runner_id") == config["runner_id"]
            and not any(field in snapshot for field in LAUNCH_EVIDENCE)):
        # Release an unlaunched slot only after checking its worktree and result path.
        proof = unlaunched_proof(AttendedRunner(root, config, api, slot=args.slot), snapshot)
    result = run_recovery(
        root, config["runner_id"], api, journal,
        observations=observations, discovery_complete=complete, recheck=args.recheck,
        no_launch_proof=proof,
    )
    print(json.dumps(result, ensure_ascii=False))
