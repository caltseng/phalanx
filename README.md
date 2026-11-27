# Phalanx

<img src="phalanx/static/phalanx-brand.svg" alt="Phalanx — Push forward: ivory chevron on a crimson circular shield with a gold rim" width="296" height="76">

**Delegate outcomes, not repeated prompts.** Phalanx keeps a queue of agent work moving
across repositories, from clear acceptance criteria through implementation, review, checks
and a merged pull request or stored Markdown report. Durable ownership preserves work
when a chat ends, a Captain changes, CI fails or a decision blocks progress; the board
shows what's moving, what's blocked and what needs you.

For one small change, a normal coding chat is simpler. Use Phalanx when coordinating
several workstreams costs more than writing the code.

## How it works

1. File a work item for a registered repository with a goal, context and acceptance criteria.
2. Start a **muster**: a Captain selects eligible work under an operator-owned charter
   and launches visible writers in parallel, each with an isolated item worktree.
3. Writers implement, check, self-review and publish. A reconciler follows GitHub,
   flags actionable problems and supplies evidence for verified delivery.

Ordinary authorized API and Scribe intake in a charter repository enters Captain pickup
without listing every item in charter notes. Notes may order or constrain work; excluded
items show a concrete pickup reason in Captain status and Scribe.

Phalanx is runtime-neutral: Codex, GitHub Copilot CLI or app, Claude Code, and other
runtimes with visible sessions can follow the [direct-session contract](docs/direct-sessions.md).
The CLI, Captain/writer clients and reconciler support Windows, macOS and Linux.
Each operator supplies their own identities, credentials, paths and repository mappings;
configured identity verification and ownership checks still apply.

## Architecture and lifecycle

| Component | Responsibility |
| --- | --- |
| **HTTP API and SQLite** | Authenticated, durable work state, fenced attempts, questions, history and verified outputs. |
| **Scribe** | Work board at `/operator/`: intake, timelines, questions, flags and dispatch settings. |
| **Captain chat** | Reads the charter and runtime inventory, prepares worktrees, registers writers and coordinates recovery. |
| **Writer session** | Implements, checks, self-reviews, publishes and fixes its primary PR through delivery. |
| **GitHub reconciler** | Independently observes GitHub and runs trusted local checks; the API derives flags and verifies delivery. |
| **Runtime keeper** | Uses native schedules and session tools to start a Captain when needed, preserving existing writers. |

Clients reach work state through the API. The execution host keeps worktrees, prepared
checks environments and private credentials; each writer has its own scoped credential.
Operator-owned Building and Total limits apply across Captains and repositories, with
waiting writers retaining Total. See [configuration](docs/configuration.md) for capacity
settings and [architecture](docs/architecture.md) for code ownership, diagrams and recovery
boundaries. Hosted operation requires HTTPS, persistent storage, authentication and backups.

PR work follows `development -> self_review -> publication -> delivery`, with review
bound to the candidate commit. Publication records one primary PR; it is not completion.
The same writer stays registered during delivery waits and fixes actionable problems.

| Target or mode | Verified output |
| --- | --- |
| **`checked_in` (default)** | A merge into the target branch, observed by the reconciler. |
| **`ready_for_check_in`** | A ready PR on the target base with required checks satisfied, feedback addressed and conflicts resolved; the owner controls merging. |
| **Draft publication** | An open draft at the self-reviewed candidate and target base; `--publication-mode draft` preserves this ceiling for either PR target. |
| **`report`** | A durably stored Markdown field report after `report_drafting -> report_review -> report_storage`. |

