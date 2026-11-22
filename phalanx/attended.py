"""Attended Captain bridge. Native app calls belong to the Captain."""

import json
import os
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .attended_drafts import development_checkpoint, github_repository, publish_draft
from . import report_execution as reports
from .handoff import WorkerHandoff, WorkPacket
from .runner_errors import RunnerError
from .workspaces import WorkspaceError, WorkspaceManager, _file_lock


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RunnerAPI:
    def __init__(self, base_url, token):
        url = urlsplit(base_url)
        if (
            not url.hostname or url.username or url.password or url.query or url.fragment
            or (url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            ))
        ):
            raise RunnerError("Use HTTPS or a loopback HTTP API URL without credentials")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def call(self, path, body=None, *, lease=None, key=None, plain_text=False):
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if lease:
            headers["Phalanx-Lease"] = lease
        if key:
            headers["Idempotency-Key"] = key
        request = Request(self.base_url + "/v1" + path, headers=headers,
                          data=None if body is None else json.dumps(body).encode())
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.read().decode("utf-8") if plain_text else json.load(response)
        except HTTPError as exc:
            raise RunnerError(f"Runner API returned HTTP {exc.code} for {path}") from None
        except (URLError, TimeoutError, ValueError):
            raise RunnerError(f"Runner API response unavailable for {path}; reservation retained") from None


STAGES = {
    "development": "Commit the candidate locally. Put the full commit hash alone in details.evidence.",
    reports.DRAFTING: (
        "Draft and self-review the field report. Address any findings in the report checkpoint. "
        "Put the complete Markdown in details.evidence."
    ),
    reports.REVIEW: reports.REVIEW_INSTRUCTIONS,
    "self_review": (
        "Review the exact candidate diff against the target branch and check the acceptance criteria. "
        "Leave the checkout unchanged. Put a summary of what you reviewed in details.evidence, or report "
        "actionable findings as changes_required in details.findings."
    ),
}
# Runner-owned stages that need no worker session.
RUNNER_STAGES = ("pr_creation", reports.STORAGE, "pre_pr_validation", "pr_promotion", "pr_validation")


