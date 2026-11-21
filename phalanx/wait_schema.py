"""Operator inbox and durable external rechecks."""

WAIT_SCHEMA = (
    """CREATE INDEX input_wait_inbox ON executions(updated_at, id)
       WHERE status = 'waiting_for_input'""",
    """CREATE TABLE external_rechecks (
        execution_id TEXT PRIMARY KEY NOT NULL REFERENCES executions(id),
        version INTEGER NOT NULL,
        due_at_ms INTEGER NOT NULL
    )""",
    "CREATE INDEX external_rechecks_due ON external_rechecks(due_at_ms, execution_id)",
)
