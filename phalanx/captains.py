"""Captain registration, heartbeats, liveness, handoff and timeline actions."""

from datetime import UTC, datetime, timedelta
import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints

from . import intake
from .intake_models import Identifier, Payload, Text
from .session_links import session_id as native_session_id, session_key, session_target


# A Captain whose last heartbeat is older than this is stale (D5).
STALE = timedelta(minutes=15)
# A Captain still ending this long after it started its handoff is treated as dead.
ENDING_LIMIT = timedelta(minutes=30)
IDLE_WITH_WORK = timedelta(minutes=30)
REINFORCEMENT = timedelta(minutes=90)
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CaptainState = Literal["active", "ending", "ended"]
# A Captain moves only forward: active, then ending, then ended.
ORDER = {"active": 0, "ending": 1, "ended": 2}



class Pickup(Payload):
    repositories: dict[Identifier, Text | None] = Field(default_factory=dict, max_length=100)


class CaptainRegistration(Payload):
    session_ref: Text
    charter_revision: Short
    parent_id: Identifier | None = None
    pickup: Pickup = Field(default_factory=Pickup)


class Session(Payload):
    """A native session the Captain can see, by its runtime ID and display name."""
    id: Short
    name: Short
    writer_nonce: Short | None = None
    availability: Literal["present", "missing", "deleted"] | None = None
    runtime: Short | None = None


class Heartbeat(Payload):
    charter_revision: Short
    wake_count: Annotated[int, Field(strict=True, ge=0)]
    compaction_count: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)] | None = None
    state: CaptainState
    focus: list[Identifier] = Field(default_factory=list, max_length=100)
    sessions: list[Session] = Field(default_factory=list, max_length=500)
    handoff_note: Text | None = None
    pickup: Pickup | None = None


class Action(Payload):
    captain_id: Identifier
    action: Short
    reason: Text


class ReinforcementAcknowledgement(Payload):
    interval: Annotated[int, Field(strict=True, ge=1)]


class SessionArchive(Payload):
    session_id: Short
    outcome: Literal["archived", "blocked", "refused"]
    reason: Text
    reported_by: Short


def archive_status(db, session_id):
    row = db.execute("SELECT * FROM session_archives WHERE session_id=?", (session_id,)).fetchone()
    writers = [dict(writer) for writer in db.execute(
        "SELECT id, item_id, execution_id, ended_at FROM writers WHERE session_id=?", (session_id,))]
    captains = [row for row in db.execute("SELECT * FROM captains")
                if native_session_id(row["session_ref"]) == session_id]
    return {
        "record": dict(row) if row else None,
        "writers": writers,
        "active": any(db.execute(
                      "SELECT 1 FROM writers WHERE item_id=? AND ended_at IS NULL LIMIT 1",
                      (writer["item_id"],)).fetchone() for writer in writers)
                  or any(captain["state"] != "ended" or open_intents(db, captain["id"])
                         or cleanup_pending(db, captain["id"])
                         or not ((captain["handoff_note"] or "").startswith("Stop condition met:")
                                 or db.execute("SELECT 1 FROM captains WHERE parent_id=? LIMIT 1",
                                               (captain["id"],)).fetchone())
                         for captain in captains),
    }


def cleanup_pending(db, captain_id):
    return db.execute(
        """SELECT count(*) FROM writers w LEFT JOIN session_archives a ON a.session_id=w.session_id
           WHERE w.captain_id=? AND w.session_id IS NOT NULL
             AND (w.ended_at IS NULL OR a.outcome IS NULL OR a.outcome='blocked')""",
        (captain_id,)).fetchone()[0]


def record_archive(db, payload):
    current = archive_status(db, payload.session_id)
    if current["active"] and payload.outcome == "archived":
        raise intake.Conflict("The session still owns a writer or an unfinished Captain handoff")
    previous = current["record"]
    if previous and previous["outcome"] == "archived":
        return previous
    if previous and previous["outcome"] == "refused" and payload.outcome != "archived":
        return previous
    db.execute(
        """INSERT INTO session_archives VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (session_id) DO UPDATE SET outcome=excluded.outcome, reason=excluded.reason,
               reported_by=excluded.reported_by, updated_at=excluded.updated_at""",
        (payload.session_id, payload.outcome, payload.reason, payload.reported_by, intake.timestamp()),
    )
    return archive_status(db, payload.session_id)["record"]


