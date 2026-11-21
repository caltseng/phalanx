# Phalanx

Phalanx is an agentic orchestrator for work across repositories. Work items live in an API-backed queue. A local runner lets multiple agents claim work in parallel and deliver either a Markdown report or a pull request.

1. File a work item for a registered repository with a goal, enough context to execute it, and the expected output.
2. Start a **muster**, a batch of eligible work launched from the Captain chat. Agents work in parallel, use isolated checkouts for changes, and verify their results.
3. Follow each item through its lifecycle. After development, Phalanx opens a draft PR. Self-review, pre-validation, and draft-only checks gate promotion to ready for review. CI, previews, independent review, feedback, and conflict resolution gate check-in. The runner checks in the PR when authorized. Report work ends with a verified **field report** in Markdown.

**Phalanx advancing** means a work item has met the conditions to move to its next stage.

The first milestone runs the HTTP API server and durable store locally, with concurrent work in visible Codex chats launched from a Captain chat. The runner has a configurable concurrency limit and can pause dispatch. The same server is intended to move to cloud hosting while agents continue to run locally. A future ChatGPT phone workflow can file work through the hosted API and return a work-item link. Claude Code and GitHub Copilot CLI are P2 execution targets.

See the [design spec](SPEC.md) for the proposed workflow and the [implementation plan](IMPLEMENTATION_PLAN.md) for milestones and acceptance checks. Open [state-machine.html](state-machine.html) locally in a browser to explore the lifecycle and execution status graphs.

Every closed work item has a final output. If it ends without the requested deliverable, Phalanx stores a field report explaining what happened and what remains.

## Run the local API

Use CPython 3.14 for deployment and pilot runs. Phalanx supports CPython 3.12 and later. From the repository root in PowerShell:

```powershell
py -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.lock -e .
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

The lock file pins runtime dependencies for repeatable local installs. The package metadata keeps compatible version ranges for downstream installs. Tests use the standard library and the same runtime dependencies.

Set `PHALANX_DB` to select a SQLite file. It defaults to `.phalanx/phalanx.sqlite3` relative to the server's working directory. Use an absolute path when running commands from different directories. The server applies pending migrations before serving requests and defaults to local loopback HTTP with the command above. Use one server process and local storage. Arrange HTTPS before exposing the API beyond loopback.

From a second shell, call `Invoke-RestMethod http://127.0.0.1:8000/health`. This public probe checks database access. Send `Authorization: Bearer <token>` to `GET /v1/owner/status` or `GET /v1/runner/status` to verify credentials. Each probe requires its matching token. The server returns 401 for missing or invalid credentials and 403 for the other role. Store the tokens privately and provide each client with its own token and API base URL. Rotate tokens by replacing the environment values and restarting the server.

### Back up and restore SQLite

Run these commands in the same environment as the server, or set `PHALANX_DB` to the same absolute path. The backup command uses SQLite's snapshot API and can run while the server is up. The destination directory must already exist and support hard links, such as NTFS. Each command validates its copy before creating the destination and refuses to overwrite an existing file.

```powershell
New-Item -ItemType Directory -Force .phalanx/backups | Out-Null
python -m phalanx backup .phalanx/backups/phalanx-01.sqlite3
```

For recovery, stop the server and point `PHALANX_DB` at a fresh path in an existing directory. Restore the backup there, restart the API using the same path, and verify `GET /health`. Keep the old database until you've verified the recovered data.

