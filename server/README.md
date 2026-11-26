# Disposable Go HTTP foundation

**Not deployable.** This is the first implementation slice of the
[HTTP server migration specification](../docs/http-server-migration.md).
It does not replace the Python service, alter deployment entrypoints, discover
operator configuration, migrate a database, or change Python clients/reconciler/Scribe.
Implemented HTTP operations are `/health`, the owner/runner role probes,
repository registration/listing, owner intake, scoped item reads, display-title
reads/writes, execution history/timeline, draft reads, owner cancel/close-failure,
owner inbox, owner retry-finalization, dispatch settings, writer
registration/update/withdrawal, writer attempt/progress and writer capacity.
Writer result/stop-confirmed, owner reply, stored-report reads, steering/rerun,
Captain registration/heartbeat/reinforcement/current routing and session-archive
reads/writes are also implemented.
Native report finalization and bounded dependency refresh use the existing
schema26 jobs. Current-writer flags, reconciliation, scout, usage and
operator/Scribe routes remain unimplemented.
A 404 outside this slice is not API parity.

## Boundaries and pinned build

`internal/httpapi` owns transport/validation/authentication; `internal/application`
owns immutable intake receipts and transaction orchestration; `internal/sqlite`
owns SQL, per-transaction connections, PRAGMAs, admission, and commit outcomes.
`internal/contract` is the presence/validation/canonical encoding
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

Windows CI provisions a job-local, non-free-threaded Python-build-standalone
`20261110` CPython 3.14.10 archive, SHA-256
`32b6cf90a03e2bc15fda8d2ab7ff78c4870f138887d612932650ce764efaab8f`,
and verifies SQLite 3.53.1 before installing the locked oracle dependencies.
`setup-python`'s Python version alone did not establish the SQLite pin; the
original runtime/corpus checks remain unchanged. Runtime evidence is saved even
on a pin failure. No global interpreter, other checkout, or deployed runtime is
modified to repair CI.

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

## Native domain extension evidence (in development)

The immutable foundation's 114 vectors and both golden files are unchanged.
Additional conformance entrypoints reuse its exact public source exports,
runtime checks, full typed-store snapshots, representation-only comparator and
fresh/retained fixtures. They do not refreeze the Python oracle:

| Corpus | Cases per implementation | Evidence |
| --- | ---: | --- |
| `history.py` | 51 on each schema shape | Item/execution-scoped history/timeline/draft errors; title audit/replay/clear/stale revisions, unbounded strict integers, Unicode, role/origin, repeated trailing-slash redirects and first-matched-route `Allow` behavior |
| `cancellation.py` | 26 on each schema shape | Owner stop, immutable pending-intent receipt replay, final Markdown, writer termination, version and role refusals |
| `inbox.py` | 52 on each schema shape | Ordered input waits/questions, strict time/ID cursors, last duplicate query values, exact integer coercion/size guards, authorization precedence and zero read effects |
| `writers.py` | 52 on each schema shape | Existing allocations, nonce replay, claim/progress, native-session confirmation, stopped withdrawal, capacity revisions/handoff, strict dispatch settings and preserved limits |
| `results.py` | 32 on each schema shape | Report/PR results, review/storage receipts, same-writer continuation through owner questions/replies |
| `steering.py` | 22 on each schema shape | Version-fenced owner steering and rerun, exact receipt replay and unchanged scope on refusals |
| `captains.py` | 41 on each schema shape | Lifecycle, current routing, reinforcement, session field-error ordering, preserved null/duplicate first-seen values and missing-key refusal |
| `archives.py` | 14 on each schema shape | Active-ownership refusal, immutable terminal archive outcomes and authorization |
| `decisions.py` | 11,298 | Native state/event/evidence matrix, including finalization/stop/merge precedence, draft head review and ready-output enrichment |
| `numbers.py` | 4,612 | Exact Python canonical numeric bytes and receipt digests, integer/float distinctions, negative zero, exponent boundaries, binary64 extremes and nonfinite refusal |

Build `cmd/conformance-decisions` and `cmd/conformance-json` with the configured
Go prefix. Run their corresponding Python corpus with `--binary` and `--output`
outside the checkout, just like the HTTP harness. `history.py` and
`cancellation.py` and `inbox.py` take the existing conformance-server binary.
`vectors-manifest.json` binds each extra corpus, source files and Go tests.
`harness.py --record-vectors` is an explicit source/test-manifest authoring
operation: it verifies the unchanged oracle/source pins and refuses changed
foundation golden bytes. It is not a golden-update or mismatch-normalization path.

Captain list views load liveness, session observations and four detail queries
once per page, retaining each Captain's 100-ended-writer limit and full cleanup
count. A two-Captain fixture checks count, ordering and independent limits.

Focused native tests additionally cover racing title revisions, report
savepoint rollback, exact 2/4/8/16-second retry scheduling and fifth-failure
stall, owner retry, exact UTF-8 report storage, and resuming existing durable
finalization intents after reopening the store. Background work drains an
already dispatched bounded batch before the disposable adapter releases its
instance lock; it does not introduce a production shutdown deadline.

These are partial implementation proofs, **not full workflow/API conformance**.
Pure decision parity does not prove transactional writer/claim/capacity
continuation. Complete finalization/dependency race/fault and package-switch
coverage, all remaining route/model families, exact-head independent review,
fresh required CI and the separately authorized release/adoption work remain
required. No server-launch adapter, live configuration, schema, credential,
Python CLI/reconciler or Scribe asset was changed.
