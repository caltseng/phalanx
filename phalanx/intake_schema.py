"""The durable intake schema (migration 2)."""

INTAKE_SCHEMA = (
    """CREATE TABLE repositories (
        id TEXT PRIMARY KEY NOT NULL,
        remote TEXT NOT NULL UNIQUE CHECK (length(trim(remote)) > 0),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE work_items (
        id TEXT PRIMARY KEY NOT NULL,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        request_json TEXT NOT NULL CHECK (json_valid(request_json)),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE executions (
        id TEXT PRIMARY KEY NOT NULL,
        item_id TEXT NOT NULL REFERENCES work_items(id),
        previous_id TEXT,
        version INTEGER NOT NULL CHECK (version >= 1),
        state_json TEXT NOT NULL CHECK (json_valid(state_json)),
        stage TEXT GENERATED ALWAYS AS (json_extract(state_json, '$.stage')) VIRTUAL NOT NULL,
        status TEXT GENERATED ALWAYS AS (json_extract(state_json, '$.status')) VIRTUAL NOT NULL
            CHECK (status IN ('queued', 'running', 'waiting_for_input', 'waiting_for_external',
                              'completed', 'failed', 'cancelled')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (item_id, id),
        UNIQUE (previous_id),
        FOREIGN KEY (item_id, previous_id) REFERENCES executions(item_id, id),
        CHECK (json_type(state_json, '$.final_output') IS NOT NULL),
        CHECK ((status IN ('completed', 'failed', 'cancelled'))
               = (json_type(state_json, '$.final_output') = 'object'))
    )""",
    """CREATE UNIQUE INDEX one_open_execution ON executions(item_id)
        WHERE status NOT IN ('completed', 'failed', 'cancelled')""",
    """CREATE INDEX executions_by_item ON executions(item_id, created_at, id)""",
    """CREATE TABLE attempts (
        id TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL REFERENCES executions(id),
        stage TEXT NOT NULL,
        runner_id TEXT NOT NULL,
        lease_digest TEXT NOT NULL,
        lease_expires_at INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT
    )""",
    "CREATE INDEX attempts_by_execution ON attempts(execution_id, stage)",
    """CREATE UNIQUE INDEX one_open_attempt ON attempts(execution_id)
        WHERE ended_at IS NULL""",
    """CREATE TABLE transition_history (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        version INTEGER NOT NULL CHECK (version >= 1),
        attempt_id TEXT REFERENCES attempts(id),
        event TEXT NOT NULL,
        details_json TEXT NOT NULL CHECK (json_valid(details_json)),
        state_json TEXT NOT NULL CHECK (json_valid(state_json)),
        created_at TEXT NOT NULL,
        PRIMARY KEY (execution_id, version)
    )""",
    """CREATE TABLE report_drafts (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        revision TEXT NOT NULL,
        markdown TEXT NOT NULL CHECK (length(trim(markdown)) > 0),
        PRIMARY KEY (execution_id, revision)
    )""",
    """CREATE TABLE reports (
        execution_id TEXT PRIMARY KEY NOT NULL REFERENCES executions(id),
        markdown TEXT NOT NULL CHECK (length(trim(markdown)) > 0)
    )""",
    """CREATE TABLE request_receipts (
        scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_json TEXT NOT NULL CHECK (json_valid(response_json)),
        PRIMARY KEY (scope, idempotency_key)
    )""",
)
