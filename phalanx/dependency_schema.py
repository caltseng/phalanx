"""Dependency links and indexed eligibility for queued executions."""

DEPENDENCY_SCHEMA = (
    """CREATE TABLE work_dependencies (
        item_id TEXT NOT NULL REFERENCES work_items(id),
        upstream_id TEXT NOT NULL REFERENCES work_items(id),
        milestone TEXT NOT NULL CHECK (milestone IN ('report', 'ready_for_check_in', 'checked_in')),
        PRIMARY KEY (item_id, upstream_id),
        CHECK (item_id <> upstream_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX dependencies_by_upstream ON work_dependencies(upstream_id, item_id)""",
    """ALTER TABLE executions ADD COLUMN dependency_wait_json TEXT
        CHECK (dependency_wait_json IS NULL OR json_valid(dependency_wait_json))""",
    "DROP INDEX queue_heads",
    """CREATE INDEX queue_heads ON executions
        (queue_repository, queue_affinity, queue_priority, ready_since, item_id, id)
        WHERE status = 'queued' AND ready_since IS NOT NULL AND dependency_wait_json IS NULL""",
    """CREATE TABLE dependency_refresh_jobs (
        upstream_id TEXT PRIMARY KEY NOT NULL REFERENCES work_items(id),
        cursor TEXT NOT NULL,
        scheduled_at INTEGER NOT NULL
    )""",
    """CREATE INDEX dependency_refresh_order ON dependency_refresh_jobs(scheduled_at, upstream_id)""",
    """CREATE TABLE dependency_audit (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        cursor TEXT
    )""",
    "INSERT INTO dependency_audit (id, cursor) VALUES (1, NULL)",
)
