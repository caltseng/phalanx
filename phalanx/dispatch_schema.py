"""Indexed scheduling metadata and runner settings."""

DISPATCH_SCHEMA = (
    "ALTER TABLE executions ADD COLUMN queue_repository TEXT REFERENCES repositories(id)",
    "ALTER TABLE executions ADD COLUMN queue_affinity TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE executions ADD COLUMN queue_priority INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE executions ADD COLUMN ready_since INTEGER",
    "ALTER TABLE executions ADD COLUMN retry_at INTEGER",
    "ALTER TABLE executions ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    """CREATE INDEX queue_heads ON executions
        (queue_repository, queue_affinity, queue_priority, ready_since, item_id, id)
        WHERE status = 'queued' AND ready_since IS NOT NULL""",
    """CREATE TABLE dispatch_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
        aging_seconds INTEGER NOT NULL CHECK (aging_seconds > 0)
    )""",
    "INSERT INTO dispatch_settings VALUES (1, 0, 300)",
    """CREATE TABLE runner_limits (
        runner_id TEXT PRIMARY KEY NOT NULL,
        capacity INTEGER NOT NULL CHECK (capacity >= 1)
    )""",
)


# An adoption re-leases the same open attempt without changing the execution's
# version, so the timeline shows it beside the version it happened at.
ADOPTION_SCHEMA = (
    """CREATE TABLE attempt_adoptions (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        version INTEGER NOT NULL,
        attempt_id TEXT NOT NULL REFERENCES attempts(id),
        runner_id TEXT NOT NULL,
        reconciliation TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX adoptions_by_execution ON attempt_adoptions(execution_id, version, created_at)",
)


STAGE_QUEUE_SCHEMA = (
    """CREATE INDEX queue_stage_heads ON executions
        (queue_repository, queue_affinity, queue_priority, stage, ready_since, item_id, id)
        WHERE status = 'queued' AND ready_since IS NOT NULL""",
)


CONCURRENCY_SCHEMA = (
    "ALTER TABLE dispatch_settings ADD COLUMN max_writers INTEGER NOT NULL DEFAULT 5 "
    "CHECK (typeof(max_writers) = 'integer' AND max_writers > 0)",
)


WRITER_CAPACITY_SCHEMA = (
    "ALTER TABLE dispatch_settings ADD COLUMN max_building_writers INTEGER NOT NULL DEFAULT 5 "
    "CHECK (typeof(max_building_writers) = 'integer' AND max_building_writers > 0)",
    "UPDATE dispatch_settings SET max_building_writers=min(5, max_writers)",
    # Conservatively keep every existing worker allocated until it follows the new handoff protocol.
    "ALTER TABLE writers ADD COLUMN building INTEGER NOT NULL DEFAULT 1 CHECK (building IN (0, 1))",
    "ALTER TABLE writers ADD COLUMN building_requested_at TEXT",
    "ALTER TABLE writers ADD COLUMN capacity_revision INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE executions ADD COLUMN handoff_head TEXT",
    """CREATE INDEX waiting_writer_builds ON writers(building_requested_at, id)
       WHERE ended_at IS NULL AND building=0 AND building_requested_at IS NOT NULL""",
)
