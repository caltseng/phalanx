# Phalanx

<img src="docs/phalanx-logo.svg" alt="Phalanx logo: an ivory spear-shaped mark on a crimson shield with a gold rim" width="96" height="96">

**Use Phalanx to delegate outcomes, not keep prompting agents.** A coding agent handles a task; Phalanx is meant to keep a queue of tasks moving across repositories—from clear acceptance criteria through implementation, review, checks and merge.

Its value is durable ownership: work shouldn’t disappear when a chat ends, a Captain changes, CI fails or a decision blocks progress. You should see what’s moving, what’s genuinely blocked and what needs you.

**For one small change, a normal coding chat is simpler.** Phalanx earns its place when coordinating several workstreams costs more than writing the code. If you still have to chase dispatches, decipher Captain identities or repeatedly unblock internal machinery, it isn’t delivering that value reliably yet. The goal is less supervision—not more agents to supervise.

Phalanx orchestrates agent work across repositories, from a queued goal to a merged pull
request or a Markdown report. A local HTTP API keeps durable work state, **Captain chats**
coordinate visible writer sessions, and **Scribe** provides the work board.

1. File a work item for a registered repository with a goal, context and acceptance criteria.
2. Start a **muster**: the Captain selects eligible work under an operator-owned charter
   and launches registered writers in parallel, each with an isolated item worktree.
3. Writers implement, check, self-review and publish their work. An independent reconciler
   follows GitHub, flags actionable problems and supplies the evidence for verified delivery.

Ordinary authorized API and Scribe intake in a charter repository enters Captain pickup
without listing each item in charter notes. Notes may order or constrain work; excluded queued
items show the same concrete pickup reason in Captain status and Scribe.

PR work defaults to **`checked_in` (merged)**, subject to the repository's required checks,
reviews and merge rules. Choose `ready_for_check_in` for a ready PR with owner-controlled
merging, `report` for a Markdown **field report**, or draft publication for a verified draft.
Every closed execution records a verified PR or stored report; failure and cancellation
reports preserve what happened, partial results and recovery context.

Phalanx is runtime-neutral. Codex, GitHub Copilot CLI or app, Claude Code, and other runtimes
with visible sessions can follow the [direct-session contract](docs/direct-sessions.md).
The CLI, Captain/writer clients and reconciler support Windows, macOS and Linux.

## Architecture

| Component | Responsibility |
| --- | --- |
| **HTTP API and SQLite** | Own repositories, items, dependencies, executions, fenced attempts, questions/replies, reports, Captain/writer bindings, history and timestamped observations. The server applies lifecycle transitions and finalizes outputs. |
| **Scribe** | Serves the work board at `/operator/` for intake, item timelines, questions, flags and dispatch settings. |
| **Captain chat** | Reads its charter, API state and native session inventory; prepares worktrees, registers writer intents, launches visible sessions and coordinates recovery and handoff. |
| **Writer session** | Implements, checks, self-reviews and publishes its item, then fixes checks, feedback and conflicts on its primary PR through delivery. |
| **GitHub reconciler** | Independently reads GitHub/git, posts observations and heartbeats, and runs trusted local checks serially in scratch worktrees. The API uses its evidence to derive flags and verify delivery. |
| **Runtime keeper** | Uses the runtime's schedule and session tools to start a Captain when one is needed, preserving existing writers. |

Clients access durable work state through the authenticated API. The execution host keeps
checkout mappings, isolated worktrees, prepared checks environments, artifacts, configuration
and credentials. Owner and runner bearer tokens authorize their respective API operations;
each writer uses its own scoped credential stored in the item's Git directory.

