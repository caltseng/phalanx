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

### Run attended Codex assignments

Use a server with durable intake and dispatch enabled. Register the repository
with the owner API, fetch the desired origin branch into its local checkout,
and file a development or report item. Give the machine one stable
runner ID and share one private local state directory and config across all
Captain commands for that runner. The directory contains active lease secrets.
Put it outside the mapped checkouts and restrict access to the runner account.
Keep the runner token in the shell that runs these commands.

Save a local JSON config such as:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "runner_id": "desktop",
  "capacity": 2,
  "repositories": {
    "registered-repo-id": {
      "checkout": "C:/enlistments/example",
      "base_branch": "main",
      "host_id": "local",
      "project_id": "project-id-from-Codex"
    }
  }
}
```

Local capacity defaults to 1. Use numbered slots from 1 through the configured
capacity. The server still applies dispatch pause, stage and item eligibility,
and its per-runner capacity. A local capacity reduction applies to new
reservations. Existing reservations in higher-numbered slots can still be
inspected, renewed, and submitted, and count toward local capacity. When
upgrading the bridge, finish any in-flight CLI commands and use the new
version for all subsequent commands. The existing journal remains usable
and its original reservation is slot 1.

Set `PHALANX_GITHUB_TOKEN` in the runner environment to publish initial drafts
for registered GitHub repositories. After development succeeds, run
`python -m phalanx attended --config runner.json --state C:/phalanx-runner prepare --draft`
to poll for draft publication. This command uses the runner's Git credentials
for the push and its GitHub token for draft creation. It returns
`draft_submitted` when the API accepts the verified PR. Keep the same state
directory to retry an interrupted publication, and use `status` to inspect a
blocked reservation. Draft assignments run directly on the runner.

From an authorized Captain muster:

1. Verify the project and host with Codex project discovery. Run
   `python -m phalanx attended --config runner.json --state C:/phalanx-runner --slot 1 prepare`.
   On `launch_intent`, invoke the app's create-thread action **once** with the
   returned `create_thread` arguments. Save the marker for receipt and result
   commands. Prepare each additional assignment in a free slot, such as
   `--slot 2 prepare`. The worker targets its assigned Git worktree explicitly.
2. With the same command prefix and slot number, run
   `receipt MARKER HOST --thread-id THREAD` for a native chat receipt, or
   `receipt MARKER HOST --client-thread-id CLIENT` for queued setup. Resolve a
   queued or lost receipt in the app and verify the exact launch marker and
   assignment before recording the native ID. Use `status` to inspect a
   reservation or `status --all` to list occupied slots. If the claim response
   was lost, repeat `prepare` in the original slot to replay the claim. Keep an
   ambiguous app launch reserved and reconcile it using the original marker.
   Other free slots can continue preparing eligible assignments.
3. Run `renew` for every known active claim in its slot independently of app
   observation, comfortably before that lease expires. The CLI supports manual
   renewal. Use short observation waits.
4. Read the native chat and verify the exact marker, assignment, and that the
   worker and any writers it started have stopped. Then run
   `submit MARKER HOST THREAD --confirmed-stopped`. The runner verifies the
   result file and current worktree before posting. If the submission response
   is lost, repeat the same command to replay the recorded submission.

Development success requires a new local candidate commit and a clean
worktree. Report drafting sends the Markdown to the server as a draft.
Worker stages record local checkpoints for questions and failures. Questions
wait for an owner reply. A technical failure from a confirmed-stopped worker
enters the shared 5, 10, 20, and 40 second retry backoff. Exhausting the budget
asks for owner intervention.
Run `prepare` again to launch an independent review with the exact stored draft
revision. Review findings return to drafting with the previous draft in the new
packet. Run `prepare --report-storage` in a free slot to submit a reviewed
revision for server finalization. A `report_storage_submitted` result needs no
native chat. If the response is lost, repeat the command in the same slot. The
server retries final storage independently and exposes the finished Markdown
through the report endpoint.

Keep the state directory when restarting. Expired leases and unverified results
require reconciliation.
Automatic desktop recovery and independent heartbeat scheduling remain
follow-up work.

### Reconcile an attended reservation

From a fresh Captain chat, inspect the runner's original configuration and state directory. Run one recovery step with the runner token.

```powershell
python -m phalanx attended-recover --config runner.json --state C:/phalanx-runner --slot 1 --observation observation.json
```

The Captain supplies a fresh JSON snapshot from native Codex history and thread reads. Set `observed_at` to the time of the snapshot as ISO 8601 with an offset. Include `discovery_complete` and a `threads` array. Set `discovery_complete` only after inspecting all relevant history pages, including archived history where relevant. A thread already bound to a native receipt can be read directly.

Each thread row includes the following fields.

| Field | Source |
| --- | --- |
| `host_id`, `project_id`, `thread_id`, `started_at` | Native host, project, thread UUID and offset-aware original launch time. |
| `marker`, `assignment` | The exact first-turn attempt marker and the original packet's `result_identity` object from the first prompt. |
| `state` | Use `active`, `stopped`, or `unknown`. Attest `stopped` only after confirming the native turn ended and accounting for any worktree writer it started. A missing thread or ambiguous state is `unknown`. |
| `client_thread_id` | The queued UUID when the native launch can be positively correlated with its saved queued receipt. Otherwise omit it. |

A saved stop request can be replayed without another observation file. A `retry` response with `retry_at` gives the earliest next probe as Unix seconds. After an `owner_wait`, address the reason before using `--recheck`. An unmatched server attempt requires the original journal or explicit recovery of its writer. The recovery command verifies the local Git checkpoint and checks any result against the original packet. A verified result with an expired lease stays reserved with its result path, digest, and checkpoint for future adoption under a server-authorized recovery claim. For a verified result with a live lease, use the exact receipt with the existing `attended submit` command. Use the original launch path to replay an unconfirmed claim, which reuses the saved claim request.

### Operator work board

Open `http://127.0.0.1:8000/operator/` on the running server and enter its owner token. The browser holds the token in memory until you disconnect or reload the page. The board uses the same-origin bearer-authenticated read API and opens stored field reports as plain text.

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
| `POST /v1/dispatch/claim` | Runner sends `runner_id`, `repository_ids` (1–100 local checkout mappings), optional `slots` to lower the capacity for this muster, and optional `stages` to claim only supported stages. Send a fresh cryptographically random secret, for example Python's `secrets.token_urlsafe(32)`, in `Phalanx-Lease`. |
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
own clock. The claim index excludes blocked stages. A stage begins to age
once its last prerequisite clears and any retry delay elapses.

