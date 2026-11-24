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

Store both tokens privately and record the `PHALANX_DB` path: Phalanx has no install locator, so you need them to resume. Then, from a second shell with the owner token set, check `http://127.0.0.1:8000/health`, register a repository and file work:

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
```

Open `http://127.0.0.1:8000/operator/` to watch it. To run the work, set up a runner and start a muster as described in [Install Phalanx](docs/install.md), which also covers macOS and Linux, repository trust and the GitHub token.

## Resume an existing install

After a restart or reboot, don't rerun the quickstart: it would create new tokens and could start an empty database. Follow [Resume an existing install](docs/resume.md) with your recorded muster directory, database path and stored tokens.

## Documentation

- [Install Phalanx](docs/install.md): first-time setup, credentials, runner setup and repository trust.
- [Resume an existing install](docs/resume.md): restart order, single-instance checks and status.
- [Runner operations](docs/runner-operations.md): the Captain loop, PR validation and check-in, and manual `attended` commands.
- [Captain relay and bridge](docs/captain-relay.md): the bridge contract, Captain chat relay, standing relay serve and runtime mapping.
- [Direct path](docs/direct-path.md): the GitHub reconciler and a Captain on the direct path.
- [Recovery and troubleshooting](docs/recovery.md): stuck relays, reservation reconciliation, and SQLite backup and restore.
- [Work items](docs/work-items.md): filing, GitHub issue import, answering questions and the operator board.
- [Configuration](docs/configuration.md): environment variables and the runner config.
- [API reference](docs/api.md) and [developing Phalanx](docs/development.md): routes, leases, transactions and tests.
- [Validation policy](docs/runner-validation-policy.md), [PR observations](docs/runner-pr-observations.md), [pilot reports](docs/pilots/) and [decision log](docs/decisions.md).

Design: the [spec](SPEC.md), [state machine](STATE_MACHINE.md), [roadmap](ROADMAP.md) and [implementation plan](IMPLEMENTATION_PLAN.md). Open [state-machine.html](state-machine.html) locally in a browser to explore the lifecycle and execution status graphs.