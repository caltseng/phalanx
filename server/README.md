# Disposable Go HTTP foundation

**Not deployable.** This is the first implementation slice of the
[HTTP server migration specification](../docs/http-server-migration.md).
It does not replace the Python service, alter deployment entrypoints, discover
operator configuration, migrate a database, or change Python clients/reconciler/Scribe.
Only `/health`, the owner/runner role probes, repository registration/listing,
owner intake, and scoped item reads are implemented. Other routes, background
finalization/refresh, board authority, complete state transitions, and deployment
shutdown/rollback remain subsequent work. A 404 outside this slice is not API parity.

## Boundaries and pinned build

`internal/httpapi` owns transport/validation/authentication; `internal/application`
owns immutable intake receipts and transaction orchestration; `internal/sqlite`
owns SQL, per-transaction connections, PRAGMAs, admission, and commit outcomes.
`internal/contract` is the narrowly scoped presence/validation/canonical encoding
boundary. `internal/instancelock` holds the Python-compatible database-adjacent OS
lock for the entire test-server lifetime. There is no production `serve` command.

Go **1.27.4**, `modernc.org/sqlite` **v1.63.0** (observed SQLite **3.54.1**),
`golang.org/x/sys` **v0.50.0** and the
transitive module graph are pinned in `go.mod`/`go.sum`. `GOTOOLCHAIN=local` refuses
implicit compiler replacement. The pure-Go driver was selected for a no-C-compiler
normal build, after actual synthetic DDL/typed-data, FK/EXTRA, rollback, busy/commit,
online-backup, and Python package-switch/lock checks. This is not a claim that CGO
drivers are incompatible, that artifacts are static on every host, or that Go
removes host CPU contention. A CGO alternative is warranted by a concrete proof
failure, not required merely to duplicate a passing implementation.

The configured isolated checks prefix supplies this toolchain. From `server`,
append these commands to that prefix:

```text
go mod verify
go build -trimpath -buildvcs=false ./...
go vet ./...
go test ./...
```

CI builds/tests on Windows, Linux and macOS and race-checks Linux/macOS with their
prepared C compilers. The supplied local Windows environment has CGO disabled
and no C compiler: a local `go test -race` is an explicit gap, not a passing check.
Windows real-loopback differential/lock evidence is a required CI job. Linux/macOS
lock implementations build there, but Python-Go bidirectional lock proof is
currently for Windows only; those hosts are not authorized deployment targets.

## Immutable disposable conformance

The harness exports **public immutable Git objects**, not another checkout,
installed server, production data/configuration, account, or credential:

| Oracle | Git revision |
| --- | --- |
| Specification source P0 | `9af29396b37f86816fd274e01632164a47718a57` |
| Operator-inventoried installed D0 source | `ce0a32cd7c83c77d50d19b7a4601cd48cdacf8ae` |
| Public retained-schema initializer | `7b86cc994916dc8c393e6217303374b1a5c2c9d0` |

`conformance/manifest.json` pins every Python source/test file, lockfile and the
CPython 3.14.10 / SQLite 3.53.1 / dependency oracle. It records the exact existing
safety-module manifest, not a claim that every migration acceptance case is done.
The compact golden files retain **exact request bytes, raw response bytes/header
multimaps and every typed SQLite row**, using content-addressed row/snapshot
deduplication, not dropped tables. The harness re-runs P0 and D0 and refuses oracle
drift before comparing Go. It never fetches a substitute oracle on a missing pin.
`vectors-manifest.json` binds the 114 exact request IDs, Go test IDs, source hashes,
module graph and both golden corpora. That is 684 ordinary wire/durable
observations across three implementations/two shapes, plus separately recorded
busy, switch, abort, lost-response/crash and lock fault windows.
Scoped Git attributes keep server sources/manifests LF on every host; oracle
exports disable caller `autocrlf` and verify actual extracted source bytes
against the unchanged Git-blob manifest.

Fresh canonical 26 and the public old-initializer -> D0 retained-26 sequence are
both exercised. Retained DDL SHA-256 is
`4186600d653716a1688bfb4a425bc55bea4fdef7aa0d756d7fdf07a730e93950`.
This preserves old `queue_affinity`, `aging_seconds`, indexes, saved settings and
opaque receipts; schema-version equality is not permission to reshape them.
All stores/ports/processes/tokens are synthetic, independently allocated fixtures.

From the repository root, use the configured checks prefix to build the fixture
adapter and run the harness with an explicitly chosen artifact directory outside
the checkout:

```text
go build -trimpath -buildvcs=false -o <artifact-directory>\conformance-server.exe ./cmd/conformance-server
```

Run that build from `server`, then from the repository root:

```text
server\conformance\harness.py --binary <artifact-directory>\conformance-server.exe --output <artifact-directory>\conformance-proof
```

The second command is a Python argument to the checks prefix. The binary refuses
missing databases and requires the harness's exact disposable marker/file name;
it only binds an ephemeral loopback port and uses public synthetic tokens.
`--freeze-manifest` and `--freeze` are explicit authoring operations: never run
them to make a mismatch pass. Golden changes require oracle provenance and review.

### Comparison and fault contract

The comparator records raw bytes but permits only JSON representation
(object-key order, escaping, whitespace) and generated transport Date/Server/
length/chunking differences. It preserves numeric types, null versus missing,
error arrays, values, array order, statuses and required headers. Both test
adapters inject the same clock/IDs. SQLite `randomblob` operator-change tokens are
retained raw and compared through one bijection per sequence: different tokens
cannot collapse into one identity or hide a changed token on refused work.

Vectors cover explicit-null/omitted/default values, coercion refusal, Unicode and
ASCII separators, exact canonical/null-compatible fingerprints, both historical
intake receipt forms, changed/unproved-key conflicts, strict removed/extra-field
rejection, dependencies, role/scope/no-follow redirects, and browser-origin rules.
Both unchanged Python client APIs read/write/retry the slice. Every HTTP refusal
checks all durable tables and DDL for side effects.

The store keeps DELETE journaling, connection-local EXTRA/FK-on/2-second busy,
four-second alternating writer/read-batch admission, and connection-per-transaction.
Measured busy/admission checks allow one second of host tolerance, never a larger
database timeout; all waiter counts must return to baseline.
HTTP fault-window elapsed times are retained separately: dispatch/scheduling
latency is not a proxy for a per-SQL-call busy timeout or an operation deadline.
Interrupted waiters are removed before and after batch transfer. HTTP disconnect
does **not** cancel dispatched/queued mutation work; explicit operation context
interruption cleans up and rolls back. Failed BUSY COMMIT rolls back before gate
release; other commit failures return explicit unknown-commit errors for
receipt/state reconciliation, with no blind replay or success-shaped fallback.
Unit barriers distinguish caller loss from explicit interruption and prove
committed-but-unacknowledged receipts remain reconcilable. No total operation
deadline or production drain deadline is inferred from admission bounds.

Package-switch rehearsal here is only for this slice on synthetic fresh/retained
data. It is not full-route D0 -> Go -> D0 rollout/rollback approval, filesystem
power-loss proof, Scribe parity, production adoption, or a performance/SLO claim.
