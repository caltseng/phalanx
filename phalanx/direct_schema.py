"""Server state for direct sessions: Captains, registered writers and deviation flags."""

DIRECT_SCHEMA = (
    """CREATE TABLE captains (
        id TEXT PRIMARY KEY NOT NULL,
        session_ref TEXT NOT NULL,
        charter_revision TEXT NOT NULL,
        parent_id TEXT REFERENCES captains(id),
        state TEXT NOT NULL CHECK (state IN ('active', 'ending', 'ended')),
        wake_count INTEGER NOT NULL DEFAULT 0,
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
