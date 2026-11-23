"""Build a bridge observe reply from worker session transcripts.

Some runtimes expose a session's first prompt only in a local transcript. The
Captain lists its worker sessions and their state with its own tools, then runs
`relay observe` to match each session's launch prompt against the queued
references. Each runtime that needs a transcript has one reader module, named
in READERS, returning `{"prompt", "started_at"}` for a session ID.
"""

from datetime import datetime, timezone
from importlib import import_module
import json

from .runner_errors import RunnerError


READERS = {"copilot": "phalanx.copilot_transcript"}
STATES = {"active", "stopped", "unknown"}


def reader(runtime: str):
    if runtime not in READERS:
        raise RunnerError(f"No transcript reader for runtime {runtime!r}; known: {', '.join(sorted(READERS))}")
    return import_module(READERS[runtime]).read_session


def _packet_identity(prompt: str):
    _, found, rest = prompt.partition("Work packet:\n")
    if not found:
        return None
    try:
        packet, _ = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return None
    return packet.get("result_identity") if isinstance(packet, dict) else None


def build_reply(request: dict, sessions: list[dict], *, host_id: str, complete: bool,
                now: datetime | None = None) -> dict:
    """Return threads whose launch prompt carries a queued reference's exact marker and assignment.

    Each session is `{"thread_id", "project_id", "state", "prompt", "started_at"}`,
    plus an optional `blocker` when the session waits for the owner.
    The marker line is compared after trimming trailing whitespace, which some
    runtimes add or remove when they store the prompt.
    """
    if request.get("operation") != "observe":
        raise RunnerError("relay observe answers only an observe request")
    references = request["arguments"]["references"]
    threads = []
    for session in sessions:
        if session["state"] not in STATES:
            raise RunnerError(f"Session state must be one of {', '.join(sorted(STATES))}")
        if session.get("blocker") and session["state"] == "stopped":
            raise RunnerError(f"Session {session['thread_id']} waits for the owner, so it is not stopped; "
                              "report it active")
        lines = {line.rstrip() for line in session["prompt"].splitlines()}
        identity = _packet_identity(session["prompt"])
        for reference in references:
            if (reference["host_id"] != host_id or reference["project_id"] != session["project_id"]
                    or f"Phalanx attempt marker: {reference['marker']}" not in lines
                    or identity != reference["assignment"]):
                continue
            threads.append({
                "host_id": host_id, "project_id": session["project_id"],
                "thread_id": session["thread_id"], "started_at": session["started_at"],
                "marker": reference["marker"], "assignment": reference["assignment"],
                "state": session["state"],
                **({"writers_stopped": True} if session["state"] == "stopped" else {}),
                **({"blocker": session["blocker"]} if session.get("blocker") else {}),
            })
    return {
        "observed_at": (now or datetime.now(timezone.utc)).isoformat(),
        "discovery_complete": complete, "threads": threads,
    }
