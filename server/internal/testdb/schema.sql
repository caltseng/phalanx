PRAGMA application_id=1346915416;
PRAGMA user_version=26;

CREATE TABLE attempts (
        id TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL REFERENCES executions(id),
        stage TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT
    );

CREATE TABLE captain_actions (
        id TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL REFERENCES executions(id),
        captain_id TEXT NOT NULL REFERENCES captains(id),
        version INTEGER NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

CREATE TABLE captains (
        id TEXT PRIMARY KEY NOT NULL,
        session_ref TEXT NOT NULL,
        charter_revision TEXT NOT NULL,
        pickup_json TEXT NOT NULL DEFAULT '{"repositories":{}}',
        parent_id TEXT REFERENCES captains(id),
        state TEXT NOT NULL CHECK (state IN ('active', 'ending', 'ended')),
        wake_count INTEGER NOT NULL DEFAULT 0,
        reinforced_interval INTEGER NOT NULL DEFAULT 0 CHECK (reinforced_interval >= 0),
        focus_json TEXT NOT NULL DEFAULT '[]',
        sessions_json TEXT NOT NULL DEFAULT '[]',
        handoff_note TEXT,
        created_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        ending_at TEXT,
        compaction_count INTEGER CHECK (compaction_count >= 0)
    );

CREATE TABLE dependency_audit (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        cursor TEXT
    );

CREATE TABLE dependency_refresh_jobs (
        upstream_id TEXT PRIMARY KEY NOT NULL REFERENCES work_items(id),
        cursor TEXT NOT NULL,
        scheduled_at INTEGER NOT NULL
    );

CREATE TABLE dispatch_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER NOT NULL CHECK (paused IN (0, 1)),
        max_writers INTEGER NOT NULL DEFAULT 10 CHECK (typeof(max_writers) = 'integer' AND max_writers > 0),
        max_building_writers INTEGER NOT NULL DEFAULT 5
            CHECK (typeof(max_building_writers) = 'integer' AND max_building_writers > 0)
    );

CREATE TABLE executions (
        id TEXT PRIMARY KEY NOT NULL,
        item_id TEXT NOT NULL REFERENCES work_items(id),
        previous_id TEXT,
        version INTEGER NOT NULL CHECK (version >= 1),
        state_json TEXT NOT NULL CHECK (json_valid(state_json)),
        stage TEXT GENERATED ALWAYS AS (json_extract(state_json, '$.stage')) VIRTUAL NOT NULL,
        status TEXT GENERATED ALWAYS AS (json_extract(state_json, '$.status')) VIRTUAL NOT NULL
            CHECK (status IN ('queued', 'running', 'waiting_for_input', 'waiting_for_external',
                              'completed', 'failed', 'cancelled')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        queue_repository TEXT REFERENCES repositories(id),
        queue_priority INTEGER NOT NULL DEFAULT 1,
        ready_since INTEGER,
        retry_at INTEGER,
        retry_count INTEGER NOT NULL DEFAULT 0,
        dependency_wait_json TEXT CHECK (dependency_wait_json IS NULL OR json_valid(dependency_wait_json)),
        outcome TEXT GENERATED ALWAYS AS (CASE
           WHEN status IN ('failed', 'cancelled') THEN status
           WHEN status <> 'completed' THEN NULL
           WHEN json_type(state_json, '$.final_output.reference') IS NOT NULL THEN 'report_stored'
           WHEN json_type(state_json, '$.final_output.merge_commit') = 'text' THEN 'checked_in'
           WHEN json_extract(state_json, '$.final_output.draft') = 1 THEN 'draft_published'
           ELSE 'ready_for_check_in' END) VIRTUAL,
        handoff_head TEXT,
        UNIQUE (item_id, id),
        UNIQUE (previous_id),
        FOREIGN KEY (item_id, previous_id) REFERENCES executions(item_id, id),
        CHECK (json_type(state_json, '$.final_output') IS NOT NULL),
        CHECK ((status IN ('completed', 'failed', 'cancelled'))
               = (json_type(state_json, '$.final_output') = 'object'))
    );

CREATE TABLE flags (
        kind TEXT NOT NULL,
        item_id TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT '',
        captain_id TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        recovery TEXT NOT NULL,
        notify_owner INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, item_id, execution_id, captain_id)
    );

CREATE TABLE github_observations (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        pr_url TEXT NOT NULL,
        head_sha TEXT NOT NULL,
        digest TEXT NOT NULL,
        version INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        observation_json TEXT NOT NULL CHECK (json_valid(observation_json)),
        PRIMARY KEY (execution_id, pr_url, head_sha, digest)
    ) WITHOUT ROWID;

