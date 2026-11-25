"""Server state for direct sessions: Captains, registered writers and deviation flags."""

DIRECT_SCHEMA = (
    """CREATE TABLE captains (
        id TEXT PRIMARY KEY NOT NULL,
        session_ref TEXT NOT NULL,
        charter_revision TEXT NOT NULL,
        pickup_json TEXT NOT NULL DEFAULT '{"repositories":{},"runner_id":null}',
        parent_id TEXT REFERENCES captains(id),
        state TEXT NOT NULL CHECK (state IN ('active', 'ending', 'ended')),
        wake_count INTEGER NOT NULL DEFAULT 0,
        reinforced_interval INTEGER NOT NULL DEFAULT 0 CHECK (reinforced_interval >= 0),
        focus_json TEXT NOT NULL DEFAULT '[]',
        sessions_json TEXT NOT NULL DEFAULT '[]',
        handoff_note TEXT,
        created_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL
    )""",
    """CREATE TABLE writers (
        id TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL REFERENCES executions(id),
        item_id TEXT NOT NULL REFERENCES work_items(id),
        captain_id TEXT NOT NULL REFERENCES captains(id),
        runtime TEXT NOT NULL,
        nonce TEXT NOT NULL UNIQUE,
        token_digest TEXT NOT NULL UNIQUE,
        replaced_id TEXT REFERENCES writers(id),
        attempt_id TEXT REFERENCES attempts(id),
        state TEXT NOT NULL CHECK (state IN ('launching', 'busy', 'idle', 'stopped', 'error')),
        session_id TEXT,
        session_url TEXT,
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        abandoned_at TEXT,
        state_reported_at TEXT,
        last_progress_at TEXT,
        progress_note TEXT,
        ended_at TEXT,
        end_reason TEXT
    )""",
    "CREATE UNIQUE INDEX one_open_writer ON writers(execution_id) WHERE ended_at IS NULL",
    """CREATE TABLE flags (
        kind TEXT NOT NULL,
        item_id TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT '',
        captain_id TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        recovery TEXT NOT NULL,
        notify_owner INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, item_id, execution_id, captain_id)
    )""",
)

# Shift handoff: when a Captain started ending, and the actions Captains record on item timelines.
CAPTAIN_SHIFT_SCHEMA = (
    "ALTER TABLE captains ADD COLUMN ending_at TEXT",
    # Sessions were bare IDs and are now objects. The next heartbeat reports them again.
    "UPDATE captains SET sessions_json = '[]'",
    """CREATE TABLE captain_actions (
        id TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL REFERENCES executions(id),
        captain_id TEXT NOT NULL REFERENCES captains(id),
        version INTEGER NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX captain_actions_by_execution ON captain_actions (execution_id, created_at)",
)

# Report finalization closed executions without ending their writers. End those writers as
# intake.end_writers does.
CLOSED_WRITER_MIGRATION = (
    """UPDATE writers SET ended_at = strftime('%Y-%m-%dT%H:%M:%f000+00:00', 'now'),
           end_reason = 'closed as ' || (SELECT status FROM executions WHERE id = writers.execution_id),
           attempt_id = NULL
       WHERE ended_at IS NULL AND execution_id IN (
           SELECT id FROM executions WHERE status IN ('completed', 'failed', 'cancelled'))""",
)

SESSION_ARCHIVE_SCHEMA = (
    "CREATE INDEX writers_by_captain ON writers(captain_id, ended_at)",
    "CREATE INDEX writers_by_session ON writers(session_id)",
    """CREATE TABLE session_archives (
        session_id TEXT PRIMARY KEY NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('archived', 'blocked', 'refused')),
        reason TEXT NOT NULL,
        reported_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX unresolved_session_archives ON session_archives(updated_at)
       WHERE outcome != 'archived'""",
)

CAPTAIN_COMPACTION_SCHEMA = (
    "ALTER TABLE captains ADD COLUMN compaction_count INTEGER CHECK (compaction_count >= 0)",
)
