# Go server release and operator rehearsal

**Source readiness is not deployment.** Go is opt-in for the existing API
supervisor, never the default engine or a second service. The author uses only
sanitized synthetic fixtures. The existing operator alone owns actual private
D0 inventory, fresh-current-data rehearsal, backup, cutover and rollback.
Apply [deployment authority and safeguards](deployment.md#standing-authority-for-compatible-upgrades).
Unknown compatibility, independent review, required CI or ownership blocks cutover.

## Versioned server-only package

Use the repository's prepared Go 1.27.4 checks environment; do not install or
replace a compiler to repair a pin. From a clean reviewed checkout, record
`git rev-parse HEAD` as the full revision. From `server`, append to that prefix:

```text
go mod verify
go build -trimpath -buildvcs=false -ldflags "-X main.revision=<full-revision>" -o <outside-checkout>\phalanx-server.exe ./cmd/phalanx-server
```

Repeat the build to another outside-checkout file and compare SHA-256. Then,
from the repository root, append to the Python checks prefix:

```text
server\package.py --binary <outside-checkout>\phalanx-server.exe --output <new-outside-checkout-directory>
```

POSIX packages omit `.exe`; use native path separators. `package.py` requires
an immutable local revision, supported build identity and unchanged server/adapter
sources. It copies canonical Git blob bytes, not CRLF-converted checkout assets.
The deterministic ZIP contains the binary, unchanged Scribe assets, this guide,
Go module graph, Python lockfile and versioned SHA-256 manifest. It contains no
database, configuration, credentials, discovery locator or client replacement.
Verify the package digest and every extracted file against `manifest.json`;
record binary `--version` output separately from the source merge.
The same compiler/module graph/flags/target produces the reproducibility claim;
different hosts/toolchains are not silently declared identical.

The launcher reads the existing `PHALANX_DB`, owner/runner credentials,
`PHALANX_HOME` and `PHALANX_LOCAL_BOARD`. It requires a regular existing database,
schema26/application identity and DELETE journaling; there is no initialization,
schema conversion, restore, credential generation or capacity override. The
same database-adjacent OS lock fences Python and Go for the entire HTTP and
background-work lifetime. Dispatched operations are not cancelled by caller
disconnect. Graceful shutdown stops HTTP admission, drains dispatched requests
and existing durable finalization/dependency batches, then releases that lock.
There is no newly approved operation/drain deadline. Forced termination requires
current-data receipt/job reconciliation, not a success-shaped response.
The existing five-second idle keep-alive timeout is retained without imposing
a new five-second request-header deadline; the real-entrypoint proof delays an
authenticated request header beyond five seconds on P0, D0 and Go.
It also repeatedly fragments keep-alive headers after one/two/three/four-byte
prefixes before idle expiry, distinguishes previously coalesced prefixes,
separately checks genuine idle closure, delayed bodies, incomplete/complete
caller disconnects and active-write drain under the same shared OS lock.
The narrow connection-deadline shim observes native connection states and new
network input only: it does not parse, buffer, rewrite or broaden requests.
This preserves Python's first-byte idle cancellation despite Go's four-byte
idle peek, including retained expiry for already buffered partial prefixes.
Windows corrects only the service's own inherited Idle/BelowNormal priority to
Normal, preserving higher priorities.

## Compatibility evidence and limits

P0 is `9af29396b37f86816fd274e01632164a47718a57`; public installed-source
D0 is `ce0a32cd7c83c77d50d19b7a4601cd48cdacf8ae`; the retained initializer is
`7b86cc994916dc8c393e6217303374b1a5c2c9d0`. These public pins do not replace the
operator's private inventory of actual package/overlay/current data.

[`server/README.md`](../server/README.md) enumerates the whole route/model/domain
corpora and counts. `manifest.json` fixes original Python bytes/runtime;
`vectors-manifest.json` fixes exact requests, test IDs, Go sources and goldens.
Retain raw wire and every typed table/DDL snapshot, not just health or final counts.
Only JSON representation and reviewed generated transport fields are normalized.
Missing/null/default, numeric types, arrays, error details, required headers,
durable bytes, hashes, receipts and versions are not dropped. The exact-request
owner-approved malformed refusals and named read-default decisions remain the
only behavioral exceptions; do not refreeze a baseline to conceal a discrepancy.

| Specification cases | Executable evidence, in addition to the full Python suite |
| --- | --- |
| C01, C03, C05, C06 | `writers.py`, `captains.py`, native writer/dispatch/claim races; existing native-launch recovery client tests |
| C02, C04, C09, C10 | `results.py`, `cancellation.py`, `rehearsal.py`; finalization savepoint/failure/backoff/pending-intent/restart tests |
| C07, C11 | `reconciliation.py`, `decisions.py`; handoff/merge/draft/base precedence and duplicate observation rollback/races |
| C08 | `steering.py`, native dependency tests and bounded 37-dependent restart/audit cursor test |
| C12, C13 | Foundation, family receipts, `numbers.py`, `history.py`, `rehearsal.py`; both exact legacy fingerprints and changed-request rejection |
| C14, C15, C16 | Foundation busy/fault/current-data probes and admission/transaction/HTTP barrier tests; BUSY versus unknown COMMIT, caller loss versus explicit interruption |
| C17, C18 | `captains.py`, `archives.py`, `flags.py`, native reinforcement/clock boundaries and existing client recovery tests |
| C19, C20 | `scout.py`, native claim/page race and late cursor-write rollback tests |
| C21 | `usage.py`, scoped timeline proof, cumulative/late/shared-session tests |
| C22, C24 | `operator_views.py`, `operator_defaults.py`, `route_models.py`, `scribe.py`, `framework.py`, `inbox.py`, `datetimes.py` |
| C23 | Foundation and eight-phase `rehearsal.py`; cross-platform `locks.py`; real packaged `release.py` current-data replay/backup/lock proof |

The family inventory covers all 66 operations and 51 models; positive/fault
family tests remain required in addition to inventory. Unit fault seams are
synthetic transactions, not filesystem power-loss guarantees. Source test review
is not independent exact-final-head approval. All cross-platform Python/Go jobs,
Unix Go races, OS exclusion, Windows pinned differential and packaged release
proof feed the required `Tests` CI gate.

`release.py --package <zip> --output <outside-checkout-directory>` verifies
extracted bytes and starts the actual versioned binary against fresh and retained
synthetic stores. P0/D0 unchanged clients create/retry/read work; both previous
packages replay NEW Go-accepted writes on the same evolved file and accept new
writes after rollback. Online backup invokes unchanged P0/D0 server-host admin
from the explicitly exported public source, never an incidental installed package.
Python-held and Go-held locks reject the other engine; missing DB refuses startup.
No old snapshot is restored. Preserve the actual output artifacts and their
source/build/package identity; never label a fixture adapter run a real package run.

Runtime/load evidence uses the exact 120-request manifest (30 successful intake
writes, 90 authenticated item/settings reads), concurrency8, 12 warmup reads,
three measured runs per P0/D0/Go on each schema shape, using each real production
entrypoint's ordinary UUIDs and wall clock (no fixture identity/clock injection).
It records every latency
and status, p50/p95/p99/max, throughput, host and package digest, and full typed
snapshots verifying exactly 30 new items/receipts per run with unchanged schema.
It is representative
synthetic control traffic, **not** the proposed 10,000-item/32-client workload,
30-minute resource soak, real production load or an approved SLO. Shared-host
uncontrolled interference remains visible. Proposed timing/resource/percentage
goals are neither approved nor claimed met. No performance miss waives durability,
authority, fencing or rollback. Add actual numeric results to the release evidence
before readiness; different manifests or host conditions are unmatched.

## Operator-only checklist

1. Pin source, binary/package digest, final exact-head independent non-author
   verdict and required CI. Privately record actual D0 package/overlay/dependencies,
   schema/DDL/integrity, assets, DB path/filesystem, URL/proxy, credentials' locations,
   saved dispatch settings, existing bindings/attempts/nonces/jobs/receipts,
   installed clients and exact supervisor/owned process tree. Do not publish them.
2. Confirm the existing service-owner authority and supervisor stop/start/drain
   policy. Inventory any raw Uvicorn launcher that lacks the lock; retire it
   through its owner before a candidate starts. A lock file alone cannot fence
   an uncooperative/raw launcher. Keep native sessions and reconciler running.
3. Take a fresh supported `server backup` to a new private destination; validate
   integrity and expected records. On a private disposable copy, rehearse the
   complete actual-D0 -> packaged-Go -> previous-Python sequence including newly
   accepted synthetic writes, current active bindings, jobs, reports and original
   keys. Use isolated ports/tokens with no provider/native-launch authority.
   Preserve typed values/DDL/receipts and both-direction lock results. Retain
   previous package/config and verify it operates on evolved current data.
4. Record runtime/load conditions and actual numbers, approved hard thresholds
   (if any), and remaining proposed/unverified metrics. Resolve all compatibility
   or safety discrepancies before cutover; do not waive them by source merge.
5. Only after those private gates, stop the exact existing API through its owning
   supervisor and prove its owned listener/tree is stopped. Do not select by
   process name/port, initialize a database, change capacities or start a new
   service. Retain the same config/environment and supervisor ownership.
6. Explicitly select the verified extracted package in the existing launch:

   ```text
   python -P -m phalanx deploy run --config <existing-absolute-deployment.json> server --engine go --go-binary <absolute-package>\phalanx-server.exe
   ```

   The adapter delegates a foreground child; the same supervisor must own/stop
   its exact process tree (POSIX group/cgroup or Windows owned tree/console).
   The Go child, not its Python parent, holds the shared DB lock. Do not add a
   second watchdog, supervisor, scheduler or service. `--initialize-database`
   is forbidden for Go. Python remains the default for every existing command.
7. Verify one intended API, authenticated owner/runner/current-writer continuity,
   preserved versions/receipts/jobs/limits, unchanged Scribe at the same URL and
   fresh existing reconciler heartbeat. Reconcile actual caller-loss/unknown
   outcomes by original keys/status. Record installed revision/readback separately;
   do not fabricate live work or revive ended credentials.
8. If a gate fails, stop only the candidate through the same supervisor. Restore
   the previous package/config/command and restart **on the current compatible
   DB**, then verify accepted Go writes, original-key replay, reports/jobs/bindings
   and reconciler continuity. Never overwrite current data with the older backup,
   drop schema, rehash/rekey, run two authoritative engines or hide uncertain
   writes. If rollback is unproven, leave the affected API safely stopped and
   escalate the exact recovery decision to its owner with preserved evidence.
9. Record actual private rehearsal/cutover/rollback receipts and notify the current
   Captain for existing bounded reconciliation/refill. Source merge, package build,
   synthetic PASS and a sent message are not those receipts.