CREATE TABLE issue_scout_decisions (
        repository_id TEXT NOT NULL,
        number INTEGER NOT NULL,
        fingerprint TEXT NOT NULL,
        decision TEXT NOT NULL,
        decided_by TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        FOREIGN KEY(repository_id, number) REFERENCES issue_sources(repository_id, number)
    );

CREATE TABLE issue_scout_passes (
        id TEXT PRIMARY KEY NOT NULL,
        revision INTEGER NOT NULL,
        state TEXT NOT NULL,
        repository_index INTEGER NOT NULL,
        page INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        claim_id TEXT,
        captain_id TEXT REFERENCES captains(id),
        lease_until TEXT,
        retry_at TEXT,
        failure_kind TEXT,
        failure_status INTEGER,
        failures INTEGER NOT NULL DEFAULT 0,
        counts_json TEXT NOT NULL DEFAULT '{}'
    );

CREATE TABLE issue_scout_settings (
        id INTEGER PRIMARY KEY CHECK (id=1),
        revision INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
        cadence_minutes INTEGER NOT NULL DEFAULT 60,
        author_login TEXT,
        author_id INTEGER,
        repositories_json TEXT NOT NULL DEFAULT '[]',
        due_at TEXT,
        changed_by TEXT,
        updated_at TEXT
    );

CREATE TABLE issue_sources (
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        number INTEGER NOT NULL,
        url TEXT NOT NULL,
        author_login TEXT,
        author_id INTEGER,
        fingerprint TEXT NOT NULL,
        issue_state TEXT NOT NULL,
        state TEXT NOT NULL,
        reason TEXT NOT NULL,
        item_id TEXT REFERENCES work_items(id),
        decision TEXT,
        decided_by TEXT,
        decided_at TEXT,
        observed_by TEXT NOT NULL REFERENCES captains(id),
        observed_at TEXT NOT NULL,
        resume_trigger TEXT NOT NULL,
        PRIMARY KEY (repository_id, number)
    );

