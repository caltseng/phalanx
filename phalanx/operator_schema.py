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

PR_OBSERVED = ("json_type({row}.details_json, '$.pr_url') = 'text' "
               "AND json_type({row}.details_json, '$.pull.number') = 'integer' "
               "AND json_type({row}.details_json, '$.pull.head_sha') = 'text' "
               "AND json_type({row}.details_json, '$.pull.state') = 'text'")
# Rework loops: re-entering one of these stages starts a new revision round.
REWORK_ENTRY = "('development', 'pr_revision', 'report_drafting')"

# PH-29a dashboard reads: labels, outcomes, PR facts the runner observed, revision rounds
# and runner presence.
DASHBOARD_SCHEMA = (
    """ALTER TABLE work_items ADD COLUMN label TEXT
       GENERATED ALWAYS AS (json_extract(request_json, '$.label')) VIRTUAL""",
    "CREATE INDEX work_items_by_label ON work_items(label, created_at, id) WHERE label IS NOT NULL",
    """ALTER TABLE executions ADD COLUMN outcome TEXT GENERATED ALWAYS AS (CASE
           WHEN status IN ('failed', 'cancelled') THEN status
           WHEN status <> 'completed' THEN NULL
           WHEN json_type(state_json, '$.final_output.reference') IS NOT NULL THEN 'report_stored'
           WHEN json_type(state_json, '$.final_output.merge_commit') = 'text' THEN 'checked_in'
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
    # The latest PR the runner observed for each execution. CI and size stay known only
    # while the observed head is unchanged.
    """CREATE TABLE operator_pr (
        execution_id TEXT PRIMARY KEY NOT NULL REFERENCES executions(id),
        version INTEGER NOT NULL,
        url TEXT NOT NULL,
        number INTEGER NOT NULL,
        state TEXT NOT NULL,
        draft INTEGER,
        merged INTEGER,
        head_sha TEXT NOT NULL,
        additions INTEGER,
        deletions INTEGER,
        ci TEXT,
        ci_head_sha TEXT,
        duplicate_count INTEGER NOT NULL
    )""",
    f"""INSERT INTO operator_pr
       SELECT latest.execution_id, latest.version,
              json_extract(latest.details_json, '$.pr_url'),
              json_extract(latest.details_json, '$.pull.number'),
              json_extract(latest.details_json, '$.pull.state'),
              json_extract(latest.details_json, '$.pull.draft'),
              json_extract(latest.details_json, '$.pull.merged'),
              json_extract(latest.details_json, '$.pull.head_sha'),
              NULL, NULL, gate.ci, gate.head,
              coalesce((SELECT json_array_length(published.details_json, '$.duplicate_numbers')
                        FROM transition_history AS published
                        WHERE published.execution_id = latest.execution_id
                          AND json_type(published.details_json, '$.duplicate_numbers') = 'array'
                        ORDER BY published.version DESC LIMIT 1), 0)
       FROM transition_history AS latest
       LEFT JOIN (
           SELECT execution_id, json_extract(details_json, '$.pull.head_sha') AS head,
                  json_extract(details_json, '$.gate_result.state') AS ci, max(version)
           FROM transition_history
           WHERE json_type(details_json, '$.gate_result') = 'object'
           GROUP BY execution_id, head
       ) AS gate ON gate.execution_id = latest.execution_id
              AND gate.head = json_extract(latest.details_json, '$.pull.head_sha')
       WHERE {PR_OBSERVED.format(row="latest")} AND latest.version = (
           SELECT max(other.version) FROM transition_history AS other
           WHERE other.execution_id = latest.execution_id AND {PR_OBSERVED.format(row="other")})""",
    f"""CREATE TRIGGER operator_pr_observed AFTER INSERT ON transition_history
       WHEN {PR_OBSERVED.format(row="NEW")}
       BEGIN
           INSERT INTO operator_pr VALUES (
               NEW.execution_id, NEW.version,
               json_extract(NEW.details_json, '$.pr_url'),
               json_extract(NEW.details_json, '$.pull.number'),
               json_extract(NEW.details_json, '$.pull.state'),
               json_extract(NEW.details_json, '$.pull.draft'),
               json_extract(NEW.details_json, '$.pull.merged'),
               json_extract(NEW.details_json, '$.pull.head_sha'),
               json_extract(NEW.details_json, '$.pull.additions'),
               json_extract(NEW.details_json, '$.pull.deletions'),
               json_extract(NEW.details_json, '$.gate_result.state'),
               CASE WHEN json_type(NEW.details_json, '$.gate_result') = 'object'
                    THEN json_extract(NEW.details_json, '$.pull.head_sha') END,
               coalesce(json_array_length(NEW.details_json, '$.duplicate_numbers'), 0)
           ) ON CONFLICT (execution_id) DO UPDATE SET
               version = excluded.version, url = excluded.url, number = excluded.number,
               state = excluded.state, draft = excluded.draft, merged = excluded.merged,
               head_sha = excluded.head_sha,
               additions = CASE WHEN excluded.additions IS NOT NULL OR excluded.head_sha IS NOT head_sha
                                THEN excluded.additions ELSE additions END,
               deletions = CASE WHEN excluded.deletions IS NOT NULL OR excluded.head_sha IS NOT head_sha
                                THEN excluded.deletions ELSE deletions END,
               ci = CASE WHEN excluded.ci_head_sha IS NOT NULL OR excluded.head_sha IS NOT ci_head_sha
                         THEN excluded.ci ELSE ci END,
               ci_head_sha = CASE WHEN excluded.ci_head_sha IS NOT NULL
                                       OR excluded.head_sha IS NOT ci_head_sha
                                  THEN excluded.ci_head_sha ELSE ci_head_sha END,
               duplicate_count = CASE
                   WHEN json_type(NEW.details_json, '$.duplicate_numbers') = 'array'
                   THEN excluded.duplicate_count ELSE duplicate_count END;
       END""",
    # Last time each runner polled for work or renewed a lease, and the slots it offered.
    """CREATE TABLE runners (
        id TEXT PRIMARY KEY NOT NULL,
        slots INTEGER,
        last_seen_at TEXT NOT NULL
    )""",
)
