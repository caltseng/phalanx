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

# Rework loops: re-entering one of these stages starts a new revision round.
REWORK_ENTRY = "('development', 'pr_revision', 'report_drafting')"

# PR facts come exclusively from the reconciler, never writer claims or lifecycle evidence.
PR_COLUMNS = """
    execution_id,
    json_extract(observation_json, '$.pr.url') AS url,
    json_extract(observation_json, '$.pr.number') AS number,
    json_extract(observation_json, '$.pr.state') AS state,
    json_extract(observation_json, '$.pr.draft') AS draft,
    json_extract(observation_json, '$.pr.merged') AS merged,
    json_extract(observation_json, '$.pr.head_sha') AS head_sha,
    json_extract(observation_json, '$.pr.additions') AS additions,
    json_extract(observation_json, '$.pr.deletions') AS deletions,
    json_extract(observation_json, '$.checks') AS ci,
    json_extract(observation_json, '$.pr.head_sha') AS ci_head_sha,
    json_extract(observation_json, '$.duplicate_count') AS duplicate_count
"""

DASHBOARD_SCHEMA = (
    """ALTER TABLE work_items ADD COLUMN label TEXT
       GENERATED ALWAYS AS (json_extract(request_json, '$.label')) VIRTUAL""",
    "CREATE INDEX work_items_by_label ON work_items(label, created_at, id) WHERE label IS NOT NULL",
    """ALTER TABLE executions ADD COLUMN outcome TEXT GENERATED ALWAYS AS (CASE
           WHEN status IN ('failed', 'cancelled') THEN status
           WHEN status <> 'completed' THEN NULL
           WHEN json_type(state_json, '$.final_output.reference') IS NOT NULL THEN 'report_stored'
           WHEN json_type(state_json, '$.final_output.merge_commit') = 'text' THEN 'checked_in'
           WHEN json_extract(state_json, '$.final_output.draft') = 1 THEN 'draft_published'
           ELSE 'ready_for_check_in' END) VIRTUAL""",
    "ALTER TABLE operator_progress ADD COLUMN revision_round INTEGER NOT NULL DEFAULT 1",
    "DROP TRIGGER operator_progress_insert",
    """CREATE TRIGGER operator_progress_insert AFTER INSERT ON executions BEGIN
           INSERT INTO operator_progress (execution_id, last_advanced_at, wait_since) VALUES (
               NEW.id, NEW.created_at,
               CASE WHEN json_extract(NEW.state_json, '$.wait') IS NOT NULL
                    THEN NEW.updated_at END
           );
       END""",
    f"""UPDATE operator_progress SET revision_round = 1 + (
           SELECT count(*) FROM (
               SELECT json_extract(state_json, '$.stage') AS stage,
                      lag(json_extract(state_json, '$.stage')) OVER (ORDER BY version) AS prior
               FROM transition_history WHERE execution_id = operator_progress.execution_id
           ) WHERE prior IS NOT NULL AND stage IS NOT prior AND stage IN {REWORK_ENTRY})""",
    f"""CREATE TRIGGER operator_revision_round AFTER UPDATE OF state_json ON executions
       WHEN NEW.stage IS NOT OLD.stage AND NEW.stage IN {REWORK_ENTRY}
       BEGIN
           UPDATE operator_progress SET revision_round = revision_round + 1
           WHERE execution_id = NEW.id;
       END""",
    """CREATE INDEX operator_observed ON github_observations
       (execution_id, last_observed_at DESC, observed_at DESC)""",
    f"CREATE INDEX operator_retry ON executions(retry_at) WHERE {OPEN}",
    "CREATE INDEX operator_lease ON attempts(lease_expires_at) WHERE ended_at IS NULL",
    f"""CREATE TABLE operator_pr AS SELECT {PR_COLUMNS}
        FROM github_observations AS observation
        WHERE (observation.pr_url, observation.head_sha, observation.digest) = (
            SELECT latest.pr_url, latest.head_sha, latest.digest FROM github_observations AS latest
            WHERE latest.execution_id = observation.execution_id
            ORDER BY latest.last_observed_at DESC, latest.observed_at DESC LIMIT 1)
        """,
    "CREATE UNIQUE INDEX operator_pr_execution ON operator_pr(execution_id)",
    *(
        f"""CREATE TRIGGER operator_pr_{name} AFTER {event} ON github_observations
            BEGIN
                INSERT OR REPLACE INTO operator_pr SELECT {PR_COLUMNS}
                FROM github_observations WHERE execution_id = NEW.execution_id
                ORDER BY last_observed_at DESC, observed_at DESC LIMIT 1;
            END"""
        for name, event in (("insert", "INSERT"), ("update", "UPDATE OF last_observed_at"))
    ),
    "CREATE TABLE operator_change (token TEXT NOT NULL)",
    "INSERT INTO operator_change VALUES (hex(randomblob(16)))",
    *(
        f"""CREATE TRIGGER operator_change_{table}_{event.lower()} AFTER {event} ON {table}
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END"""
        for table in (
            "work_items", "executions", "attempts", "dispatch_settings", "writers",
            "external_rechecks", "attempt_workers", "github_observations", "transition_history",
            "local_check_results", "captain_actions",
        )
        for event in ("INSERT", "UPDATE", "DELETE")
    ),
)