CREATE TABLE item_titles (
        item_id TEXT NOT NULL REFERENCES work_items(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        title TEXT,
        changed_by TEXT NOT NULL CHECK (changed_by = 'owner'),
        created_at TEXT NOT NULL,
        PRIMARY KEY (item_id, revision)
    );

CREATE TABLE local_check_results (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        head_sha TEXT NOT NULL,
        version INTEGER NOT NULL,
        checked_at TEXT NOT NULL,
        failures_json TEXT NOT NULL CHECK (json_valid(failures_json)),
        PRIMARY KEY (execution_id, head_sha)
    ) WITHOUT ROWID;

CREATE TABLE operator_change (token TEXT NOT NULL);

CREATE TABLE operator_pr(
  execution_id TEXT,
  url,
  number,
  state,
  draft,
  merged,
  head_sha,
  additions,
  deletions,
  ci,
  ci_head_sha,
  duplicate_count
);

CREATE TABLE operator_progress (
        execution_id TEXT PRIMARY KEY NOT NULL REFERENCES executions(id),
        last_advanced_at TEXT NOT NULL,
        wait_since TEXT,
        revision_round INTEGER NOT NULL DEFAULT 1
    );

CREATE TABLE reconciler_heartbeats (
        runner_id TEXT PRIMARY KEY NOT NULL,
        last_seen TEXT NOT NULL
    );

CREATE TABLE report_drafts (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        revision TEXT NOT NULL,
        markdown TEXT NOT NULL CHECK (length(trim(markdown)) > 0),
        PRIMARY KEY (execution_id, revision)
    );

CREATE TABLE report_finalizations (
        execution_id TEXT PRIMARY KEY NOT NULL,
        intent_version INTEGER NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0,
        due_at INTEGER,
        FOREIGN KEY (execution_id, intent_version)
            REFERENCES transition_history(execution_id, version)
    );

CREATE TABLE reports (
        execution_id TEXT PRIMARY KEY NOT NULL REFERENCES executions(id),
        markdown TEXT NOT NULL CHECK (length(trim(markdown)) > 0)
    );

CREATE TABLE repositories (
        id TEXT PRIMARY KEY NOT NULL,
        remote TEXT NOT NULL UNIQUE CHECK (length(trim(remote)) > 0),
        created_at TEXT NOT NULL
    );

CREATE TABLE request_receipts (
        scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_json TEXT NOT NULL CHECK (json_valid(response_json)),
        PRIMARY KEY (scope, idempotency_key)
    );

CREATE TABLE session_archives (
        session_id TEXT PRIMARY KEY NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('archived', 'blocked', 'refused')),
        reason TEXT NOT NULL,
        reported_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

CREATE TABLE transition_history (
        execution_id TEXT NOT NULL REFERENCES executions(id),
        version INTEGER NOT NULL CHECK (version >= 1),
        attempt_id TEXT REFERENCES attempts(id),
        event TEXT NOT NULL,
        details_json TEXT NOT NULL CHECK (json_valid(details_json)),
        state_json TEXT NOT NULL CHECK (json_valid(state_json)),
        created_at TEXT NOT NULL,
        PRIMARY KEY (execution_id, version)
    );

CREATE TABLE work_dependencies (
        item_id TEXT NOT NULL REFERENCES work_items(id),
        upstream_id TEXT NOT NULL REFERENCES work_items(id),
        milestone TEXT NOT NULL CHECK (milestone IN ('report', 'ready_for_check_in', 'checked_in')),
        PRIMARY KEY (item_id, upstream_id),
        CHECK (item_id <> upstream_id)
    ) WITHOUT ROWID;

CREATE TABLE work_items (
        id TEXT PRIMARY KEY NOT NULL,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        request_json TEXT NOT NULL CHECK (json_valid(request_json)),
        created_at TEXT NOT NULL,
        label TEXT GENERATED ALWAYS AS (json_extract(request_json, '$.label')) VIRTUAL
    );

CREATE TABLE writer_usage (
        writer_id TEXT PRIMARY KEY NOT NULL REFERENCES writers(id),
        sequence INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        snapshot_json TEXT,
        failure_json TEXT
    );

CREATE TABLE writers (
        id TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL REFERENCES executions(id),
        item_id TEXT NOT NULL REFERENCES work_items(id),
        captain_id TEXT NOT NULL REFERENCES captains(id),
        runtime TEXT NOT NULL,
        nonce TEXT NOT NULL UNIQUE,
        token_digest TEXT NOT NULL UNIQUE,
        replaced_id TEXT REFERENCES writers(id),
        attempt_id TEXT REFERENCES attempts(id),
        state TEXT NOT NULL CHECK (state IN ('launching', 'busy', 'idle', 'stopped', 'error')),
        session_id TEXT,
        session_url TEXT,
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        abandoned_at TEXT,
        state_reported_at TEXT,
        last_progress_at TEXT,
        progress_note TEXT,
        ended_at TEXT,
        end_reason TEXT,
        building INTEGER NOT NULL DEFAULT 1 CHECK (building IN (0, 1)),
        building_requested_at TEXT,
        capacity_revision INTEGER NOT NULL DEFAULT 0
    );

CREATE INDEX attempts_by_execution ON attempts(execution_id, stage);

CREATE INDEX captain_actions_by_execution ON captain_actions (execution_id, created_at);

CREATE INDEX dependencies_by_upstream ON work_dependencies(upstream_id, item_id);

CREATE INDEX dependency_refresh_order ON dependency_refresh_jobs(scheduled_at, upstream_id);

CREATE INDEX executions_by_item ON executions(item_id, created_at, id);

CREATE INDEX input_wait_inbox ON executions(updated_at, id)
       WHERE status = 'waiting_for_input';

CREATE INDEX issue_sources_needs_owner ON issue_sources(state, observed_at, repository_id, number);

CREATE UNIQUE INDEX one_active_issue_scout ON issue_scout_passes ((1))
       WHERE completed_at IS NULL;

CREATE UNIQUE INDEX one_open_attempt ON attempts(execution_id)
        WHERE ended_at IS NULL;

CREATE UNIQUE INDEX one_open_execution ON executions(item_id)
        WHERE status NOT IN ('completed', 'failed', 'cancelled');

CREATE UNIQUE INDEX one_open_writer ON writers(execution_id) WHERE ended_at IS NULL;

CREATE INDEX operator_closed ON executions (updated_at DESC, id DESC)
        WHERE status IN ('completed', 'failed', 'cancelled');

CREATE INDEX operator_observed ON github_observations
       (execution_id, last_observed_at DESC, observed_at DESC);

CREATE INDEX operator_open ON executions ((CASE status WHEN 'waiting_for_input' THEN 0 ELSE 1 END), created_at, id)
        WHERE NOT (status IN ('completed', 'failed', 'cancelled'));

CREATE UNIQUE INDEX operator_pr_execution ON operator_pr(execution_id);

CREATE INDEX operator_retry ON executions(retry_at) WHERE NOT (status IN ('completed', 'failed', 'cancelled'));

CREATE INDEX queue_heads ON executions
        (queue_repository, queue_priority, ready_since, item_id, id)
        WHERE status = 'queued' AND ready_since IS NOT NULL AND dependency_wait_json IS NULL;

CREATE INDEX queue_stage_heads ON executions
        (queue_repository, queue_priority, stage, ready_since, item_id, id)
        WHERE status = 'queued' AND ready_since IS NOT NULL AND dependency_wait_json IS NULL;

CREATE INDEX report_finalizations_due ON report_finalizations(due_at, execution_id)
        WHERE due_at IS NOT NULL;

CREATE INDEX unresolved_session_archives ON session_archives(updated_at)
       WHERE outcome != 'archived';

CREATE INDEX waiting_writer_builds ON writers(building_requested_at, id)
       WHERE ended_at IS NULL AND building=0 AND building_requested_at IS NOT NULL;

CREATE INDEX work_items_by_label ON work_items(label, created_at, id) WHERE label IS NOT NULL;

CREATE INDEX work_items_by_repository ON work_items(repository_id, created_at, id);

CREATE INDEX writers_by_captain ON writers(captain_id, ended_at);

CREATE INDEX writers_by_item ON writers(item_id, created_at);

CREATE INDEX writers_by_session ON writers(session_id);

CREATE TRIGGER operator_change_attempts_delete AFTER DELETE ON attempts
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_attempts_insert AFTER INSERT ON attempts
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_attempts_update AFTER UPDATE ON attempts
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_captain_actions_delete AFTER DELETE ON captain_actions
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_captain_actions_insert AFTER INSERT ON captain_actions
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_captain_actions_update AFTER UPDATE ON captain_actions
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_dispatch_settings_delete AFTER DELETE ON dispatch_settings
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_dispatch_settings_insert AFTER INSERT ON dispatch_settings
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_dispatch_settings_update AFTER UPDATE ON dispatch_settings
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_executions_delete AFTER DELETE ON executions
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_executions_insert AFTER INSERT ON executions
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_executions_update AFTER UPDATE ON executions
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_github_observations_delete AFTER DELETE ON github_observations
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_github_observations_insert AFTER INSERT ON github_observations
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_github_observations_update AFTER UPDATE ON github_observations
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_item_titles AFTER INSERT ON item_titles
       BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_local_check_results_delete AFTER DELETE ON local_check_results
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_local_check_results_insert AFTER INSERT ON local_check_results
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_local_check_results_update AFTER UPDATE ON local_check_results
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_transition_history_delete AFTER DELETE ON transition_history
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_transition_history_insert AFTER INSERT ON transition_history
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_transition_history_update AFTER UPDATE ON transition_history
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_work_items_delete AFTER DELETE ON work_items
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_work_items_insert AFTER INSERT ON work_items
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_work_items_update AFTER UPDATE ON work_items
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_writer_usage_delete AFTER DELETE ON writer_usage
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_writer_usage_insert AFTER INSERT ON writer_usage
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_writer_usage_update AFTER UPDATE ON writer_usage
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_writers_delete AFTER DELETE ON writers
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_writers_insert AFTER INSERT ON writers
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_change_writers_update AFTER UPDATE ON writers
            BEGIN UPDATE operator_change SET token = hex(randomblob(16)); END;

CREATE TRIGGER operator_pr_insert AFTER INSERT ON github_observations
            BEGIN
                INSERT OR REPLACE INTO operator_pr SELECT 
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

                FROM github_observations WHERE execution_id = NEW.execution_id
                ORDER BY last_observed_at DESC, observed_at DESC LIMIT 1;
            END;

CREATE TRIGGER operator_pr_update AFTER UPDATE OF last_observed_at ON github_observations
            BEGIN
                INSERT OR REPLACE INTO operator_pr SELECT 
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

                FROM github_observations WHERE execution_id = NEW.execution_id
                ORDER BY last_observed_at DESC, observed_at DESC LIMIT 1;
            END;

CREATE TRIGGER operator_progress_insert AFTER INSERT ON executions BEGIN
           INSERT INTO operator_progress (execution_id, last_advanced_at, wait_since) VALUES (
               NEW.id, NEW.created_at,
               CASE WHEN json_extract(NEW.state_json, '$.wait') IS NOT NULL
                    THEN NEW.updated_at END
           );
       END;

CREATE TRIGGER operator_progress_update AFTER UPDATE OF state_json ON executions
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
       END;

CREATE TRIGGER operator_revision_round AFTER UPDATE OF state_json ON executions
       WHEN NEW.stage IS NOT OLD.stage AND NEW.stage IN ('development', 'report_drafting')
       BEGIN
           UPDATE operator_progress SET revision_round = revision_round + 1
           WHERE execution_id = NEW.id;
       END;
