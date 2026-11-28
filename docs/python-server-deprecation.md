# Python HTTP server retirement

The versioned packaged Go HTTP server is the **only current HTTP implementation**.
Python HTTP routes, app startup and serving dependencies are removed. Explicit
`--engine python`, saved `server.engine: "python"`, the old raw app import and
`server_admin.serve` fail clearly with migration guidance, before any database/config
change. They never silently choose Go or restore Python.
Python CLI, Captain/writer clients, reconciler, domain models, offline admin and Scribe
remain supported. This stage changes source/tooling defaults, not any live installation,
scheduled task, private configuration, credential, saved capacity or current database.
Actual live Go adoption/revision requires the service owner's separate receipts.

`deploy run` resolves CLI engine, saved engine, then Go; a resolved Python selection
fails. An absolute verified packaged
binary is required via `--go-binary` or `server.go_binary`; missing/failed Go never
silently starts Python. Before upgrading a Python task, its owner must complete the
[Go rehearsal](go-server-release.md), or retain the separate known-good installation
for coordinated rollback. No command rewrites the config. New-store setup uses
`server initialize` offline under the service lock; Go never initializes/migrates data.

## Reviewed parity evidence

The complete independently stored report is item `023e817ff8724f679a5ef16ae7478c9a`,
execution `7b5f08381f8447dc97e90eb73c782502`, report SHA-256
`3ab34806f6c060419f22315d97e8185450f576d69fc59fc1c1aaa82775cacdde`
(61,809 UTF-8 bytes). Its exact reviewed checkout was
`b54370a24a6f785c5edefd37699b30456ac26c49`; latest server change
`c87dca30d7dd0596b93529823ae561c2fe6e5216`.
P0 is `9af29396b37f86816fd274e01632164a47718a57`, public historical D0
`ce0a32cd7c83c77d50d19b7a4601cd48cdacf8ae`, retained initializer
`7b86cc994916dc8c393e6217303374b1a5c2c9d0`.

**Historical verdict: complete parity was NOT established.** Four reproducible gaps
were required findings, not accepted differences. Green inventory/family names missed
production-launcher behavior and one positive retirement effect. The report's entire
history matrix, raw artifacts, original failing cases and documented limits remain
historical; subsequent fixes do not rewrite that verdict.

| Required finding | Integrated remediation and executable replacement coverage |
| --- | --- |
| G1: trusted HTTPS proxy scheme caused bearer same-origin 403 and HTTP redirects | Reviewed head `bc7a5584b93189d44ef682fae0c5ca66326c2d5d`, merge `c1d3fffdba07e9790f6d9d6d012d53d03b35dcc9`; `server/conformance/identity.py` compares actual current Python/Go production entrypoints, trusted/untrusted schemes, redirects and unchanged tokenless-board refusal. |
| G2: noncanonical/relative muster home broke unchanged discovery | Same reviewed head; `identity.py` covers expansion, canonical paths, dot segments, missing tails, links/junctions, relative/unset identity and unchanged `muster status`. |
| G3: null/changed deployment check heartbeat failed 500 instead of retiring the observation | Reviewed head `d60a9ca9f6bcb85a001db36064cbdfc5de4ad942`, merge `8073d1a5c2aa6e126d666decb788e2aaf3a48fae`; `conformance/reconciliation.py` now requires a strictly later observation and actual disabled effect; the unchanged original `WriterTest.test_deployment_observations_refresh_flags_without_reopening_delivery` must also run on production Go rather than relying on a corpus name. Record removal/renamed-check and old-completed-state coverage explicitly, not from labels alone. |
| G4: fragmented incomplete oversized headers were accepted and could mutate Go state | Same reviewed head; `conformance/headers.py` records exact requests/responses, early refusal/close and typed unchanged state, complete/coalesced positive controls, body/pipeline/idle/disconnect/shutdown behavior and finite refusal-resource cleanup. Prior failed wire/drain/resource evidence remains preserved. |

Both merged fixes are ancestors of this stage's starting revision
`8073d1a5c2aa6e126d666decb788e2aaf3a48fae`. This is integration evidence, not a
blanket certification of arbitrary inputs, all interleavings, private deployment data,
power-loss behavior, browser layout or unmeasured resource/SLO targets. Final delivery
still requires fresh relevant regressions, actual packaged Go E2E/current-data rollback/
sole-owner proof, independent exact-current-head approval and required CI. No unresolved
required finding may be waived to declare deprecation ready or remove the fallback.
The exact named owner-approved malformed/default exceptions remain exact; no refreeze
or broad error normalization is authorized.

## Removed and retained consumers

HTTP removal does not remove supported Python consumers or historical evidence.
No historical package, Git revision, source manifest, golden or Scribe asset is deleted.

