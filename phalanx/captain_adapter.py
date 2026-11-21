"""Bounded JSON subprocess boundary for an attended worker runtime bridge.

The external bridge owns actual project discovery, worker launch and observation.
This module validates its replies; it does not simulate native acceptance.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryFile
from time import monotonic
from typing import Callable
from uuid import UUID

from .runner_errors import RunnerError
from .attended_recovery import NativeObservation
from .handoff import _unique_object


MAX_ADAPTER_REPLY_BYTES = 1024 * 1024


class LaunchNotDelivered(RunnerError):
    """The transport proves the app never received the request, so it may be sent again."""


# Version 2 renamed the operations to resolve_project, launch and observe.
BRIDGE_CONTRACT_VERSION = 2


class CommandTransport:
    def __init__(self, command: list[str], *, timeout: float = 15):
        if not command or not 0 < timeout <= 30:
            raise RunnerError("An adapter command and a timeout from 0 to 30 seconds are required")
        self.command = command
        self.timeout = timeout

    def __call__(self, operation: str, arguments: dict) -> dict:
        request = {"contract_version": BRIDGE_CONTRACT_VERSION, "operation": operation, "arguments": arguments}
        process = None
        try:
            # Inherited pipes can keep communicate() draining after the helper
            # is killed on Windows. Files require neither EOF nor a writer join.
            with TemporaryFile() as request_file, TemporaryFile() as response_file:
                request_file.write(json.dumps(request).encode("utf-8"))
                request_file.seek(0)
                deadline = monotonic() + self.timeout
                process = subprocess.Popen(
                    self.command, stdin=request_file, stdout=response_file, stderr=subprocess.DEVNULL,
                    shell=False,
                    env={key: value for key, value in os.environ.items()
                         if key.upper() not in {"PHALANX_RUNNER_TOKEN", "PHALANX_OWNER_TOKEN", "PHALANX_GITHUB_TOKEN"}},
                )
                while process.poll() is None:
                    if os.fstat(response_file.fileno()).st_size > MAX_ADAPTER_REPLY_BYTES:
                        raise RunnerError(f"Bridge adapter {operation} response exceeds the size limit")
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(self.command, self.timeout)
                    try:
                        process.wait(timeout=min(remaining, 0.05))
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise RunnerError(f"Bridge adapter {operation} failed with exit code {process.returncode}")
                response_file.seek(0)
                raw = response_file.read(MAX_ADAPTER_REPLY_BYTES + 1)
                if len(raw) > MAX_ADAPTER_REPLY_BYTES:
                    raise RunnerError(f"Bridge adapter {operation} response exceeds the size limit")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RunnerError(f"Bridge adapter {operation} unavailable; reconcile before retrying launch") from error
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired as error:
                    raise RunnerError(f"Bridge adapter {operation} helper did not exit; retain its reservation") from error
        try:
            response = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (ValueError, RecursionError) as error:
            raise RunnerError(f"Bridge adapter {operation} returned invalid JSON") from error
        if not isinstance(response, dict):
            raise RunnerError(f"Bridge adapter {operation} must return a JSON object")
        return response


@dataclass(frozen=True)
class ObservationBatch:
    threads: tuple[NativeObservation, ...]
    discovery_complete: bool
    cursors: dict[str, str | None]
    observed_at: datetime
    errors: dict[str, str] = field(default_factory=dict)


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("An offset-aware native timestamp is required")
    return result


class BridgeAdapter:
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
            raise RunnerError("Project discovery did not verify the exact host, project and checkout")

    def launch(self, launch: dict) -> dict:
        response = self.transport("launch", {
            "host_id": launch["host_id"], "marker": launch["marker"],
            "session": launch["session"],
        })
        if response.get("host_id") != launch["host_id"] or response.get("marker") != launch["marker"]:
            raise RunnerError("Launch receipt does not match the exact marker and host")
        native, queued = response.get("thread_id"), response.get("client_thread_id")
        if bool(native) == bool(queued):
            raise RunnerError("Launch requires exactly one native or queued receipt")
        field, value = ("thread_id", native) if native else ("client_thread_id", queued)
        try:
            return {field: str(UUID(value))}
        except (ValueError, TypeError, AttributeError) as error:
            raise RunnerError("Launch receipt must contain a UUID") from error

    def observe(self, references: list[dict], *, cursors: dict[str, str],
                     timeout: float) -> ObservationBatch:
        response = self.transport("observe", {
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
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise RunnerError("Observation has invalid or stale identity evidence") from error
        threads, errors = [], {}
        markers = {ref["marker"] for ref in references}
        for row in rows:
            try:
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
                marker = row.get("marker") if isinstance(row, dict) else None
                marker = marker if isinstance(marker, str) and marker in markers else ""
                errors[marker] = f"Malformed native observation for {marker or 'unidentified writer'}: {error}"
                if not marker:
                    complete = False
        return ObservationBatch(tuple(threads), complete, next_cursors, observed, errors)
