# Phalanx

Phalanx is an agentic orchestrator for work across repositories. Work items live in an API-backed queue. A local runner lets multiple agents claim work in parallel and deliver either a Markdown report or a pull request.

1. File a work item for a registered repository with a goal, enough context to execute it, and the expected output.
2. Start a **muster**, a batch of eligible work launched from the Captain chat. Agents work in parallel, use isolated checkouts for changes, and verify their results.
3. Follow each item through its lifecycle. After development, Phalanx opens a draft PR. Self-review, pre-validation and draft-only checks gate promotion to ready for review. CI, previews, independent review, feedback and conflict resolution gate check-in. The runner checks in the PR when authorized. Report work ends with a verified **field report** in Markdown.

**Phalanx advancing** means a work item has met the conditions to move to its next stage. Every closed work item has a final output: if it ends without the requested deliverable, Phalanx stores a field report explaining what happened and what remains.

The first milestone runs the HTTP API server and durable store locally, with concurrent work in visible worker sessions launched from a Captain chat. Phalanx is runtime-neutral: any agent runtime that can run visible worker sessions, such as Codex, GitHub Copilot CLI or the GitHub Copilot app, and Claude Code, can serve as Captain through the [bridge contract](docs/captain-relay.md#bridge-contract). The same server is intended to move to cloud hosting while agents keep running locally.

## Quickstart: new install

Use CPython 3.12 or later (3.14 for deployment). These steps create new tokens and a new database. In PowerShell, from the repository root:

```powershell
py -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.lock -e .
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_DB = "C:/muster/db/phalanx.sqlite3"
$env:PHALANX_LOCAL_BOARD = "1"
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

Store both tokens privately and record the `PHALANX_DB` path: you need them to resume. The optional [direct muster locator](#direct-muster-discovery-and-status) finds existing configuration; it never creates a database. Then, from a second shell with the owner token set, check `http://127.0.0.1:8000/health`, register a repository and file work:

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
```

Open `http://127.0.0.1:8000/operator/` to watch it. To run the work, set up a runner and start a muster as described in [Install Phalanx](docs/install.md), which also covers macOS and Linux, repository trust and the GitHub token.

`intake` and `ingest-issue` default to `--target checked_in`. On the direct path the writer
enables GitHub auto-merge only for that target; required reviews, checks and rulesets still gate
the merge. Use `--target ready_for_check_in` to stop at a ready PR or `--target report` for a
Markdown report. Owners and Captains can steer an open PR item between the two PR targets,
including during direct delivery; a completed ready item still needs a rerun to merge.

**One-time repository trust:** the owner must grant trust in the worker runtime before work can run.
In the GitHub Copilot app, also approve the per-project repository trust card in the project's session;
a checkout path in the runtime's `trustedFolders` configuration does not approve that app-level card.
For direct sessions, approve the configured shared worktrees project (`session_workspace`), rather than
registering a project for each worktree. Only the owner grants trust. A Captain that finds a pending
trust approval notifies the owner and keeps the same writer/session blocked until approval, rather than
relaunching it.

## Resume an existing install

After a restart or reboot, don't rerun the quickstart: it would create new tokens and could start an empty database. Follow [Resume an existing install](docs/resume.md) with your recorded muster directory, database path and stored tokens.

## Direct muster discovery and status

For an existing direct muster, discovery uses `--home <absolute directory>` first, then
`PHALANX_HOME`, then exactly one locator: `~/.phalanx/muster.json` (`~` is the current user's
home on Windows, macOS and Linux). Create that locator yourself, containing only:

```json
{"home": "/absolute/path/to/existing/muster"}
```

On Windows use a JSON path such as `"C:\\muster"`. Arrays, extra fields, duplicate `home`
fields, relative paths and missing directories fail; Phalanx never scans for another muster
or falls back after an invalid override. Discovery never initializes, migrates, moves or deletes
state. It is optional: existing explicit config commands still work without a locator.

The selected directory contains `shift.json`, `charter.toml`, `runner.json` and `private.env`.
The shift config can omit `charter` or use `charter.toml` (an absolute path to that same file
also works). Other config paths, such as checkout and worktree mappings, should be absolute.
`private.env` holds literal `KEY=VALUE` lines for `PHALANX_RUNNER_TOKEN`,
`PHALANX_GITHUB_TOKEN`, optionally `PHALANX_OWNER_TOKEN`, `PHALANX_DB` and
`PHALANX_LOCAL_BOARD`. Blank lines and whole-line `#` comments are allowed; matching outer
single/double quotes are removed, without escapes, interpolation, inline comments or shell
execution. Duplicate/unknown keys and empty values fail. Protect this file with your OS's
private-file permissions; never commit it. Existing environment values take precedence.
Tokens are loaded only into the environment and never included in status output.

```text
python -m phalanx muster status
python -m phalanx muster status --home <absolute directory>
python -m phalanx shift status
python -m phalanx reconcile
```

Without explicit configs, `shift` derives `shift.json` and the charter, and `reconcile` derives
`runner.json` and its scratch directory `<muster>/reconciler` (overridable with `--state`).
Both accept `--home`; `shift` accepts it before or after its action. Explicit `--config`
and `PHALANX_SHIFT_CONFIG` retain their existing behavior without loading `private.env`;
an explicit `--home` instead selects the muster layout. Explicit reconciler configs still need
`--state`. `shift prompt` remains usable without any configuration.

Start the intended API yourself with `PHALANX_HOME` set to this directory, alongside its
**existing** database path and credentials. Authenticated `GET /v1/muster/status` reports the
API's configured home identity, live Captains, registered open writers, each reconciler's
last heartbeat/age/staleness (15 minutes), and dispatch pause. `muster status` reads this API,
prints JSON, and fails nonzero if it is unavailable or its home identity differs. No recorded
reconcilers are reported as an empty list, not as evidence that one is running. Writer presence
is registration state, not an independent runtime process probe. No local database is opened.
There is deliberately no `muster start`: discovery/status never launch processes or run server
startup migrations. Existing installs with another layout can keep their explicit config paths;
do not move live state to adopt the locator.

## Read one item's correlated timeline

Use the authenticated API, not direct database queries. Inspect the item to find its
current `execution.id` (`GET /v1/items/ITEM_ID`); use
`GET /v1/items/ITEM_ID/executions` to find earlier executions after a rerun.
Then page that execution's existing operator timeline:

```powershell
$base = "http://127.0.0.1:8000"
$headers = @{ Authorization = "Bearer $env:PHALANX_OWNER_TOKEN" }
$item = Invoke-RestMethod "$base/v1/items/ITEM_ID" -Headers $headers
$route = "$base/v1/operator/executions/$($item.execution.id)/timeline?limit=100"
do {
    $page = Invoke-RestMethod $route -Headers $headers
    $page.events | Select-Object version, attempt_id, worker_reference, from_stage, stage, reason, pr_url, candidate
    $route = "$base/v1/operator/executions/$($item.execution.id)/timeline?limit=100&cursor=$($page.next_cursor)"
} while ($page.next_cursor)
```

The response's `item.item_id` and `item.id` correlate the work item and execution.
Events are newest first; `attempt_id` and that attempt's `worker_reference` identify
the worker when recorded (otherwise null). `from_stage` and `stage` show a stage change.
`pr_url` is the PR recorded on that event, and `candidate` is its full applied
candidate/head SHA, not the execution's possibly newer revision.

For example, the paginated backward-transition fixture in
`tests/test_operator.py::test_backward_result_reasons_correlate_the_recorded_attempt_and_revision`
includes this event (other response fields omitted):

```json
{
  "version": 4,
  "attempt_id": "result-attempt-1",
  "worker_reference": "worker-1",
  "from_stage": "pr_promotion",
  "stage": "development",
  "reason": "changes_required; merge_conflict; at bbbbbbbbbbbb",
  "pr_url": "https://github.com/a/b/pull/42",
  "candidate": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

Result reasons derive only from persisted outcome, issue, failed local check names
and reasons, failure reason, and applied SHA. They are at most 400 characters,
with bounded check/failure excerpts and a reserved short-SHA suffix. Recognizable
GitHub tokens, bearer credentials and named secret assignments are redacted before
truncation. Task bodies, reports, review notes and other arbitrary evidence are
not copied into these reasons. Existing pause/retry waits are unchanged; no slot,
capacity or rank-position wait reasons are inferred on the direct path.

## Documentation

- [Install Phalanx](docs/install.md): first-time setup, credentials, runner setup and repository trust.
- [Resume an existing install](docs/resume.md): restart order, single-instance checks and status.
- [Runner operations](docs/runner-operations.md): the Captain loop, PR validation and check-in, and manual `attended` commands.
- [Captain relay and bridge](docs/captain-relay.md): the bridge contract, Captain chat relay, standing relay serve and runtime mapping.
- [Direct path](docs/direct-path.md): the GitHub reconciler and a Captain on the direct path.
- [Recovery and troubleshooting](docs/recovery.md): stuck relays, reservation reconciliation, and SQLite backup and restore.
- [Work items](docs/work-items.md): filing, draft-only PR delivery, GitHub issue import, answering questions and the operator board.
- [Configuration](docs/configuration.md): environment variables and the runner config.
- [API reference](docs/api.md) and [developing Phalanx](docs/development.md): routes, leases, transactions and tests.
- [Validation policy](docs/runner-validation-policy.md), [PR observations](docs/runner-pr-observations.md), [pilot reports](docs/pilots/) and [decision log](docs/decisions.md).

Design: the [spec](SPEC.md), [state machine](STATE_MACHINE.md), [roadmap](ROADMAP.md) and [implementation plan](IMPLEMENTATION_PLAN.md). Open [state-machine.html](state-machine.html) locally in a browser to explore the lifecycle and execution status graphs.