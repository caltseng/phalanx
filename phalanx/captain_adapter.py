"""Bounded JSON subprocess boundary for an attended Codex desktop bridge.

The external bridge owns actual desktop discovery, thread creation and reads.
This module validates its replies; it does not simulate native acceptance.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Callable
from uuid import UUID

from .attended import RunnerError
from .attended_recovery import NativeObservation
from .codex_handoff import _unique_object


class CommandTransport:
    def __init__(self, command: list[str], *, timeout: float = 15):
        if not command or not 0 < timeout <= 30:
            raise RunnerError("An adapter command and a timeout from 0 to 30 seconds are required")
        self.command = command
        self.timeout = timeout

    def __call__(self, operation: str, arguments: dict) -> dict:
        request = {"contract_version": 1, "operation": operation, "arguments": arguments}
        try:
            result = subprocess.run(
                self.command, input=json.dumps(request), encoding="utf-8",
                capture_output=True, timeout=self.timeout, shell=False,
                env={key: value for key, value in os.environ.items()
                     if key.upper() not in {"PHALANX_RUNNER_TOKEN", "PHALANX_OWNER_TOKEN", "PHALANX_GITHUB_TOKEN"}},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RunnerError(f"Desktop adapter {operation} unavailable; reconcile before retrying launch") from error
        if result.returncode:
            raise RunnerError(f"Desktop adapter {operation} failed with exit code {result.returncode}")
        try:
            response = json.loads(result.stdout, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError) as error:
            raise RunnerError(f"Desktop adapter {operation} returned invalid JSON") from error
        if not isinstance(response, dict):
            raise RunnerError(f"Desktop adapter {operation} must return a JSON object")
        return response


@dataclass(frozen=True)
class ObservationBatch:
    threads: tuple[NativeObservation, ...]
    discovery_complete: bool
    cursors: dict[str, str | None]
    observed_at: datetime


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("An offset-aware native timestamp is required")
    return result


class CodexCLIAdapter:
    def __init__(self, transport: Callable[[str, dict], dict]):
        self.transport = transport

    def resolve_project(self, mapping: dict) -> None:
        expected = {field: mapping[field] for field in ("host_id", "project_id", "checkout")}
        response = self.transport("resolve_project", expected)
        if (
            response.get("host_id") != expected["host_id"]
            or response.get("project_id") != expected["project_id"]
            or not isinstance(response.get("checkout"), str)
            or Path(response["checkout"]).resolve(strict=True) != Path(expected["checkout"]).resolve(strict=True)
        ):
            raise RunnerError("Desktop project discovery did not verify the exact host, project and checkout")

    def launch(self, launch: dict) -> dict:
        response = self.transport("create_thread", {
            "host_id": launch["host_id"], "marker": launch["marker"],
            "create_thread": launch["create_thread"],
        })
        if response.get("host_id") != launch["host_id"] or response.get("marker") != launch["marker"]:
            raise RunnerError("Desktop launch receipt does not match the exact marker and host")
        native, queued = response.get("thread_id"), response.get("client_thread_id")
        if bool(native) == bool(queued):
            raise RunnerError("Desktop launch requires exactly one native or queued receipt")
        field, value = ("thread_id", native) if native else ("client_thread_id", queued)
        try:
            return {field: str(UUID(value))}
        except (ValueError, TypeError, AttributeError) as error:
            raise RunnerError("Desktop launch receipt must contain a UUID") from error

    def wait_threads(self, references: list[dict], *, cursors: dict[str, str],
                     timeout: float) -> ObservationBatch:
        response = self.transport("wait_threads", {
            "references": references, "cursors": cursors, "timeout_seconds": timeout,
        })
        try:
            observed = _timestamp(response["observed_at"])
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            complete = response["discovery_complete"]
            next_cursors = response.get("cursors", {})
            rows = response["threads"]
            if (
                not -5 <= age <= 60 or type(complete) is not bool or not isinstance(rows, list)
                or not isinstance(next_cursors, dict)
                or set(next_cursors) - {ref["marker"] for ref in references}
                or any(value is not None and not isinstance(value, str) for value in next_cursors.values())
            ):
                raise ValueError("Invalid freshness, discovery or cursor evidence")
            threads = []
            for row in rows:
                state = row["state"]
                started = _timestamp(row["started_at"])
                if (
                    state not in {"active", "stopped", "unknown"} or started > observed
                    or not isinstance(row["assignment"], dict)
                    or any(not isinstance(row[field], str) or not row[field]
                           for field in ("host_id", "project_id", "marker"))
                    or (state == "stopped" and row.get("writers_stopped") is not True)
                ):
                    raise ValueError("Invalid native state or stopped-writer attestation")
                threads.append(NativeObservation(
                    host_id=row["host_id"], project_id=row["project_id"],
                    thread_id=UUID(row["thread_id"]), started_at=started,
                    marker=row["marker"], assignment=row["assignment"], state=state,
                    client_thread_id=UUID(row["client_thread_id"]) if row.get("client_thread_id") else None,
                ))
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise RunnerError("Desktop observation has invalid or stale identity evidence") from error
        return ObservationBatch(tuple(threads), complete, next_cursors, observed)