The operator keeps a charter outside mapped repositories. On each wake, the Captain uses
`python -m phalanx shift` commands, re-reads the charter and chooses work within its repository scope
and lane policy.
The [Captain operating guide](docs/direct-path.md#captain-shifts-and-handoff) owns shift,
wake, reinforcement and handoff rules.
The owner's persisted Building (`max_building_writers`, initially 5) and Total (`max_writers`,
initially 10 on new installs) limits, editable together under **Writer concurrency** in Scribe,
apply across all Captains and repositories. Existing installs retain their approved Total until
[reviewed adoption](docs/deployment.md#existing-runtime-reviewed-adoption-not-a-rename).
Registration enforces both limits atomically; waiting writers retain Total.

Captains launch and confirm sessions through their runtime's native tools. In the Copilot
app, writers share the configured worktrees folder project, `session_workspace`, and run
`python -m phalanx work` from their own item worktree. The guides use `phx` as a configured
local shorthand for the installed Python command.

```text
Owner CLI / Scribe intake -----> HTTP API -----> server SQLite
                                   ^
Charter -> Captain (shift) ---------| bindings / actions / heartbeat
Keeper  -> fresh Captain           |
              | native launch     |
              v                   |
        Writer (work) -------------| scoped stage results / questions
              |                   |
        Item worktree -> GitHub PR  |
                          |        |
                          +-> Reconciler -- observations -> API
```

The API and execution host can run on the same machine. The configurable API URL keeps
their responsibilities separate; hosted operation requires HTTPS, persistent storage,
authentication and backups. See the [implementation map and diagrams](docs/architecture.md)
for code ownership and recovery boundaries, and [Direct path](docs/direct-path.md) for
charters, runtime schedules and configuration.

## Work lifecycle

PR work follows `development -> self_review -> publication -> delivery`. Each stage has a
separately fenced attempt, and self-review is bound to the exact candidate commit.
Publication records one primary PR. **Publishing a PR or releasing a writer is distinct
from verified delivery**: during normal delivery waits the writer stays registered,
fixes actionable problems on that PR and follows its current target.

- **`checked_in` (default):** the writer merges or enables GitHub auto-merge only after the
  [merge decision gates](docs/writer-operations.md#merge-decision-before-auto-merge) pass.
  Completion requires the reconciler to observe a verified merge into the target branch.
- **`ready_for_check_in`:** completion requires a ready PR on the target base with the
  policy's required checks satisfied, actionable feedback addressed and conflicts resolved.
  The owner controls merging. A later verified owner merge updates the output and releases
  check-in dependencies.
- **Draft publication:** `--publication-mode draft` sets a ceiling for either PR target.
  Delivery verifies an open draft at the self-reviewed candidate and target base, then ends
  the writer with a draft output. The writer preserves that ceiling.
- **`report`:** `report_drafting -> report_review -> report_storage` binds review and
  storage to the exact Markdown draft, which the server finalizes durably.

GitHub owns PR facts and merging. Its required checks, reviews and rulesets apply throughout;
writer self-review complements those safeguards. Reconciler local-check failures raise
visible flags for the writer to address. Trusted validation policy defines required ready
checks. Owners and Captains can steer an open PR item's completion target; the writer
re-reads it and enables auto-merge only for ready-mode `checked_in` work after those gates pass.
Publication need not wait for them: the same writer remains in delivery with auto-merge off.
See the [merge decision and retained-session activation](docs/writer-operations.md#merge-decision-before-auto-merge).

Questions, dependency waits, external waits and retries retain the writer and its context.
Accepted evidence advances the item to its next stage; verified completion ends the writer.
Stopped-writer replacement preserves local work and reconciles external effects. See
[Writer operations](docs/writer-operations.md), the [state machine](STATE_MACHINE.md) and
[Recovery](docs/recovery.md) for the detailed contracts.

## Repository layout

| Path | Contents |
| --- | --- |
| [`phalanx/api.py`](phalanx/api.py), [`store.py`](phalanx/store.py), [`transitions.py`](phalanx/transitions.py) | FastAPI application, durable transactions and lifecycle decisions. |
| [`phalanx/__main__.py`](phalanx/__main__.py) | CLI entry point for intake, inspection, replies, onboarding, sessions, reconciliation and administration. |
| [`phalanx/shift.py`](phalanx/shift.py), [`writers.py`](phalanx/writers.py), [`work_cli.py`](phalanx/work_cli.py) | Captain client, server writer bindings and scoped writer commands. |
| [`phalanx/reconciler.py`](phalanx/reconciler.py), [`reconciliation.py`](phalanx/reconciliation.py) | GitHub observation client and server-side evidence/flag handling. |
| [`phalanx/prompts/`](phalanx/prompts/), [`phalanx/static/`](phalanx/static/) | Native-session prompts and Scribe browser assets. |
| [`phalanx/deployment.py`](phalanx/deployment.py), [`muster.py`](phalanx/muster.py), [`scripts/`](scripts/) | Installation/service entrypoints, existing-muster discovery and prepared-checks helpers. |
| [`tests/`](tests/), [CI workflow](.github/workflows/ci.yml) | Standard-library unittest suite, HTTP/Git fixtures and cross-platform CI. |
| [`.agents/skills/`](.agents/skills/) | Reusable agent skills: [PR review](.agents/skills/pr-review/SKILL.md) and [complexity-killer](.agents/skills/complexity-killer/SKILL.md) for evidence-based complexity and blocking audits. |
| [`docs/`](docs/), [`SPEC.md`](SPEC.md), [`STATE_MACHINE.md`](STATE_MACHINE.md) | Operating guides, implementation map and authoritative design/lifecycle references. |

CI runs the full unit suite on Windows/Python 3.12 (three shards), Linux/Python 3.14
(two), and macOS/Python 3.14 (two), plus one standalone envelope probe: eight test jobs.
The `Tests` aggregate remains the required check. Shards use deterministic longest-first
assignment with platform-specific runtime estimates in `tests/shard_timings.json`, not
test-count round-robin. New tests inherit their module's estimate (one second for a new
module), so discovery never drops unmeasured tests. Each shard still runs in test-ID order.
The timing file links its source CI run; initial estimates use elapsed time between verbose
test-completion timestamps. Values are seconds, with a module mean for ordinary tests and
exact-ID overrides for outliers above both one second and twice the module mean.
Module means exclude those outliers. When balance drifts, refresh estimates from a green
run's complete duration log on each target, then compare actual slowest/fastest shard
runtimes and end-to-end workflow wall time against the baseline. CI logs all test durations
to support that comparison; estimates alone are not performance acceptance evidence.

HTTP tests use isolated API applications running in-process on real loopback sockets
and selector event loops (avoiding Windows Proactor reset/teardown leaks),
so urllib and CLI subprocesses still exercise HTTP, authentication and background tasks.
Each fixture start runs a fresh application lifespan; explicit restart tests still stop
and start it against the same database. The bootstrap backup/restore test also retains
real server-process launches to cover process boundaries and durable recovery.

## Quickstart: new install

Use CPython 3.12 or later (3.14 for deployment), Git, and a runtime with native visible
sessions. Install the package and locked dependencies from the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock -e .
```

For reproducible setup, use
[`python -m phalanx deploy init`](docs/deployment.md): it creates credential-free templates
under a new `~/.phalanx_muster` directory by default. Configure the private environment,
charter, runtime workspace, repository mappings and prepared checks environments before
using its foreground service entrypoints. The [deployment guide](docs/deployment.md)
covers initialization and service ownership.

For a manual new install, generate and privately store the owner and runner tokens once.
The following starts the API with a new database in PowerShell:

```powershell
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_DB = Join-Path $HOME ".phalanx_muster\db\phalanx.sqlite3"
$env:PHALANX_LOCAL_BOARD = "1"
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

Record the database path and API URL with the stored tokens in a private runbook.
From a second shell with the same environment activated and the stored owner token set,
check health, register a repository and file work:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
# PR work defaults to checked_in:
python -m phalanx intake example "Add regression tests for the parser" --key parser-tests-1
```

Open `http://127.0.0.1:8000/operator/` to view and create work without pasting a token on
this explicitly enabled, directly accessed loopback-only board. `/v1` writes still require
their bearer credentials; leave local-board mode off behind proxies/tunnels or on hosted
servers. See [local-board security and rollout](docs/work-items.md#operator-work-board).
To execute the work, follow [Install Phalanx](docs/install.md) and
[Direct path](docs/direct-path.md): configure the runner and GitHub observation tokens,
reconciler mappings and validation policy, then start `python -m phalanx reconcile`.
Prepare the shift config, charter, shared worktrees workspace and per-repository
`checks_environment`. Use `python -m phalanx shift prompt captain` to obtain the instructions
for a native Captain chat, then attach Captain and keeper schedules through the runtime.
The install guide includes macOS/Linux commands and GitHub credential setup.

**One-time repository trust:** the owner must grant trust in the worker runtime before work can run.
In the GitHub Copilot app, also approve the per-project repository trust card in the project's session;
a checkout path in the runtime's `trustedFolders` configuration does not approve that app-level card.
For direct sessions, approve the configured shared worktrees project (`session_workspace`).
Only the owner grants trust. A Captain that finds a pending trust approval notifies the owner
and retains the same blocked writer/session until approval.

## Resume an existing install

After a restart or reboot, follow [Resume an existing install](docs/resume.md) with your
recorded muster directory, database path and stored tokens. Preserve these paths and
credentials when adopting deployment tooling; coordinate service changes through the
[service-owner rollout](docs/deployment.md#existing-runtime-reviewed-adoption-not-a-rename).
Reserve the new-install steps above for a new database and credentials.

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

The selected directory contains `shift.json`, `charter.toml`, `reconciler.json` and `private.env`.
The shift config can omit `charter` or use `charter.toml` (an absolute path to that same file
also works). Other config paths, such as checkout and worktree mappings, should be absolute.
`private.env` holds literal `KEY=VALUE` lines for `PHALANX_RUNNER_TOKEN`,
`PHALANX_GITHUB_TOKEN`, optionally `PHALANX_OWNER_TOKEN`, `PHALANX_DB`,
`PHALANX_LOCAL_BOARD`, `PHALANX_API_URL`, `PHALANX_HOME` and
`PHALANX_SHIFT_CONFIG`. Blank lines and whole-line `#` comments are allowed; matching outer
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
`reconciler.json` and its scratch directory `<muster>/reconciler` (overridable with `--state`).
Both accept `--home`; `shift` accepts it before or after its action. Explicit `--config`
and `PHALANX_SHIFT_CONFIG` retain their existing behavior without loading `private.env`;
an explicit `--home` instead selects the muster layout. Explicit reconciler configs still need
`--state`. `shift prompt` remains usable without any configuration.

Start the intended API yourself with `PHALANX_HOME` set to this directory, alongside its
**existing** database path and credentials. Authenticated `GET /v1/muster/status` reports the
API's configured home identity, live Captains, registered open writers, each reconciler's
last heartbeat/age/staleness (15 minutes), and dispatch pause. `muster status` reads this API,
prints JSON, and fails nonzero if it is unavailable or its home identity differs. An empty
reconciler list means none has recorded a heartbeat. Writer presence reports registration
state; inspect native sessions separately to verify process liveness. Status reads the API.
Use the [deployment entrypoints](docs/deployment.md#new-installation) to start services
explicitly and runtime schedules to wake Captain/keeper sessions. Existing installs can
keep their explicit config paths and live state locations when adopting the locator.

To retrieve one item's correlated timeline through the supported API, see the [paginated example and backward-transition fixture](docs/api.md#read-one-items-correlated-timeline).

## Documentation

- [Install Phalanx](docs/install.md): first-time setup, credentials, runner setup and repository trust.
- [Deployment tooling](docs/deployment.md): installation templates, service entrypoints and service-owner rollout.
- [Resume an existing install](docs/resume.md): restart order, single-instance checks and status.
- [Writer operations](docs/writer-operations.md): direct writers, publication, validation flags and delivery.
- [Direct path](docs/direct-path.md): the GitHub reconciler and a Captain on the direct path.
- [Architecture](docs/architecture.md): implemented ownership, sequence, deployment and recovery boundaries.
- [Recovery and troubleshooting](docs/recovery.md): writer discovery and replacement, and SQLite backup and restore.
- [Work items](docs/work-items.md): filing, draft-only PR delivery, GitHub issue import, answering questions and the operator board.
- [Configuration](docs/configuration.md): environment variables and the reconciler config.
- [API reference](docs/api.md) and [developing Phalanx](docs/development.md): scoped attempts, routes, transactions and tests.
- [Validation policy](docs/validation-policy.md), [GitHub observations](docs/direct-path.md#run-the-github-reconciler), [pilot reports](docs/pilots/) and [decision log](docs/decisions.md).

Design: the [spec](SPEC.md), [state machine](STATE_MACHINE.md), [roadmap](ROADMAP.md) and [implementation plan](IMPLEMENTATION_PLAN.md). Open [state-machine.html](state-machine.html) locally in a browser for the lifecycle diagram and execution status reference. Its inline SVG is maintained by hand alongside the Markdown Mermaid source.