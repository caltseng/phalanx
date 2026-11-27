# Python HTTP server deprecation

The versioned packaged Go HTTP server is the supported repository default.
Python HTTP is deprecated, **not removed**: explicit `--engine python` or saved
`server.engine: "python"` is retained temporarily for rollback/reference work.
Python CLI, Captain/writer clients, reconciler, domain models, offline admin and Scribe
remain supported. This stage changes source/tooling defaults, not any live installation,
scheduled task, private configuration, credential, saved capacity or current database.
Actual live Go adoption/revision requires the service owner's separate receipts.

`deploy run` resolves CLI engine, saved engine, then Go. An absolute verified packaged
binary is required via `--go-binary` or `server.go_binary`; missing/failed Go never
silently starts Python. Before upgrading an implicit Python task, its owner must pin
Python explicitly or complete the [Go rehearsal](go-server-release.md). Explicit existing
engines remain effective and no command rewrites the config. New-store setup uses
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
| G1: trusted HTTPS proxy scheme caused bearer same-origin 403 and HTTP redirects | [PR #312](https://github.com/caltseng/phalanx/pull/312), reviewed head `bc7a5584b93189d44ef682fae0c5ca66326c2d5d`, merge `c1d3fffdba07e9790f6d9d6d012d53d03b35dcc9`; `server/conformance/identity.py` compares actual current Python/Go production entrypoints, trusted/untrusted schemes, redirects and unchanged tokenless-board refusal. |
| G2: noncanonical/relative muster home broke unchanged discovery | Same PR/head; `identity.py` covers expansion, canonical paths, dot segments, missing tails, links/junctions, relative/unset identity and unchanged `muster status`. |
| G3: null/changed deployment check heartbeat failed 500 instead of retiring the observation | [PR #317](https://github.com/caltseng/phalanx/pull/317), reviewed head `d60a9ca9f6bcb85a001db36064cbdfc5de4ad942`, merge `8073d1a5c2aa6e126d666decb788e2aaf3a48fae`; `conformance/reconciliation.py` now requires a strictly later observation and actual disabled effect; the unchanged original `WriterTest.test_deployment_observations_refresh_flags_without_reopening_delivery` must also run on production Go rather than relying on a corpus name. Record removal/renamed-check and old-completed-state coverage explicitly, not from labels alone. |
| G4: fragmented incomplete oversized headers were accepted and could mutate Go state | Same PR/head; `conformance/headers.py` records exact requests/responses, early refusal/close and typed unchanged state, complete/coalesced positive controls, body/pipeline/idle/disconnect/shutdown behavior and finite refusal-resource cleanup. Prior failed wire/drain/resource evidence remains preserved. |

Both merged fixes are ancestors of this stage's starting revision
`8073d1a5c2aa6e126d666decb788e2aaf3a48fae`. This is integration evidence, not a
blanket certification of arbitrary inputs, all interleavings, private deployment data,
power-loss behavior, browser layout or unmeasured resource/SLO targets. Final delivery
still requires fresh relevant regressions, actual packaged Go E2E/current-data rollback/
sole-owner proof, independent exact-current-head approval and required CI. No unresolved
required finding may be waived to declare deprecation ready or remove the fallback.
The exact named owner-approved malformed/default exceptions remain exact; no refreeze
or broad error normalization is authorized.

## Retained code and follow-on removal inventory

The follow-on removal must trace consumers before deleting anything. This stage removes
no Python module, dependency, historical package or Scribe asset.

| Entrypoint / dependency / call site | Removal boundary and retained consumer |
| --- | --- |
| `python -m uvicorn phalanx.api:app`; `phalanx/api.py` | Deprecated raw Python HTTP entrypoint/reference lifespan and routers. Not the supported new-install command and does not acquire the deployment lock. Historical tests/oracles still invoke it. |
| `phalanx/server_admin.py:serve`; `deployment.run_service` Python branch; CLI `deploy run ... --engine python` and saved engine | Temporary single-owner rollback entrypoint. Retain until separately reviewed removal and compatible recovery replacement. |
| `auth.py`, `intake_api.py`, `dispatch_api.py`, `operator_api.py`, `operator_web.py`, `muster_api.py` | Python HTTP/auth/router projections; production authority now lives in Go. Test fixtures and temporary rollback still require them. |
| `captains.py`, `writers.py`, `flags.py`, `usage.py`, `issue_scout.py` | Mix Python routers with domain helpers. Do not delete by filename: other Python reference modules/tests and shared domain projections import these helpers. Trace/split only in the follow-on scope. |
| FastAPI/Uvicorn `server` extra in `pyproject.toml`; Starlette/AnyIO/h11 and related pins in `requirements.lock` | Retained Python HTTP/oracle dependencies. Do not remove the complete lockfile, refreeze historical runtimes or assume every transitive pin is server-only. |
| `tests/test_bootstrap.py`, server HTTP tests, original assertions, `server/conformance/oracle.py`, exported P0/D0 sources | Historical compatibility/reference fixtures, including explicit Python selection in `release.production`. Preserve original bytes, exact exclusions, goldens and labeled failures; inventory-only coverage was insufficient. |
| `api_client.py`, `work_cli.py`, `shift.py`, `reconciler.py`, `issue_scout_client.py`, `issue_intake.py`, `muster.py`, Git/provider/workspace helpers and prompts | Supported Python clients, reconcilers and runtime integration, **not** HTTP-server removal targets. |
| Pydantic and `*_models.py`, `transitions.py`, `json_utils.py`, shared enums/domain behavior | Still used by clients and domain/validation code. Examples: reconciler/workspaces import `transitions.item_branch`; intake models import lifecycle enums. Retain even where also used by Python HTTP. |
| `server initialize/backup/restore`, `server_admin.py`, `store.py`, `schema.py`, `scripts/convert_store.py` | Supported offline server-host admin/schema/backup/conversion, not client-side queue access. Keep storage/schema dependencies until separately replaced; no live schema conversion is authorized here. |
| `phalanx/static`, package-data declarations, Go package `assets`, `httpapi/contract-docs` | Unchanged Scribe and intentionally frozen Python-generated OpenAPI/docs/redoc bytes served natively by Go, not runtime Python requests. Preserve assets and route/model accuracy. |

## Rollback, testing and removal prerequisites

Use the [versioned package/rehearsal commands](go-server-release.md), prepared checks
environment and isolated fresh/retained stores. Run default/saved/explicit-engine and
new-store regressions, production identity, original writer retirement, complete header
corpus, real packaged E2E, unchanged writer/client/reconciler tests and both-direction
OS exclusion. Record exact source/build/package/test identities outside tracked work.
No live config/task/database or credential may be borrowed for writer tests.

For rollback, stop/drain only the exact Go process tree through the existing sole owner,
then select the retained reviewed Python package with `--engine python` on the **same
evolved current database**. Verify new Go-accepted writes, original-key replay,
auth/bindings/reports/jobs and existing reconciler continuity. Retain previous package,
configuration, fresh validated backup and earlier evidence; never restore an old snapshot
over newly accepted work. See the [complete temporary rollback procedure](go-server-release.md#temporary-python-http-rollback).
No new supervisor/service/watchdog or automatic fallback is introduced.

Removing the Python HTTP fallback is a later separately reviewed stage. It requires all
required parity findings resolved, final exact-head review/CI, current-data operational
rollback evidence and a consumer-by-consumer inventory preserving supported Python/client/
admin/shared-model/assets functions. This stage does not claim that later removal is ready.
