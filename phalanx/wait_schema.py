"""Operator inbox indexing."""

WAIT_SCHEMA = (
    """CREATE INDEX input_wait_inbox ON executions(updated_at, id)
       WHERE status = 'waiting_for_input'""",
)