def captain_row(db, captain_id):
    row = db.execute("SELECT * FROM captains WHERE id=?", (captain_id,)).fetchone()
    if row is None:
        raise intake.Missing("Captain not found")
    return row


def require_acting_captain(db, captain_id, *, message="An ended Captain cannot act"):
    row = captain_row(db, captain_id)
    if row["state"] == "ended":
        raise intake.Conflict(message)
    return row


def gone_at(row):
    """When a Captain stops counting as live: a missed heartbeat, or a handoff that ran too long."""
    if row["state"] == "ended":
        return datetime.fromisoformat(row["heartbeat_at"])
    gone = datetime.fromisoformat(row["heartbeat_at"]) + STALE
    if row["ending_at"] is not None:
        gone = min(gone, datetime.fromisoformat(row["ending_at"]) + ENDING_LIMIT)
    return gone


def is_live(row, now):
    return row["state"] != "ended" and now < gone_at(row)


def live_captains(db, now):
    """Live active and ending registrations, keyed by ID in heartbeat order."""
    return {row["id"]: row for row in db.execute(
        "SELECT * FROM captains WHERE state != 'ended' ORDER BY heartbeat_at, id")
        if is_live(row, now)}


def active_holds(db, now, *, live=None):
    """Current-version holds remain advisory and expire with their Captain or after 30 minutes."""
    live = live_captains(db, now) if live is None else live
    if not live:
        return {}
    return {
        row["execution_id"]: row["reason"]
        for row in db.execute(
            """SELECT action.* FROM captain_actions AS action
               JOIN executions AS execution ON execution.id = action.execution_id
               WHERE action.action = 'held' AND action.created_at > ?
                 AND execution.status = 'queued' AND action.version = execution.version
                 AND NOT EXISTS (
                     SELECT 1 FROM captain_actions AS latest
                     WHERE latest.execution_id = action.execution_id AND latest.version = action.version
                       AND (latest.created_at, latest.rowid) > (action.created_at, action.rowid))""",
            ((now - IDLE_WITH_WORK).isoformat(timespec="microseconds"),))
        if row["captain_id"] in live
    }


def open_intents(db, captain_id):
    """Writer intents this Captain launched and has neither confirmed, abandoned nor ended."""
    return [dict(row) for row in db.execute(
        """SELECT id AS writer_id, item_id, execution_id, nonce, created_at FROM writers
           WHERE captain_id=? AND confirmed_at IS NULL AND abandoned_at IS NULL AND ended_at IS NULL
           ORDER BY created_at, id""", (captain_id,))]


def availability_reports(db):
    """Latest provider-scoped native observations, including other Captains' sessions."""
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