class AttendedRunner:
    def __init__(self, root, config, api, *, slot=1):
        self.root = root.resolve()
        self.config = config
        self.api = api
        self.capacity = config.get("capacity", 1)
        if type(self.capacity) is not int or not 1 <= self.capacity <= 256:
            raise RunnerError("Local capacity must be an integer from 1 to 256")
        if type(slot) is not int or not 1 <= slot <= 256:
            raise RunnerError("Slot must be an integer from 1 to 256")
        self.slot = slot
        self.root.mkdir(parents=True, exist_ok=True)
        self.results = self.root / "results"
        self.results.mkdir(exist_ok=True)
        self.journal = self.root / "attended.sqlite3"

    @contextmanager
    def _journal(self):
        with closing(sqlite3.connect(self.journal, timeout=30)) as db, db:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS slot (id INTEGER PRIMARY KEY, record TEXT)")
            yield db

    def _transaction(self, action):
        # Keep network, Git and evidence reads outside the journal lock.
        with self._journal() as db:
            row = db.execute("SELECT record FROM slot WHERE id=?", (self.slot,)).fetchone()
            record = json.loads(row[0]) if row else {}
            archived = record.copy() if "submission" in record and "cancel_stop" not in record else None
            result = action(record, db)
            if archived is not None and not record:
                archived.pop("lease")
                db.execute("CREATE TABLE IF NOT EXISTS completed (attempt_id TEXT PRIMARY KEY, record TEXT)")
                db.execute("INSERT INTO completed VALUES (?, ?)",
                           (archived["claim"]["attempt_id"], json.dumps(archived)))
            if record:
                serialized = json.dumps(record)
                if row is None or serialized != row[0]:
                    db.execute("INSERT OR REPLACE INTO slot VALUES (?, ?)", (self.slot, serialized))
            elif row is not None:
                db.execute("DELETE FROM slot WHERE id=?", (self.slot,))
            return result

    def _manager(self, mapping, remote):
        return WorkspaceManager(Path(mapping["checkout"]), remote, self.root / "worktrees")

    def _snapshot(self):
        return self._transaction(lambda record, db: record)

    @staticmethod
    def _ensure_reservation(record, lease):
        if record.get("lease") != lease:
            raise RunnerError("The original reservation has ended; inspect status")

    def _verified_mappings(self, *, draft=False):
        eligible = {}
        blocked = []
        for registered in self.api.call("/repositories"):
            repo = registered["id"]
            mapping = self.config["repositories"].get(repo)
            if mapping is None or (draft and github_repository(registered["remote"]) is None):
                continue
            try:
                self._manager(mapping, registered["remote"]).verify_checkout()
            except WorkspaceError:
                blocked.append(repo)
                continue
            eligible[repo] = registered["remote"]
        if not eligible:
            raise RunnerError("No registered checkout passed verification; check local mappings and origins")
        return eligible, blocked

    def prepare(self, *, draft=False, report_storage=False, stages=None):
        # Only competing preparations of this slot wait here. Heartbeats and
        # other slots can still use the journal during HTTP and Git operations.
        with _file_lock(self.root / f"attended-prepare-{self.slot}.lock"):
            return self._prepare_slot(draft=draft, report_storage=report_storage, stages=stages)

    def _prepare_slot(self, *, draft, report_storage, stages=None):
        runner_stage = "pr_creation" if draft else reports.STORAGE if report_storage else None
        if stages is not None and (
            runner_stage or not stages or set(stages) - (set(STAGES) | set(RUNNER_STAGES))
        ):
            raise RunnerError("Continuous dispatch requires a nonempty supported stage list")
        github_token = os.environ.get("PHALANX_GITHUB_TOKEN")
        snapshot = self._snapshot()
        if "cancel_stop" in snapshot:
            raise RunnerError("Unlaunched stop confirmation owns this reservation; resume the Captain loop")
        if not snapshot:
            if draft and not github_token:
                raise RunnerError("Set PHALANX_GITHUB_TOKEN in the runner environment for draft publication")
            if self.slot > self.capacity:
                raise RunnerError("Slot exceeds local capacity; use an enabled slot")
            eligible, blocked = self._verified_mappings(draft=draft)

            def reserve(record, db):
                if db.execute("SELECT count(*) FROM slot").fetchone()[0] >= self.capacity:
                    raise RunnerError("Local capacity is full; inspect status and reconcile active slots")
                record.update(lease=secrets.token_urlsafe(32), runner_id=self.config["runner_id"],
                              repositories=eligible, blocked=blocked,
                              stages=sorted(stages) if stages is not None else
                              [runner_stage] if runner_stage else sorted(STAGES),
                              slots=self.capacity)
                return record

            snapshot = self._transaction(reserve)

        lease = snapshot["lease"]
        if "claim" not in snapshot:
            if runner_stage and snapshot.get("stages") != [runner_stage]:
                raise RunnerError("Resume the existing reservation in its original prepare mode")
            request = {
                "runner_id": snapshot["runner_id"],
                "repository_ids": sorted(snapshot["repositories"]),
                "slots": snapshot.get("slots", 1),
            }
            if "stages" in snapshot:
                request["stages"] = snapshot["stages"]
            result = self.api.call("/dispatch/claim", request, lease=lease)

            def save_claim(record, db):
                self._ensure_reservation(record, lease)
                if result["status"] != "claimed":
                    record.clear()
                else:
                    record.update(claim=result, lease_expires_at=result["lease_expires_at"])
                return record

            blocked = snapshot["blocked"]
            snapshot = self._transaction(save_claim)
            if not snapshot:
                return {"status": result["status"], "slot": self.slot, "unverified_mappings": blocked}

        stage = snapshot["claim"]["execution"]["state"]["stage"]
        if runner_stage and stage != runner_stage:
            raise RunnerError("Resume the existing assignment in its original prepare mode")
        if stage == "pr_creation":
            return {**self._run_draft(snapshot, github_token), "slot": self.slot}
        if stage == reports.STORAGE:
            return self._store_report(snapshot)
        if stage in RUNNER_STAGES:
            from . import validation_stages

            return {**validation_stages.run(self, snapshot), "slot": self.slot}
        launch = self._prepare_launch(snapshot)

        def commit_launch(record, db):
            self._ensure_reservation(record, lease)
            if "packet" in record:
                raise RunnerError("Launch intent already issued; inspect status and reconcile in the worker runtime")
            if "recover_stop" in record or "cancel_stop" in record:
                raise RunnerError("Recovery owns this reservation; inspect attended-recover")
            for field in ("packet", "project_id", "host_id", "checkout", "started_at"):
                record[field] = snapshot[field]
            if "development_base" in snapshot:
                record["development_base"] = snapshot["development_base"]
            record["lease_expires_at"] = max(record["lease_expires_at"], snapshot["lease_expires_at"])

        self._transaction(commit_launch)
        return {**launch, "slot": self.slot}

    def _run_draft(self, snapshot, token):
        claim = snapshot["claim"]
        mapping = self.config["repositories"][claim["request"]["repository_id"]]
        checkpoint = development_checkpoint(self.journal, claim)
        if checkpoint is None:
            status = {"status": "draft_blocked", "reason": (
                "The candidate has no accepted development base checkpoint. "
                "Recover the exact development base or return the item to development."
            )}
        elif not token:
            status = {"status": "draft_blocked",
                      "reason": "Set PHALANX_GITHUB_TOKEN in the runner environment and retry"}
        else:
            result = publish_draft(self.root, self.api, snapshot, mapping, checkpoint, token)
            if result.execution is not None:
                status = {"status": "draft_submitted", "execution": result.execution}
                if result.publication is not None and result.publication.status != "verified":
                    status["reason"] = result.publication.reason
            else:
                status = {"status": "draft_" + result.publication.status,
                          "reason": result.publication.reason}

        def finish(record, db):
            if record.get("lease") == snapshot["lease"]:
                if status["status"] == "draft_submitted":
                    record.clear()
                else:
                    record["draft_status"] = status

        self._transaction(finish)
        return status

    def _store_report(self, snapshot):
        lease = snapshot["lease"]
        submission = snapshot.get("submission")
        if submission is None:
            self._renew(snapshot)
            submission = {"body": reports.storage_body(snapshot["claim"], self.api)}

            def save(record, db):
                self._ensure_reservation(record, lease)
                record["submission"] = submission
                record["lease_expires_at"] = max(record["lease_expires_at"],
                                                snapshot["lease_expires_at"])

            self._transaction(save)

        key = f"phalanx-attempt-{snapshot['claim']['attempt_id']}"
        result = self._send_submission(snapshot, submission, key)
        return {"status": "report_storage_submitted", "slot": self.slot, "execution": result}

    def _prepare_launch(self, record):
        if "packet" in record:
            raise RunnerError("Launch intent already issued; inspect status and reconcile in the worker runtime")
        claim = record["claim"]
        execution = claim["execution"]
        state = execution["state"]
        stage = state["stage"]
        if stage not in STAGES:
            raise RunnerError(f"Stage {stage} needs a different handler; claim retained for recovery")
        request = claim["request"]
        repo = request["repository_id"]
        mapping = self.config["repositories"][repo]
        base = request.get("target_branch") or mapping["base_branch"]
        self._renew(record)
        manager = self._manager(mapping, record["repositories"][repo])
        base_ref = f"refs/remotes/origin/{base}"
        if stage == "development":
            base_sha = manager.resolve_origin_base(base)
            record["development_base"] = {"ref": base, "sha": base_sha}
            base_ref = base_sha
        workspace = manager.prepare(claim["item_id"], base=base_ref)
        candidate = state.get("candidate")
        if candidate and candidate != workspace.head:
            raise RunnerError("Worktree HEAD differs from the current server candidate; reconcile before launch")
        input_reply = claim.get("input_reply")
        report_checkpoint = None
        findings = state.get("report_review_findings")
        if stage == reports.REVIEW or (stage == reports.DRAFTING and findings):
            report_checkpoint = reports.checkpoint(self.api, claim)
            if findings:
                report_checkpoint["findings"] = findings
        packet = WorkPacket(
            item_id=claim["item_id"], attempt_id=UUID(claim["attempt_id"]),
            state_version=execution["version"], stage=stage, repository=repo,
            worktree=workspace.path, branch=workspace.branch, expected_revision=workspace.head,
            goal=request["goal"], output_type="report" if request["target"] == "report" else "pull_request",
            evidence_requirements=STAGES[stage], base_branch=base,
            report_checkpoint=report_checkpoint,
            dependencies=tuple(claim.get("dependencies", ())),
            context=json.dumps({field: request.get(field, []) for field in
                                ("context_links", "acceptance_criteria")}, ensure_ascii=False),
            handoff=json.dumps(input_reply, ensure_ascii=False) if input_reply is not None else "",
        )
        handoff = WorkerHandoff(packet, self.results)
        if os.path.lexists(handoff.result_path) or os.path.lexists(handoff.temporary_path):
            raise RunnerError("Attempt already has local evidence; reconcile before launch")
        self._verify_launch_claim(record)
        self._renew(record)
        record.update(packet={**asdict(packet), "attempt_id": str(packet.attempt_id),
                              "worktree": str(packet.worktree)},
                      project_id=mapping["project_id"], host_id=mapping["host_id"],
                      checkout=mapping["checkout"], started_at=datetime.now(timezone.utc).isoformat())
        return {"status": "launch_intent", "marker": packet.marker, "host_id": record["host_id"],
                "unverified_mappings": record["blocked"],
                "session": handoff.launch_arguments(record["project_id"])}

    def _verify_launch_claim(self, record):
        claim = record["claim"]
        execution = claim["execution"]
        current = self.api.call(f"/items/{claim['item_id']}")["execution"]
        if (current["id"] != execution["id"] or current["version"] != execution["version"]
                or current["state"]["status"] != "running" or current["state"].get("stopping")):
            raise RunnerError("Claimed execution changed before launch; reconcile the reservation")

    def status(self, *, all_slots=False):
        def view(record, loop=None):
            if not record:
                return {"status": "idle"}
            packet = record.get("packet")
            claim = record.get("claim")
            if "cancel_stop" in record:
                status = "stop_confirming"
            elif "submission" in record:
                status = "submitting"
            elif "receipt" in record:
                status = "attached" if "thread_id" in record["receipt"] else "launch_pending"
            elif packet:
                status = "launch_uncertain"
            else:
                status = "claimed" if claim else "claim_pending"
            if record.get("draft_status"):
                status = record["draft_status"]["status"]
            return {"status": status, "packet": packet, "attempt_id": claim and claim["attempt_id"],
                    "stage": claim and claim["execution"]["state"]["stage"],
                    "project_id": record.get("project_id"), "host_id": record.get("host_id"),
                    "started_at": record.get("started_at"), "receipt": record.get("receipt"),
                    "lease_expires_at": record.get("lease_expires_at"),
                    "unverified_mappings": record.get("blocked", []),
                    **({"loop": loop} if loop is not None else {}),
                    **({"recovery": record["recovery"]} if "recovery" in record else {}),
                    **({"reason": record["draft_status"]["reason"]} if record.get("draft_status") else {})}

        with self._journal() as db:
            rows = db.execute("SELECT id, record FROM slot ORDER BY id").fetchall()
            has_captain = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='captain'"
            ).fetchone()
            loop_status = [json.loads(row[0]) for row in db.execute(
                "SELECT value FROM captain WHERE key LIKE 'status-%' ORDER BY key"
            )] if has_captain else None
        statuses = {event["slot"]: event for event in loop_status or []}
        if not all_slots:
            return view(next((json.loads(data) for slot, data in rows if slot == self.slot), {}),
                        statuses.get(self.slot))
        return {"capacity": self.capacity, "reservations": [
            {"slot": slot, **view(json.loads(data), statuses.get(slot))} for slot, data in rows
        ], **({"loop_status": loop_status} if loop_status is not None else {})}

    def receipt(self, marker, host_id, *, thread_id=None, client_thread_id=None, worker_url=None):
        def save(record, db):
            packet = self._handoff(record).packet
            if marker != packet.marker or host_id != record["host_id"]:
                raise RunnerError("Receipt does not match the launch marker and host")
            if bool(thread_id) == bool(client_thread_id):
                raise RunnerError("Supply exactly one native thread or queued client thread ID")
            receipt = ({"thread_id": str(UUID(thread_id))} if thread_id else
                       {"client_thread_id": str(UUID(client_thread_id))})
            current = record.get("receipt")
            if current:
                if (any(field in current and current[field] != value for field, value in receipt.items())
                        or ("client_thread_id" in receipt and "thread_id" in current
                            and "client_thread_id" not in current)):
                    raise RunnerError("Receipt conflicts with the recorded identity")
                receipt = {**current, **receipt}
            if worker_url:
                receipt["worker_url"] = worker_url
            record["receipt"] = receipt
            return {"status": "receipt_recorded", **receipt}

        return self._transaction(save)

    def _renew(self, record):
        claim = record.get("claim")
        if not claim:
            raise RunnerError("There is no known claim to renew")
        endpoint = f"/dispatch/runners/{record['runner_id']}/attempts/{claim['attempt_id']}/renew"
        # Each renewal republishes the bridge's opaque worker reference for the operator board.
        receipt = record.get("receipt", {})
        reference = receipt.get("thread_id") or receipt.get("client_thread_id")
        body = {"worker": {"reference": reference, **(
            {"url": receipt["worker_url"]} if receipt.get("worker_url") else {}
        )}} if reference else {}
        result = self.api.call(endpoint, body, lease=record["lease"])
        record["lease_expires_at"] = result["lease_expires_at"]
        return result

    def renew(self):
        snapshot = self._snapshot()
        result = self._renew(snapshot)

        def save_renewal(record, db):
            self._ensure_reservation(record, snapshot["lease"])
            record["lease_expires_at"] = max(record["lease_expires_at"], snapshot["lease_expires_at"])

        self._transaction(save_renewal)
        return result

    def _handoff(self, record):
        if "packet" not in record:
            raise RunnerError("No durable launch intent exists")
        packet = record["packet"]
        return WorkerHandoff(WorkPacket(**{
            **packet, "attempt_id": UUID(packet["attempt_id"]), "worktree": Path(packet["worktree"]),
            "dependencies": tuple(packet.get("dependencies", ())),
        }), self.results)

    def submit(self, marker, host_id, thread_id):
        """The Captain attests it inspected the exact launch and observed a stopped writer."""
        def verify(record):
            handoff = self._handoff(record)
            packet = handoff.packet
            receipt = record.get("receipt", {})
            if (marker != packet.marker or host_id != record["host_id"]
                    or str(UUID(thread_id)) != receipt.get("thread_id")):
                raise RunnerError("Terminal observation does not match the native receipt and marker")
            if "submission" in record:
                return
            result = handoff.read_result()
            if result is None:
                raise RunnerError("No final result file; keep the reservation and reconcile")
            manager = self._manager(
                {"checkout": record["checkout"]}, record["repositories"][packet.repository],
            )
            workspace = manager.inspect(packet.item_id)
            if workspace.path != packet.worktree or workspace.branch != packet.branch:
                raise RunnerError("Result worktree no longer matches the launch intent")
            body = {"attempt_id": record["claim"]["attempt_id"], "stage": packet.stage,
                    "expected_version": packet.state_version, "outcome": result.outcome}
            if packet.output_type == "report" and workspace.head != packet.expected_revision:
                raise RunnerError("Report checkout HEAD changed during the assignment")
            if packet.stage == "self_review":
                from .candidate_local import CandidateLocal

                body = CandidateLocal(self.api, manager, record["claim"], runner_id=record["runner_id"],
                                      lease=record["lease"]).self_review(result)
                if any(len(text) > 16000 for text in (body["review_notes"], body.get("failure_reason", ""))):
                    raise RunnerError("Worker evidence exceeds the API limit")
            elif packet.stage == reports.REVIEW and result.outcome in {"succeeded", "changes_required"}:
                body.update(reports.review_evidence(packet, result))
            elif result.outcome == "succeeded":
                evidence = result.details["evidence"].strip()
                if packet.stage == "development":
                    if (not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", evidence)
                            or evidence != workspace.head or workspace.dirty
                            or evidence == packet.expected_revision):
                        raise RunnerError("Development requires a new candidate at the clean worktree HEAD")
                    if "development_base" in record:
                        manager.verify_base_ancestry(workspace, record["development_base"]["sha"])
                    body["candidate"] = workspace.head
                else:
                    body["report_markdown"] = result.details["evidence"]
            elif result.outcome == "failed":
                reason = "\n\n".join(f"{field.replace('_', ' ').capitalize()}\n{text}"
                                     for field, text in result.details.items())
                if len(reason) > 16000:
                    raise RunnerError("Worker evidence exceeds the API limit")
                body["failure_reason"] = reason
            elif result.outcome == "needs_input":
                if any(len(text.strip()) > 16000 for value in result.details.values()
                       for text in (value if isinstance(value, list) else [value])):
                    raise RunnerError("Worker question exceeds the API limit")
                body["question"] = {
                    "prompt": result.details["question"],
                    "options": result.details["options"],
                    "proposed_default": result.details.get("proposed_default"),
                    "handoff": result.details["handoff"],
                }
            else:
                raise RunnerError("Outcome is unsupported for this attended stage")
            submission = {
                "body": body,
                "checkpoint": {**asdict(workspace), "path": str(workspace.path)},
                "details": result.details,
                "stop_confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
            if packet.stage == "development" and "development_base" in record:
                submission["checkpoint"]["base"] = record["development_base"]
            return submission

        # Workspace inspection can run Git commands. Keep the journal available
        # for renewals and verify the reservation again before saving evidence.
        snapshot = self._snapshot()
        submission = verify(snapshot)

        def save_submission(record, db):
            if (record.get("lease") != snapshot.get("lease")
                    or record.get("receipt") != snapshot.get("receipt")):
                raise RunnerError("The original reservation ended; inspect status")
            if "submission" not in record:
                record["submission"] = submission

            return record["submission"]

        submission = self._transaction(save_submission)
        return self._send_submission(snapshot, submission, marker)

    def _send_submission(self, snapshot, submission, key):
        claim = snapshot["claim"]
        route = submission.get("route", "result")
        path = f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/{route}"
        response = self.api.call(path, submission["body"], lease=snapshot["lease"], key=key)

        def finish(record, db):
            # A concurrent idempotent submit may already have archived this attempt.
            if record.get("lease") == snapshot["lease"]:
                record.clear()

        self._transaction(finish)
        return response


def add_attended_command(commands):
    parser = commands.add_parser("attended", help="operate attended Captain reservations")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--slot", type=int, default=1)
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    runner_stage = prepare.add_mutually_exclusive_group()
    runner_stage.add_argument("--draft", action="store_true", help="poll for runner-owned initial draft publication")
    runner_stage.add_argument("--report-storage", action="store_true", help="poll for reviewed report storage")
    actions.add_parser("renew")
    actions.add_parser("status").add_argument("--all", action="store_true")
    receipt = actions.add_parser("receipt")
    receipt.add_argument("marker")
    receipt.add_argument("host_id")
    identity = receipt.add_mutually_exclusive_group(required=True)
    identity.add_argument("--thread-id")
    identity.add_argument("--client-thread-id")
    submit = actions.add_parser("submit")
    submit.add_argument("marker")
    submit.add_argument("host_id")
    submit.add_argument("thread_id")
    submit.add_argument("--confirmed-stopped", action="store_true", required=True)


def run_attended(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    token = os.environ.get("PHALANX_RUNNER_TOKEN")
    if args.action == "prepare":
        from .runner_preflight import preflight
        preflight(config, token)
    elif not token and args.action in {"renew", "submit"}:
        raise RunnerError("Set PHALANX_RUNNER_TOKEN in the runner environment")
    runner = AttendedRunner(args.state, config, RunnerAPI(config["base_url"], token), slot=args.slot)
    if args.action == "prepare":
        result = runner.prepare(draft=args.draft, report_storage=args.report_storage)
    elif args.action == "receipt":
        result = runner.receipt(args.marker, args.host_id, thread_id=args.thread_id,
                                client_thread_id=args.client_thread_id)
    elif args.action == "submit":
        result = runner.submit(args.marker, args.host_id, args.thread_id)
    elif args.action == "status":
        result = runner.status(all_slots=args.all)
    else:
        result = getattr(runner, args.action)()
    print(json.dumps(result, ensure_ascii=False))