GitHub's required checks, reviews and merge rules apply. Before merging or enabling
auto-merge, writers satisfy the [merge decision gates](docs/writer-operations.md#merge-decision-before-auto-merge).
Publication may proceed with auto-merge off while gates remain outstanding. Questions,
dependencies and retries preserve context; failure and cancellation reports retain partial
results and recovery information. Every closed execution records a verified PR or stored report.

## Quickstart: new install

The packaged **Go HTTP server is the supported default**. The Python HTTP server is
deprecated and temporarily retained for [explicit rollback](docs/python-server-deprecation.md).
Python CLI, Captain/writer clients, reconciler and server-host admin remain supported;
Scribe assets are unchanged. Repository defaults do not change any live installation.

Use CPython 3.12 or later (3.14 for deployment/admin), Git, a verified
[Go server package](docs/go-server-release.md), and a runtime with native visible
sessions. From the repository root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock -e .
```

For reproducible setup, [deployment tooling](docs/deployment.md) creates credential-free
templates with `python -m phalanx deploy init` under a new `~/.phalanx_muster` directory
by default. Configure credentials, the charter, runtime workspace, repository mappings and
prepared checks environments before starting its foreground service entrypoints.

For a manual **new** install, generate and privately store the owner and runner tokens
once. Start the API with a new database:

```powershell
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_DB = Join-Path $HOME ".phalanx_muster\db\phalanx.sqlite3"
$env:PHALANX_LOCAL_BOARD = "1"
python -m phalanx server initialize
& "C:\packages\reviewed\phalanx-server.exe" --host 127.0.0.1 --port 8000 --assets "C:\packages\reviewed\assets"
```

Replace the example package path with the verified extracted package. Initialization
is a new-store-only offline admin operation, not a Python HTTP launch or a migration;
it refuses existing data. For supervised operation use `deploy run` with `server.go_binary`
or `--go-binary` as described in [deployment](docs/deployment.md).
Record the database path, package revision/digest, API URL and credential locations in a private runbook.
From a second shell with the same environment activated and stored owner token set:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
python -m phalanx intake example "Add regression tests for the parser" --key parser-tests-1
```

Open `http://127.0.0.1:8000/operator/` to view and create work on the explicitly enabled,
directly accessed loopback-only board. `/v1` writes still require bearer credentials.
Leave local-board mode off behind proxies/tunnels or on hosted servers; read
[local-board security](docs/work-items.md#operator-work-board) before enabling it.

To execute work, follow [Install Phalanx](docs/install.md) and [Direct path](docs/direct-path.md):
configure runner and GitHub observation tokens, reconciler mappings and validation policy,
then start `python -m phalanx reconcile`. Prepare the shift config, charter, shared worktrees
workspace and per-repository `checks_environment`. Obtain native Captain instructions with
`python -m phalanx shift prompt captain`, then attach Captain and keeper schedules through
the runtime. The install guide covers macOS/Linux setup and
[one-time owner-granted repository trust](docs/install.md#grant-repository-trust-once).

## Resume an existing install

Use [Resume an existing install](docs/resume.md) with the recorded muster directory,
database path and stored tokens. Do not repeat new-install initialization or replace
credentials. Coordinate service changes through the
[service-owner rollout](docs/deployment.md#existing-runtime-reviewed-adoption-not-a-rename).

[Direct muster discovery and status](docs/configuration.md#direct-muster-discovery-and-status)
covers locator precedence, private configuration and status commands. For contributor
checks and shared fixtures, use [Develop Phalanx](docs/development.md).

## Documentation

- [Install Phalanx](docs/install.md): first-time setup, credentials and repository trust.
- [Deployment tooling](docs/deployment.md): templates, service ownership and reviewed rollout.
- [Resume an existing install](docs/resume.md): restart order and existing-state recovery.
- [Configuration](docs/configuration.md): environment, muster discovery, capacity and reconciler settings.
- [Work items](docs/work-items.md): intake, issue import, questions and the operator board.
- [Direct path](docs/direct-path.md): Captain shifts, schedules, scouting and GitHub observation.
- [Direct sessions](docs/direct-sessions.md): runtime-neutral session and dispatch contracts.
- [Writer operations](docs/writer-operations.md): publication, validation flags and delivery.
- [Recovery](docs/recovery.md): writer replacement, troubleshooting, backup and restore.
- [Architecture](docs/architecture.md): implementation map and ownership diagrams.
- [Python HTTP deprecation](docs/python-server-deprecation.md): reviewed parity findings, retained Python scope, follow-on removal inventory and temporary rollback.
- [HTTP server migration](docs/http-server-migration.md): historical proposal/decision evidence and current implementation pointer; not a live-deployment receipt.
- [API reference](docs/api.md): routes, scoped attempts and correlated timelines.
- [Develop Phalanx](docs/development.md): tests, CI, fixtures and transactions.
- [Validation policy](docs/validation-policy.md): trusted local checks and required GitHub checks.
- [Observed usage](docs/usage.md): optional display-only usage and coverage.
- [Decision log](docs/decisions.md): recorded design and operating decisions.
- [Dashboard](DASHBOARD.md): Scribe design and interaction reference.

Design references: [spec](SPEC.md), [state machine](STATE_MACHINE.md),
[roadmap](ROADMAP.md), [implementation plan](IMPLEMENTATION_PLAN.md) and
[pilot reports](docs/pilots/). Open [state-machine.html](state-machine.html) locally for
the hand-maintained lifecycle diagram.
