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

#### Dispatch and leases

Owner routes set dispatch policy. Runner routes require the runner bearer token.
In the local deployment, the runner token identifies a trusted runner role.
Use one stable runner ID per local runner across restarts. The ID scopes
capacity and recovery within that trusted role.

| Method and path | Request and behavior |
| --- | --- |
| `PUT /v1/dispatch/settings` | Owner sets `paused` and optionally `aging_seconds` (default 300). A pause stops new assignments while existing leases, results, and recovery continue. |
| `PUT /v1/dispatch/runners/{runner_id}/capacity` | Owner sets `capacity` (1–256, default 4 per runner). Reductions take effect for the next claim and leave all reservations in place. |
| `POST /v1/dispatch/claim` | Runner sends `runner_id`, `repository_ids` (1–100 local checkout mappings), and optional `slots` to lower the capacity for this muster. Send a fresh cryptographically random secret, for example Python's `secrets.token_urlsafe(32)`, in `Phalanx-Lease`. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/renew` | Runner sends the original `Phalanx-Lease` before expiry. The response gives `lease_expires_at` in Unix seconds. Renew independently of worker observation, initially about every 20 seconds. |
| `GET /v1/dispatch/runners/{runner_id}/recovery` | Runner lists expired reservations for its ID with the item, execution, attempt, stage, current execution version, and reconciliation action. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/recover-stopped` | Runner supplies `item_id`, `expected_version`, and a `reconciliation` note describing how it verified the worker stopped and accounted for downstream effects. Accepts `Idempotency-Key`. A live, replaced or changed attempt is rejected. |

A claim returns HTTP 200 with one of `claimed`, `paused`, `at_capacity`, or
`no_eligible_work`. A successful assignment includes the work request,
execution and version, attempt ID, and lease expiry (90 seconds from claim).
Pass its attempt ID, execution version, and stage to the existing result route
with the same lease header. Keep the bearer token and lease secret with the runner.
After a lost claim response, replay the same body and lease secret to retrieve
the original assignment. Deduplicate by attempt ID and verify that the lease
and execution are still current before starting work on a replayed assignment.
Empty polls leave no receipts. The server stores only the lease-secret digest.

An intake item can set `runner_affinity` to require a specific runner ID.
For matching repositories and affinity, urgency rises one band per aging
interval. Within the effective band, the server chooses the oldest eligible
timestamp and breaks ties by item ID. Execution views report
`ready_since_ms` and `retry_at_ms` as Unix milliseconds. A reconciled retry
starts accruing age when its delay elapses. A newly queued stage gets its
own clock. Prerequisite handling can keep a queued stage out of the claim
index by clearing `ready_since`, then set it when the last gate clears.

An expired lease prevents stale results and renewals. Its attempt still holds
the slot and item reservation. Leave uncertain or live workers reserved.
After confirming that a worker stopped and that repeating the stage is safe,
submit stopped recovery. It records the reconciliation and uses delays of
5, 10, 20, and 40 seconds for the first four retries. A fifth stopped attempt
waits for owner intervention. If cancellation is already pending, stopped
recovery closes it with a stored cancellation report. PH-12 extends recovery
to launch intent, live session reattachment, and collection of finished
results under a recovery claim.

#### Transactions and tests

Use the `require_owner` and `require_runner` dependencies in `phalanx.auth` to declare route permissions. Synchronous handlers can open short-lived read connections with `phalanx.store.connect` and the path in `request.app.state.database_path`. Use `phalanx.store.write_transaction(path)` for writes, including all reads that inform a write. It serializes write transactions in the server process, starts with `BEGIN IMMEDIATE`, and commits or rolls back on exit. Keep the transaction short and do all external I/O outside it. Connections use foreign-key enforcement, `synchronous=EXTRA`, and a two-second busy timeout. Add schema changes as a new tuple of SQL statements at the end of `MIGRATIONS` in `phalanx.store`. The migrator runs the SQL and updates the schema version in one transaction.

Run the localhost HTTP and persistence tests with `python -m unittest discover -s tests -v`.

### Answer a waiting item

With the owner token configured, poll the local question inbox and use the item's IDs and version from the result:

```powershell
python -m phalanx inbox
python -m phalanx reply ITEM_ID EXECUTION_ID "Use the existing interface" --version 3 --key reply-001
```

Reuse the same key and body if retrying the reply after a lost response. For more inbox entries, pass the last entry's `waiting_since` to `--after-time` and its `execution_id` to `--after-id`.

Runners submit a structured `question` with `prompt`, `handoff`, optional `options`, and optional `proposed_default` in a stage result that needs input. The next claim after a reply includes `input_reply` with the answer and question. The authenticated execution timeline retains both.

Runners can post `/external-wait` under an execution with the live attempt lease, expected version, and an idempotency key. Include the attempt and stage, a reason, a gate ID, the expected head and base, and an optional provider retry delay in seconds. Poll `GET /v1/runner/due-rechecks?limit=50` for due waits and post `/recheck` under each execution with its listed version and a stable idempotency key. Both operations use the runner token. A recheck queues the same stage for verification. Provider observation and recurring polling are runner integration work.

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
