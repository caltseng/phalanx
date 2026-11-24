# Phalanx

<img src="docs/phalanx-logo.svg" alt="Phalanx logo: an ivory spear-shaped mark on a crimson shield with a gold rim" width="96" height="96">

Phalanx is an agentic orchestrator for work across repositories. Work items live in an API-backed queue. Captain chats launch registered writer sessions in parallel to deliver a Markdown report or a pull request.

1. File a work item for a registered repository with a goal, enough context to execute it, and the expected output.
2. Start a **muster**, a batch of eligible work launched from the Captain chat. Agents work in parallel, use isolated checkouts for changes, and verify their results.
3. Follow each item through its lifecycle. The writer develops, self-reviews, pushes and opens its PR. The reconciler observes GitHub and flags problems for the writer to fix. GitHub's required checks and reviews gate merging. Draft-only work stops at a verified draft; ready-only work stops at a ready PR. Report work ends with a verified **field report** in Markdown.

**Phalanx advancing** means a work item has met the conditions to move to its next stage. Every closed work item has a final output: if it ends without the requested deliverable, Phalanx stores a field report explaining what happened and what remains.

The HTTP API server and durable store run locally, with visible worker sessions launched by Captain chats through their runtime's native tools. Phalanx is runtime-neutral: Codex, GitHub Copilot CLI or app, Claude Code, or any runtime with visible sessions can follow the [direct-session contract](docs/direct-sessions.md). The same server is intended to move to cloud hosting while agents keep running locally.

## Architecture

The HTTP API server owns the SQLite queue, executions, attempts, questions, reports and
timestamped evidence. It also serves **Scribe**, the local work board at `/operator/`.
Clients reach queue state only through the API; Git checkouts and credentials stay on
their host. `python -m phalanx reconcile` independently reads GitHub and git, posts
observations and heartbeats, and lets the server derive flags and verify delivery.

The operator supplies a charter file outside every repository. A native **Captain chat**
runs `phx shift` (shorthand for the configured `python -m phalanx shift` command), re-reads
that charter each wake, and chooses work within its repository scope, lane order and
`max_writers` budget. It launches one registered **writer session** per item in an isolated
worktree. In the Copilot app all writers open in the shared worktrees folder project,
`session_workspace`, rather than one project per worktree. Writers run `phx work` from
their own worktree with a scoped credential. A runtime-scheduled **keeper** starts a
Captain when none is live, including after a stale Captain, without restarting its writers.
See [the direct-path operating guide](docs/direct-path.md).

```text
Owner intake -> API + SQLite <-> Scribe (/operator/)
                    ^
Charter -> Captain (phx shift) -> writers (phx work) -> GitHub PR
Keeper  -> fresh Captain             |                     |
                    +---------------+   reconciler <------+
                                              |
                                              +-> API delivery evidence
```

Intake defaults to `checked_in`; choose `ready_for_check_in` for owner-only merging or
`report` for Markdown. Runner affinity is retained request metadata: this muster used
`direct-hold` to keep work off the former scheduler, but direct registration needs no such
sentinel. Charter lanes are Captain policy, explained by refreshed `held` actions, not
server queues or reserved slots. PR work flows through `development`, `self_review`,
`publication` and `delivery`. For ready-only work, the writer opens a ready PR, required
checks pass, and the reconciler verifies completion. The owner merges; a later reconciler
observation records that merge and releases check-in dependents. For `checked_in`, the
writer enables GitHub auto-merge under repository rules. Reports instead use drafting,
review and storage; draft-only publication is a separate ceiling.

Native Captain/writer sessions and the reconciler are the only execution path. Clients
keep worktrees, configuration, credentials, artifacts and OS locks; all durable execution
and observation records live behind the API. There are no client SQLite queues or journals.

## Quickstart: new install

Use CPython 3.12 or later (3.14 for deployment). For reproducible setup, use
[`python -m phalanx deploy init`](docs/deployment.md): it creates credential-free templates
under `~/.phalanx_muster` by default, never overwrites an existing directory, and starts
nothing. Configure the generated files, then use the tracked foreground service entrypoints.
Existing `phalanx-muster` installations keep their paths; adopt tooling only through the
[service-owner rollout](docs/deployment.md#existing-runtime-reviewed-adoption-not-a-rename).

The following manual alternative creates new tokens and a new database. In PowerShell, from the repository root:

```powershell
py -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.lock -e .
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_DB = Join-Path $HOME ".phalanx_muster\db\phalanx.sqlite3"
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
including during direct delivery. After ready completion, an owner's merge of the primary PR at
the verified head or a descendant is recorded automatically and releases check-in dependencies.

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
prints JSON, and fails nonzero if it is unavailable or its home identity differs. No recorded
reconcilers are reported as an empty list, not as evidence that one is running. Writer presence
is registration state, not an independent runtime process probe. No local database is opened.
There is deliberately no `muster start`: discovery/status never launch processes or run server
startup migrations. Existing installs with another layout can keep their explicit config paths;
do not move live state to adopt the locator.

To retrieve one item's correlated timeline through the supported API, see the [paginated example and backward-transition fixture](docs/api.md#read-one-items-correlated-timeline).

## Documentation

- [Install Phalanx](docs/install.md): first-time setup, credentials, runner setup and repository trust.
- [Deployment tooling](docs/deployment.md): versioned setup, service entrypoints and reviewed legacy adoption.
- [Resume an existing install](docs/resume.md): restart order, single-instance checks and status.
- [Writer operations](docs/writer-operations.md): direct writers, publication, validation flags and delivery.
- [Direct path](docs/direct-path.md): the GitHub reconciler and a Captain on the direct path.
- [Recovery and troubleshooting](docs/recovery.md): writer discovery and replacement, and SQLite backup and restore.
- [Work items](docs/work-items.md): filing, draft-only PR delivery, GitHub issue import, answering questions and the operator board.
- [Configuration](docs/configuration.md): environment variables and the reconciler config.
- [API reference](docs/api.md) and [developing Phalanx](docs/development.md): routes, leases, transactions and tests.
- [Validation policy](docs/validation-policy.md), [GitHub observations](docs/direct-path.md#run-the-github-reconciler), [pilot reports](docs/pilots/) and [decision log](docs/decisions.md).

Design: the [spec](SPEC.md), [state machine](STATE_MACHINE.md), [roadmap](ROADMAP.md) and [implementation plan](IMPLEMENTATION_PLAN.md). Open [state-machine.html](state-machine.html) locally in a browser to explore the lifecycle and execution status graphs.