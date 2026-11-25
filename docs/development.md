# Develop Phalanx

## Run the tests

Run the localhost HTTP and persistence tests with `python -m unittest discover -s tests -v`. Before opening or updating a PR, run only the modules that cover the change, as [AGENTS.md](../AGENTS.md) describes. Test modules share fixtures by importing sibling modules such as `test_bootstrap`, so run one module with `python -m unittest discover -s tests -p test_intake.py` rather than `python -m unittest tests.test_intake`. Tests use the standard library and the runtime dependencies. Git fixtures set their own commit identity and address bare repositories with `--git-dir`, so they need no global Git configuration and work with `safe.bareRepository=explicit`.

The [CI workflow](../.github/workflows/ci.yml) runs the same suite with the locked dependencies on every pull request and push to `main`. It covers Windows with CPython 3.12 and 3.14, and Ubuntu and macOS with CPython 3.14. Each run is split into three parallel jobs with `python tests/shard.py INDEX 3`, which deals the discovered tests round-robin so each test runs exactly once, and the single `Tests` check passes only when every shard passes.

CLI behavior tests use `tests/cli_runner.py` to call the real entry point in process,
capturing stdout, stderr and the exit code while restoring arguments, environment and
working directory after each call. The isolated deployment lifecycle test retains
`python -m phalanx` subprocess coverage for startup, restart and exclusive ownership.

`tests/operator-create-fixture.html` exercises the real shell/client with mocked transport,
including owner connections (`?owner`) and tokenless local mode. For the real-API flow, use
an isolated temporary database/server bound to loopback with `PHALANX_LOCAL_BOARD=1` and
one test repository registered. In that test app only, mount the repository root at
`/fixtures` using `StaticFiles`, then open `/fixtures/tests/operator-create-fixture.html?live`.
It creates a report work item, changes the isolated canonical capacity, and checks server
validation, persistence, tokenless requests, focus restoration and unchanged `/v1`
authorization. Never expose this fixture mount or run
its writes on a production muster. No browser package installation is required.

## Route permissions and transactions

Use `phalanx.auth` route dependencies: `require_owner` for owner writes,
`require_runner` for trusted reconciler writes, `require_coordinator` for owner/runner
coordination, and `require_writer` for scoped writer operations. Board reads use the
separate opt-in local-read dependency, never write authorization. See the
[architecture map](architecture.md) for ownership and code entry points.
Only explicitly selected `/operator/api/` actions use `require_board_owner`, plus
`require_same_origin` for writes. That dependency admits trusted local dashboard requests
under the explicit local-board flag, without returning or injecting the server's owner token.
Do not apply it to a shared API router or unrelated owner/runner/writer routes.

Synchronous handlers read one consistent snapshot with `phalanx.store.read_transaction(path)` and the path in `request.app.state.database_path`. It takes its read lock between serialized writes: a read that arrives during a write starts before the next writer, and one that arrives while a writer waits starts right after that writer. Neither reads nor writes wait more than one turn of the other, however deep the write queue gets in DELETE journal mode. The gate adds no timeout of its own, because one write turn may legitimately outlast the busy timeout across `BEGIN` and `COMMIT`. Don't call it inside a write transaction.

Use `phalanx.store.write_transaction(path)` for writes, including all reads that inform a write. It serializes write transactions in the server process, starts with `BEGIN IMMEDIATE`, and commits or rolls back on exit. Keep the transaction short and do all external I/O outside it.

Connections use foreign-key enforcement, `synchronous=EXTRA` and a two-second busy timeout. When the store stays locked or busy past that timeout, any route returns 503 with `Retry-After: 1` and the detail `SQLite store unavailable (<SQLite reason>); retry the same request`. Current writer/attempt fencing, expected versions and idempotency keys protect retries; direct writers do not renew expiring leases. Other SQLite operational errors, such as a missing table or a read-only file, stay 500 because retrying won't fix them.

Follow [AGENTS.md](../AGENTS.md) and decision 67 before adding schema changes; do not
add migrations or compatibility machinery without authorization. Existing startup
uses the SQL tuples in `MIGRATIONS` in `phalanx.store`, committing each step and version
together. The authorized retirement steps drop old tables and normalize closed
execution snapshots once, preserving immutable history and outputs. They are not a
general invitation to rewrite live data. Stores older than `OLDEST_SUPPORTED_VERSION`
are refused.

## Queue storage

Execution snapshots are JSON with indexed stage/status columns, one open execution per item
and one open attempt per execution. A Captain registers a named writer intent after checking
eligibility, pause and the persisted workspace writer budget. The server stores its scoped token digest;
the plaintext credential stays in the worktree's Git directory. `writer/attempt` calls
`intake.claim_attempt` transactionally for that writer's own queued execution, recording the
new attempt, pinned dependency evidence, running state and incremented version. Direct writer
bindings have no timed expiry. A committed result ends the attempt and, when immediately
runnable, claims the next stage for the same writer in that transaction.

Questions and waits retain the writer; stopped-writer replacement requires native-session
evidence and preserved local work, not an expired lease. All stage results use the writer
binding; no other PR-stage executor or client SQLite journal exists. Closed snapshots with
retired stage names are normalized once while preserving their timeline and outputs.

The result API accepts scoped stage evidence; trusted reconciler observations verify direct
PR delivery. The operator API provides bounded history, overview/group/filter reads and
conditional ETags; PR facts come only from reconciler observations. See
[direct writers and dispatch](api.md#direct-writers-and-dispatch).
