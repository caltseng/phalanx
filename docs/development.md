# Develop Phalanx

## Run the tests

Run the localhost HTTP and persistence tests with `python -m unittest discover -s tests -v`. Test modules share fixtures by importing sibling modules such as `test_bootstrap`, so run one module with `python -m unittest discover -s tests -p test_intake.py` rather than `python -m unittest tests.test_intake`. Tests use the standard library and the runtime dependencies. Git fixtures set their own commit identity and address bare repositories with `--git-dir`, so they need no global Git configuration and work with `safe.bareRepository=explicit`.

The [CI workflow](../.github/workflows/ci.yml) runs the same suite with the locked dependencies on every pull request and push to `main`. It covers Windows with CPython 3.12 and 3.14, and Ubuntu and macOS with CPython 3.14. Each run is split into three parallel jobs with `python tests/shard.py INDEX 3`, which deals the discovered tests round-robin so each test runs exactly once, and the single `Tests` check passes only when every shard passes.

## Route permissions and transactions

Use the `require_owner` and `require_runner` dependencies in `phalanx.auth` to declare route permissions.

Synchronous handlers read one consistent snapshot with `phalanx.store.read_transaction(path)` and the path in `request.app.state.database_path`. It takes its read lock between serialized writes: a read that arrives during a write starts before the next writer, and one that arrives while a writer waits starts right after that writer. Neither reads nor writes wait more than one turn of the other, however deep the write queue gets in DELETE journal mode. The gate adds no timeout of its own, because one write turn may legitimately outlast the busy timeout across `BEGIN` and `COMMIT`. Don't call it inside a write transaction.

Use `phalanx.store.write_transaction(path)` for writes, including all reads that inform a write. It serializes write transactions in the server process, starts with `BEGIN IMMEDIATE`, and commits or rolls back on exit. Keep the transaction short and do all external I/O outside it.

Connections use foreign-key enforcement, `synchronous=EXTRA` and a two-second busy timeout. When the store stays locked or busy past that timeout, any route returns 503 with `Retry-After: 1` and the detail `SQLite store unavailable (<SQLite reason>); retry the same request`. Lease checks and idempotency keys make retrying the identical request safe. Other SQLite operational errors, such as a missing table or a read-only file, stay 500 because retrying won't fix them.

Add schema changes as a new tuple of SQL statements at the end of `MIGRATIONS` in `phalanx.store`. The migrator runs the SQL and updates the schema version in one transaction.

## Queue storage

Migration 2 stores execution snapshots as JSON and exposes `stage` and `status` as queryable columns. It enforces one open execution per item and one open attempt per execution. A claim selects a queued execution and calls `intake.claim_attempt` inside the same `store.write_transaction` after checking admission. Supply a stable `runner_id`, the SHA-256 hex digest of a freshly generated URL-safe lease secret, and `lease_expires_at` as Unix seconds. Return the plaintext secret only to the claiming runner. The claim returns the new attempt ID and execution snapshot, appends history, increments the state version and changes the execution to running. A committed result ends the attempt. Renewal and recovery use the persisted attempt ID, runner ID, lease digest and expiry. A cancellation request keeps the attempt open until stop confirmation or recovery.

PH-06 owns candidate indexing, time eligibility, aging, capacity, admission and lease renewal. PH-08 owns reply and external recheck routes. PH-09 owns general failure finalization and retries. Provider verification and recovery of uncertain writes belong to the GitHub and runner integrations. The result API rejects caller-supplied PR observations. The operator API provides bounded history reads for the board.
