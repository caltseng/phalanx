"""Restart decisions for the attended journal and dispatch recovery API.

Run under the local reservation transaction. The caller obtains native and Git
evidence outside that transaction, rechecks the reservation before applying it,
and persists any returned stop request before sending it.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from .codex_handoff import WorkPacket


RECHECK_SECONDS = (2, 4, 8, 16)


@dataclass(frozen=True, slots=True)
class NativeObservation:
    """A fresh read of the native thread and its original assignment."""

    host_id: str
    thread_id: UUID
    project_id: str
    started_at: datetime
    marker: str
    assignment: dict
    state: Literal["active", "stopped", "unknown"]
    client_thread_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class StoppedEvidence:
    """Local Git and output checks for the exact attempt after its writer stopped.

    The caller verifies workspace registration, branch, checkpoint and effects.
    Missing or ambiguous output belongs in 'unresolved'. An absent result can
    be 'retry_safe' only after checkpoints and effects have been accounted for.
    """

    attempt_id: UUID
    thread_id: UUID
    worktree: str
    disposition: Literal["retry_safe", "verified_result", "unresolved"]
    summary: str


def _matches(record, packet, observation):
    receipt = record.get("receipt", {})
    thread_id = receipt.get("thread_id")
    queued_id = receipt.get("client_thread_id")
    return (
        observation.host_id == record["host_id"]
        and observation.project_id == record["project_id"]
        and observation.started_at >= datetime.fromisoformat(record["started_at"])
        and observation.marker == packet.marker
        and observation.assignment == packet.result_identity()
        and all(type(observation.assignment[key]) is type(value)
                for key, value in packet.result_identity().items())
        and (not thread_id or str(observation.thread_id) == thread_id)
        and (thread_id or not queued_id or str(observation.client_thread_id) == queued_id)
    )


def reconcile(record, *, runner_id, packet: WorkPacket | None, expired,
              observations=(), discovery_complete=False, stopped=None, now, recheck=False,
              stop_failure: Literal["unavailable", "conflict"] | None = None):
    """Update the journal record and return a redacted decision for the Captain.

    'expired' is a fresh server recovery listing. None means unavailable.
    'discovery_complete' means all scoped pages were checked for this probe.
    An empty listing never establishes that an attempt ended. Native status is
    trusted only for the exact launch, and silence never establishes stop.
    """
    if not record:
        return {"status": "idle"}
    if record["runner_id"] != runner_id:
        return {"status": "owner_wait", "reason": "Open the original runner's local journal"}
    claim = record.get("claim")
    if claim is None:
        return {"status": "replay_claim", "reason": "Replay the original claim using its saved secret"}
    attempt = claim["attempt_id"]
    if packet is not None:
        serialized = {**asdict(packet), "attempt_id": str(packet.attempt_id),
                      "worktree": str(packet.worktree)}
        saved = record.get("packet") or {}
        saved = {**saved, "dependencies": tuple(saved.get("dependencies", ()))}
        if (saved != serialized or packet.attempt_id.hex != attempt.replace("-", "")
                or packet.item_id != claim["item_id"]
                or packet.stage != claim["execution"]["state"]["stage"]
                or packet.state_version != claim["execution"]["version"]
                or packet.dependencies != tuple(claim.get("dependencies", ()))):
            return {"status": "owner_wait", "reason": "Restore the exact packet from the original reservation"}
    elif "packet" in record:
        return {"status": "owner_wait", "reason": "Load the original launch packet before recovery"}

    progress = record.setdefault("recovery", {})

    def publish(status, reason, **details):
        progress.pop("next_action", None)
        if status != "retry":
            progress.pop("retry_at", None)
        progress.update(status=status, reason=reason, checked_at=now)
        if "next_action" in details:
            progress["next_action"] = details["next_action"]
        return {"attempt_id": attempt, **progress, **details}

    def uncertain(reason):
        failures = progress.get("failures", 0)
        if failures == len(RECHECK_SECONDS):
            return publish("owner_wait", reason, next_action=(
                "Inspect the original Codex launch and workspace, then explicitly recheck this reservation"
            ))
        progress.update(failures=failures + 1, retry_at=now + RECHECK_SECONDS[failures])
        return publish("retry", reason)

    if recheck:
        progress.clear()
    if "recover_stop" in record and stop_failure == "conflict":
        return publish("owner_wait", "The server rejected the saved stopped-attempt request",
                       next_action="Reconcile the exact attempt and version before retrying")
    if "recover_stop" in record and stop_failure == "unavailable":
        return uncertain("The stopped-attempt response is unavailable")
    if "recover_stop" in record and packet:
        states = {obs.state for obs in observations if _matches(record, packet, obs)}
        if states - {"stopped"}:
            record["stop_proof_stale"] = True
        if record.get("stop_proof_stale"):
            if states != {"stopped"} or not recheck:
                return publish("owner_wait", "A fresh observation invalidated the saved stop proof",
                               next_action="Verify the exact native writer has stopped and recheck")
            if stopped is None:
                return publish("verify_outputs", "The exact writer stopped again",
                               next_action="Reverify the worktree and its outputs")
            if (stopped.attempt_id != packet.attempt_id
                    or str(stopped.thread_id) != record["receipt"]["thread_id"]
                    or stopped.worktree != str(packet.worktree)
                    or stopped.disposition != "retry_safe"):
                return publish("owner_wait", "The prior stopped checkpoint could not be reverified",
                               next_action="Account for the changed worktree or result before replay")
            del record["stop_proof_stale"]

    if progress.get("status") == "owner_wait" and (
        "recover_stop" in record or progress.get("failures") == len(RECHECK_SECONDS)
    ):
        return {"attempt_id": attempt, **progress}
    if progress.get("status") == "retry" and now < progress["retry_at"]:
        return {"attempt_id": attempt, **progress}

    if "recover_stop" in record:
        return publish("send_stop", "Replay the original stopped-attempt request",
                       **record["recover_stop"])

    candidate = None
    if expired is not None:
        candidate = next((row for row in expired if row["attempt_id"] == attempt), None)
    if candidate is not None and (
        candidate["item_id"] != claim["item_id"]
        or candidate["execution_id"] != claim["execution"]["id"]
        or candidate["stage"] != claim["execution"]["state"]["stage"]
    ):
        return publish("owner_wait", "Server recovery identity conflicts with the local claim",
                       next_action="Inspect the server attempt and restore the matching local journal")

    if "submission" in record:
        if candidate is not None:
            return publish("owner_wait", "Saved submission needs reconciliation under a fresh claim",
                           next_action="Replay the exact submission receipt before arranging adoption")
        return publish("replay_submission", "Replay the original verified submission and idempotency key")

    if packet:
        matches = [obs for obs in observations if _matches(record, packet, obs)]
        if not record.get("receipt", {}).get("thread_id") and not discovery_complete:
            return uncertain("Native discovery is incomplete")
        if len(matches) != 1:
            return uncertain("Could not verify one exact native thread for the launch")
        observation = matches[0]
        record.setdefault("receipt", {})["thread_id"] = str(observation.thread_id)
        if observation.state == "active":
            progress.clear()
            return publish("reattach", "The recorded writer is active",
                           next_action="Resume observing the exact thread and renew while authorized")
        if observation.state != "stopped":
            return uncertain("The exact native thread has no verified stopped turn")
        if (stopped is None or stopped.attempt_id != packet.attempt_id
                or stopped.thread_id != observation.thread_id
                or stopped.worktree != str(packet.worktree)):
            return publish("verify_outputs", "The exact writer stopped",
                           next_action="Verify its workspace, checkpoint and outputs")
        if stopped.disposition not in {"retry_safe", "verified_result"} or not stopped.summary.strip() or len(stopped.summary) > 15800:
            return uncertain("Local checkpoint or output reconciliation is incomplete")
    else:
        # The launch protocol cannot call Codex before committing the packet.
        stopped = None

    if expired is None:
        return uncertain("Server recovery listing is unavailable")
    if candidate is None:
        progress.clear()
        return publish("reconcile_server", "The server has no matching expired attempt",
                       next_action="Replay a saved submission or verify the current lease and execution")
    if stopped and stopped.disposition == "verified_result":
        progress.clear()
        return publish("owner_wait", "Verified result needs a server-authorized recovery claim",
                       next_action="Preserve the evidence and arrange adoption under a fresh claim")

    proof = (f"Exact stopped native thread {observation.thread_id}. {stopped.summary}"
             if stopped else "The original local reservation has no durable launch intent.")
    request = {
        "attempt_id": attempt,
        "key": f"attended-stop-{attempt}",
        "body": {"item_id": claim["item_id"], "expected_version": candidate["version"],
                 "reconciliation": proof},
    }
    record["recover_stop"] = request
    progress.clear()
    return publish("send_stop", "Stopped reservation is ready for server reconciliation", **request)


def acknowledge_stop(record, request, response):
    """Validate the reply before the journal owner archives the reservation."""
    claim = record.get("claim")
    if claim is None or record.get("recover_stop") != request:
        return False
    execution_id = claim["execution"]["id"]
    if response.get("id") != execution_id:
        return False
    version = request["body"]["expected_version"]
    state = response.get("state", {})
    status = state.get("status")
    if response.get("version") == version + 1:
        return status in {"queued", "waiting_for_input", "waiting_for_external"}
    if response.get("version") != version + 2 or status not in {"failed", "cancelled"}:
        return False
    # The stop can enqueue a report and the HTTP handler can finalize it
    # before replying. The report reference must belong to this execution.
    output = state.get("final_output")
    item_id = claim["item_id"]
    return isinstance(output, dict) and output.get("reference") == (
        f"/v1/items/{item_id}/executions/{execution_id}/report"
    )


def unmatched_leases(expired, local_attempt_ids):
    """Report server attempts without local ownership evidence. Never auto-stop them."""
    return [
        {"attempt_id": row["attempt_id"], "item_id": row["item_id"], "status": "owner_wait",
         "reason": "The expired lease has no matching local reservation",
         "next_action": "Restore the original journal or locate and account for the original writer"}
        for row in expired if row["attempt_id"] not in local_attempt_ids
    ]
