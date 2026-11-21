"""Continuous attended dispatch with independent slot, observation and lease lanes."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event
from time import monotonic, time
from urllib.parse import quote

from .attended import AttendedRunner, RunnerAPI, RunnerError, STAGES
from .attended_recovery import _matches
from .attended_recovery_service import RecoveryJournal, run_recovery
from .captain_adapter import CodexCLIAdapter, CommandTransport, ObservationBatch
from .workspaces import _file_lock


class CaptainLoop:
    def __init__(self, root: Path, config: dict, api, adapter: CodexCLIAdapter, *,
                 poll_seconds: float = 2, heartbeat_seconds: float = 20,
                 batch_size: int = 8, wait_seconds: float = 2, emit=None):
        if not 0 < poll_seconds <= 30 or not 0 < heartbeat_seconds <= 20:
            raise RunnerError("Poll interval must be at most 30s and heartbeat interval at most 20s")
        if type(batch_size) is not int or not 1 <= batch_size <= 32 or not 0 < wait_seconds <= 5:
            raise RunnerError("Native batches need 1-32 slots and a wait of at most 5s")
        if not isinstance(config.get("runner_id"), str) or not config["runner_id"]:
            raise RunnerError("Use one nonempty stable runner ID")
        self.runner = AttendedRunner(root, config, api)
        self.root, self.config, self.api, self.adapter = self.runner.root, config, api, adapter
        for mapping in config["repositories"].values():
            if self.root.is_relative_to(Path(mapping["checkout"]).resolve()):
                raise RunnerError("The private runner state must be outside mapped checkouts")
        self.poll_seconds, self.heartbeat_seconds = poll_seconds, heartbeat_seconds
        self.batch_size, self.wait_seconds = batch_size, wait_seconds
        self.emit = emit or (lambda event: print(json.dumps(event), flush=True))
        self.slot_pool = ThreadPoolExecutor(max_workers=256, thread_name_prefix="captain-slot")
        self.lease_pool = ThreadPoolExecutor(max_workers=256, thread_name_prefix="captain-lease")
        self.native_pool = ThreadPoolExecutor(max_workers=256, thread_name_prefix="captain-native")
        self.control_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="captain-recovery")
        self.work: dict[int, Future] = {}
        self.renewals: dict[int, tuple[Future, str]] = {}
        self.observations: dict[tuple[int, ...], Future] = {}
        self.pending_observations: dict[int, ObservationBatch] = {}
        self.work_due, self.lease_due, self.observe_due = {}, {}, {}
        self.recovery_future = None
        self.recovery_due = 0
        self.recovery_ready = False
        self.expired, self.unmatched = {}, []
        self.last_unmatched = None
        self.rotation = 0
        self.closed = False
        self._bind_identity()

    def _runner(self, slot):
        return AttendedRunner(self.root, self.config, self.api, slot=slot)

    def _records(self):
        with self.runner._journal() as db:
            return {slot: json.loads(row) for slot, row in db.execute("SELECT id, record FROM slot ORDER BY id")}

    def _bind_identity(self):
        with self.runner._journal() as db:
            db.execute("CREATE TABLE IF NOT EXISTS captain (key TEXT PRIMARY KEY, value TEXT)")
            expected = json.dumps({"runner_id": self.config["runner_id"], "base_url": self.config["base_url"]},
                                  sort_keys=True)
            row = db.execute("SELECT value FROM captain WHERE key='identity'").fetchone()
            records = [json.loads(data) for (data,) in db.execute("SELECT record FROM slot")]
            if (row and row[0] != expected) or any(
                record["runner_id"] != self.config["runner_id"] for record in records
            ):
                raise RunnerError("State directory belongs to another runner or API; restore its original configuration")
            db.execute("INSERT OR IGNORE INTO captain VALUES ('identity', ?)", (expected,))

    def _metadata(self, key, value=None):
        with self.runner._journal() as db:
            if value is None:
                row = db.execute("SELECT value FROM captain WHERE key=?", (key,)).fetchone()
                return json.loads(row[0]) if row else None
            db.execute("INSERT OR REPLACE INTO captain VALUES (?, ?)", (key, json.dumps(value)))

    def _status(self, slot, status, reason, *, lease=None):
        event = {"slot": slot, "status": status, "reason": reason,
                 "at": datetime.now(timezone.utc).isoformat()}
        def save(record, db):
            if record and (lease is None or record.get("lease") == lease):
                record["loop"] = event
        if slot:
            self._runner(slot)._transaction(save)
        self._metadata(f"status-{slot}", event)
        self.emit(event)

    def _prepare(self, slot):
        runner = self._runner(slot)
        original = runner._snapshot()
        if not original:
            capabilities = self.config.get("stages", sorted(set(STAGES) | {"pr_creation", "report_storage"}))
            if not capabilities or set(capabilities) - (set(STAGES) | {"pr_creation", "report_storage"}):
                raise RunnerError("Runner stage capabilities contain an unsupported stage")
            stages = [stage for stage in capabilities
                      if stage != "pr_creation" or os.environ.get("PHALANX_GITHUB_TOKEN")]
            if not stages:
                raise RunnerError("No configured stages are available; set PHALANX_GITHUB_TOKEN for initial drafts")
            # Report storage needs no desktop. Worker stages verify their exact
            # configured target before any new claim is admitted.
            if set(stages) & set(STAGES):
                verified = {}
                for repo, mapping in self.config["repositories"].items():
                    try:
                        self.adapter.resolve_project(mapping)
                    except (RunnerError, OSError) as error:
                        self.emit({"slot": slot, "status": "mapping_blocked", "repository": repo,
                                   "reason": str(error)})
                    else:
                        verified[repo] = mapping
                if not verified:
                    raise RunnerError("No exact desktop project and host passed discovery; restore the adapter or mappings")
                config = {**self.config, "repositories": verified}
                runner = AttendedRunner(self.root, config, self.api, slot=slot)
            result = runner.prepare(stages=stages)
        else:
            # A legacy or interrupted claim retains the original filters and
            # secret. Do not substitute this loop's capabilities on replay.
            result = runner.prepare()
        if result["status"] != "launch_intent":
            return result
        snapshot = runner._snapshot()
        self.adapter.resolve_project({
            field: snapshot[field] for field in ("host_id", "project_id", "checkout")
        })
        runner._verify_launch_claim(snapshot)
        runner.renew()
        def relay_intent(record, db):
            runner._ensure_reservation(record, snapshot["lease"])
            if record.get("receipt") or record.get("relay") or record.get("recover_stop"):
                raise RunnerError("Launch relay already attempted or owned by recovery; reconcile its exact marker")
            if record.get("packet") != snapshot["packet"]:
                raise RunnerError("The original launch packet changed; reconcile before any relay")
            record["relay"] = {"marker": result["marker"], "status": "started"}
        runner._transaction(relay_intent)
        receipt = self.adapter.launch(result)
        runner.receipt(result["marker"], result["host_id"], **receipt)
        return {"status": "receipt_recorded", "slot": slot, **receipt}

    def _advance(self, slot, snapshot, observation):
        runner = self._runner(slot)
        if not snapshot:
            return self._prepare(slot)
        if "submission" in snapshot:
            packet = snapshot.get("packet")
            if packet:
                receipt = snapshot["receipt"]
                return {"status": "submitted", "execution": runner.submit(
                    runner._handoff(snapshot).packet.marker, snapshot["host_id"], receipt["thread_id"],
                )}
            return self._prepare(slot)
        if observation is not None and (
            datetime.now(timezone.utc) - observation.observed_at
        ).total_seconds() > 60:
            raise RunnerError("Native observation aged while this slot was busy; read the exact writer again")
        if snapshot.get("recover_stop") or snapshot.get("claim", {}).get("attempt_id") in self.expired:
            return run_recovery(
                self.root, self.config["runner_id"], self.api, RecoveryJournal(self.root, slot),
                observations=observation.threads if observation else (),
                discovery_complete=observation.discovery_complete if observation else False,
            )
        if "packet" not in snapshot:
            return self._prepare(slot)
        if observation is None:
            return {"status": "observing", "reason": "Awaiting an exact native observation; launch is never replayed"}
        packet = runner._handoff(snapshot).packet
        matches = [row for row in observation.threads if _matches(snapshot, packet, row)]
        if len(matches) != 1 or (
            not snapshot.get("receipt", {}).get("thread_id") and not observation.discovery_complete
        ):
            raise RunnerError("Native discovery has not established one exact writer; retain this slot and inspect history")
        native = matches[0]
        runner.receipt(packet.marker, snapshot["host_id"], thread_id=str(native.thread_id))
        if native.state == "stopped":
            return {"status": "submitted", "execution": runner.submit(
                packet.marker, snapshot["host_id"], str(native.thread_id),
            )}
        return {"status": "observing", "reason": f"Exact native writer is {native.state}; reservation retained"}

    def _wait(self, slots, records):
        references = []
        for slot in slots:
            record = records[slot]
            references.append({
                "slot": slot, "host_id": record["host_id"], "project_id": record["project_id"],
                "started_at": record["started_at"], "marker": self._runner(slot)._handoff(record).packet.marker,
                "receipt": record.get("receipt"), "assignment": self._runner(slot)._handoff(record).packet.result_identity(),
            })
        cursors = {}
        for ref in references:
            saved = self._metadata(f"cursor-{ref['slot']}")
            if saved and saved["marker"] == ref["marker"] and saved.get("cursor"):
                cursors[ref["marker"]] = saved["cursor"]
        batch = self.adapter.wait_threads(references, cursors=cursors, timeout=self.wait_seconds)
        for ref in references:
            if ref["marker"] in batch.cursors:
                self._metadata(f"cursor-{ref['slot']}", {
                    "marker": ref["marker"], "cursor": batch.cursors[ref["marker"]],
                })
        return batch

    def step(self):
        """Nonblocking scheduling pass; slow operations never run on this lane."""
        now = monotonic()
        for slot, (future, lease) in list(self.renewals.items()):
            if future.done():
                del self.renewals[slot]
                try:
                    future.result()
                except Exception as error:
                    self._status(slot, "renewal_blocked", f"{type(error).__name__}: {error}; reconcile the lease",
                                 lease=lease)
        for slots, future in list(self.observations.items()):
            if future.done():
                del self.observations[slots]
                try:
                    batch = future.result()
                except Exception as error:
                    for slot in slots:
                        self._status(slot, "observation_blocked", f"{type(error).__name__}: {error}")
                else:
                    for slot in slots:
                        self.pending_observations[slot] = batch
        for slot, future in list(self.work.items()):
            if future.done():
                del self.work[slot]
                try:
                    result = future.result()
                except Exception as error:
                    self._status(slot, "slot_blocked", f"{type(error).__name__}: {error}; reconcile this reservation")
                else:
                    self._status(slot, result["status"], result.get("reason", "Operation recorded"))
        records = self._records()
        if self.recovery_future is not None and self.recovery_future.done():
            future, self.recovery_future = self.recovery_future, None
            try:
                rows = future.result()
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) or not isinstance(row.get("attempt_id"), str) for row in rows
                ):
                    raise RunnerError("Server recovery listing has an invalid attempt identity")
            except Exception as error:
                self.recovery_ready = False
                self._status(0, "recovery_blocked", f"{type(error).__name__}: {error}; new admission paused")
            else:
                self.expired = {row["attempt_id"]: row for row in rows}
                self.recovery_ready = True
        owned = {record["claim"]["attempt_id"] for record in records.values() if "claim" in record}
        self.unmatched = sorted(set(self.expired) - owned)
        if self.unmatched and self.recovery_ready and self.unmatched != self.last_unmatched:
            self._status(0, "unmatched_attempts", "Restore original reservations before admission: " + ", ".join(self.unmatched))
        self.last_unmatched = self.unmatched.copy()
        if self.recovery_future is None and now >= self.recovery_due:
            route = f"/dispatch/runners/{quote(self.config['runner_id'], safe='')}/recovery"
            self.recovery_future = self.control_pool.submit(self.api.call, route)
            self.recovery_due = now + self.poll_seconds
        for slot, record in records.items():
            if ("claim" in record and record.get("lease_expires_at", 0) > time()
                    and slot not in self.renewals and now >= self.lease_due.get(slot, 0)):
                self.renewals[slot] = (self.lease_pool.submit(self._runner(slot).renew), record["lease"])
                self.lease_due[slot] = now + self.heartbeat_seconds
            if slot not in self.work and now >= self.work_due.get(slot, 0):
                observation = self.pending_observations.pop(slot, None)
                self.work[slot] = self.slot_pool.submit(self._advance, slot, record, observation)
                self.work_due[slot] = now + self.poll_seconds
        observed_slots = {slot for slots in self.observations for slot in slots}
        candidates = [slot for slot, record in records.items()
                      if "packet" in record and "submission" not in record and slot not in observed_slots
                      and now >= self.observe_due.get(slot, 0)]
        if candidates:
            offset = self.rotation % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
            self.rotation += self.batch_size
            for start in range(0, len(candidates), self.batch_size):
                slots = tuple(candidates[start:start + self.batch_size])
                self.observations[slots] = self.native_pool.submit(self._wait, slots, records)
                for slot in slots:
                    self.observe_due[slot] = now + self.poll_seconds
        if self.recovery_ready and not self.unmatched:
            occupied = set(records) | set(self.work)
            room = max(0, self.runner.capacity - len(occupied))
            for slot in range(1, self.runner.capacity + 1):
                if room and slot not in occupied and now >= self.work_due.get(slot, 0):
                    self.work[slot] = self.slot_pool.submit(self._prepare, slot)
                    self.work_due[slot] = now + self.poll_seconds
                    room -= 1

    def run(self, stop: Event):
        # Only one loop relays launches from this journal. Manual receipt and
        # recovery commands still operate through short journal transactions.
        with _file_lock(self.root / "captain-loop.lock"):
            try:
                while not stop.is_set():
                    self.step()
                    stop.wait(0.05)
            finally:
                self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            for pool in (self.slot_pool, self.lease_pool, self.native_pool, self.control_pool):
                pool.shutdown(wait=True, cancel_futures=True)


def add_captain_command(commands):
    parser = commands.add_parser("captain", help="run the continuous attended Captain loop")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--heartbeat-seconds", type=float, default=20)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--wait-seconds", type=float, default=2)
    parser.add_argument("--adapter-command", required=True, nargs=argparse.REMAINDER,
                        help="trusted JSON bridge executable and arguments; put this option last")


def run_captain(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    token = os.environ.get("PHALANX_RUNNER_TOKEN")
    if not token:
        raise RunnerError("Set PHALANX_RUNNER_TOKEN in the runner environment")
    loop = CaptainLoop(
        args.state, config, RunnerAPI(config["base_url"], token),
        CodexCLIAdapter(CommandTransport(args.adapter_command)),
        poll_seconds=args.poll_seconds, heartbeat_seconds=args.heartbeat_seconds,
        batch_size=args.batch_size, wait_seconds=args.wait_seconds,
    )
    try:
        loop.run(Event())
    except KeyboardInterrupt:
        pass
