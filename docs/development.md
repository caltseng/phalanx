# Develop Phalanx

## Run the tests

Build `server/cmd/phalanx-server` with the prepared Go environment, outside the checkout,
and set `PHALANX_TEST_SERVER` to that absolute binary path. Copy `phalanx/static` to its
sibling `assets` directory for deployment/package tests. Run localhost HTTP and persistence
tests with `python -m unittest discover -s tests -v`. Before opening or updating a PR,
run only the modules that cover the change, as [AGENTS.md](../AGENTS.md) describes.
Test modules import sibling fixtures, so use discovery with `-p test_intake.py`, not
`python -m unittest tests.test_intake`. Tests use standard-library unittest and locked
Python client/admin dependencies; historical oracle dependencies have their own
`server/conformance/requirements.lock`. Git fixtures set their own identity and use
`--git-dir`, requiring no global Git configuration.

### CI shards and timing profiles

The [CI workflow](../.github/workflows/ci.yml) runs the full unit suite with locked
dependencies on every pull request and push to `main`: Windows/Python 3.12 (three
shards), Linux/Python 3.14 (two), and macOS/Python 3.14 (two), plus one standalone
envelope probe: eight test jobs. The `Tests` aggregate remains the required check
and passes only when all shards, the probe, native Go checks and runtime-safety jobs pass.

`python tests/shard.py INDEX COUNT` uses deterministic longest-first assignment with
platform-specific runtime estimates in `tests/shard_timings.json`, not test-count
round-robin. New tests inherit their module's estimate (one second for a new module),
so discovery never drops unmeasured tests. Each shard still runs in test-ID order.
The timing file links each platform's source CI run. Initial estimates use elapsed
time between verbose test-completion timestamps; refreshed profiles use complete
duration logs. Values are seconds, with a module mean for ordinary tests and exact-ID
overrides for outliers above both one second and twice the module mean. Module means
exclude those outliers.

When balance drifts, refresh estimates from a green run's complete duration log on
each target, then compare actual slowest/fastest shard runtimes and end-to-end workflow
wall time against the baseline. CI logs all test durations to support that comparison;
estimates alone are not performance acceptance evidence.

### Shared fixtures

HTTP tests use the actual native Go production executable on isolated loopback sockets,
so urllib and unchanged CLI subprocesses exercise HTTP, authentication and background
tasks. Fixtures create synthetic stores with retained offline schema helpers; Go never
initializes a missing store. Every start owns a separate process group and stops/drains
that exact process before cleanup. Restart tests reuse the same evolved database.
Shared Git and HTTP fixtures live in `tests/test_bootstrap.py`. Register temporary
directories with `self.enterContext(TemporaryDirectory())` before starting a server
so fixture cleanup stops the server before deleting its database.

CLI behavior tests use `tests/cli_runner.py` to call the real entry point in process,
capturing stdout, stderr and the exit code while restoring arguments, environment and
working directory after each call. The isolated deployment lifecycle test retains
`python -m phalanx` subprocess coverage for startup, restart and exclusive ownership.

`ServerFixture` does not accept Python apps or custom HTTP routes. Native internal
admission faults live in Go tests; production HTTP contention uses real external SQLite
locks and unchanged refusal/replay assertions. Python offline fixture/store unit tests
retain their own explicit short fault seams, not claimed Go coverage. The standalone
envelope probe also launches the real Go executable.
Operator tests seed only the rows they need, keeping the large keyset-paging dataset
in the deep-paging case.

`tests/operator-create-fixture.html` exercises the real shell/client with mocked transport,
including owner connections (`?owner`) and tokenless local mode. The production Go
route/asset flows are covered by `test_operator_web.py` and the Scribe conformance
family on disposable loopback stores. There is no Python fixture mount or production
`/fixtures` route. Never run fixture writes on a production muster; no browser
visual/layout recertification is inferred from HTTP tests.

## Route permissions and transactions

### Native Go server

