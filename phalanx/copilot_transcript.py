"""GitHub Copilot transcript reader for `relay observe`.

Copilot CLI and the GitHub Copilot app keep each session's events in
`~/.copilot/session-state/<session id>/events.jsonl`.
"""

import json
from pathlib import Path
from uuid import UUID

from .runner_errors import RunnerError


def read_session(session_id: str, home: Path | None = None) -> dict:
    """Return the session's first user prompt and start time."""
    try:
        session_id = str(UUID(session_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise RunnerError("A Copilot session ID is a UUID") from error
    path = (home or Path.home()) / ".copilot" / "session-state" / session_id / "events.jsonl"
    started = None
    try:
        with path.open(encoding="utf-8") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                data = event.get("data") if isinstance(event, dict) else None
                if not isinstance(data, dict):
                    continue
                if event.get("type") == "session.start":
                    started = data.get("startTime") or event.get("timestamp")
                elif event.get("type") == "user.message" and isinstance(data.get("content"), str):
                    started = started or event.get("timestamp")
                    if not started:
                        break
                    return {"prompt": data["content"], "started_at": started.replace("Z", "+00:00")}
    except OSError as error:
        raise RunnerError(f"Cannot read the Copilot transcript for session {session_id}: {error}") from error
    raise RunnerError(f"The Copilot transcript for session {session_id} has no first prompt yet")
