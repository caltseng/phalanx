"""Schema for dependency links, registered when claim enforcement is wired."""

DEPENDENCY_SCHEMA = (
    """CREATE TABLE work_dependencies (
        item_id TEXT NOT NULL REFERENCES work_items(id),
        upstream_id TEXT NOT NULL REFERENCES work_items(id),
        milestone TEXT NOT NULL CHECK (milestone IN ('report', 'ready_for_check_in', 'checked_in')),
        PRIMARY KEY (item_id, upstream_id),
        CHECK (item_id <> upstream_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX dependencies_by_upstream ON work_dependencies(upstream_id, item_id)""",
)
