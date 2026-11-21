"""Single-slot attended Captain bridge. Native app calls belong to the Captain."""

import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .codex_handoff import CodexHandoff, WorkPacket
from .workspaces import WorkspaceError, WorkspaceManager


class RunnerError(RuntimeError):
    pass


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

    def call(self, path, body=None, *, lease=None, key=None):
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
                return json.load(response)
        except HTTPError as exc:
            raise RunnerError(f"Runner API returned HTTP {exc.code} for {path}") from None
        except (URLError, TimeoutError, ValueError):
            raise RunnerError(f"Runner API response unavailable for {path}; reservation retained") from None


STAGES = {
    "development": "Commit the candidate locally. Put the full commit hash alone in details.evidence.",
    "report_drafting": "Put the field report Markdown in details.evidence.",
}


class AttendedRunner:
    def __init__(self, root, config, api):
        self.root = root.resolve()
        self.config = config
        self.api = api
        self.root.mkdir(parents=True, exist_ok=True)
        self.results = self.root / "results"
        self.results.mkdir(exist_ok=True)
        self.journal = self.root / "attended.sqlite3"

    def _transaction(self, action):
        with closing(sqlite3.connect(self.journal, timeout=30)) as db, db:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS slot (id INTEGER PRIMARY KEY, record TEXT)")
            row = db.execute("SELECT record FROM slot WHERE id=1").fetchone()
            record = json.loads(row[0]) if row else {}
            archived = record.copy() if "submission" in record else None
            result = action(record)
            if archived is not None and not record:
                archived.pop("lease")
                db.execute("CREATE TABLE IF NOT EXISTS completed (attempt_id TEXT PRIMARY KEY, record TEXT)")
                db.execute("INSERT INTO completed VALUES (?, ?)",
                           (archived["claim"]["attempt_id"], json.dumps(archived)))
            if record:
                db.execute("INSERT OR REPLACE INTO slot VALUES (1, ?)", (json.dumps(record),))
            else:
                db.execute("DELETE FROM slot WHERE id=1")
            return result

    def _manager(self, mapping, remote):
        return WorkspaceManager(Path(mapping["checkout"]), remote, self.root / "worktrees")

    def prepare(self):
        def reserve(record):
            if record:
                return record["lease"]
            registrations = self.api.call("/repositories")
            eligible = {}
            blocked = []
            for registered in registrations:
                repo = registered["id"]
                mapping = self.config["repositories"].get(repo)
                if mapping is None:
                    continue
                try:
                    self._manager(mapping, registered["remote"]).verify_checkout()
                except WorkspaceError:
                    blocked.append(repo)
                    continue
                eligible[repo] = registered["remote"]
            if not eligible:
                raise RunnerError("No registered checkout passed verification; check local mappings and origins")
            record.update(lease=secrets.token_urlsafe(32), runner_id=self.config["runner_id"],
                          repositories=eligible, blocked=blocked, stages=sorted(STAGES))
            return record["lease"]

        lease = self._transaction(reserve)

        def ensure_reservation(record):
            if record.get("lease") != lease:
                raise RunnerError("The original reservation has ended; inspect status")

        def claim(record):
            ensure_reservation(record)
            if "claim" in record:
                return None
            request = {
                "runner_id": record["runner_id"], "repository_ids": sorted(record["repositories"]),
                "slots": 1,
            }
            if "stages" in record:
                request["stages"] = record["stages"]
            result = self.api.call("/dispatch/claim", request, lease=record["lease"])
            if result["status"] != "claimed":
                status = {"status": result["status"], "unverified_mappings": record["blocked"]}
                record.clear()
                return status
            record.update(claim=result, lease_expires_at=result["lease_expires_at"])
            return None

        result = self._transaction(claim)
        if result is not None:
            return result

        snapshot = self._transaction(dict)
        ensure_reservation(snapshot)
        launch = self._prepare_launch(snapshot)

        def commit_launch(record):
            ensure_reservation(record)
            if "packet" in record:
                raise RunnerError("Launch intent already issued; inspect status and reconcile in Codex")
            if "recover_stop" in record:
                raise RunnerError("Recovery owns this reservation; inspect attended-recover")
            for field in ("packet", "project_id", "host_id", "checkout", "started_at"):
                record[field] = snapshot[field]
            record["lease_expires_at"] = max(record["lease_expires_at"], snapshot["lease_expires_at"])

        self._transaction(commit_launch)
        return launch

    def _prepare_launch(self, record):
        if "packet" in record:
            raise RunnerError("Launch intent already issued; inspect status and reconcile in Codex")
        claim = record["claim"]
        execution = claim["execution"]
        stage = execution["state"]["stage"]
        if stage not in STAGES:
            raise RunnerError(f"Stage {stage} needs a different handler; claim retained for recovery")
        request = claim["request"]
        repo = request["repository_id"]
        mapping = self.config["repositories"][repo]
        base = request.get("target_branch") or mapping["base_branch"]
        self._renew(record)
        workspace = self._manager(mapping, record["repositories"][repo]).prepare(
            claim["item_id"], base=f"refs/remotes/origin/{base}",
        )
        candidate = execution["state"].get("candidate")
        if candidate and candidate != workspace.head:
            raise RunnerError("Worktree HEAD differs from the current server candidate; reconcile before launch")
        input_reply = claim.get("input_reply")
        packet = WorkPacket(
            item_id=claim["item_id"], attempt_id=UUID(claim["attempt_id"]),
            state_version=execution["version"], stage=stage, repository=repo,
            worktree=workspace.path, branch=workspace.branch, expected_revision=workspace.head,
            goal=request["goal"], output_type="report" if request["target"] == "report" else "pull_request",
            evidence_requirements=STAGES[stage], base_branch=base,
            dependencies=tuple(claim.get("dependencies", ())),
            context=json.dumps({field: request.get(field, []) for field in
                                ("context_links", "acceptance_criteria")}, ensure_ascii=False),
            handoff=json.dumps(input_reply, ensure_ascii=False) if input_reply is not None else "",
        )
        handoff = CodexHandoff(packet, self.results)
        if os.path.lexists(handoff.result_path) or os.path.lexists(handoff.temporary_path):
            raise RunnerError("Attempt already has local evidence; reconcile before launch")
        current = self.api.call(f"/items/{packet.item_id}")["execution"]
        if (current["id"] != execution["id"] or current["version"] != packet.state_version
                or current["state"]["status"] != "running" or current["state"].get("stopping")):
            raise RunnerError("Claimed execution changed before launch; reconcile the reservation")
        self._renew(record)
        record.update(packet={**asdict(packet), "attempt_id": str(packet.attempt_id),
                              "worktree": str(packet.worktree)},
                      project_id=mapping["project_id"], host_id=mapping["host_id"],
                      checkout=mapping["checkout"], started_at=datetime.now(timezone.utc).isoformat())
        return {"status": "launch_intent", "marker": packet.marker, "host_id": record["host_id"],
                "unverified_mappings": record["blocked"],
                "create_thread": handoff.create_thread_arguments(record["project_id"])}

    def status(self):
        def view(record):
            if not record:
                return {"status": "idle"}
            packet = record.get("packet")
            claim = record.get("claim")
            if "submission" in record:
                status = "submitting"
            elif "receipt" in record:
                status = "attached" if "thread_id" in record["receipt"] else "launch_pending"
            elif packet:
                status = "launch_uncertain"
            else:
                status = "claimed" if claim else "claim_pending"
            return {"status": status, "packet": packet, "attempt_id": claim and claim["attempt_id"],
                    "stage": claim and claim["execution"]["state"]["stage"],
                    "project_id": record.get("project_id"), "host_id": record.get("host_id"),
                    "started_at": record.get("started_at"), "receipt": record.get("receipt"),
                    "lease_expires_at": record.get("lease_expires_at")}

        return self._transaction(view)

    def receipt(self, marker, host_id, *, thread_id=None, client_thread_id=None):
        def save(record):
            packet = self._handoff(record).packet
            if marker != packet.marker or host_id != record["host_id"]:
                raise RunnerError("Receipt does not match the launch marker and host")
            if bool(thread_id) == bool(client_thread_id):
                raise RunnerError("Supply exactly one native thread or queued client thread ID")
            receipt = ({"thread_id": str(UUID(thread_id))} if thread_id else
                       {"client_thread_id": str(UUID(client_thread_id))})
            current = record.get("receipt")
            if current and current != receipt:
                if "thread_id" in current or "thread_id" not in receipt:
                    raise RunnerError("Receipt conflicts with the recorded identity")
                receipt.update(current)
            elif current:
                return {"status": "receipt_recorded", **current}
            record["receipt"] = receipt
            return {"status": "receipt_recorded", **receipt}

        return self._transaction(save)

    def _renew(self, record):
        claim = record.get("claim")
        if not claim:
            raise RunnerError("There is no known claim to renew")
        endpoint = f"/dispatch/runners/{record['runner_id']}/attempts/{claim['attempt_id']}/renew"
        result = self.api.call(endpoint, {}, lease=record["lease"])
        record["lease_expires_at"] = result["lease_expires_at"]
        return result

    def renew(self):
        return self._transaction(self._renew)

    def _handoff(self, record):
        if "packet" not in record:
            raise RunnerError("No durable launch intent exists")
        packet = record["packet"]
        return CodexHandoff(WorkPacket(**{
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
            workspace = self._manager(
                {"checkout": record["checkout"]}, record["repositories"][packet.repository],
            ).inspect(packet.item_id)
            if workspace.path != packet.worktree or workspace.branch != packet.branch:
                raise RunnerError("Result worktree no longer matches the launch intent")
            body = {"attempt_id": record["claim"]["attempt_id"], "stage": packet.stage,
                    "expected_version": packet.state_version, "outcome": result.outcome}
            if packet.output_type == "report" and workspace.head != packet.expected_revision:
                raise RunnerError("Report checkout HEAD changed during drafting")
            if result.outcome == "succeeded":
                evidence = result.details["evidence"].strip()
                if packet.stage == "development":
                    if (not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", evidence)
                            or evidence != workspace.head or workspace.dirty
                            or evidence == packet.expected_revision):
                        raise RunnerError("Development requires a new candidate at the clean worktree HEAD")
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
                if any(len(text.strip()) > 16000 for text in result.details.values()):
                    raise RunnerError("Worker question exceeds the API limit")
                body["question"] = {
                    "prompt": result.details["question"],
                    "options": [result.details["options"]],
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
            return submission

        # Workspace inspection can run Git commands. Keep the journal available
        # for renewals and verify the reservation again before saving evidence.
        snapshot = self._transaction(dict)
        submission = verify(snapshot)

        def save_submission(record):
            if (record.get("lease") != snapshot.get("lease")
                    or record.get("receipt") != snapshot.get("receipt")):
                raise RunnerError("The original reservation ended; inspect status")
            if "submission" not in record:
                record["submission"] = submission

        self._transaction(save_submission)

        def send(record):
            packet = self._handoff(record).packet
            if packet.marker != marker:
                raise RunnerError("A different launch owns the slot")
            claim = record["claim"]
            path = f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/result"
            response = self.api.call(path, record["submission"]["body"],
                                     lease=record["lease"], key=marker)
            record.clear()
            return response

        return self._transaction(send)


def add_attended_command(commands):
    parser = commands.add_parser("attended", help="operate a single attended Captain slot")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "status", "renew"):
        actions.add_parser(action)
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
    if not token and args.action in {"prepare", "renew", "submit"}:
        raise RunnerError("Set PHALANX_RUNNER_TOKEN in the runner environment")
    runner = AttendedRunner(args.state, config, RunnerAPI(config["base_url"], token))
    if args.action == "receipt":
        result = runner.receipt(args.marker, args.host_id, thread_id=args.thread_id,
                                client_thread_id=args.client_thread_id)
    elif args.action == "submit":
        result = runner.submit(args.marker, args.host_id, args.thread_id)
    else:
        result = getattr(runner, args.action)()
    print(json.dumps(result, ensure_ascii=False))
