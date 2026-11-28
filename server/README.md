# Go HTTP server and disposable compatibility proof

**Supported repository default; not automatically deployed.** The native server implements the
[HTTP server migration specification](../docs/http-server-migration.md).
`deploy run` selects the verified Go package; explicit CLI/saved Python selections
fail with migration guidance. It never rewrites live configuration/tasks, migrates a database,
or changes Python clients/reconciler/Scribe. Python HTTP is
[removed; rollback uses a retained known-good package](../docs/python-server-deprecation.md);
the live installation's engine/revision remains a separate operator-owned fact.
Implemented HTTP operations are `/health`, the owner/runner role probes,
repository registration/listing, owner intake, scoped item reads, display-title
reads/writes, execution history/timeline, draft reads, owner cancel/close-failure,
owner inbox, owner retry-finalization, dispatch settings, writer
registration/update/withdrawal, writer attempt/progress and writer capacity.
Writer result/stop-confirmed, owner reply, stored-report reads, steering/rerun,
Captain registration/heartbeat/reinforcement/current routing and session-archive
reads/writes are also implemented. Native GitHub observation, local-check,
reconciler heartbeat/retirement and delivered-deployment retirement operations
preserve observation deduplication and delivery/stop/rework precedence.
Writer and scoped Captain usage observations preserve cumulative attribution,
acquisition gaps, bounded late observations and decimal provider quantities
without touching lifecycle or inferring billing. Native usage projections are
integrated in the scoped operator-timeline differential proof.
Native scout settings, claim/page/failure/decision and source storage preserve
the existing client-driven pass, lease/cursor/identity fences, fresh
fingerprint-bound approval and atomic ordinary intake/page receipts.
Stored and derived flag reads/writes and scoped current-writer reads are native,
including shared pickup/liveness/capacity context, observation/head/deployment
precedence, bounded warning families and exact elapsed-age boundaries.
Native operator execution/item/group/timeline/overview and muster reads include
filter-bound cursors, weak conditional validators, retained opaque pickup
metadata, bounded candidate windows and usage projections. Native Scribe shell/
assets use standard `http.ServeContent` GET/HEAD, range and conditional responses,
with root-confined regular files, symlink refusal and security headers.
Direct local-board transport retains narrowly wired owner actions. Existing documentation URLs serve
byte-pinned identical P0/D0 generated schema/HTML, not a Python runtime proxy.
Native report finalization and bounded dependency refresh use the existing
schema26 jobs. Every pinned public operation is inventoried; complete whole-port
fault/race/framework/rollback and final delivery proof still remain required.

Captain routing, pickup, flags and unconfirmed-intent recovery share the same
named heartbeat/handoff cutoffs and live-row scan. Pickup and writer admission
share the capacity/rework decision; claims reuse their settings snapshot when
projecting a blocked binding. Captain/reinforcement/session-link and archive
views are typed without changing their JSON contract. Archive status uses at
most seven SQL queries regardless of writer or matching Captain count, retaining
replacement-writer, pending-cleanup and unfinished-handoff blockers.
Reconciler posts reuse their loaded execution through apply; typed observation
payloads retain the existing stored bytes and digests. Usage writes and projections
decode typed snapshots once, preserving acquisition gaps and exact counter totals;
usage and scout claims share Captain repository-scope parsing. Each scout claim
projects status once, after any claim mutation.

## Boundaries and pinned build

