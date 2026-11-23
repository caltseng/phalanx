"""Indexes and timestamps for bounded operator reads."""

CLOSED = "status IN ('completed', 'failed', 'cancelled')"
OPEN = f"NOT ({CLOSED})"
INPUT_RANK = "CASE status WHEN 'waiting_for_input' THEN 0 ELSE 1 END"

OPERATOR_SCHEMA = (
    f"""CREATE INDEX operator_open ON executions (({INPUT_RANK}), created_at, id)
        WHERE {OPEN}""",
    f"""CREATE INDEX operator_closed ON executions (updated_at DESC, id DESC)
        WHERE {CLOSED}""",
    """CREATE TABLE operator_progress (
        execution_id TEXT PRIMARY KEY NOT NULL REFERENCES executions(id),
        last_advanced_at TEXT NOT NULL,
        wait_since TEXT
    )""",
    # Reconstruct the latest stage change and latest wait change from PH-05 history.
    """INSERT INTO operator_progress
       WITH changes AS (
           SELECT execution_id, version,
                  json_extract(state_json, '$.stage') AS stage,
                  json_extract(state_json, '$.wait') AS wait,
                  lag(json_extract(state_json, '$.stage')) OVER history AS prior_stage,
                  lag(json_extract(state_json, '$.wait')) OVER history AS prior_wait
           FROM transition_history
           WINDOW history AS (PARTITION BY execution_id ORDER BY version)
       ), latest AS (
           SELECT execution_id,
                  max(CASE WHEN stage IS NOT prior_stage THEN version END) AS advanced_version,
                  max(CASE WHEN wait IS NOT prior_wait THEN version END) AS wait_version
           FROM changes GROUP BY execution_id
       )
       SELECT latest.execution_id, advanced.created_at, waiting.created_at
       FROM latest
       JOIN transition_history AS advanced
           ON advanced.execution_id = latest.execution_id AND advanced.version = latest.advanced_version
       LEFT JOIN transition_history AS waiting
           ON waiting.execution_id = latest.execution_id AND waiting.version = latest.wait_version""",
    """UPDATE operator_progress SET wait_since = NULL
       WHERE execution_id IN (
           SELECT id FROM executions WHERE json_extract(state_json, '$.wait') IS NULL
       )""",
    """CREATE TRIGGER operator_progress_insert AFTER INSERT ON executions BEGIN
           INSERT INTO operator_progress VALUES (
               NEW.id, NEW.created_at,
               CASE WHEN json_extract(NEW.state_json, '$.wait') IS NOT NULL
                    THEN NEW.updated_at END
           );
       END""",
    """CREATE TRIGGER operator_progress_update AFTER UPDATE OF state_json ON executions
       WHEN NEW.stage IS NOT OLD.stage
         OR json_extract(NEW.state_json, '$.wait') IS NOT json_extract(OLD.state_json, '$.wait')
       BEGIN
           UPDATE operator_progress
           SET last_advanced_at = CASE WHEN NEW.stage IS NOT OLD.stage THEN NEW.updated_at
                                       ELSE last_advanced_at END,
               wait_since = CASE
                   WHEN json_extract(NEW.state_json, '$.wait') IS NULL THEN NULL
                   WHEN json_extract(NEW.state_json, '$.wait')
                        IS NOT json_extract(OLD.state_json, '$.wait') THEN NEW.updated_at
                   ELSE wait_since END
           WHERE execution_id = NEW.id;
       END""",
)

# Opaque worker session reference and optional link that the runner's bridge supplied.
WORKER_SCHEMA = (
    """CREATE TABLE attempt_workers (
        attempt_id TEXT PRIMARY KEY NOT NULL REFERENCES attempts(id),
        reference TEXT NOT NULL,
        url TEXT
    )""",
)

# The latest blocker a runner reported for its open attempt, shown as the item's wait reason.
BLOCKER_SCHEMA = ("ALTER TABLE attempts ADD COLUMN blocker TEXT",)

# When the current blocker was first reported and when the current prerequisite wait began.
# Rows from before this migration keep NULL, which the board shows as unavailable.
WAIT_START_SCHEMA = (
    "ALTER TABLE attempts ADD COLUMN blocker_since TEXT",
    "ALTER TABLE executions ADD COLUMN dependency_wait_since TEXT",
)
