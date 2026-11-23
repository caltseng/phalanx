"""Reconciler observations, local check results and heartbeats; see reconciliation.py."""

RECONCILIATION_SCHEMA = (
    """CREATE TABLE github_observations (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        pr_url TEXT NOT NULL,
        head_sha TEXT NOT NULL,
        digest TEXT NOT NULL,
        version INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        observation_json TEXT NOT NULL CHECK (json_valid(observation_json)),
        PRIMARY KEY (execution_id, pr_url, head_sha, digest)
    ) WITHOUT ROWID""",
    "CREATE INDEX github_observations_latest ON github_observations (execution_id, last_observed_at)",
    """CREATE TABLE local_check_results (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        head_sha TEXT NOT NULL,
        version INTEGER NOT NULL,
        checked_at TEXT NOT NULL,
        failures_json TEXT NOT NULL CHECK (json_valid(failures_json)),
        PRIMARY KEY (execution_id, head_sha)
    ) WITHOUT ROWID""",
    """CREATE TABLE reconciler_heartbeats (
        runner_id TEXT PRIMARY KEY NOT NULL,
        last_seen TEXT NOT NULL
    )""",
)
