"""Continuous attended dispatch with independent slot, observation and lease lanes."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from threading import Event
from time import monotonic, time
from urllib.parse import quote
from uuid import UUID

from .attended import AttendedRunner, RunnerAPI, RunnerError, STAGES
from .attended_recovery import _matches
from .attended_recovery_service import RecoveryJournal, _verify_local, run_recovery
from .captain_adapter import CodexCLIAdapter, CommandTransport, LaunchNotDelivered, ObservationBatch
from .captain_relay import RelayTransport
from .runner_preflight import preflight
from .workspaces import _file_lock, _git, _item_branch


_LAUNCH_EFFECTS = ("relay", "receipt", "submission", "recover_stop")


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
        self.admission, self.cursors = {}, {}
        self.discovery_future = None
        self.discovery_due = 0
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
            db.execute("DELETE FROM captain WHERE key LIKE 'admission-%' OR key LIKE 'cursor-%'")
            for slot, data in db.execute("SELECT id, record FROM slot").fetchall():
                record = json.loads(data)
                if "loop" in record:
                    del record["loop"]
                    db.execute("UPDATE slot SET record=? WHERE id=?", (json.dumps(record), slot))

    def _status(self, slot, status, reason, *, lease=None):
        with self.runner._journal() as db:
            if lease is not None:
                row = db.execute("SELECT record FROM slot WHERE id=?", (slot,)).fetchone()
                if row is None or json.loads(row[0]).get("lease") != lease:
                    return
            key = f"status-{slot}"
            row = db.execute("SELECT value FROM captain WHERE key=?", (key,)).fetchone()
            event = {"slot": slot, "status": status, "reason": reason}
            if row and all(json.loads(row[0]).get(field) == value for field, value in event.items()):
                return
            event["at"] = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT OR REPLACE INTO captain VALUES (?, ?)", (key, json.dumps(event)))
        self.emit(event)

    @staticmethod
    def _mapping_key(mapping):
        return tuple(mapping[field] for field in ("host_id", "project_id", "checkout"))

    def _discover(self, records):
        mappings = list(self.config["repositories"].values())
        mappings.extend(record for record in records.values() if "packet" in record)
        verified = {}
        for mapping in mappings:
            key = self._mapping_key(mapping)
            if key in verified:
                continue
            try:
                self.adapter.resolve_project(mapping)
            except (RunnerError, OSError) as error:
                verified[key] = str(error)
            else:
                verified[key] = None
        return verified

    def _prepare(self, slot, discovery=None):
        runner = self._runner(slot)
        original = runner._snapshot()
        if discovery is None:
            discovery = self.native_pool.submit(self._discover, self._records())
        if not original:
            runner, result = self._claim_new(slot, discovery)
        elif "packet" in original:
            if any(field in original for field in (*_LAUNCH_EFFECTS, "cancel_stop")):
                raise RunnerError("Launch relay already attempted or owned by recovery; reconcile its exact marker")
            self._unlaunched_proof(runner, original)
            handoff = runner._handoff(original)
            if not handoff.packet.worktree.is_dir():
                raise RunnerError("Prepared worktree is missing; restore it before launch")
            result = {
                "status": "launch_intent", "marker": handoff.packet.marker, "host_id": original["host_id"],
                "create_thread": handoff.create_thread_arguments(original["project_id"]),
            }
        else:
            # A legacy or interrupted claim retains the original filters and
            # secret. Do not substitute this loop's capabilities on replay.
            result = runner.prepare()
        if result["status"] != "launch_intent":
            return result
        snapshot = runner._snapshot()
        error = discovery.result()[self._mapping_key(snapshot)]
        if error is not None:
            raise RunnerError(error)
        runner._verify_launch_claim(snapshot)
        runner.renew()
        def relay_intent(record, db):
            runner._ensure_reservation(record, snapshot["lease"])
            if any(field in record for field in (*_LAUNCH_EFFECTS, "cancel_stop")):
                raise RunnerError("Launch relay already attempted or owned by recovery; reconcile its exact marker")
            if record.get("packet") != snapshot["packet"]:
                raise RunnerError("The original launch packet changed; reconcile before any relay")
            record["relay"] = {"marker": result["marker"], "status": "started"}
        runner._transaction(relay_intent)
        try:
            receipt = self.adapter.launch(result)
        except LaunchNotDelivered:
            # The app never saw this launch, so the original packet may relay again.
            def retract(record, db):
                if (record.get("lease") == snapshot["lease"] and "receipt" not in record
                        and record.get("relay") == {"marker": result["marker"], "status": "started"}):
                    del record["relay"]
            runner._transaction(retract)
            raise
        runner.receipt(result["marker"], result["host_id"], **receipt)
        return {"status": "receipt_recorded", "slot": slot, **receipt}

    def _claim_new(self, slot, discovery=None):
        capabilities = self.config.get("stages", sorted(set(STAGES) | {"pr_creation", "report_storage"}))
        if not capabilities or set(capabilities) - (set(STAGES) | {"pr_creation", "report_storage"}):
            raise RunnerError("Runner stage capabilities contain an unsupported stage")
        available = [stage for stage in capabilities
                     if stage != "pr_creation" or os.environ.get("PHALANX_GITHUB_TOKEN")]
        if not available:
            raise RunnerError("No configured stages are available; set PHALANX_GITHUB_TOKEN for initial drafts")
        groups = {
            "runner": [stage for stage in available if stage not in STAGES],
            "worker": [stage for stage in available if stage in STAGES],
        }
        first = self.admission.get(slot, "runner")
        second = "worker" if first == "runner" else "runner"
        self.admission[slot] = second
        result = None
        blocked = []
        for mode in (first, second):
            stages = groups[mode]
            if not stages:
                continue
            mappings = self.config["repositories"]
            if mode == "worker":
                if discovery is None:
                    discovery = self.native_pool.submit(self._discover, self._records())
                resolved = discovery.result()
                verified = {}
                for repo, mapping in mappings.items():
                    error = resolved[self._mapping_key(mapping)]
                    if error is not None:
                        blocked.append(repo)
                        self.emit({"slot": slot, "status": "mapping_blocked", "repository": repo,
                                   "reason": error})
                    else:
                        verified[repo] = mapping
                mappings = verified
                if not mappings:
                    continue
            runner = AttendedRunner(self.root, {**self.config, "repositories": mappings}, self.api, slot=slot)
            result = runner.prepare(stages=stages)
            if result["status"] != "no_eligible_work":
                return runner, result
        if blocked:
            return self._runner(slot), {
                "status": "desktop_blocked", "slot": slot,
                "reason": "No eligible runner-only work; restore desktop mappings: " + ", ".join(blocked),
            }
        if result is None:
            raise RunnerError("No mapping is eligible for configured stages; restore the repository/desktop mappings")
        return self._runner(slot), result

    def _unlaunched_proof(self, runner, snapshot):
        claim = snapshot["claim"]
        attempt = UUID(claim["attempt_id"])
        for identity in (str(attempt), attempt.hex):
            result = runner.results / f"phalanx-attempt-{identity}.json"
            if os.path.lexists(result) or os.path.lexists(result.with_suffix(".json.tmp")):
                raise RunnerError("Attempt evidence exists; reconcile it before confirming stop or launch")
        repo = claim["request"]["repository_id"]
        mapping = self.config["repositories"][repo]
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

    def _confirm_stop(self, runner, current=None, native=None):
        # Exclude preparations while accounting for the writer and local effects,
        # and persist the exact stop request before sending it.
        with _file_lock(self.root / f"attended-prepare-{runner.slot}.lock"), ExitStack() as locks:
            snapshot = runner._snapshot()
            unsafe = ("recover_stop",)
            if any(field in snapshot for field in unsafe):
                raise RunnerError("Stop needs native/effect reconciliation; an ambiguous launch is not a stopped writer")
            claim = snapshot["claim"]
            stage = claim["execution"]["state"]["stage"]
            if stage == "pr_creation":
                item = _item_branch(claim["item_id"]).rsplit("/", 1)[1]
                publication = self.root / "publication"
                publication.mkdir(exist_ok=True)
                locks.enter_context(_file_lock(publication / f"{item}.lock"))
                for filename, table in (("draft-publication.sqlite3", "draft_publication"),
                                        ("draft-observations.sqlite3", "draft_observations")):
                    path = publication / filename
                    if os.path.lexists(path):
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                            if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                           (table,)).fetchone()
                                    and db.execute(f"SELECT 1 FROM {table} WHERE item_id=?",
                                                   (claim["item_id"],)).fetchone()):
                                raise RunnerError("Runner-only stop needs publication/storage effect reconciliation")
            elif stage not in {*STAGES, "report_storage"}:
                raise RunnerError("Unsupported runner-only stop needs effect reconciliation")
            request = snapshot.get("cancel_stop")
            launched = any(field in snapshot for field in ("relay", "receipt"))
            if launched:
                if "packet" not in snapshot or (native is None and not (request or {}).get("thread_id")):
                    raise RunnerError("An ambiguous launch is not a stopped writer; verify its native receipt")
                packet = runner._handoff(snapshot).packet
                thread_id = request.get("thread_id") if request else None
                if native is not None:
                    if native.state != "stopped" or not _matches(snapshot, packet, native):
                        raise RunnerError("An ambiguous launch is not a stopped writer; observe its exact stopped turn")
                    thread_id = str(native.thread_id)
                if not thread_id or snapshot.get("receipt", {}).get("thread_id") != thread_id:
                    raise RunnerError("An ambiguous launch is not a stopped writer; verify its native receipt")
                evidence, checkpoint = _verify_local(self.root, snapshot, packet, UUID(thread_id))
                if evidence.disposition not in {"retry_safe", "verified_result"}:
                    raise RunnerError(evidence.summary)
                proof = f"Exact stopped native thread {thread_id}. " + json.dumps(checkpoint, sort_keys=True)
            else:
                if "submission" in snapshot:
                    raise RunnerError("Runner-only stop needs publication/storage effect reconciliation")
                proof = self._unlaunched_proof(runner, snapshot)
            if request is not None and request["proof"] != proof:
                raise RunnerError("Stopped checkpoint changed; reconcile it before replaying stop")
            if request is None:
                if (current is None or current["id"] != claim["execution"]["id"]
                        or current["state"]["stopping"] not in {"cancelled", "failed"}
                        or current["state"]["stage"] != claim["execution"]["state"]["stage"]):
                    raise RunnerError("Stop does not match the current execution")
                request = {
                    "key": f"attended-unlaunched-stop-{claim['attempt_id']}-{current['version']}",
                    "body": {"attempt_id": claim["attempt_id"], "stage": current["state"]["stage"],
                             "expected_version": current["version"]},
                    "disposition": current["state"]["stopping"], "proof": proof,
                }
                if launched:
                    request["thread_id"] = thread_id
                if ("submission" in snapshot
                        and snapshot["submission"]["body"]["expected_version"] >= current["version"]):
                    raise RunnerError("Reconcile the saved result before confirming stop")
                def save(record, db):
                    runner._ensure_reservation(record, snapshot["lease"])
                    if (any(field in record for field in unsafe)
                            or any(record.get(field) != snapshot.get(field) for field in (*_LAUNCH_EFFECTS, "packet"))):
                        raise RunnerError("Launch changed during stop verification; retain the reservation")
                    record["cancel_stop"] = request
                runner._transaction(save)
            route = f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/stop-confirmed"
            response = self.api.call(route, request["body"], lease=snapshot["lease"], key=request["key"])
            state = response.get("state", {})
            version, disposition = request["body"]["expected_version"], request["disposition"]
            pending = (response.get("version") == version + 1 and state.get("status") == "waiting_for_external"
                       and state.get("pending_output") == disposition)
            output = state.get("final_output")
            finalized = (
                response.get("version") == version + 2 and state.get("status") == disposition
                and isinstance(output, dict)
                and output.get("reference") == "/v1" + route.removesuffix("/stop-confirmed") + "/report"
            )
            if response.get("id") != claim["execution"]["id"] or not (pending or finalized):
                raise RunnerError("Stop acknowledgment did not verify finalization; retain its exact request")
            def finish(record, db):
                runner._ensure_reservation(record, snapshot["lease"])
                if (record.get("cancel_stop") != request or any(field in record for field in unsafe)
                        or any(record.get(field) != snapshot.get(field) for field in _LAUNCH_EFFECTS)):
                    raise RunnerError("Reservation changed before stop acknowledgment; reconcile it")
                archive = {key: value for key, value in record.items() if key != "lease"}
                archive["stop_acknowledgment"] = response
                db.execute("CREATE TABLE IF NOT EXISTS completed (attempt_id TEXT PRIMARY KEY, record TEXT)")
                db.execute("INSERT INTO completed VALUES (?, ?)", (claim["attempt_id"], json.dumps(archive)))
                record.clear()
            runner._transaction(finish)
            return {"status": "stop_confirmed", "execution": response, "reason": request["proof"]}

    def _advance(self, slot, snapshot, observation, discovery=None):
        runner = self._runner(slot)
        if not snapshot:
            return self._prepare(slot, discovery)
        if "cancel_stop" in snapshot:
            native = None
            if observation is not None and "thread_id" in snapshot["cancel_stop"]:
                native = self._native(runner, snapshot, observation)
            return self._confirm_stop(runner, native=native)
        unlaunched = not any(field in snapshot for field in _LAUNCH_EFFECTS)
        current, stopping = None, False
        if "claim" in snapshot and (unlaunched or "packet" in snapshot):
            current = self.api.call(f"/items/{snapshot['claim']['item_id']}")["execution"]
            stopping = (current["id"] == snapshot["claim"]["execution"]["id"]
                        and current["state"]["stage"] == snapshot["claim"]["execution"]["state"]["stage"]
                        and current["state"].get("stopping") in {"cancelled", "failed"})
        if unlaunched and stopping:
            return self._confirm_stop(runner, current)
        if "submission" in snapshot and not stopping:
            packet = snapshot.get("packet")
            if packet:
                receipt = snapshot["receipt"]
                return {"status": "submitted", "execution": runner.submit(
                    runner._handoff(snapshot).packet.marker, snapshot["host_id"], receipt["thread_id"],
                )}
            return self._prepare(slot, discovery)
        if snapshot.get("recover_stop") or snapshot.get("claim", {}).get("attempt_id") in self.expired:
            if "packet" in snapshot and not unlaunched and observation is None and "recover_stop" not in snapshot:
                return {"status": "observing", "reason": "Awaiting native observation before recovery"}
            if observation is not None and "packet" in snapshot:
                self._check_observation(runner, snapshot, observation)
            return run_recovery(
                self.root, self.config["runner_id"], self.api, RecoveryJournal(self.root, slot),
                observations=observation.threads if observation else (),
                discovery_complete=observation.discovery_complete if observation else False,
                no_launch_proof=self._unlaunched_proof(runner, snapshot) if unlaunched else None,
            )
        if "packet" not in snapshot or unlaunched:
            return self._prepare(slot, discovery)
        if observation is None:
            return {"status": "observing", "reason": "Awaiting an exact native observation; launch is never replayed"}
        packet = runner._handoff(snapshot).packet
        native = self._native(runner, snapshot, observation)
        runner.receipt(packet.marker, snapshot["host_id"], thread_id=str(native.thread_id))
        if native.state == "stopped":
            if stopping:
                return self._confirm_stop(runner, current, native)
            return {"status": "submitted", "execution": runner.submit(
                packet.marker, snapshot["host_id"], str(native.thread_id),
            )}
        return {"status": "observing", "reason": f"Exact native writer is {native.state}; reservation retained"}

    def _check_observation(self, runner, snapshot, observation):
        if (datetime.now(timezone.utc) - observation.observed_at).total_seconds() > 60:
            raise RunnerError("Native observation aged while this slot was busy; read the exact writer again")
        packet = runner._handoff(snapshot).packet
        if packet.marker in observation.errors:
            raise RunnerError(observation.errors[packet.marker])
        return packet

    def _native(self, runner, snapshot, observation):
        packet = self._check_observation(runner, snapshot, observation)
        matches = [row for row in observation.threads if _matches(snapshot, packet, row)]
        if len(matches) != 1 or (
            not snapshot.get("receipt", {}).get("thread_id") and not observation.discovery_complete
        ):
            raise RunnerError("Native discovery has not established one exact writer; retain this slot and inspect history")
        return matches[0]

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
            saved = self.cursors.get(ref["slot"])
            if saved and saved["marker"] == ref["marker"] and saved.get("cursor"):
                cursors[ref["marker"]] = saved["cursor"]
        batch = self.adapter.wait_threads(references, cursors=cursors, timeout=self.wait_seconds)
        for ref in references:
            if ref["marker"] in batch.cursors:
                self.cursors[ref["slot"]] = {
                    "marker": ref["marker"], "cursor": batch.cursors[ref["marker"]],
                }
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
                    if "" in batch.errors:
                        self._status(0, "observation_blocked", batch.errors[""])
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
        for slot in list(self.cursors):
            if slot not in records and not any(slot in slots for slots in self.observations):
                del self.cursors[slot]
        if now >= self.discovery_due and (
            self.discovery_future is None or self.discovery_future.done()
        ):
            self.discovery_future = self.native_pool.submit(self._discover, records)
            self.discovery_due = now + self.poll_seconds
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
            self._status(0, "unmatched_attempts", "Unmatched attempts reserve capacity; restore their journals: "
                         + ", ".join(self.unmatched))
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
                self.work[slot] = self.slot_pool.submit(self._advance, slot, record, observation,
                                                       self.discovery_future)
                self.work_due[slot] = now + self.poll_seconds
        observed_slots = {slot for slots in self.observations for slot in slots}
        candidates = [slot for slot, record in records.items()
                      if "packet" in record and slot not in observed_slots
                      and any(field in record for field in ("relay", "receipt"))
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
        if self.recovery_ready:
            occupied = set(records) | set(self.work)
            room = max(0, self.runner.capacity - len(occupied) - len(self.unmatched))
            for slot in range(1, self.runner.capacity + 1):
                if room and slot not in occupied and now >= self.work_due.get(slot, 0):
                    self.work[slot] = self.slot_pool.submit(self._prepare, slot, self.discovery_future)
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
    parser.add_argument("--adapter-command", nargs=argparse.REMAINDER,
                        help="trusted JSON bridge executable and arguments instead of the Captain chat relay; "
                             "put this option last")


def run_captain(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    token = os.environ.get("PHALANX_RUNNER_TOKEN")
    preflight(config, token)
    transport = (CommandTransport(args.adapter_command) if args.adapter_command
                 else RelayTransport(args.state))
    loop = CaptainLoop(
        args.state, config, RunnerAPI(config["base_url"], token), CodexCLIAdapter(transport),
        poll_seconds=args.poll_seconds, heartbeat_seconds=args.heartbeat_seconds,
        batch_size=args.batch_size, wait_seconds=args.wait_seconds,
    )
    try:
        loop.run(Event())
    except KeyboardInterrupt:
        pass