def captain_details(db, captain_ids):
    """Load list details in batches, retaining the per-Captain ended-writer limit."""
    details = {key: {"open_intents": [], "successors": [], "cleanup_pending": 0, "ended_writers": []}
               for key in captain_ids}
    if not details:
        return details
    placeholders = ",".join("?" for _ in details)
    parameters = tuple(details)
    for row in db.execute(
            f"""SELECT captain_id, id AS writer_id, item_id, execution_id, nonce, created_at FROM writers
                WHERE captain_id IN ({placeholders}) AND confirmed_at IS NULL
                  AND abandoned_at IS NULL AND ended_at IS NULL ORDER BY created_at, id""", parameters):
        entry = dict(row)
        details[entry.pop("captain_id")]["open_intents"].append(entry)
    for row in db.execute(
            f"SELECT parent_id, id FROM captains WHERE parent_id IN ({placeholders}) ORDER BY created_at, id",
            parameters):
        details[row["parent_id"]]["successors"].append(row["id"])
    for row in db.execute(
            f"""SELECT w.captain_id, count(*) AS pending
                FROM writers w LEFT JOIN session_archives a ON a.session_id=w.session_id
                WHERE w.captain_id IN ({placeholders}) AND w.session_id IS NOT NULL
                  AND (w.ended_at IS NULL OR a.outcome IS NULL OR a.outcome='blocked')
                GROUP BY w.captain_id""", parameters):
        details[row["captain_id"]]["cleanup_pending"] = row["pending"]
    for row in db.execute(
            f"""SELECT * FROM (
                    SELECT w.captain_id, w.id, w.item_id, w.execution_id, w.session_id, w.ended_at, w.end_reason,
                           a.outcome AS archive_outcome, a.reason AS archive_reason,
                           row_number() OVER (
                               PARTITION BY w.captain_id ORDER BY a.updated_at, w.ended_at, w.id) AS position
                    FROM writers w LEFT JOIN session_archives a ON a.session_id=w.session_id
                    WHERE w.captain_id IN ({placeholders}) AND w.ended_at IS NOT NULL
                      AND w.session_id IS NOT NULL AND (a.outcome IS NULL OR a.outcome='blocked'))
                WHERE position <= 100 ORDER BY captain_id, position""", parameters):
        entry = dict(row)
        entry.pop("position")
        details[entry.pop("captain_id")]["ended_writers"].append(entry)
    return details


def view(db, row, now=None, session_reports=None, live=None, details=None):
    now = now or datetime.now(UTC)
    live = live_captains(db, now) if live is None else live
    availability = (availability_reports(db) if session_reports is None else session_reports).get(
        session_key(row["session_ref"]), "unverified")
    return {
        "id": row["id"], "session_ref": row["session_ref"], "charter_revision": row["charter_revision"],
        "pickup": json.loads(row["pickup_json"]),
        "session_availability": availability,
        "session_link": session_target(row["session_ref"], availability=availability),
        "parent_id": row["parent_id"], "state": row["state"], "wake_count": row["wake_count"],
        "compaction_count": row["compaction_count"],
        "reinforcement": reinforcement(row, now),
        "focus": json.loads(row["focus_json"]), "sessions": json.loads(row["sessions_json"]),
        "handoff_note": row["handoff_note"], "created_at": row["created_at"],
        "heartbeat_at": row["heartbeat_at"], "ending_at": row["ending_at"], "live": row["id"] in live,
        "stale": row["state"] != "ended" and row["id"] not in live,
        **(captain_details(db, [row["id"]])[row["id"]] if details is None else details),
        "successor_name": successor_name(row["id"]),
    }