The supported HTTP default lives in `server`: `internal/httpapi` owns route/auth/
transport projection, `internal/contract` validates presence/defaults and canonical
receipt bytes, `internal/application` owns atomic domain operations and durable
finalization/refresh jobs, and `internal/sqlite` owns admission/connections/transactions.
Reuse their existing helpers; do not put network/provider/native-session work inside
transactions or reimplement domain rules in clients. Accepted operations outlive HTTP
disconnect; explicit interruption rolls back and unknown commit outcomes require
receipt reconciliation. The shared OS lock covers HTTP and background-work drain.

Use the prepared checks prefix from `server` for `go mod verify`, `go build -trimpath
-buildvcs=false ./...`, `go vet ./...` and `go test ./...`. The native Go CI job needs
no Python oracle or generated source manifest. Test schemas are initialized in one
transaction without disabling foreign keys, production synchronization or admission.
Native JSON test logs preserve exact test IDs and per-test durations.

The separate required runtime-safety job retains [real packaged release/identity/rollback proofs](go-server-release.md),
both-direction OS locks, Windows Task Scheduler supervision and production headers.
The historical P0/D0 differential corpora are opt-in through the CI workflow's
`historical_conformance` dispatch input; no historical source, manifest or golden is
deleted. Their harness checks immutable golden integrity and records current
working-file provenance instead of rejecting routine `.go` edits against an old
source hash. Neither normal checks nor explicit reruns require manifest regeneration.
The independent
[parity report and remediation map](python-server-deprecation.md#reviewed-parity-evidence)
are evidence, not permission to normalize a failure or refreeze P0/D0.
Unix race/OS exclusion and the complete Python suite remain required CI.

Native replacement coverage is maintained in executable Go tests, not only an inventory:

| Contract | Go regression |
| --- | --- |
| Roles and origin refusals | `httpapi.TestRouteRoleMatrix`, `TestRouteAuthorityWireBytes`, `TestRetainedDashboardPeerAndOriginVectors` |
| 422 field highlighting, exact idempotent replay and history | `httpapi.TestNativeValidationDetailsAndIntakeReplay`, `TestHistoryAndTimelineScopeAndMissing` |
| Inbox keyset and filter-bound cursors | `httpapi.TestNativeInboxKeysetAndOperatorCursorScope`, `application.TestFilteredOperatorPageContinuesAfterExactCandidateWindow` |
| Captains and archives | `application.TestCaptainLivenessBoundariesAgreeAcrossViews`, `TestArchiveStatusRetainsReplacementAndCaptainBlockers` |
| Reconciliation and transactional delivery/replay | `application.TestObservationAndDeliveryRollbackTogetherThenReplay`, `TestObservationStoragePreservesCanonicalPayload` |
| Scribe asset bytes, CSP, conditional reads, ranges and traversal | `httpapi.TestScribeAssetsServeRepositoryBytesAndSafeBoundaries` |

Original Python HTTP assertions run against the production Go binary through a
startup/teardown adapter; retain exact selected IDs/exclusions and complete typed state.
Python monkeypatch/clock/SQL-spy tests remain Python reference tests, not claimed Go
coverage. Keep auth, writer CLI, muster and reconciler tests even after HTTP deprecation.
`test_deployment.py` covers default/saved/explicit engine precedence, no fallback,
unmodified configuration, removed-Python refusals and actual Go single ownership. `test_server_admin.py`
covers exclusive new-store initialization without launching HTTP or overwriting data.

### Retained Python offline helpers

Python clients, models, admin/schema/backup and synthetic domain fixtures remain.
`tests/operator_fixture.py` retains only offline expected projections, not routes,
request/response framework classes, application startup or a server. Mixed domain
modules retain the helpers consumed by fixture seeding and assertions, with their
HTTP imports/decorators removed. P0/D0 exports and historical goldens remain immutable
independent evidence in conformance; they are not current production implementations.

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
before using a new key. There is no general old-digest fallback; intake alone accepts
the exact current and historical default-null fingerprints defined in
[SPEC.md](../SPEC.md#intake-receipt-fingerprints), without rewriting receipts.

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