```powershell
$env:PHALANX_DB = "$PWD/.phalanx/phalanx-restored.sqlite3"
python -m phalanx restore .phalanx/backups/phalanx-01.sqlite3
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

### Develop the API

Use the `require_owner` and `require_runner` dependencies in `phalanx.auth` to declare route permissions. Synchronous handlers can open short-lived read connections with `phalanx.store.connect` and the path in `request.app.state.database_path`. Use `phalanx.store.write_transaction(path)` for writes, including all reads that inform a write. It serializes write transactions in the server process, starts with `BEGIN IMMEDIATE`, and commits or rolls back on exit. Keep the transaction short and do all external I/O outside it. Connections use foreign-key enforcement, `synchronous=EXTRA`, and a two-second busy timeout. Add schema changes as a new tuple of SQL statements at the end of `MIGRATIONS` in `phalanx.store`. The migrator runs the SQL and updates the schema version in one transaction.

Run the localhost HTTP and persistence tests with `python -m unittest discover -s tests -v`.

### File and inspect work

Set `PHALANX_OWNER_TOKEN` to the same owner token used by the server. Set `PHALANX_API_URL` or pass `--api-url` before the command to override `http://127.0.0.1:8000`.

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
python -m phalanx inspect <item_id>
```

Register the expected remote without credentials. Use `ready_for_check_in` for a reviewed PR or `checked_in` for a verified merge. These are intake targets. The later GitHub integration supplies provider-verified facts for PR stages.

The authenticated API exposes these routes under `/v1`.

| Route | Access | Purpose |
| --- | --- | --- |
| `POST /repositories` | Owner | Register an ID and expected remote. Repeating the same registration returns it. |
| `GET /repositories` | Owner or runner | Read registered repositories. |
| `POST /items` | Owner | File a new item with an initial queued execution. |
| `GET /items/{item_id}` | Owner or runner | Read the request and current execution, including its version, stage, status, wait and output. |
| `GET /items/{item_id}/executions` | Owner or runner | Read past and current executions. |
| `GET /items/{item_id}/executions/{execution_id}/timeline` | Owner or runner | Read durable transition history and evidence. |
| `GET /items/{item_id}/executions/{execution_id}/drafts/{revision}` | Owner or runner | Retrieve the exact draft for review. |
| `GET /items/{item_id}/executions/{execution_id}/report` | Owner or runner | Retrieve the final stored Markdown. |
| `POST /items/{item_id}/executions/{execution_id}/cancel` | Owner | Request cancellation with `expected_version` and `reason`. |
| `POST /items/{item_id}/executions/{execution_id}/rerun` | Owner | Create a linked execution from the current closed execution with `expected_version` and an explicit `target`. |
| `POST /items/{item_id}/executions/{execution_id}/result` | Runner | Submit `outcome`, stage-specific evidence and the current attempt preconditions. |
| `POST /items/{item_id}/executions/{execution_id}/stop-confirmed` | Runner | Confirm a cancellation under a live lease after accounting for the worker and its effects. |

Intake accepts `repository_id`, `goal` and `target`, plus optional `context_links`, `acceptance_criteria`, `target_branch` and `priority` (`low`, `normal` or `urgent`). Use an `Idempotency-Key` header when retrying a mutation. The server returns the original response for a replay of the same validated request in its operation scope, or HTTP 409 for a different request under the same key. Registration is keyed by the repository ID and unique remote. Version conflicts also return 409. Versions start at 1 for each execution. A rerun preserves the prior execution and keeps the work item's output type.

Results and stop confirmations require a matching `attempt_id`, `stage` and `expected_version`, plus the current secret in the `Phalanx-Lease` header. A successful report-drafting result sends `report_markdown`. The server persists it and derives a SHA-256 revision. Report review and storage send `report_revision`. The server copies the reviewed draft into final storage in the same transaction that closes the execution. A `needs_input` result supplies `wait.reason` and `wait.trigger`. Active cancellation preserves the attempt while awaiting reconciliation. An expired lease or uncertain provider effect requires recovery before confirmation.

#### Queue storage handoff

Migration 2 stores execution snapshots as JSON and exposes `stage` and `status` as queryable columns. It enforces one open execution per item and one open attempt per execution. PH-06 can select a queued execution and call `intake.claim_attempt` inside the same `store.write_transaction` after checking admission. Supply a stable `runner_id`, the SHA-256 hex digest of a freshly generated URL-safe lease secret, and `lease_expires_at` as Unix seconds. Return the plaintext secret only to the claiming runner. The claim returns the new attempt ID and execution snapshot. It appends history, increments the state version and changes the execution to running. A committed result ends the attempt. Renewal and recovery should use the persisted attempt ID, runner ID, lease digest and expiry. A cancellation request keeps the attempt open until stop confirmation or recovery.

PH-06 owns candidate indexing, time eligible, aging, capacity, admission and lease renewal. PH-08 owns reply and external recheck routes. PH-09 owns general failure finalization and retries. Provider verification and recovery of uncertain writes belong to the GitHub and runner integrations. The result API rejects caller-supplied PR observations. PH-17 owns bounded history reads.
