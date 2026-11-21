"""Schema for pending report finalization, indexed independently of dispatch."""

FINALIZATION_SCHEMA = (
    """CREATE TABLE report_finalizations (
        execution_id TEXT PRIMARY KEY NOT NULL,
        intent_version INTEGER NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0,
        due_at INTEGER,
        FOREIGN KEY (execution_id, intent_version)
            REFERENCES transition_history(execution_id, version)
    )""",
    """CREATE INDEX report_finalizations_due ON report_finalizations(due_at, execution_id)
        WHERE due_at IS NOT NULL""",
)


def enqueue(db, execution_id, intent_version):
    db.execute(
        "INSERT INTO report_finalizations(execution_id, intent_version, due_at) VALUES (?, ?, 0)",
        (execution_id, intent_version),
    )