`internal/httpapi` owns transport/validation/authentication; `internal/application`
owns immutable receipts, orchestration and the injected `Clock`, also used by validation.
`internal/sqlite` owns typed metrics with exact sums and
SQL, store-lifetime read-only/read-write connection pools, PRAGMAs, admission,
and commit outcomes. Each transaction exclusively leases a connection. Pools retain
at most eight idle readers and one idle writer, without a second active-connection
admission limit; health remains un-gated. Driver connection initialization applies
FK/EXTRA and the existing busy timeout to every new physical connection. Failed or
interrupted transactions roll back before reuse; failed cleanup discards the connection.
Shutdown drains HTTP/background work, closes both pools, then releases the instance lock.
Application responses remain JSON values through view composition and finalization;
HTTP serializes them once. Stored JSON is decoded at the storage boundary, while
receipt request digests and persisted snapshots retain their canonical encoding.
Response projection preserves sorted keys, exact numeric tokens and the existing
wire escaping without serializing typed views just to recover their fields.
Dependency resolution loads ordered links, current executions and stored-report
existence in one SQL statement. Intake and rerun share execution creation and
initial dependency-wait refresh.
`internal/contract` is the presence/validation/canonical encoding
boundary. `internal/instancelock` holds the Python-compatible database-adjacent OS
lock for the entire server lifetime. The explicitly selected
`cmd/phalanx-server` and deterministic `package.py` produce a server-only package
for [operator-owned rehearsal/cutover/rollback](../docs/go-server-release.md).
The existing `deploy run` adapter defaults to Go and requires an absolute verified
package binary through `server.go_binary` or `--go-binary`; it never falls back
automatically. Explicit Python selection remains available temporarily. Go cannot
initialize a DB: new-install-only `python -m phalanx server initialize` uses retained
offline Python schema/admin tooling under the same lock without starting Python HTTP.