| Entrypoint / dependency / call site | Removal boundary and retained consumer |
| --- | --- |
| `python -m uvicorn phalanx.api:app`; `phalanx/api.py` | No app, lifespan or routes remain. A migration-only tombstone explains the removed command. |
| `server_admin.py:serve`; Python branch of `deployment.run_service` | Serving branch removed; old selections fail. Offline initialize/backup/restore remain. Rollback uses a separately retained package/interpreter. |
| `auth.py`, `intake_api.py`, `dispatch_api.py`, `operator_api.py`, `operator_web.py`, `muster_api.py` | HTTP modules removed. Dispatch payload is retained in `dispatch_models.py`. Offline operator projections used by synthetic unit fixtures move to `tests/operator_fixture.py`, without HTTP request/response classes, handlers or routes. |
| `captains.py`, `writers.py`, `flags.py`, `usage.py`, `issue_scout.py` | HTTP imports, routers and handlers removed; non-HTTP models/domain helpers remain for synthetic seeding, reference projections and assertions. They do not run the API. |
| FastAPI/Uvicorn `server` extra and runtime-only transitives | Removed from package metadata/current runtime lock. Pydantic and its required transitives remain for supported clients/models. Exact historical oracle pins remain separately in `server/conformance/requirements.lock` and immutable P0/D0 exports; no refreeze. |
| `tests/test_bootstrap.py`, HTTP tests, envelope probe | Actual Go production process, isolated stores, unchanged CLI/network assertions, graceful stop/restart and ownership. No current Python server app/custom-route fixture. |
| `server/conformance/oracle.py`, exported P0/D0 sources | Historical immutable comparisons/rollback remain extra evidence, not a current Python runtime. Native contract docs, original golden vectors and raw wire/typed-state assertions remain. |
| `api_client.py`, `work_cli.py`, `shift.py`, `reconciler.py`, `issue_scout_client.py`, `issue_intake.py`, `muster.py`, Git/provider/workspace helpers and prompts | Supported Python clients, reconcilers and runtime integration, **not** HTTP-server removal targets. |
| Pydantic and `*_models.py`, `transitions.py`, `json_utils.py`, shared enums/domain behavior | Still used by clients and domain/validation code. Examples: reconciler/workspaces import `transitions.item_branch`; intake models import lifecycle enums. Retain even where also used by Python HTTP. |
| `server initialize/backup/restore`, `server_admin.py`, `store.py`, `schema.py`, `scripts/convert_store.py` | Supported offline server-host admin/schema/backup/conversion, not client-side queue access. Keep storage/schema dependencies until separately replaced; no live schema conversion is authorized here. |
| `phalanx/static`, package-data declarations, Go package `assets`, `httpapi/contract-docs` | Unchanged Scribe and intentionally frozen Python-generated OpenAPI/docs/redoc bytes served natively by Go, not runtime Python requests. Preserve assets and route/model accuracy. |

## Durable replacement vectors and rollback

The original historical matrix H01-H15 remains in the complete independently stored
report above, not rewritten as a current parity certificate. Current regression coverage
includes the real Go fixture for original intake/writer/dependency/scout/operator/usage/
CLI/reconciler tests, plus native transaction/admission/commit-failure/concurrency tests.
Python internal query/clock spies explicitly remain offline fixture tests, not purported
Go HTTP coverage. The complete peer/origin matrix is retained in native
`TestRetainedDashboardPeerAndOriginVectors`; the old worker-slot contention intent is
covered by `TestAdmissionRefusalsKeepWorkerIndependentResponsesAndRecover` on the actual
native gate, and real packaged HTTP uses both SQLite lock modes.

G1/G2 now have standalone retained production vectors in `conformance/identity.py`:
trusted/untrusted forwarding, exact redirect scheme, complete capacity/muster responses,
immutable receipts, local-board refusals, every home/link case and unchanged muster
client identity checks, with raw wire and typed state. These no longer require a current
Python HTTP source. Pre-removal Python/Go evidence remains historical.
G3 retains the original writer regression plus `test_go_contract.py` null/renamed/same-name
controls on explicitly older-than-24-hour completed executions, heartbeat effects,
actual disabled observations, immutable closed state and replay.
G4 retains every production header/framing/prefix/resource vector and native prompt-disposal
tests; immutable P0/D0 sources and exact allowed exceptions are unchanged.

Use the [versioned package/rehearsal commands](go-server-release.md), prepared checks
environment and isolated fresh/retained stores. Run default/saved/explicit-engine and
new-store regressions, production identity, original writer retirement, complete header
corpus, real packaged E2E, unchanged writer/client/reconciler tests and both-direction
OS exclusion. Record exact source/build/package/test identities outside tracked work.
No live config/task/database or credential may be borrowed for writer tests.

For rollback, stop/drain only the exact Go process tree through the existing sole owner,
then select the retained reviewed Python package **using its own interpreter/source**
with `--engine python` on the **same
evolved current database**. Verify new Go-accepted writes, original-key replay,
auth/bindings/reports/jobs and existing reconciler continuity. Retain previous package,
configuration, fresh validated backup and earlier evidence; never restore an old snapshot
over newly accepted work. See the [complete temporary rollback procedure](go-server-release.md#temporary-python-http-rollback).
No new supervisor/service/watchdog or automatic fallback is introduced.

Source removal still requires final exact-head independent review, required CI and real
packaged current-data/backup/rollback/sole-owner proof. Source delivery is not private
deployment. The existing authorized operator alone coordinates later live adoption with
fresh actual-current-data receipts, preserving config, credentials, settings and writers;
no new service, data reset, implicit cutover or history/artifact deletion is authorized.