#### Prerequisites and queued steering

An owner can include `dependencies` in `POST /v1/items`. Each entry names
an `upstream_id` and a `milestone` of `report`, `ready_for_check_in`, or
`checked_in` (the default). Prerequisites may belong to other repositories.
Item inspection includes configured links and the execution's
`unmet_dependencies`, with an explanation for each wait.

An owner can send `PATCH /v1/items/{item_id}/executions/{execution_id}` with
`expected_version` and changes to `goal`, `context_links`,
`acceptance_criteria`, `priority`, `runner_affinity`, or `dependencies`.
Omitted fields keep their values. An empty list clears a list field, and
`null` clears runner affinity. The patch accepts `Idempotency-Key`.
Stale versions and executions outside the queued state return HTTP 409.

A successful claim includes a `dependencies` snapshot with each upstream
execution ID, output reference, and exact revision. For a reviewed PR it
includes the reviewed head and base. For a check-in it also identifies the
verified merge commit. Use these revisions in the work packet. The snapshot
is recorded on the attempt's claim event and returned on claim replay even
if an upstream execution is rerun.

Milestone changes refresh up to 32 dependents in the upstream transaction.
The API processes larger fan-outs and restart audits in indexed batches
across short transactions. Queued dependents in later batches may become
eligible after the upstream transaction commits. The claim transaction
always checks the current durable evidence before assigning work.

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

Register the expected remote without credentials. Use `ready_for_check_in` for a reviewed PR or `checked_in` for a verified merge. These are intake targets. The [runner PR observation API](docs/runner-pr-observations.md) records evidence for publication, promotion, and PR validation.

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

PH-06 owns candidate indexing, time eligible, aging, capacity, admission and lease renewal. PH-08 owns reply and external recheck routes. PH-09 owns general failure finalization and retries. Provider verification and recovery of uncertain writes belong to the GitHub and runner integrations. The result API rejects caller-supplied PR observations. The operator API provides bounded history reads for the board.

### Read the operator board data

Both owner and runner bearer tokens can call these routes.

| Route | Response |
| --- | --- |
| `GET /v1/operator/executions?kind=open` | `items` and `next_cursor`. Input waits appear first. Each group is ordered by execution creation time, oldest first. |
| `GET /v1/operator/executions?kind=closed` | `items` and `next_cursor`. Closures appear newest first, including outputs from earlier executions of rerun items. |
| `GET /v1/operator/executions/{execution_id}/timeline` | The execution summary in `item`, the current attempt ID in `attempt`, newest-first transition `events`, and `next_cursor`. |

Each route defaults to 25 entries and accepts `limit` from 1 through 100. Pass the
returned `next_cursor` as `cursor` on the same route and list kind until it is
null. Pages read live state. An open execution entering or leaving the input-wait
group can cross the cursor between requests. Refresh from the first page to
see the current order. New timeline events and closures normally precede the
current cursor. The timestamp and version fields describe the persisted state.

`last_advanced_at` records entry into the current stage. `last_progress_at` records
the latest execution transition. Wait details include the stored reason, resume
trigger, and age of the current recorded wait. A scheduled retry is available
as `retry_at_ms` in Unix milliseconds. `primary_pr_url` represents the recorded
primary PR. The draft state and worker chat link have no current read mapping.
Prerequisite links are not yet wired into the API. Report output URLs use the
authenticated report route.