The [independent parity audit and fix map](../docs/python-server-deprecation.md#reviewed-parity-evidence)
retain exact audited/fix heads and all four original findings. Inventory/family labels
alone never established full parity: real-entrypoint `identity.py` now covers proxy/
home identity, retirement tests require actual disabled effects, and `headers.py`
preserves incomplete-header refusal, complete/coalesced positive controls and framing.
Prior failed wire/drain/resource evidence and P0/D0 packages remain historical evidence,
not deleted or relabeled PASS by this default change.

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

Go database tests share `internal/testdb`: `New` creates an empty, disposable
database from its embedded Go-owned `schema.sql` in one committed DDL transaction,
avoiding a durable commit per table/index/trigger, and `Open` manages connections
used to inspect or reopen that fixture. Package-specific helpers only seed their
domain data. The helper's schema test pins the compact JSON DDL SHA-256
`c8dd5872e18dad37d74f800028922a439814365bcc2fee6d4f3f8fa5c5f4e288`
from the current golden schema, including implicit indexes, trigger text and NULL
SQL values, plus application ID, schema version and journal mode. Go tests do not
read the conformance corpus or require Python. This helper is test-only; the
production server still refuses to initialize a database.

CI builds/tests on Windows, Linux and macOS and race-checks Linux/macOS with their
prepared C compilers. The supplied local Windows environment has CGO disabled
and no C compiler: a local `go test -race` is an explicit gap, not a passing check.
Native tests run without Python, source-hash manifests or regenerated goldens.
CI preserves their exact test IDs and durations as `go-tests-<os>` artifacts.
The separate required runtime-safety jobs retain production identity, OS exclusion,
Windows Task Scheduler supervision, reproducible packaging/current-data rollback and
production header/framing proofs. Moving these out of the fast native job does not
remove them from the required `Tests` aggregate.
`conformance/locks.py` separately exercises actual Python-Go exclusion in both
directions on Windows/Linux/macOS. Its OS-only runtime is recorded, not falsely
called the exact Windows wire oracle. Host eligibility still requires the
operator's actual private rehearsal and service ownership.

Windows CI provisions a job-local, non-free-threaded Python-build-standalone
`20261110` CPython 3.14.10 archive, SHA-256
`32b6cf90a03e2bc15fda8d2ab7ff78c4870f138887d612932650ce764efaab8f`,
and verifies SQLite 3.53.1 before installing the locked oracle dependencies.
`setup-python`'s Python version alone did not establish the SQLite pin; the
original runtime/corpus checks remain unchanged. Runtime evidence is saved even
on a pin failure. No global interpreter, other checkout, or deployed runtime is
modified to repair CI.

The production launcher retains Uvicorn's `FORWARDED_ALLOW_IPS` boundary:
unset trusts only `127.0.0.1`, an explicitly empty value trusts no proxy, and
configured IPs/networks (or an explicitly configured `*`) select trusted peers.
Only a trusted socket peer's last valid `X-Forwarded-Proto` sets the effective
scheme for redirects and browser-origin checks. Forwarded Host/client claims
cannot grant authority; every forwarding header still disqualifies anonymous
local-board access. `PHALANX_HOME` is exposed only after user expansion yields
an absolute path, resolved through existing filesystem links with a non-strict
missing suffix, matching the unchanged Python muster identity comparison.

Run `conformance/identity.py --binary <production-server> --output <outside-checkout>`
with the prepared Python checks prefix. This starts the production Go binary in isolated current-schema
stores, not the in-process oracle adapter. It retains wire/header evidence and
complete typed before/after snapshots for trusted/untrusted schemes, redirects,
owner/runner/writer/browser/local-board refusals and home identity, including
dot segments, missing tails, Unix links/Windows junctions and the unchanged
`muster status` client. All three OS CI jobs run this additional proof; the
original immutable P0/D0 corpora and their approved exceptions are unchanged.

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
`vectors-manifest.json` retains the historical 114 exact request IDs, Go test IDs,
source hashes, module graph and both golden corpora. That was 684 ordinary wire/durable
observations across three implementations/two shapes, plus separately recorded
busy, switch, abort, lost-response/crash and lock fault windows.
Scoped Git attributes keep server sources/manifests LF on every host; oracle
exports disable caller `autocrlf` and verify actual extracted source bytes
against the unchanged Git-blob manifest.

The historical differential corpora are retired from ordinary PR/push CI. They can
still be run explicitly with CI's `historical_conformance` workflow-dispatch input or
the commands below. Their source/test inventory is historical evidence, not a
current-source equality gate. `verify_vectors` still refuses changes to either
foundation golden; immutable P0/D0 exports and oracle/runtime pins remain checked.
Every foundation harness run records the actual working-file hashes and test IDs in
`source-provenance.json`, including dirty source bytes. Rebuild the fixture binary
after editing; never use an old binary as evidence for edited sources. A `.go` edit
requires no manifest or golden regeneration.

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

The original foundation package-switch rehearsal covers only its slice.
`rehearsal.py` additionally exercises eight package-only phases against each of
P0 and D0 on fresh/retained evolved current data, without restoring an older
database: lost-response committed draft replay, the same writer/Building through
questions/replies/review/storage, immutable receipts, finalization/ended-token
refusal, exact UTF-8 report bytes, scout page/source replay and fresh approved
intake, acquisition gaps and late usage. It checks full typed snapshots and
unchanged schema/application identity/journaling. These focused continuations
are not full-route rollback approval, filesystem power-loss proof, production
adoption, or a performance/SLO claim.

`framework.py` retains raw HTTP requests/responses and complete zero-effect
snapshots for twelve parser/framing cases. Invalid chunk-body reads now match
the pinned 400 refusal instead of returning 500. Eight standard-library parser
differences are explicitly owner-approved named changes: duplicate Host, missing Host,
invalid HTTP version, conflicting Content-Length, invalid header name and
invalid method have different 400 text bodies; invalid percent URL is pinned
404 versus Go 400; unsupported transfer encoding is pinned 400 versus Go 501.
The original valid HTTP/1.0-without-Host characterization returned pinned HTTP/1.1 with
`Connection: close` versus Go HTTP/1.0 without that header; both return the exact
200 JSON payload and close. Protocol and Connection differences are retained,
not silently excluded from comparison. The owner accepted these two separate
choices in execution22f0004de8634d7b9aa8523aa3c62d77 replyv12; the
[tracked exact limits](../docs/http-server-migration.md#411-separately-approved-named-transport-changes)
and request-SHA-bound expectations preserve strict rejection, actual close and
zero effects on both schema shapes. Every other comparison remains strict.
No body/status rewriting, broad framework exemption or silent normalization
is authorized. Independent final-head review and all remaining full-proof gates
still apply.

Independent review found valid HTTP/1.0 requests beyond the one approved request:
repositories with an owner token, health with Host, health requesting keep-alive,
and repositories without authorization. These are strict comparisons in
`framework.py`, not extensions of v12. The native handler now restores pinned
HTTP/1.1 replies and actual `Connection: close` for already-parsed HTTP/1.0
requests, including health and keep-alive. It adds no parser/listener adapter or
broader input acceptance. The original failing raw evidence and separately
permitted health deviation are retained; the current implementation no longer
uses that permitted deviation.
Authoritative state decoding now refuses explicit-null publication mode before
mutation; the named operator projections still return null without effects,
and omitted mode retains its ready default.
Operator summaries decode each stored execution state once, reusing that typed
state for pickup decisions. Typed summary, PR, writer and output rows replace
per-row maps; stored deployment JSON is decoded only at the response boundary.

### Deployment retirement and incomplete-header regression

The original unchanged Python writer regression
`test_deployment_observations_refresh_flags_without_reopening_delivery` found
a Go 500 when a heartbeat disabled a real latest deployment with
`{"deployment_checks":{"repo":null}}`. Validation already accepted that null.
Retirement removed the generated stored PR URL but revalidated the decoded
object's stale original key list. The edited PR now uses its actual map keys;
request validation still rejects a caller-supplied PR URL. The strengthened
`reconciliation.py` uses a strictly earlier deployment timestamp, checks that
the latest signal actually becomes disabled, retains complete before/after
typed stores, and asserts every unrelated table stays unchanged. This avoids
the former equal-timestamp fixture's vacuous success. Absent and null bodies,
absent configuration, null per-repository names and refused malformed
configuration retain the actual Python contract.

The strict production regression in `headers.py`
exercises P0, D0 and Go through their real production launchers on both schema
shapes. Run from the repository root with the approved Python checks prefix:

```text
server\conformance\headers.py --binary <outside-checkout>\phalanx-server.exe --output <outside-checkout>\headers
```

`--record-only` preserves exploratory observations without claiming parity.
The strict command retains exact request segments, raw
replies, early-refusal timing, actual close, valid neighboring connections and
every typed table/DDL before and after each request, including an authenticated
mutation Python refuses without effects. The pinned Windows production parser
is Uvicorn's h11 implementation: an incomplete receive event larger than
16,384 bytes is refused with HTTP/1.1 400, plain-text
`Invalid HTTP request received.` and `Connection: close` before the terminator.
An event of exactly 16,384 bytes may wait; a complete oversized head in the same
receive event can be accepted. Splitting at or below the boundary then
coalescing the rest with the terminator differs from crossing it while still
incomplete. Body and pipeline cases retain their measured receive-event
segmentation; one client `sendall` is not proof of one server receive event.

Go's global `MaxHeaderBytes` is not an equivalent fix. Neither a blanket size
cap, a request deadline, a broader malformed-input exception nor a replacement
parser is approved. The owner authorized a narrowly scoped passive observer
experiment in execution `db1cd4356eaa4e6992ed10fd25c8f9ef`, reply v4. The
observer uses the pinned asyncio receive-event sizes (Windows 64 KiB, POSIX
256 KiB), checks only incomplete heads and follows fixed/chunked-body framing
to defer pipelined heads until native idle. `net/http` remains the request/body
parser and receives unchanged bytes. Native malformed framing remains its
authority. Buffered read-ahead does not count as new idle input, and native
pending-read aborts are transient, not cached connection failures. The native
500-field default is disabled because Python accepts complete multi-field
events above that count; no new global byte/count limit or timeout is added.
The current-head first byte also follows h11's early control/space refusal;
otherwise native Go's POST CR/LF tolerance can silently bypass observation.
This check applies only at head boundaries, never to fixed/chunked body bytes.

The corpus includes 29 header boundary/segmentation/body/pipeline cases, 26
initial/post-body/coalesced CR/LF/space/tab boundary cases, the
18 unchanged framework requests with their exact v12 classifications, full
typed-store refusal checks, and a valid same-key intake receipt after a refused
request. Raw response bytes remain recorded; only the existing generated
Date/Server/length/chunk-framing allowances are used for semantic comparison.
Original first-byte idle, buffered-prefix, body, disconnect, lock and drain
checks remain required alongside exact-head independent review and fresh CI.
An authorized experiment or source merge is not deployment readiness.

Repeated real Windows entrypoint requests with coalesced body/next-head input
exposed TCP resets that discarded the 400 or the entire response. A direct
Windows `SHUT_RDWR`-before-close experiment did not eliminate that loss and was
removed. Those failing raw responses, reset errors, typed stores and matched
repetition manifests remain evidence; a passing single matrix or package run
does not supersede them.

The owner separately authorized receive/closure investigation in execution
`db1cd4356eaa4e6992ed10fd25c8f9ef`, reply v11. A write FIN followed by a drain
until peer EOF passed wire tests, but independent review rejected its indefinite
per-peer socket/goroutine/buffer lifetime. That experiment and its evidence
remain historical, not adoption approval.

The current strategy sends the refusal and a write-side FIN, snapshots the
finite bytes already queued by the native socket, reads only that snapshot,
then fully closes. It never waits for future data, peer EOF or service
cancellation. Windows uses Winsock `FIONREAD`; Linux and macOS use their native
queued-byte ioctl. There is no new timer, request cap, request parsing or
dispatch of drained bytes. Temporary raw close work remains inside the native
connection goroutine/tracking lifetime; service cancellation can interrupt it.
Refused request buffers are released.

The native TCP/HTTP tests verify prompt raw-descriptor and connection-tracking
disposal before peer EOF, including batches of EOF-open peers and concurrent
closes with quiet or sending peers. The production corpus verifies no retained
database transaction/admission/accepted-operation ownership, ordinary
authenticated work and its receipt, zero receipts from authenticated-looking
refused noise, and clean shutdown/lock release without first closing peers.
Independent final-head review must assess both resource disposal and wire
preservation; investigation permission is not an adoption waiver.

`headers.py` also repeats the original fixed/chunked-body response-preservation
cases 100 times each per implementation and schema shape. Every response,
close/reset error and full typed store remains recorded. The prior unexplained
post-load drain failure is independently UNKNOWN; successful later runs do not
relabel its cause. Exact-final-head independent approval, original strict
stream/body/disconnect/drain/receipt checks and fresh CI remain required.

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
| `reconciliation.py` | 84 on each schema shape | Exact observation digests/timestamps, immutable local checks, heartbeat/retirement, delivery handoff/Building fencing, ready-output enrichment, stop/merge precedence and nonvacuous deployment retirement with null/absent/malformed configuration boundaries |
| `datetimes.py` | 1,449 | Exact pinned aware-datetime coercion, serialization and error envelopes, including numeric precision, calendar/offset boundaries and deterministic randomized timestamps |
| `usage.py` | 71 on each schema shape | Exact JSON storage, cumulative replay/monotonicity, attribution/scope, acquisition-gap retention, provided decimal quantities and late observations; every other durable table is unchanged |
| `scout.py` | 98 on each schema shape | Settings/models, single pass/page claim, failure/retry/expiry/successor fences, immutable page receipts, verified login/ID, approval/edit/decline handling, closed manual source identity and bounded multiple-repository cursor |
| `flags.py` | 72 on each schema shape | Stored flag models/update/clear, liveness/launch/progress/session/archive warnings, 30/60/strict-90 boundaries, head/handoff/CI/deployment precedence, scoped current writer and zero read/refusal effects |
| `operator_views.py` | 79 on each schema shape | Filters, exact cursors/ETags, summaries/groups/overview, timeline causes/questions and usage projection; only the named v4 current-item default discrepancy is classified |
| `operator_defaults.py` | 33 on each schema shape | Explicit v4/v9 named read omissions, ready/draft/null/no-writer/ended-writer preservation, malformed-state/mutation refusal and exact complete D0 responses with zero effects |
| `scribe.py` | 90 on each schema shape and authority mode | Canonical unchanged assets/CSP, file/range/conditional/HEAD behavior, documentation URLs and direct local-board/owner/proxy/host/origin boundaries |
| `route_models.py` | All 66 operations / 51 schemas on each shape | Complete pinned generated request-schema comparison and actual read/refusal wire/store inventory; not a substitute for each family’s positive/fault corpus |
| `framework.py` | 18 raw HTTP cases on each shape | Eight exact owner-v12 malformed refusals; strict restored HTTP/1.0 protocol/header/close/body/HEAD behavior, full raw fields/zero effects and only existing JSON representation comparison |
| `rehearsal.py` | Eight phases per reference/shape | Package-only P0/Go and D0/Go alternation on evolved data; same-writer/grants, caller-loss receipt reconciliation, report/scout/usage continuation and immutable receipts |
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
Observation tests inject a transition-history failure and verify the observation,
state/version and writer ownership all roll back, then race identical posts and
require one observation, one delivery transition and one ended attempt.
Datetime tests include real-loopback observation validation; the standalone
model corpus supplements those routes rather than claiming whole-family parity.
Usage storage separately retains Python's declared JSON field order and default
separators; it does not change canonical state, fingerprints or receipts.
Native usage tests race duplicate observations and check preserved zero/null
measurements, coverage gaps, exclusion of shared sessions and integer subtotal
overflow as text. Operator timeline integration has scoped differential proof;
this does not establish complete Scribe or whole-port conformance.
Scout unit tests inject a cursor-update failure after intake/source creation and
require all work/source/receipts/claim state to roll back. Racing identical page
posts retain one intake and two immutable receipts; racing Captains acquire
one claim, and handoff/settings supersession fence prior pages. Ordinary
`datetime` accepts naive issue timestamps while the shared `AwareDatetime`
contracts still require timezone information; both pinned model corpora pass.
The common transaction-scoped intake helper retains the exact two permitted
legacy/current fingerprints without nested transactions or receipt rewrites.
Flag tests retain per-family 200-result limits, scoped writer isolation and
non-null empty dependency wait markers as blockers. Shared request-local context
does not turn flags, stale clocks or derived warning ages into lifecycle gates.
Operator tests require an empty filtered page to continue beyond exactly 2,000
indexed candidates, and weak validators ignore ticking ages but change at due
deadlines and when retained opaque pickup metadata changes.

`operator_defaults.py` retains fresh/retained raw wire and full-store evidence.
The execution's v4 reply classifies only current-item omission; its separate v9
reply explicitly adds list/group/timeline omission. These four named read cases
expect P0 500 versus D0/Go 200 with the documented ready default and zero durable
effects. The 24-case corpus matches complete D0 responses, preserving explicit
ready/draft/null values and malformed-state refusal. No other 500 normalization,
public-field restoration or durable state/receipt/hash rewrite is authorized.
Qualified independent exact-final-head review must cover each classification.

Static proof exports canonical immutable Git blob bytes. Windows checkout CRLF
conversion is retained as separate source evidence, not normalized out of HTTP
responses; fixture copies serve the exact canonical bytes with a shared synthetic
mtime. No live/package-owned asset is rewritten. Production packaging/launch
adoption remains separately authorized and must preserve the pinned asset bytes.

Focused native tests additionally cover racing title revisions, report
savepoint rollback, exact 2/4/8/16-second retry scheduling and fifth-failure
stall, owner retry, exact UTF-8 report storage, and resuming existing durable
finalization intents after reopening the store. Background work drains an
already dispatched bounded batch before the disposable adapter releases its
instance lock; it does not introduce a production shutdown deadline.

The full family corpus, transactional faults/races and eight-phase package
continuation remain mandatory alongside inventory and pure decision tests.
`conformance/release.py` adds the real packaged binary, unchanged clients,
online Python admin backup, same-data new-write rollback and measured matched
synthetic load. Exact-final-head independent review and fresh required CI remain
merge gates; operator-private current-data rehearsal is a distinct adoption gate.
Only the server launch adapter is extended; no live configuration/schema/data/
credentials, client protocols, reconciler or canonical Scribe asset is changed.
