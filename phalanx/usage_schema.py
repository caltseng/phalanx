USAGE_SCHEMA = (
    """CREATE TABLE writer_usage (
        writer_id TEXT PRIMARY KEY NOT NULL REFERENCES writers(id),
        sequence INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        snapshot_json TEXT,
        failure_json TEXT
    )""",
    "CREATE INDEX writers_by_item ON writers(item_id, created_at)",
)
