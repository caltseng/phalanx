"""Indexed scheduling metadata and runner settings."""

DISPATCH_SCHEMA = (
    "ALTER TABLE executions ADD COLUMN queue_repository TEXT REFERENCES repositories(id)",
    "ALTER TABLE executions ADD COLUMN queue_affinity TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE executions ADD COLUMN queue_priority INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE executions ADD COLUMN ready_since INTEGER",
    "ALTER TABLE executions ADD COLUMN retry_at INTEGER",
    "ALTER TABLE executions ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    """UPDATE executions SET
        queue_repository = (SELECT repository_id FROM work_items WHERE id = executions.item_id),
        queue_priority = (SELECT CASE json_extract(request_json, '$.priority')
            WHEN 'urgent' THEN 2 WHEN 'low' THEN 0 ELSE 1 END
            FROM work_items WHERE id = executions.item_id),
        ready_since = CASE WHEN status = 'queued' THEN CAST(
            round((unixepoch(updated_at, 'subsec')) * 1000) AS INTEGER) END
        WHERE status NOT IN ('completed', 'failed', 'cancelled')""",
    """CREATE INDEX queue_heads ON executions
        (queue_repository, queue_affinity, queue_priority, ready_since, item_id, id)
        WHERE status = 'queued' AND ready_since IS NOT NULL""",
    """CREATE INDEX open_attempts_by_runner ON attempts(runner_id, lease_expires_at, id)
        WHERE ended_at IS NULL""",
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
