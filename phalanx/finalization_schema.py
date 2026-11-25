"""Schedule pending report finalization independently of dispatch."""


def enqueue(db, execution_id, intent_version):
    db.execute(
        "INSERT INTO report_finalizations(execution_id, intent_version, due_at) VALUES (?, ?, 0)",
        (execution_id, intent_version),
    )
