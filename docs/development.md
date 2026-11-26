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

Synchronous handlers read one consistent snapshot with `phalanx.store.read_transaction(path)` and the path in `request.app.state.database_path`. It takes its read lock between serialized writes: a read that arrives during a write starts before the next writer, and one that arrives while a writer waits starts right after that writer. Admission has a four-second timeout separate from SQLite's two-second busy timeout; it does not interrupt an admitted transaction. Timed-out or interrupted waiters must remove their count from whichever queue currently owns it, including a read batch already handed off by a completed writer. Don't call it inside a write transaction.

Use `phalanx.store.write_transaction(path)` for writes, including all reads that inform a write. It serializes write transactions in the server process, starts with `BEGIN IMMEDIATE`, and commits or rolls back on exit. Keep the transaction short and do all external I/O outside it.

Connections use foreign-key enforcement, `synchronous=EXTRA` and a two-second busy timeout. When the store stays locked or busy past that timeout, any route returns 503 with `Retry-After: 1` and the detail `SQLite store unavailable (<SQLite reason>); retry the same request`. Current writer/attempt fencing, expected versions and idempotency keys protect retries; direct writers do not renew expiring leases. Other SQLite operational errors, such as a missing table or a read-only file, stay 500 because retrying won't fix them.

The contention exception handler is async and only constructs the JSON response: it must
not acquire another request-worker slot to report that storage is busy. Keep SQLite in
synchronous route/authentication workers, or `asyncio.to_thread` for background refresh
and report finalization, not on the event loop. Canceling an async await or disconnecting
an HTTP caller does not cancel the underlying transaction thread; reconcile durable state
before retrying an uncertain mutation.

Follow [AGENTS.md](../AGENTS.md) and decision 67 before adding schema changes; do not
add migrations or compatibility machinery without authorization. `store.initialize`
creates the canonical SQL in `phalanx.schema` transactionally on an empty database.
An existing database must have the current application ID and schema version (26);
startup never replays schema history or rewrites records.

### Existing-store conversion

The service owner, not a writer or startup hook, owns activation. Do not run this
against live state or reset a queue. Rehearse on an operator-supplied snapshot first:

1. The service owner retains a reviewed supported-source snapshot at an explicitly documented
   archive path, makes it read-only, and retains its source revision as the rollback/archive
   pair. Supported schema-24 sources carry retired NOT NULL attempt lease columns;
   schema-25 sources have already removed them.
   Take that backup with the pinned deployed schema-24 environment and the explicit
   existing `PHALANX_DB`: `python -P -m phalanx server backup <snapshot>`.
   Do not use the target schema-26 admin command to open a schema-24/25 source.
2. From the reviewed target checkout, use the configured checks interpreter to run
   `python -m scripts.convert_store <snapshot> <new-database>`. Both paths must be
   operator-selected; the destination must not exist. This explicit offline converter uses
   SQLite backup for a consistent input, builds the canonical schema, disables triggers
   only while copying, and copies every retained table/column without changing values.
   It rejects unexpected tables/columns and never writes to the source.
3. Keep the JSON result with the owner-controlled archive. It identifies the source's
   absolute path/file SHA-256 and every source table's stored columns, row count and
   SHA-256 (ordered rows encoded as compact ASCII JSON arrays with a newline; blobs
   encoded as `{"blob_hex":"..."}`). Source changes during conversion abort publication.
   It also verifies row counts, typed-value hashes, bidirectional value comparison,
   foreign keys, integrity, and equality of `(type, name, tbl_name, sql)` from
   `sqlite_master` against a fresh database. `rootpage` is physical allocation, not
   schema, and naturally differs with data volume. Attempts, history, reports, active
   writer bindings, versions, timestamps, capacity and board caches remain intact.
   Retired attempt fields, `attempt_workers` and `external_rechecks` remain available in the retained source
   archive, not runtime tables.
   The reviewed converter now covers **24 -> 26 directly**, including the retained-column
   conversion formerly used for 24 -> 25, or **25 -> 26** if that prior conversion was already
   activated. Both initialize only the four new scouting tables, with scouting disabled;
   the source inventory must otherwise match exactly. There is no need to run an obsolete
   intermediate service or temporarily expose schema 25. Preserve source-specific overlays,
   credentials and configured capacities in the cutover plan; source merge is not adoption.
4. Open the converted copy with an isolated target API and verify authenticated item,
   writer and board reads; rehearse a writer result/next-attempt transition only on that
   disposable copy. Do not run a reconciler or publish external effects from the rehearsal.
5. After source review and operator approval, quiesce API/reconciler/client writes,
   take a fresh supported backup, repeat conversion and verification, and let the owner
   install the converted file at the existing configured database path. Preserve the
   application identity, configuration, credentials, capacity values and reconciler.
   Verify health and existing bindings before resuming dispatch. Retain the original
   file in its documented immutable archive for rollback; never restore an old snapshot
   over active writers or switch back after new writes without reconciling those writes.

Receipt records are copied unchanged. New intake/results hash the complete validated
request, including defaults and nulls; old-format keys can conflict after conversion.
Reconcile any uncertain pre-cutover request against its stored response/current state
before using a new key. There is no old-digest fallback.

## Queue storage

Execution snapshots are JSON with indexed stage/status columns, one open execution per item
and one open attempt per execution. A Captain registers a named writer intent after checking
eligibility, pause and the persisted workspace writer budget. The server stores its scoped token digest;
the plaintext credential stays in the worktree's Git directory. `writer/attempt` calls
`intake.claim_attempt` transactionally for that writer's own queued execution, recording the
new attempt, pinned dependency evidence, running state and incremented version. Direct writer
bindings have no timed expiry. Results verify the current open writer's attempt binding in
the write transaction, not a copied credential or synthetic expiry on the attempt.
A committed result ends the attempt and, when immediately
runnable, claims the next stage for the same writer in that transaction.

Questions and waits retain the writer; stopped-writer replacement requires native-session
evidence and preserved local work, not an expired lease. All stage results use the writer
binding; no other PR-stage executor or client SQLite journal exists. Closed snapshots with
retired stage names are normalized once while preserving their timeline and outputs.

The result API accepts scoped stage evidence; trusted reconciler observations verify direct
PR delivery. The operator API provides bounded history, overview/group/filter reads and
conditional ETags; PR facts come only from reconciler observations. See
[direct writers and dispatch](api.md#direct-writers-and-dispatch).