def reinforcement(row, now):
    interval = max(0, (now - datetime.fromisoformat(row["created_at"])) // REINFORCEMENT)
    return {"interval_minutes": 90, "interval": interval,
            "acknowledged_interval": row["reinforced_interval"],
            "due": row["state"] == "active" and interval > row["reinforced_interval"]}


def acknowledge_reinforcement(db, captain_id, payload):
    row = captain_row(db, captain_id)
    now = datetime.now(UTC)
    current = reinforcement(row, now)
    if payload.interval > max(current["interval"], current["acknowledged_interval"]):
        raise intake.Conflict("Cannot acknowledge a future reinforcement interval")
    db.execute("UPDATE captains SET reinforced_interval=max(reinforced_interval, ?) WHERE id=?",
               (payload.interval, captain_id))
    return reinforcement(captain_row(db, captain_id), now)


def successor_name(captain_id):
    """The session name for this Captain's successor. It is the launch nonce, so a rerun finds the same chat."""
    return f"phx captain {captain_id[:8]}"


def register(db, payload):
    if payload.parent_id is not None:
        captain_row(db, payload.parent_id)
    captain_id = uuid4().hex
    now = intake.timestamp()
    db.execute(
        """INSERT INTO captains (id, session_ref, charter_revision, parent_id, state, created_at, heartbeat_at,
                                 pickup_json)
           VALUES (?, ?, ?, ?, 'active', ?, ?, ?)""",
        (captain_id, payload.session_ref, payload.charter_revision, payload.parent_id, now, now,
         intake.encode(payload.pickup.model_dump())),
    )
    return view(db, captain_row(db, captain_id))


def heartbeat(db, captain_id, payload):
    row = captain_row(db, captain_id)
    if ORDER[payload.state] < ORDER[row["state"]]:
        raise intake.Conflict(f"A Captain that is {row['state']} cannot become {payload.state}. "
                              "Register a new Captain")
    now = intake.timestamp()
    if payload.pickup is not None:
        db.execute("UPDATE captains SET pickup_json=? WHERE id=?",
                   (intake.encode(payload.pickup.model_dump()), captain_id))
    elif payload.charter_revision != row["charter_revision"]:
        db.execute("UPDATE captains SET pickup_json=? WHERE id=?",
                   (intake.encode(Pickup().model_dump()), captain_id))
    # Each session keeps the time it was first reported, so its flags keep their first_seen.
    seen = {session["id"]: session["first_seen"] for session in json.loads(row["sessions_json"])}
    sessions = [{**session.model_dump(exclude_none=True), "first_seen": seen.get(session.id, now)}
                for session in payload.sessions]
    ending_at = row["ending_at"] or (now if payload.state == "ending" else None)
    # Native counters are cumulative for this session; retries and older reports cannot add or undo one.
    compaction_count = row["compaction_count"]
    if payload.compaction_count is not None:
        compaction_count = max(compaction_count or 0, payload.compaction_count)
    db.execute(
        """UPDATE captains SET charter_revision=?, wake_count=?, state=?, focus_json=?, sessions_json=?,
               handoff_note=?, heartbeat_at=?, ending_at=?, compaction_count=? WHERE id=?""",
        (payload.charter_revision, payload.wake_count, payload.state, intake.encode(payload.focus),
         intake.encode(sessions), payload.handoff_note, now, ending_at, compaction_count, captain_id),
    )
    return view(db, captain_row(db, captain_id))


def captains(db, limit):
    now = datetime.now(UTC)
    rows = db.execute(
        "SELECT * FROM captains ORDER BY state = 'ended', heartbeat_at DESC, id LIMIT ?", (limit,),
    ).fetchall()
    reports = availability_reports(db)
    live = live_captains(db, now)
    details = captain_details(db, [row["id"] for row in rows])
    return {"captains": [view(db, row, now, reports, live, details[row["id"]]) for row in rows]}


def current_captain(db):
    """Resolve routing from all active registrations, never the recent-history page."""
    now = datetime.now(UTC)
    candidates = [
        {"id": row["id"], "session_ref": row["session_ref"]}
        for row in sorted(live_captains(db, now).values(), key=lambda row: (row["created_at"], row["id"]))
        if row["state"] == "active"
    ]
    captain = None
    if not candidates:
        reason = "No live active Captain; ending, ended and stale registrations are not routing destinations"
    elif len(candidates) != 1:
        reason = f"{len(candidates)} live active Captains; no unique routing destination"
    elif not candidates[0]["session_ref"].strip():
        reason = "The live active Captain has no native session binding"
    else:
        captain = candidates[0]
        reason = "Unique live active registration; verify its session_ref and conversation link in the runtime"
    return {"captain": captain, "candidates": candidates, "reason": reason,
            "observed_at": now.isoformat(timespec="microseconds")}


def record_action(db, item_id, payload):
    """Put a Captain's action and its reason on the item's current execution timeline."""
    require_acting_captain(db, payload.captain_id)
    execution = intake.current_execution(db, item_id)
    action_id = uuid4().hex
    db.execute("INSERT INTO captain_actions VALUES (?, ?, ?, ?, ?, ?, ?)",
               (action_id, execution["id"], payload.captain_id, execution["version"], payload.action,
                payload.reason, intake.timestamp()))
    return {"id": action_id, "item_id": item_id, "execution_id": execution["id"],
            "version": execution["version"], **payload.model_dump()}
