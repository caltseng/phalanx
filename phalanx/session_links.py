"""Recorded native session identities; never launch or probe an application."""

import re


NATIVE_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
LINK = re.compile(rf"(?:ghapp://sessions/|codex://threads/)({NATIVE_ID})")
PREFIX = {"copilot-app": "ghapp://sessions/", "codex": "codex://threads/"}


def session_id(reference):
    match = LINK.fullmatch(reference or "")
    return match[1] if match else reference


def session_reference(reference, runtime=None):
    """Keep unknown/legacy references intact instead of guessing their host."""
    prefix = PREFIX.get(runtime)
    if prefix and re.fullmatch(NATIVE_ID, reference):
        return prefix + reference
    return reference


def session_key(reference, runtime=None):
    if runtime is None:
        runtime = next((name for name, prefix in PREFIX.items()
                        if (reference or "").startswith(prefix)), None)
    return runtime, session_id(reference)


def availability_reports(db):
    """Latest explicit observations, including reports about another Captain's sessions."""
    return {
        session_key(row["session_id"], row["runtime"] or session_key(row["session_ref"])[0]):
            row["availability"]
        for row in db.execute(
            """SELECT json_extract(session.value, '$.id') AS session_id,
                      json_extract(session.value, '$.availability') AS availability,
                      json_extract(session.value, '$.runtime') AS runtime, captains.session_ref
               FROM captains, json_each(captains.sessions_json) AS session
               WHERE json_extract(session.value, '$.availability') IN ('present', 'missing', 'deleted')
               ORDER BY captains.heartbeat_at, captains.rowid""")
    }
