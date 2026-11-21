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

Use Python 3.12 or later. From the repository root in PowerShell:

```powershell
py -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -e .
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

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

Use the `require_owner` and `require_runner` dependencies in `phalanx.auth` to declare route permissions. Synchronous handlers can open short-lived connections with `phalanx.store.connect` and the path in `request.app.state.database_path`. Connections enable foreign keys and use explicit SQL transactions for multi-statement writes. Add schema changes as a new tuple of SQL statements at the end of `MIGRATIONS` in `phalanx.store`. The migrator runs the SQL and updates the schema version in one transaction.

Run the localhost HTTP and persistence tests with `python -m unittest discover -s tests -v`.
