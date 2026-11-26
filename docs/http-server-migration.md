# HTTP server migration: compatibility and architecture specification

**Status: substantial draft for collaborative owner review, not authorization to implement or deploy.**

Prepared 2026-11-26 for Phalanx item `e9eec24973954316938ca68aef79a0f2`, execution `67032e84089b425d963a4b937cedef9c`. Scope: replace the Python HTTP server and authoritative server-side domain/storage implementation with a proposed Go implementation. Preserve the API, SQLite data, workflows, Python CLI/Captain clients/reconciler, and Scribe. No source implementation, implementation PR, service changes, schema conversion, production writes for comparison, or cutover is part of this report.

**Repository documentation revision:** checked in for item `0fc3d05ba87a44f2a9a9b295869aa052`, execution `75fda96228f64f8b88fbe620ae6efd0a`, under the owner's subsequent documentation-only check-in request. The original report remains immutable at SHA-256 `5de8687c62989e45170edb51e3cf637ffacd953e79a163855f456c22f14c81e6`. Historical report-only ceilings and review evidence below describe that original assignment, not a prohibition on this separately authorized documentation PR. This check-in does not authorize Go implementation, deployment, schema changes, or resolution of the open owner decisions. Section 12 distinguishes original report review from exact-head repository documentation review.

## 1. Executive architecture decision

Propose one Go HTTP service, one authoritative SQLite file, and three substantive internal boundaries: HTTP contract/authentication, application/domain operations, and SQLite storage. Retain static Scribe assets and all current client entrypoints. Move durable background report finalization and dependency refresh into the same service, preserving their existing jobs and restart semantics. Do not move GitHub observation, Git commands, native-session launches, issue fetching, usage acquisition, or merge actions into the server.

The benefit is simpler execution ownership and explicit transaction/admission behavior, not a change in product semantics. Go's goroutines do not remove SQLite serialization, establish durability, guarantee fairness, or cancel already committed work. The migration is accepted only by comparing both wire behavior and durable effects, including retry/restart/rollback, against pinned Python behavior and current owner contracts.

Keep DELETE journaling and `synchronous=EXTRA` initially. Select a SQLite driver only after an explicit build/runtime proof. No default microservices, PostgreSQL, ORM, generic workflow engine, message bus, durable client queue, new scheduler, or application framework. WAL, storage redesign, multi-workspace authorization, and API redesign remain separately reviewable proposals, not incidental porting improvements.

## 2. Evidence, source pins, and limits

### 2.1 Reproducible baseline

The inspected worktree was clean and pinned to:

* **P0**: `9af29396b37f86816fd274e01632164a47718a57`, remote `main` returned the same SHA during this investigation. This is the report's latest-main/source baseline, not proof of production adoption.
* **R0**: supplied checks interpreter CPython `3.14.10`, SQLite `3.53.1`, FastAPI `0.165.0`, Pydantic `2.14.4`, Starlette `1.10.0`, Uvicorn `0.54.0`, AnyIO `4.16.0`. `requirements.lock` SHA-256 `c8e37bac06cba562a19b9203809f76128a7a974b06628dd68876d769004ffb63`. The environment is observed, not assumed identical to the deployed package or lock installation.
* **K0**: original report's supplied canonical writer kickoff SHA-256 `239DD2FA0FDC397876BC9F98A0F8995D2A11B450B9F3DD7CE55AFA047F718F5C`, all 181 lines read by the report author. That assignment's target was explicitly `report`; ordinary PR/merge defaults could not expand that ceiling.

Permalinks below use P0:

| Reference | Evidence and responsibility |
| --- | --- |
| [S1: SPEC](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/SPEC.md) | Product/data contract, receipt exception, capacity, usage, scouting, source-of-truth boundaries |
| [S2: STATE_MACHINE](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/STATE_MACHINE.md) | Authoritative stage/status transitions, verified output, cancellation and recovery |
| [S3: API reference](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/docs/api.md) | Public routes, scopes, summaries, filters, pagination and conditional reads |
| [S4: architecture](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/docs/architecture.md) | Current server/client boundaries, admission/thread behavior, batching |
| [S5: decisions](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/docs/decisions.md) | Attribution and supersession; especially 13, 39-42, 67-71, 81-97 |
| [S6: deployment](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/docs/deployment.md) | Sole service owner, package-only rollback, continuity, no old-snapshot restoration |
| [S7: development](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/docs/development.md) | Transaction contract, explicit offline conversion, test fixtures |
| [S8: usage](https://github.com/caltseng/phalanx/blob/9af29396b37f86816fd274e01632164a47718a57/docs/usage.md) | Attribution, safe numbers, unknown/final gaps, no lifecycle effects |

Source anchors: `phalanx/api.py:23-70` startup/error/health; `auth.py:27-143` credentials/local authority; `store.py:16-154` gate/connection/schema/snapshot; `intake.py:36-73` encoding/receipt; `intake_api.py:25-196` operation wrapper/routes; `writers.py:85-174,192-349` binding/continuation/replacement; `transitions.py:104-134,210-381` state/output and decisions; `schema.py` complete canonical SQL; `dependencies.py:98-282` evidence/batches; `reconciliation.py:113-226` observation/closure; `captains.py:131-168,270-424` liveness/reinforcement/registration; `issue_scout.py:150-345` page fencing/intake; `usage.py:28-158` observations/projections; `operator_api.py:82-108,478-599` cursors/pages/overview. Appendix A captures the complete generated request/parameter schema from P0/R0, without starting an app lifespan.

### 2.2 Deployment is a separate axis

No production database, installed server package, private supervisor configuration, or service owner's rollout receipts were inspected. Successful authenticated `work status` proves this writer's binding and allocation, not deployed code SHA, schema inventory, or bidirectional migration compatibility. **Deployed baseline D0 remains unknown.** Before implementation acceptance, the operator must privately establish D0's exact package/overlay, runtime/dependencies, SQLite identity/schema/DDL, effective configuration, existing client versions, and supervisor contract. Do not equate P0 or a repository merge with D0.

Read-only remote-main pinning changed no checkout. Evidence/test activity used this item's checkout and supplied interpreter; all test writes were isolated fixtures. No private credential or unrelated runtime history was read. The report's route export is source introspection, not an assertion that every route is deployed.

### 2.3 Historical provenance relevant to the port

The complete original pin/evidence table is retained in the [decision log's HTTP migration historical evidence](decisions.md#http-migration-historical-evidence). Repository documentation keeps PR history in that historical log, not in operating/design guidance. This relocation preserves every original row and its attribution; it is not a new owner decision.

These are local repository history pins, not deployment receipts or new permissions. Historical PH-19/PH-DS5 numbers and decision 87's unmatched claim mix are not a Go baseline. Measure an identical manifest and successful-claim mix before drawing a performance conclusion.

## 3. Decision/provenance ledger

| ID | Classification | Decision or discrepancy | Required handling |
| --- | --- | --- | --- |
| D01 | Confirmed assignment | Server/domain/storage only; Python CLI/reconciler and Scribe unchanged; SQLite initially retained; report only | Hard scope for this deliverable; no implementation/cutover |
| D02 | Proposed target in assignment | Go | Proposed architecture here; owner reviews driver/build/runtime details before execution |
| D03 | Historical explicit owner approval | Decision 13: Python v1, later TypeScript/Node | Not evidence that Go is fully approved. Current report explores Go as requested; owner explicitly resolves language replacement before implementation |
| D04 | Confirmed owner principles | Decisions 67-71: simplicity, API+GitHub truth, no client SQLite, concurrent Captains, direct writers | Keep architecture; current requested compatibility study is bounded exception, not blanket legacy support |
| D05 | Current source, historical settings superseded | Decision 71 advisory capacity and 89 original Total 5 versus canonical atomic Building/Total, new-install 5/10 | Preserve saved capacities, not defaults on startup. Never infer capacity from stage |
| D06 | Confirmed owner current policy | Decision 92: 24 elapsed hours, workflow reinforcement every 90 minutes, ordinary five-minute reconciliation | Preserve client-owned rotation and server monotone acknowledgement. Earlier 4h/wake/compaction rotation is historical |
| D07 | Confirmed owner current policy | Decisions 96/97 outage continuity | Preserve verified writer/Captain ownership; unknown control state cannot admit a replacement |
| D08 | Current canonical instruction refinement | Decision 93 supersedes publication-time auto-merge in 81/85 | Publication can precede gates; independent exact-head review when applicable is not a new server approval stage |
| D09 | Current source contract | DELETE/EXTRA, admission 4s, SQLite busy 2s, 503 retry same request | Baseline invariant; do not copy historical 2s admission wording as current |
| D10 | Current source contract plus narrow compatibility rule | Two exact intake digests; full validated request/default-null material | Preserve all receipts unchanged; outside intake there is no fallback |
| D11 | Documentation conflict | S7 ends with "There is no old-digest fallback"; S1 and `intake.replayable` now define exact intake exception | Prefer specific current S1/code/tests. Do not broaden it. Flag stale general wording for later documentation reconciliation |
| D12 | Historical documents | S2 "migration 20", S8 additive-schema/deployment narrative, retired lease rows in S5 | Historical provenance, not startup migrations or evidence those exact live steps are now required |
| D13 | Current source versus intent nuance | `usage.captain_observation` checks non-ended Captain and repository membership; S8 says live Captain | Preserve P0 observed behavior in oracle; owner resolves tightening separately. Do not silently insert stricter migration authorization |
| D14 | Current source versus prose nuance | Ready acceptance permits `checks=NOT_CONFIGURED` and `mergeable=None`; explicit missing policy can wait | Preserve actual observation/policy adapter distinctions; do not replace "not configured" with invented mandatory checks |
| D15 | Current source nuance | Receipt replay skips the original mutation, but `finish=True` routes re-enter post-commit finalization using captured pending state; response can be current execution view rather than stored writer binding | Preserve two-phase response behavior and logged pending-intent fallback; client status-reconciles shape/stale attempts |
| D16 | Current-source startup | Canonical application ID `0x50484C58`, version 26, no automatic migration | First port opens only explicitly inventoried supported schema. D0 incompatibility requires separately owner-approved offline path |
| D17 | Unverified deployment | D0 package, runtime, schema, overlays and launcher unknown | Operator evidence prerequisite; no promise of package-only rollback yet |
| D18 | Open architecture choice | SQLite driver, CGO/static builds, supported server OS/architecture | Run proof matrix before selecting |
| D19 | Open architecture choice | Exact compatibility treatment of framework errors/headers, duplicate JSON keys, unpaired surrogates, request body limits | Characterize P0/R0 and D0; preserve or obtain explicit versioned change approval |
| D20 | Open operational choice | Runtime resource/latency goals and shutdown deadline; applying ordinary-performance budget to port optimization | Proposed measurable criteria below; owner approves hard-versus-aspirational classification; decision 94 budget applied by analogy, not new blanket waiver |
| D21 | Deferred separately | WAL, tenancy implementation, DB redesign, new admin launch syntax | Not required for compatible HTTP port; no accidental approval |

No Captain best guess is relabeled owner approval. Review of this report approves neither a driver nor service adoption. Current owner restrictions override historical policies, but source/prose disagreements remain visible instead of being silently "cleaned up."

## 4. Compatibility perimeter and wire specification

### 4.1 Contract comparison levels

**Exact durable bytes:** existing `request_receipts.request_digest/response_json`, Markdown/draft revisions, history JSON and timestamps already stored, IDs/versions/bindings, configuration values, schema/index/trigger DDL where unchanged. No repair/re-encode/mass normalization. New compatible receipt encodings must also be replayable by the rollback Python package.

**Wire exactness:** methods, paths, redirects where consumed, statuses, error `detail` structure, required headers, cookies if any actually deployed, media types, weak ETags, cursors, UTF-8 Markdown bytes, body null/omitted fields, array order, integer versus string quantities. Compare raw JSON bytes as evidence; JSON object key order/irrelevant whitespace may be declared noncontractual only in a reviewed normalization rule. Never normalize arrays, numeric types, missing fields or error arrays away. Content-Length/chunking/Date/Server headers need explicit transport-only classification; the default comparator records them and allows only reviewed differences.

**Durable semantic exactness with generated-value mapping:** independent disposable runs create different UUID/time/random-change tokens. Establish a bijective ID map by corresponding accepted event and relational position. Retain original values in artifacts. Inject a common clock/ID source in test adapters where practical, otherwise compare relative/monotone time and relations without dropping fields. Never map two entities to one or ignore history versions/order. ETags and cursors must work with each service's own issued value and crossing a package switch on current data.

There is no second production writer. Test two services only on separate isolated database copies; read-only production export for rehearsal is operator-owned and sanitized/privately held.

### 4.2 Routes and authority

Appendix A is the generated exhaustive P0 request/parameter inventory (66 operations, 51 schemas). It supplements, not substitutes for, the domain rules below. Auth codes: O=owner bearer; R=trusted runner; C=owner or runner coordinator; W=current scoped writer; B=board-read token or explicitly authorized direct local board. A valid writer may read only its own item/execution routes where the route admits that role. Exact source dependencies determine role errors, not "all authenticated" middleware.

| Family (full path prefix) | Methods/body | Authority and returned contract |
| --- | --- | --- |
| `/health`, `/v1/owner/status`, `/v1/runner/status` | GET; no body | Health anonymous `{status:"ok"}`; exact-role probes return `{role}`. Health failure 503 `SQLite store unavailable`; not a general authenticated availability proxy |
| `/v1/repositories` | POST Registration; GET | POST O, GET authenticated scoped roles as wired. Register stable ID/unique remote idempotently; return repository object/list in ID order |
| `/v1/items` | POST Intake, optional key | O + browser same-origin; 201 original created item/execution on replay |
| `/v1/items/{item}` | GET | Authenticated, writer scope enforced; original validated request/current execution/dependencies/latest reply |
| `/v1/items/{item}/title` | GET; PUT DisplayTitle, mandatory key | O, writes same-origin; independent revision/audit metadata. `title` is required nullable, clear with null |
| `/v1/items/{item}/executions` and `.../{execution}/timeline` | GET | Authenticated own-scope reads; history arrays/correlated evidence, not paged operator projections |
| `.../{execution}/drafts/{revision}` and `.../{execution}/report` | GET | Draft uses normal authenticated scope; final report uses B. `text/plain` exact Markdown |
| `.../{execution}` | PATCH Steering, optional key | O; queued edits, active target-only and attempt-free-delivery acceptance-only exceptions. `exclude_unset` semantics |
| `.../{execution}/cancel`, `/close-failure`, `/retry-finalization`, `/rerun` | POST Cancel/Versioned/Rerun, optional keys | O, current expected version; stopped-effect or finalization flow; return execution view, not synthetic success |
| `.../{execution}/result`, `/stop-confirmed` | POST Result/AttemptEvent, optional key | W only; complete validated body plus writer identity receipt; return captured binding unless post-commit finalization returns plain current execution view |
| `/v1/owner/inbox`, `.../{execution}/reply` | GET; POST Reply, mandatory key | O; inbox default 50, limit 1..100, `after_time`/`after_id`; reply version/current question, attributed answer, original receipt |
| `.../{execution}/github-observation`, `/local-checks` | POST Observation/LocalChecks | R only; observation `{recorded,local_checks_recorded}`, local-check receipt/projection; writer evidence cannot supply provider facts |
| `/v1/reconcilers/{runner}/heartbeat`, `/v1/reconcilers/{runner}` | POST optional nullable Heartbeat; DELETE | R heartbeat, C retirement. Preserve latest deployment-check config and retirement semantics |
| `/v1/dispatch/settings` | GET; PUT DispatchSettings | B read; O+same-origin write; saved pair and actual counts, not caller limits |
| `/v1/captains`, `/{captain}` | POST CaptainRegistration optional key; GET list/detail; PUT Heartbeat | C writes; B reads; list default 25, max 100, no fake continuation cursor |
| `/v1/captains/current`, `/{captain}/reinforcement` | GET; PUT interval | B routing snapshot; C acknowledgement. Ambiguous destination returns explicit reason, not arbitrary winner |
| `/v1/items/{item}/captain-actions` | POST Action optional key | C, 201, append current-version action/reason; not execution state advancement |
| `/v1/session-archives` | GET session_id; PUT SessionArchive | C; active/block/refused/archived evidence, not a native archive command |
| `/v1/flags`, `/v1/flags/clear` | GET; PUT Flag; POST FlagKey | B reads, C writes; scoped stored and derived flags, explicit reasons/triggers |
| `.../{execution}/writer`, `/writer/withdraw` | POST Registration; PATCH Update; POST Withdrawal | C; nonce replay, stopped replacement, native confirmation/withdrawal; return binding |
| `/v1/writer`, `/attempt`, `/progress`, `/capacity` | GET; POST empty; POST Progress; POST Capacity | W; same binding, no attempt lease; capacity revision/handoff fence separate from lifecycle version |
| `/v1/writer/usage`, `.../{execution}/writers/{writer}/usage` | POST Snapshot/CaptainObservation | W own live token or C scoped observation; telemetry-only `{accepted,...}` |
| `/v1/muster/status` | GET | O/R, even on local board; read-only identity/Captains/writers/reconciler freshness/pause/time |
| `/v1/issue-scout`, `/settings`, `/claim`, `/passes/{pass}/page`, `/failure`, `/issues/{repo}/{number}/decision` | GET; PUT Settings; POST Claim/Page/Failure/Decision | B status, O+same-origin settings/decision, C claim/page/failure. Durable generated page keys; no server GitHub fetch |
| `/v1/operator/executions`, `/overview`, `/items/{item}`, `/groups/{label}`, `/executions/{execution}/timeline` | GET | B; summary projections, paging, conditional reads, exact current execution target and reason precedence |
| `/operator/api/repositories`, `/items`, `/dispatch/settings`, `/issue-scout/{repo}/{number}/decision` | GET/POST/PUT as inventory | Narrow board-owner adapters, not an owner-role upgrade for `/v1`; reuse canonical intake/settings/decision operations |
| `/operator/`, `/queue`, `/history`, `/items/{item}`, `/executions/{execution}`; `/operator/assets/*` | GET static shell/assets | Preserve pinned shell/assets and route behavior without frontend rebuild/redesign; static routes absent from generated API inventory handled explicitly |

Generated `/openapi.json`, `/docs`, `/redoc` and default redirect/HEAD/OPTIONS/405 behavior must be characterized as framework surface. Decide whether to preserve generated documentation URLs in first port; no unannounced broken operational link. Do not add CORS or cookie sign-in based on historical proposals.

Route precedence is an oracle item: P0 registers literal `/captains/current` before `/captains/{captain_id}`; Go routing must preserve literal-versus-parameter, escaped-segment, static asset and trailing-slash behavior, including 404 versus 405 and redirects. Generated documentation preservation is D19/D21 owner-review scope, not a mandate to implement a new documentation service.

### 4.3 Payload rules that a Go decoder cannot infer from struct tags

All public payloads inherit unknown-field rejection; exact length/regex/enums/list bounds/defaults are in Appendix A and P0 model definitions. Text trims Python whitespace and is 1..16,000 characters; identifiers are `[A-Za-z0-9_-]{1,100}`; Markdown is nonempty/non-whitespace and at most 256,000 characters, without trimming its stored bytes. Python character counts are Unicode code points, not bytes or UTF-16 code units. Go must not substitute `len(string)` or ASCII-only trimming.

Intake defaults: `target=checked_in`, `context_links=[]`, `acceptance_criteria=[]`, `dependencies=[]`, `priority=normal`, `publication_mode=ready`, `target_branch=null`, `label=null`. Report+draft is invalid. Prerequisite default milestone is checked-in, lists max 100; context/criteria max 50. Goal/context/criteria are normalized by the validated model before hashing.

Execution `expected_version` strict integer >=1; title/capacity/scout revisions strict integer >=0; bools/floats/numeric strings are not interchangeable with strict integers. Dispatch limits strict 1..2,147,483,647, paired Building<=Total. `paused` strict boolean. Some historical/current models use non-strict `bool`; do not silently tighten all booleans. Appendix schemas do not encode every Pydantic coercion, so executable invalid/coercion cases are mandatory.

Steering presence is semantic: omitted keeps, `[]` clears lists; label null clears. Not all nulls mean clear (optional goal/target handling is operation-specific). Hash only supplied Steering fields including `expected_version` and supplied/default-attribution behavior according to `exclude_unset=True`. A Go pointer alone cannot distinguish absent from explicit JSON null; decode to an explicit presence map/optional value.

Result requires stage, outcome and expected version; only the structured delivery external-wait question may omit/null attempt. Success evidence: development candidate, self-review exact reviewed_candidate, publication PR URL, report drafting Markdown, report review/storage exact revision. SHA and URL/domain validation have both transport and application layers; matching JSON Schema alone is insufficient.

Question `{prompt,options:[],proposed_default:null,handoff}`; max 20 options, explicit handoff and reason/resume semantics. Reply `{expected_version,answer,answered_by:"owner"}`; Captain attribution is recorded only when supplied. PR URL canonicalizes repository case consistently with registered remote, not an arbitrary current GitHub alias.

Scout model details: settings full update with expected revision, allowed cadence 15/30/60/180/360/720/1440 minutes, numeric author ID, unique 1..100 repo list; issue author fields required nullable; page max 30 issues, repository/page/claim and verified login+ID; issue title/body preserved subject to bounds; body allowed up to 1,000,000 chars but normal admitted goal still <=16,000. Oversize issue is visibly skipped, not silently truncated.

Usage counters are nullable strict safe integers <=9,007,199,254,740,991; decimal quantities are strings with unit/basis/source, no float conversion. Required window/provenance/status/reason, aware timestamps, window_start<=cutoff<=observed_at, observed_at not future. Sums above safe integer range become decimal strings; unknown remains null, never zero.

### 4.4 Authentication, scopes, and error shape

Constant-time owner/runner token comparisons; writer SHA-256 digest lookup joined to nonterminal execution and non-ended writer. Owner and runner privileges are separate. Hash writer tokens' UTF-8 exactly. Validate scope before operation and recheck current writer/attempt/version under the mutation transaction: auth snapshot alone is not sufficient against replacement races.

401 `{"detail":"Valid bearer token required"}`, `WWW-Authenticate: Bearer`; valid wrong-role 403 with exact role-specific detail; cross-item writer 403. Missing entity 404, mutation/precondition/receipt conflict 409. Model/header/path/query errors generally 422 with ordered FastAPI/Pydantic `detail` entries (loc/type/msg/input/ctx where present). Domain cursor/filter errors are 400, not 422. Malformed JSON and invalid UTF-8 behavior must be recorded separately; don't invent one general 400 conversion.

SQLite BUSY/LOCKED (primary code or source fallback) -> 503, `Retry-After: 1`, `{"detail":"SQLite store unavailable (<reason>); retry the same request"}`. Preserve admission-specific reasons and driver's canonical reason mapping if driver messages differ. Other operational faults remain 500 and logged, not busy/retry-shaped. Health has its own narrower failure body. Do not return success on unknown commit or malformed persistent JSON.

Explicit local-board flag, direct loopback peer, exactly one acceptable loopback Host, no Forwarded/X-Forwarded-*/X-Real-IP. A supplied Authorization header must authenticate even if invalid/empty; no local fallback. Tokenless local writes require exact same Origin, no null/untrusted Origin, duplicated origin/fetch-site, or cross-site metadata. Authenticated nonbrowser requests without browser headers remain valid as wired. Preserve URL scheme/host handling at the actual deployment proxy boundary; do not trust Go proxy headers by default.

No new auth identities, workspace grants, host-account bypass, plaintext credential storage, request-body logging, broad permissive middleware or public mutable settings. Single implicit workspace remains a current limitation; logical workspace readiness is future architecture, not permission to migrate to a multi-tenant schema.

### 4.5 Paging, views and cache validators

Operator execution pages default 25, max 100. Open order input-wait rank then creation time+ID; closed newest closure/update time+ID; timeline newest first. Cursors are canonical unpadded URL-safe base64 of compact ASCII JSON `[1,scope,key]`, strict re-encoding verified, maximum 512 chars, same filter/list scope. Filter scope includes SHA-256 first 16 hex chars of Python sorted JSON encoding, not a new Go serialization.

Filters: repository/label/status/stage/outcome/q/closed_after. Open lists reject closure/outcome filters and incompatible status (400). Closed naive dates are treated UTC at source. Filtered scans inspect at most 2,000 indexed candidates and may produce an empty page with non-null cursor. Do not replace keyset paging with OFFSET, add unbounded aggregates, or omit empty continuation pages. Groups aggregate only that page, `scope:"page"`.

Weak ETags with `private, no-cache`, If-None-Match ->304 without JSON body. Age alone does not invalidate; queue/retry deadlines, launch deadlines, 24h closure windows, relevant state/config/native observations do. Preserve trigger-maintained operator change tokens and conditional-read inputs. Authentication still applies to conditional reads. New Go responses need exact projection shape: null PR when unobserved; writer latest native facts; current execution target/mode, active-start=min persisted attempt, correlated event candidate/attempt/pr_url, bounded/redacted 400-character reasons, criteria/context only in authorized detail not every queue row.

## 5. Durable state and transaction inventory

Open the exact supported canonical database with application ID PHLX and schema version 26; reject mismatches. Existing startup is not a converter. Preserve tables, typed values, foreign keys, generated columns, partial unique indexes, triggers and cached projections. Compare complete `sqlite_master` `(type,name,tbl_name,sql)` and PRAGMAs; physical rootpages/page order need not match. Schema SQL is part of the rollback package proof, not an ORM-generated equivalent.

Tables retained (exact SQL names): repositories, work_items, executions, attempts, transition_history, report_drafts, reports, request_receipts, dispatch_settings, issue_scout_settings, issue_scout_passes, issue_sources, issue_scout_decisions, report_finalizations, operator_progress, work_dependencies, dependency_refresh_jobs, dependency_audit, captains, writers, flags, writer_usage, github_observations, local_check_results, reconciler_heartbeats, captain_actions, operator_pr, operator_change, session_archives, item_titles. Trigger-maintained state is authoritative for projections; do not rebuild it by scanning everything on each request.

| Operation | Single transaction atomic unit | Outside transaction |
| --- | --- | --- |
| Intake | receipt check; item/execution/dependencies; history; cached projections; response receipt | Decode/auth transport; no GitHub/native I/O |
| Writer registration/replacement | nonce/current version; pause/limits/eligibility; stop-transfer; digest/binding; allocation; claim and dependency pins | Native launch/credential file remains client |
| Stage result | receipt/fences; evidence/draft; transition; history/version; old attempt end; next claim or wait; allocation effects; response receipt | Inline finalization may execute afterward in a new transaction |
| Question/reply/steering | current question/version/presence rules; complete audit; scheduling/dependency effects; receipt | Actual owner/native notification remains client/runtime |
| GitHub observation | fact digest, timestamps; trusted primary-PR transition; output closure; writer release; dependency refresh seed | GitHub/Git fetching and verification remain reconciler |
| Report finalization | savepoint artifact insert/check; FINALIZE history/output/status; writer end; job deletion | Durable intent was previously committed; periodic worker retry |
| Capacity handoff | capacity revision; acquire/release request; reviewed head; binding response | Editing/review/push remains writer |
| Scout page | claim/cursor/identity; source/decision checks; intake+receipts; counts/cursor/lease release/due | Local authenticated GitHub page fetched by unchanged client |
| Usage | attribution/sequence/cumulative comparison; one source aggregate/update | Runtime acquisition and provider permission remain outside |
| Dependency fanout/audit | up to 32 updates plus cursor/job checkpoint; claim rechecks current evidence | Existing periodic loop, no new scheduler |

Do not split these into independent repository calls each committing itself. Pure transition evaluation does not own auth, transactions or external observation truth. Repositories must accept an explicit transaction handle.

### 5.1 State machine and invariant register

Each invariant has an executable acceptance case in section 9. "Preserve" includes rejection side effects: invalid requests cannot append audit, increment version, consume capacity, or insert a receipt.

| ID | Invariant | Source / case |
| --- | --- | --- |
| I01 | Exactly one open execution per item, one open attempt per execution, one open writer per execution | `schema` unique indexes + `writers.register`; C01/C03 |
| I02 | Every terminal execution has exactly one verified PR or stored report; no closed row without output | `Execution.__post_init__`, SQL CHECK, finalization; C02/C10 |
| I03 | One writer across stage continuations/questions/dependency/external/retry waits; next attempt separately fenced | `writers.advance/claim_next`, `check_attempt`; C03/C04 |
| I04 | Writer digest/scope and current item/execution/attempt/stage/version checked; replaced/ended identities cannot mutate | `auth`, `intake.result`; C01/C04/C05 |
| I05 | Total and Building atomic persisted workspace limits; never preempt on reduction; stopped replacement transfers allocation/request | `dispatch`, `writers.register/capacity`; C03/C06 |
| I06 | Building held for all nonpublication/nondelivery stages; explicit rework acquire; false allocation is wait, never permission | `dispatch.needs_building`, `claim_next`; C06 |
| I07 | Self-review success releases Building, not Total; delivering head/revision prevent stale green observations ending unpushed rework | `writers.advance/capacity`, `reconciliation._advance`; C06/C07 |
| I08 | Pause refuses new admission/acquisition/attempt, not accepted existing progress/result or independent finalization | dispatch/claim/finalization; C06/C10 |
| I09 | Dependency evidence pins exact upstream execution/output/revision on claim; current durable evidence rechecked | `dependencies.resolve_dependencies`, claim history; C08 |
| I10 | Report review/storage match UTF-8 SHA-256 current Markdown; failure/cancellation dispositions do not satisfy report milestone | `result`, `_finish`, dependency resolution; C02/C08/C10 |
| I11 | Primary PR identity is current execution's; prior merged execution PR cannot deliver rerun | `intake.result`, `_advance`; C07/C11 |
| I12 | Ready/draft/merged distinct; current target/base; applicable independent review/CI gates stay writer policy, not invented engine stage | transitions, worker canonical policy; C07/C11 |
| I13 | Verified merge by any actor/method wins before stop confirmation, including head drift; older nonmerge facts cannot regress state | transitions/reconciliation; C07/C09 |
| I14 | Attempt-free delivery question only current live binding, no open attempt, external wait, exact version; reply retains writer | result/waits; C04/C09 |
| I15 | Cancellation/forced failure retain effect-capable writer through input/queued/attempt-free delivery until verified stop; no timer substitutes | request_stop/release; C09 |
| I16 | Request receipt and operation effects commit atomically; replay returns original response; changed scope/body conflicts | `replayable`, route payload material; C12 |
| I17 | Intake exact two hashes only; new default-null digest; no rewriting or mutable-item reconstruction | `replayable`, receipt tests; C12/C13 |
| I18 | DELETE/EXTRA + FK, BEGIN IMMEDIATE; admission cleanup/fair alternation; BUSY/LOCKED only 503 | store/api; C14/C15 |
| I19 | Disconnect/await cancel is not durable rollback; committed writes survive; failed/unfinished transaction rolls back before release | source thread model/store; C14/C16 |
| I20 | Native identity/nonce, not title, drives discovery; concurrent Captains permitted; unconfirmed intent ownership rules explicit | captains/writers/session_links; C05/C17 |
| I21 | Captain state monotone; liveness not writer expiry; reinforced interval max persists through clock rollback/restart | captains; C17/C18 |
| I22 | Scout claim lease only page fencing; settings/handoff supersede; login+numeric ID verification; atomic intake/cursor/receipt | issue_scout; C19/C20 |
| I23 | Manual/source issue identity avoids duplicate work including closed history; approved fingerprint fresh; edits never rerun | scout historical_item/_observe; C20 |
| I24 | Usage cumulative provenance/sequence, unknown gaps, no billing invention/lifecycle/capacity effects; late scoped observations don't revive identity | usage/models; C21 |
| I25 | Bounded fanout/read projections; valid cursor/ETag; summary current target/age/reason precedence | dependencies/operator/flags; C08/C22 |
| I26 | One production API writer, no offline transitions or restored older database; rollback previous package on evolved current data only | deployment/owner scope; C23 |

## 6. Lifecycle and recovery detail

PR stages remain `development -> self_review -> publication -> delivery`; self-review findings return to development at exact candidate. Publication/delivery reject `changes_required`; delivery fixes stay on same PR/writer without a second PR executor. Draft observed head drift queues self-review and must reacquire Building. Reports remain drafting/review/storage, with content-addressed draft and exact revision findings.

Status is separate: queued/running/input-wait/external-wait/completed/failed/cancelled. Pending output is a persisted finalization intent, not terminal success. Stopping is a persisted effect-reconciliation state, not a cancelled outcome. Technical failure uses existing bounded 5/10/20/40-second retry scheduling before owner intervention after five; supplied retry delay cannot shorten backoff. Self-review rework bound 10 rounds or repeated findings (third identical finding) since latest reply; do not reset by changing writer or stage. Report finalization retries 2/4/8/16 seconds then named blocker/manual versioned retry; retains durable job on total store outage.

### 6.1 Claims, leases and capacity

Current direct writers have **no timed attempt/writer expiry**, no lease renewal, no old runner slots, no attempt_workers/client publication journal. Historical 300-second attempt lease and lease-expiry tests are superseded. This does not mean all leases disappear: issue-scout page claims have a ten-minute lease. Captain 15-minute liveness, 30-minute ending timeout, 10-minute unconfirmed-launch warning, two-hour no-progress warning and strict >90-minute active-overdue are signals/scoped authority, not blanket writer revocation.

Writer registration may replay identical nonce even after state advances; conflicting identity/digest/runtime/replacement fields reject. Fresh admission checks pause and saved pair. Confirmed intent may be coordinated by another non-ended Captain under existing rules; unconfirmed intent remains owner's while live unless abandoned, ended or stale. Replacement names exact current writer and attests stopped, transfers held allocation/pending request even after lowered limits, ends old credential, and preserves worktree responsibility outside server.

New-intake pickup priority urgent/normal/low then execution creation time+ID. Building rework preference uses oldest **eligible** registered PR fix request; blocked input/dependency/retry does not starve ordinary admission. Capacity revision is independent: repeated or stale handoffs cannot release later acquisition. `capacity delivering` records full reviewed handoff head before push; a different observed head cannot close ready/draft work while allocation or mismatched handoff remains. Actual verified merge remains authoritative.

### 6.2 Delivery and cancellation

Writers own GitHub writes, exact-head applicable independent review and repository CI/merge rules. Server accepts only trusted reconciler observations of deterministic item head branch/registered repo/current primary URL and target base. Observations de-duplicate by payload digest and refresh last-seen time; older nonmerge facts cannot advance. Policy adapter supplies satisfied/not-configured/pending/failed checks; not-configured isn't missing evidence accidentally mapped to success.

Verified merge records final head, merge commit, method `merge` or `squash_or_rebase`, merged_at; wrong base requires question. Current source trusts reconciler verification rather than redoing GitHub I/O. Merge commit parent/reachability evidence remains unchanged reconciler responsibility. Ready-only completed output later enriches once on same or proven-descendant merged head, keeps completed/target, releases check-in dependents without rerun/new merge authority.

Cancellation in input wait or queued-after-reply delivery still has effect-capable writer. Stop it only through exact native ownership/effect reconciliation and scoped stop-confirmed or Captain withdrawal with stopped=true. Merge observed before stop confirmation delivers rather than falsely cancelling. Do not simulate an attempt for attempt-free withdrawal. No active attempt/open writer permits immediate stop/finalization where current operation allows.

### 6.3 Captain recovery, scouting and usage

Keep current Captain ID/start clock through shared API outages. No replacement based on timeout/auth-service failure alone. Same-writer/Captain offline physical work only under last verified authority/allocation; no server-side offline queue or retroactive unconfirmed admission. Unknown result/release may have ended ownership: inspect durable status/receipts before any new capacity-sensitive work.

Normal 24h rotation/five-minute wakes is unchanged Python/runtime policy, not a Go timer that spawns chats. Server 90-minute registration-relative reinforcement interval uses UTC, negative elapsed clamp zero, max acknowledged interval, no future unacknowledged acceptance. Heartbeat cannot acknowledge unread instructions; wake/compaction counts are telemetry; omitted/null compaction preserves greatest known value. Current routing reads all live active registrations rather than recent-page limit; ambiguous overlap has no fabricated unique destination.

Scout wakes reuse existing client: verified local GitHub identity per page, one durable cursor, source metadata not issue bodies in diagnostics. Claim requires live active Captain covering configured repos. Page completion checks current claim lease, Captain still active, expected repo/page/identity; settings supersede open pass; handoff permits successor reclaim without stale page commit. Page receipt key scopes pass plus claim/repo/page, failure by claim. Owner approval is fingerprint-bound metadata, not queued intake; next fresh page performs ordinary intake with stable source key. Closed/imported/declined/edited/oversized source handling retains evidence and visible next action.

Usage acquisition stays outside server. Preserve full source/account/origin/epoch/model/window/scope identity; cumulative sequence and nondecreasing counters/cutoff; unavailable acquisition keeps previous measurement plus failure. Reused native ID anywhere conservatively excludes all such bindings from subtotal; separate replacement sessions counted once. Late Captain observation cutoff <=ownership end; ended writer token remains unusable. Token field totals do not imply a bill, different provider units/currencies never summed, shared overhead unallocated.

## 7. Minimal Go design and alternatives

### 7.1 Proposed package responsibilities

| Boundary | Owns | Must not own |
| --- | --- | --- |
| HTTP | Route table, decode/presence/default/validation, error serializer, local-board facts, auth, static assets; frozen OpenAPI test fixture/documentation only if approved | Stage transitions, per-handler SQL, native/GitHub writes |
| Application/domain | Typed commands/views; current-state checks; pure transition/evidence rules; receipt material; transaction orchestration; finalization/refresh operations | HTTP objects, driver error strings, process launch, durable client queues |
| SQLite store | Explicit read/write transaction admission, connection PRAGMAs, query methods, canonical SQL/schema validation, typed errors, snapshots | Product retries hidden in driver, independently committing each repository method, owner decisions |
| Service composition | Config, lifetime/sole-instance lock, bounded existing background loops, shutdown/logging/metrics | New coordinator, replacement Captain, cross-service workflow |

Conceptual operation: decode and validate -> authenticate -> application executes one transaction -> store commits/returns captured view -> serialize HTTP. Under transaction, recheck all mutation fences. Pure state evaluator receives trusted typed evidence and returns a proposed next snapshot; application records snapshot/history/attempt/dependency/allocation/receipt as one unit. Only finalized stored outputs close execution.

Use explicit structs for domain states and discriminated PR/report outputs, string enums preserving wire values, optional/presence types for request fields, int64 or arbitrary-precision aggregation where required. Don't use a universal `map[string]any` domain or float64 JSON numbers. A limited raw-JSON/presence/canonical-value layer is justified by exact Pydantic compatibility; it is not a generic serialization framework.

Prefer concrete operation store methods and small transaction-scoped interfaces only at fault seams. Start with one store implementation. A workspace concept can remain in naming/context without introducing tables/auth changes. Keep `transitions`, writer allocation, intake receipts, Captain/scout, usage, and projections as cohesive modules rather than one file per table or a service per subsystem.

### 7.2 SQLite and build decision matrix

| Choice | Advantages | Costs/risks | Initial recommendation |
| --- | --- | --- | --- |
| CGO SQLite driver (for example mattn/go-sqlite3) | Mature direct SQLite, explicit SQLite version/features/errors, familiar native semantics | C toolchain/cross-build matrix, static-lib/link choices, per-platform packaging | Candidate, not selected; demonstrate canonical DDL/EXTRA/backup/error/cancel behavior |
| Pure-Go SQLite driver (for example modernc.org/sqlite) | Simpler no-C-toolchain cross builds, one executable | Larger artifact/dependency graph, translated SQLite differences, busy/interrupt/backup/durability proof still needed | Candidate, not automatic preference; measure same workload |
| Existing Python retained | Baseline compatibility and known tooling | Does not realize requested non-Python service goal | Rollback reference; always retain pinned package/config |
| WAL | Reader/writer coexistence may improve tails | Changes snapshot/checkpoint/durability/admin/exclusion behavior and rollback assumptions | Deferred; separate proof after compatible port |
| PostgreSQL/new database | Multi-host throughput | New service, migration/operational complexity; violates initial scope | Not proposed |

Pin actual Go toolchain, module graph, driver and SQLite engine versions, GOOS/GOARCH/CGO flags and reproducible build commands in future proof artifact; no unsupported "static everywhere" claim. Server only needs supported hosting platforms; unchanged clients must still work Windows/macOS/Linux. Propose production-host OS/architecture first plus CI cross-platform integration where operator needs it; owner confirms server platform scope. Test Windows file paths/permissions/locks and Unicode if that host remains supported.

Use standard `net/http` routing by default if sufficient; a minimal router requires demonstrated route/escaping/405 compatibility value. No framework default. Embed unchanged pinned static assets or serve a pinned package-owned directory; choose based on rollback/package layout, not a frontend redesign. Retain CLI binary/package interface through existing supported deployment adapter only after owner review. Unchanged Python clients must not need a renamed route or new local protocol to reach Go.

### 7.3 Connections, admission and concurrency

First port reproduces one in-process serialized write admission and waiting read batches around acquiring SQLite read snapshot. **Default to short-lived connection per transaction, matching `store.connect`, and close on all exits.** This naturally discards failed transaction state and avoids unnecessary persistent-connection recovery machinery. Do not assume `database/sql` max-open-connections=1 reproduces fairness: it serializes reads too and may starve control traffic behind writes. A writer/read pool is a later measured optimization only; it requires PRAGMAs on every physical connection, clean transaction state, snapshot/schema freshness and explicit pool-acquisition measurements.

Use four-second process admission bound and two-second SQLite busy bound initially. Waiting cancellation removes the waiter from the correct queue even after transfer to admitted reader batch. Acquired read gate is released only after BEGIN/schema snapshot lock acquisition, not throughout the whole read as an accidental global lock. Writer finally releases and transfers readers on all commit/rollback/error paths. Background jobs share admission; no privileged bypass.

Goroutine cancellation after disconnect differs from Python worker-thread continuation. P0 synchronous handler work can continue after caller disconnect **while waiting for admission as well as after admission**. The four-second admission bound limits gate waiting, not the entire operation. Proposed parity choice: once the mutation handler is dispatched, execute it under service-owned lifetime/drain tracking independent of request cancellation; disconnect stops response transport, not queued/admitted database work. Explicit operation interruption or admission timeout must clean the gate; caller disconnect alone is not that interruption. Removing a dispatched but unadmitted mutation on disconnect would be an owner-approved deviation, not exact parity. Explicit rollback/connection close on operation failure, including injected COMMIT failure; uncertain commit requires receipt/state reconciliation, never blind rerun.

Driver cancellation alternative (ExecuteContext with caller context) may abort SQLite and improve cleanup, but is a behavioral change unless characterized/approved. Don't pick it merely because it is idiomatic Go. A future total request deadline must distinguish preadmission rejection from admitted/possibly committed result and cannot relabel cancellation as "not accepted." Shutdown stops new admissions, drains tracked admitted operations/background transactions up to owner-reviewed limit, then preserves durable intents for restart. No new success-shaped fallback on deadline.

SQLite synchronous durability is contingent on real filesystem/device guarantees. Keep FK and EXTRA per connection, DELETE mode, private local persistent file, no network-share assumption. Driver default WAL/NORMAL, missing busy timeout, SQLite feature mismatch and auto-created empty DB are rejection conditions. One supported production launcher must hold existing database-adjacent sole-instance OS lock for lifetime; inventory raw launchers that lack lock before adoption. `deployment.run_service` uses `<resolved-db-path>.service.lock`; Python takes nonblocking exclusive `flock` on POSIX and `msvcrt.LK_NBLCK` on a one-byte range at offset zero on Windows (`workspaces._lock`). Prove the actual Go lock conflicts with Python in both directions on each supported OS; same filename alone is insufficient. A raw launcher bypass is a deployment hazard, not a license to rely only on the lock.

### 7.4 Internal improvements with provenance and proof

| Improvement | Source reason | Required proof |
| --- | --- | --- |
| Separate transport validation and transaction/application boundaries | Current `intake_api.run`, circular writer route imports and mixed payload/domain modules | Same operation atomicity/errors; no duplicated handler business logic |
| Centralize Python-compatible canonical value encoding | `intake.encode`, receipt hashes and filter scope JSON | Golden byte/digest vectors, older package replay, no incidental standard-Go encoder defaults |
| Make admission state explicit/testable | `store._Gate`, interrupted reader fix | Transfer-race cleanup, saturated-error path independent of DB worker budget, no starvation |
| Transaction-scoped repositories | Atomic result+continuation and scout intake+cursor | Crash between logical writes cannot leave partial effects |
| Request-local projection context and batches | Batched Captain/flag/pickup reads at P0 | No N+1 regression; indexed scan bounds and exact reason/ETag dependencies |
| Driver-coded error classification | Current primary SQLite code check | Only busy/locked gets 503; schema/read-only errors visible 500 |
| Build/asset/service revision manifest (optional deployment aid) | Source versus deployment ambiguity | Distinct installed revision proof in operator artifacts, not a mandatory new server endpoint/auth feature |

These improve internals while keeping authoritative paths. Any proposed difference that changes workflow, authorization, data or error/retry semantics moves to decision ledger and owner review rather than being justified as cleanup.

## 8. Canonicalization, receipt fingerprints and rollback

Python uses `json.dumps(value, sort_keys=True, separators=(",",":"), allow_nan=False)` with default `ensure_ascii=True`, then UTF-8 SHA-256. Canonicalization happens **after** Pydantic validation/defaults/coercion, with route-specific presence selection. Preserve Python string escaping, Unicode code-point key sorting, number representation where models permit it, nullable fields, arrays and timestamps. Go `encoding/json` HTML-escapes `<>&`, emits Unicode differently, float representations differ, and maps decoding to float64 loses integer identity. Raw incoming-body hashing is wrong.

String test vectors must include `"caf\u00e9"`, literal UTF-8 cafe-accent, non-BMP emoji as surrogate pair escaping, `<>&`, U+2028/U+2029, backslash/quote/control escapes, CRLF versus LF Markdown, composed versus decomposed accents (no normalization), Python whitespace trimming, zero/null/omitted, bool/1/1.0/"1", nested object key order. Treat unpaired surrogates/duplicate keys as explicit oracle cases; do not silently replace invalid input with U+FFFD. Timestamp normalization follows each model, not a blanket UTC encoder.

Examples of exact Python canonical material:

* `{"text":"cafe\\u00e9"}` here is descriptive only; actual accented-string canonical output is `{"text":"caf\u00e9"}` with six ASCII characters for the escape.
* For literal `<>&`, canonical object is `{"text":"<>&"}` (no HTML escaping).
* Markdown revision is SHA-256 of **original accepted Markdown UTF-8**, not JSON encoding or trimmed Markdown.

### 8.1 Intake exception

Let V be the complete validated P0 Intake JSON value. New intake receipt digest is H(canonical(V plus `"runner_affinity":null`)). Existing intake receipt matches exactly either that digest or H(canonical(V)). A public `runner_affinity` field, including null, is rejected; historical non-null/unknown fields or unverifiable digests do not match. Other receipt scopes do not get this exception.

Preserve existing scope/key/digest/response rows, items and history unchanged. Do not infer receipt request from edited work-item state, discard defaults, rewrite digest, or retry conflicts under fresh keys. Earlier default-null package can replay a Go-created default-null digest only if its complete validated request shape also agrees. Intermediate current-model receipts without null are P0-replayable but not necessarily earlier-package-replayable; enumerate that limitation honestly.

### 8.2 Result and other receipts

Result/stop receipt material is `{"body": complete validated body, "writer_id": authenticated writer ID}`, including null/default fields (not arbitrary history exclusions). `report_markdown` is excluded from persisted transition details but remains receipt hash material. Changing writer identity changes fingerprint even for same body/key. Scope uses operation name/item/execution. Steering excludes unset. Display title/reply require keys; ordinary mutations with optional missing key have no replay receipt and use version/nonce/state fences.

Responses are serialized and stored at operation return inside the first transaction. Receipt replay itself returns that captured parsed value and cannot reapply the original mutation. **The HTTP wrapper has a second phase for `finish=True` routes** (`cancel`, `close-failure`, `retry-finalization`, `result`, `stop-confirmed`): if the captured value's `state.pending_output` is non-null, `intake_api.run` invokes `finalization.attempt` after that transaction, including on replay. It returns the attempt's current/finalized execution view if non-null, otherwise the captured receipt value. This can replace a captured writer-binding response with a plain execution view. Even when no job is due, `attempt` returns the current execution view.

On SQLite access failure in this second phase, Python logs the error and returns the captured 2xx pending-intent view; this is an existing explicit durable-intent acceptance contract, not proof storage succeeded. The port must preserve it or obtain an approved change; it must not invent similar fallbacks for first-phase commit uncertainty. Receipt bytes stay unchanged while actual row may later close. Retrying can authenticate differently after writer ended: an old writer token may now get 401 rather than access its historical receipt. Reconciliation uses authorized current status/timeline; receipt replay must not bypass auth to manufacture availability.

### 8.3 Package-only rollback matrix

On disposable data run P0->Go->P0 and, once D0 is inventoried, D0->Go->D0. Include keys created before/after switch, all mutation scopes, current old attempts retained across switches, null-omitting intermediate intake keys, newer state/draft/report/job/usage/scout rows, cursor/ETag continuation, open delivery/stop states. Preserve byte-exact receipts, typed-value tables and schema before/after. Replays conflict only in the documented incompatible case; do not silently forgive it.

No schema upgrade in compatible first port. If D0 is schema 24/25 or carries overlays, explicit prior conversion/replay issue is outside assumed package-only compatibility. Owner decides separate supported transition and rollback; report cannot promise rollback to an old package after schema change. A backup is evidence/recovery input, **not** permission to replace evolved current production data. Once candidate accepts writes, rollback old package only on current data it can open/read/mutate/replay. If unproven, stop affected API safely, retain current file/receipts and escalate; never restore old snapshot to erase accepted writes.

## 9. Differential conformance and fault acceptance

### 9.1 Staged disposable comparison harness

This is a design for future executable harness work; no Go comparison implementation exists in this report.

1. **Freeze corpus:** record P0/D0/R0, schema SQL hash, test-ID manifest/digest/count, source contracts, sanitized input request bytes and expected authority. Generate clean seeded copies with relational states through ordinary API/setup fixtures; immutable export of existing data is optional operator-owned rehearsal, never a writer read of production DB.
2. **Golden transport:** use real loopback sockets, unchanged Python `APIClient`, CLI runner and Scribe fixtures, not only direct handler mocks. Capture request method/path/query/headers/body bytes; response status/header multimap/raw bytes/parsed JSON; no redirects hidden by client. Record invalid/auth/browser/proxy permutations as carefully as happy paths.
3. **Deterministic domain differential:** common event/clock/ID seeds or explicit bijection mapping; run each sequence against independent copies. Snapshot durable typed values after every accepted/refused request, crash and retry, including receipts/triggers/jobs. Compare schema and immutable old values exactly. Never normalize capacity/version/history/output out of mismatch.
4. **Race/fault/restart:** barriers at auth snapshot, admission, BEGIN, evidence writes, receipt insert, attempt continuation, before/after COMMIT, before response, finalization savepoint/job delete, refresh cursor, scout page completion. External SQLite locks, owned service crash, dropped response, socket disconnect and cancellation; reopen same copy and retry original body/key/version. Each fault has narrow recorded window and expected acceptance/unknown/rejection outcome.
5. **Compatibility/rollback:** package switching only, current evolved data retained, unchanged CLI/Captain/reconciler/Scribe flows. Test installed D0 independently of main. Stub external providers/launches; no GitHub write or native launch in port rehearsal.
6. **Matched performance:** same manifest/request mix/seed/concurrency/capacity/data size/hardware/priority/filesystem/window. At least three measured runs per implementation after warmup; keep full distributions/count/status mix and misses, not averaged p99 alone. Report empty claims separately from successful claims/atomic continuations.

Independent copies must never share a production DB, port/supervisor identity, token, native writer credential or reconciler scratch checkout. Use test credentials with no provider access. A logical fake "shadow write" to production is still dual writing and is forbidden.

### 9.2 Executable acceptance case register

Each C case specifies actions/oracle/assertions and maps to I register. Existing tests are evidence seeds, not proof the future Go port passes. Expand current Python fixtures into transport/durable adapters rather than copying every implementation mock.

| Case | Sequence/fault and required assertion | Existing seed at P0 |
| --- | --- | --- |
| C01 | Two Captains register same execution concurrently, replay one nonce, conflict other; cross-item token/auth-replacement race -> no second writer/attempt/receipt | `test_writers.py` registration/scoping; unique SQL |
| C02 | Draft report Unicode/CRLF, mutate revision, failed finalization savepoint, restart; terminal report/hash exact or durable pending job, never terminal missing output | `test_intake.py`, `test_transitions.py::test_open_or_closed_state_cannot_violate_final_output_invariant` |
| C03 | Named claim and atomic result continuation under full Total/Building; pause between result and next claim; no slot surrendered or unallocated work | `test_writer_claims_a_named_item_and_carries_it_through_every_stage` |
| C04 | Replay result after lost HTTP response/current next attempt, stale version/stage/attempt/null misuse; captured receipt doesn't rerun original mutation; `finish=True` re-entry may finalize/return current plain view | `test_lost_result_reply_reconciles_accepted_attempt_without_replaying_next_stage`, `test_result_receipt_fingerprints_complete_validated_body_and_writer`; add two-phase response-shape assertions |
| C05 | Lost native launch before confirmation, full nonce with renamed/colliding titles; stopped replacement with dirty work evidence and current token revocation | `test_shift.py::test_native_creation_before_confirmation_recovers_the_same_full_nonce_at_capacity`, writer title/nonce tests |
| C06 | Competing new admissions/rework, atomic pair change, lower below usage, paused acquire, stale capacity revision and waiting priority; no preemption/over-allocation | `test_dispatch.py`, `test_rework_reacquisition_is_atomic_with_competing_admissions`, `test_recovered_self_review_cannot_claim_without_building` |
| C07 | Old green observation during Building or before superseding handoff push cannot finish; verified merge still wins; draft drift re-review; wrong base waits | `test_merge_is_authoritative_during_building_or_mismatched_publication_handoff`, transition/reconciler tests |
| C08 | >32 dependents, crash after one batch, rerun upstream, multiple prerequisites; restart cursor drains and claim pins current exact evidence; disposition not report milestone | `test_dependency_dispatch.py::test_named_claim_checks_both_upstreams_and_pins_their_revisions`, `test_fanout_resumes_from_durable_cursor_in_bounded_batches` |
| C09 | Cancellation in running/input/attempt-free/queued reply delivery; delayed verified merge vs stop barriers; writer retained until reconciled, no fabricated attempt | `test_delivery_input_wait_cancel_and_failure_retain_writer_until_stop_or_verified_merge`, `test_a_verified_delivery_merge_wins_over_a_pending_stop` |
| C10 | Five finalization failures, total store loss unable to record failure, restart/pause/manual retry; intent retained, one exact report, closure+release atomic; replay pending receipt re-enters finalization, logged SQLite failure returns captured pending 2xx | `test_intake.py::test_cancel_storage_error_rolls_back_and_recovers_after_restart`, finalization fixtures; add exact captured-versus-current response cases |
| C11 | Prior merged PR rerun rejected; actual merge/squash/rebase facts, ready later descendant merge once, no new writer or reopened execution | `test_completed_ready_merge_advances_once_and_releases_check_in_dependency`, `test_completed_ready_rejects_other_pr_and_ignores_unverified_merges` |
| C12 | Every receipt scope exact replay/changed body/writer/missing or explicit null/defaults; raw JSON order/coercion differs but validated equivalence consistent | receipt tests in intake/writers; mandatory title/reply tests |
| C13 | Both exact intake historical fingerprints, retired nonnull/unknown fields, other scopes, auth on replay; Go-written receipts replay after old package switch | `test_retired_null_and_exact_current_receipts_replay_without_rewriting_history`, `test_unproven_legacy_receipts_and_invalid_payloads_stay_rejected` |
| C14 | External BEGIN EXCLUSIVE and process gate saturation; interrupted waiter before/after batch transfer; failed COMMIT; normal request resumes in same process | `test_contention.py::test_interrupted_reader_does_not_leave_phantom_admission`, `test_failed_commit_rolls_back_and_releases_admission` |
| C15 | Exhaust DB workers/connections then BUSY error; 503 writer-independent serializer+Retry-After, permanent schema/read-only 500 visible; counts not leaked | `test_sqlite_busy_response_does_not_need_a_free_request_worker`, `test_admission_timeout_releases_request_threads_and_gate_counts` |
| C16 | Socket drops while dispatched handler waits for admission / midtransaction / after COMMIT: Python continues pending/admitted work despite disconnect; explicit interruption cleans/rolls back. Kill at receipt/continuation boundary; restart original key -> one/no effect, explicit unknown resolution | `test_contention.py::test_canceled_transaction_rolls_back_before_same_process_resumes`, existing crash probe; new disconnect barriers distinguish caller loss from operation cancellation |
| C17 | Captain overlap/current routing beyond recent page; stale/ending thresholds and intent transfer; outages retain identities; archival blocks/refusals don't clear work | shift/writers/muster tests; `captains.current_captain` |
| C18 | Reinforcement exactly 90m, delayed multi-interval, future acknowledgement, clock rollback, restart; greatest ack/counter survives, no compaction rotation | `test_shift.py::test_reinforcement_boundaries_and_clock_rollback`, `test_wake_reinforces_at_90_minutes_until_acknowledged_and_coalesces_missed_intervals` |
| C19 | Two scout page claimants, ten-minute expiry, settings supersede, explicit Captain ending, lost page response; one atomic intake/cursor/count and exact receipt | `test_issue_scout.py::test_single_claim_page_replay_and_captain_handoff_preserve_cursor` |
| C20 | Own login wrong numeric ID, other-author approve/edit/close, oversized body, approved issue passed earlier cursor, manual import and historical closed source; no unauthorized/duplicate/rerun | `test_stable_manual_receipt_survives_edited_context_without_duplicate_intake`, `test_known_unlinked_source_reconciles_later_manual_intake_key` |
| C21 | Cumulative usage zero/null/missing/overflow/reused session/late/end/wrong scope/currency; unavailable retains previous; unchanged versions/state/allocations | `test_usage.py::test_cumulative_replay_zero_partial_and_no_lifecycle_side_effects`, `test_late_observation_does_not_revive_ended_writer_or_advance_execution` |
| C22 | Deep/live paging, invalid/noncanonical/filter-mismatched cursors, empty filtered continuation, ETag deadlines/304 auth, details redaction and strict overdue boundary | `test_operator.py::test_deep_queue_pages_seek_to_cursor`, `test_backward_result_reasons_correlate_the_recorded_attempt_and_revision`, writer overdue tests |
| C23 | P0/D0->Go->previous package on evolved disposable file, newer receipts/active attempts/jobs/config/Markdown; Python-held lock excludes Go and Go-held excludes Python on same exact path/range/lock kind across supported OS; identify/refuse raw bypass launcher | `test_deployment.py::test_isolated_server_bootstrap_identity_restart_and_single_owner`, `test_store.py`, `test_bootstrap.py`; future cross-language lock/rollback matrix required |
| C24 | Browser/tokenless Host/peer/forwarding/header duplication/origin + malformed JSON/query/path/unknown fields + unchanged CLI/Scribe fixtures; literal/parameter routes, statics, escaping, 404/405/trailing-slash redirects | `test_operator_web.py`, `test_intake.py`, `test_dispatch.py`, `test_issue_scout.py::test_local_needs_you_decision_is_same_origin_and_no_queue_receipt` |

Test labels in rows referring to `test_*` methods without module qualifiers are `tests/test_writers.py` unless stated otherwise. Before executing a future matrix, resolve exact unittest discovery IDs and retain manifest; table is not a claimed exhaustive automated suite.

### 9.3 Measurements and acceptance gates proposed for owner review

Proposed executable migration acceptance gates below operationalize existing safety/data requirements, not a new authority grant. Existing assignment/canonical deployment invariants remain binding: no unauthorized mutations, duplicate accepted work, lost accepted writes, terminal rows without output, over-budget fresh admission, forgotten durable job/cursor, unexplained receipt/schema/typed-value differences. Package-only recoverable rollback and no old-snapshot restoration are already required by S6. Proposed zero unexpected 500/connection failures outside declared startup/fault windows is a conformance criterion for owner review; deliberately injected permanent faults remain visible 500 within their own window. No false final completeness/usage billing.

Proposed deployment/runtime goals, not measured achievements:

| Goal | Proposed measurement and threshold | Classification |
| --- | --- | --- |
| Admission recoverability | Each blocked waiter exits within 4s plus documented harness tolerance; DB busy within 2s plus tolerance; after release all gate/pool counters return baseline | Preserve current local bounds; total HTTP latency is separately measured |
| Mixed control latency | At 10,000 items, saved 5/10 writer pair, 8 repos, 32 concurrent mixed clients: authenticated status/progress/result/read p95 <=250ms, p99 <=1s outside fault windows | Owner-review target; not promise from goroutines |
| No-regression comparison | Each matched category throughput >=95% of Python; p95/p99 <=110% baseline; compare successful claims/continuations separately | Ordinary optimization unless owner marks a hard SLO |
| Restart recovery | Ready authenticated health within 10s; 10,000 due dependency events drain within 60s under fixed fixture concurrency, without losing checkpoint/jobs | Proposed operating targets; boot poll excluded from traffic violations |
| Runtime resources | Go steady RSS <=150 MiB in this workload; no growing goroutine/connection/admission count over 30m soak; report artifact size/cold start/CPU | Proposed constraints require agreed host/build, not universal bounds |
| Write durability | Every 2xx accepted mutation and any committed-before-response mutation is retained after owned crash/restart; receipt replay reproduces one effect | Hard, contingent filesystem flush guarantees documented |
| Package rollback | Previous package reads/mutates/replays evolved current disposable data and preserved active bindings; zero restoration of older snapshot | Hard rollout prerequisite |

Use monotonic duration timing and UTC persisted event clock. Include request queue, auth read, admission, connection/busy, SQL/commit and serialization phases. Report throughput count/status distribution, p50/p95/p99/max, CPU/RSS/GC/goroutines/connections and fault recovery, cold/warm startup and artifact footprint; health alone cannot establish control availability. Keep host CPU/IO load and priority comparable. Do not benchmark production or increase live writer limits to make a target pass.

For ordinary port-performance optimization, propose applying S5 decision 94's two-round improvement budget by analogy, subject to owner classification of these targets. Retain baseline/per-round manifest/digest and numeric remaining miss; different mixes unmatched. Existing correctness/rollback obligations cannot be waived by budget exhaustion. Owner reviews target classification or explicit requirement change; report all misses without claiming success.

## 10. Implementation and deployment plan for later owner review

This sequence is **not permission to execute**:

| Stage | Deliverable / exit evidence | No-go condition |
| --- | --- | --- |
| A: contract closure | D0/P0/runtime/schema inventory, decision ledger review, exact wire/durable corpus and compatibility deviations | Unknown installed revision/schema/authority or unresolved behavior that affects accepted writes |
| B: driver/build spike | Disposable canonical schema/EXTRA/FK/backup/error/admission/cancel build matrix and artifact metrics | Driver lacks required features/durability or unsupported hosting build |
| C: minimal service vertical slice | HTTP/auth/read/intake/receipts/transaction with unchanged client, then complete state/capacity/finalization/observations/projections/scouting/usage | Scope expands or partial state machine claimed deployable |
| D: differential complete | All C cases, every exported route/model family, matched workload, raw discrepancies reviewed | Safety mismatch, unexplained null/hash/replay changes, incomplete fault surface |
| E: package-switch rehearsal | Operator-selected disposable current-data copy, D0->Go->D0 proof, supported backup, exact sole-instance launcher/config/assets | Previous package cannot operate on evolved data |
| F: owner-controlled adoption | Existing supervisor pauses only necessary admissions, drains/stops exact API, fresh backup, one new service, same DB/URL/credentials/config, authenticated continuity | Unknown ownership, dual API, new schema without approval, native replacements |
| G: verify/observe/rollback | Genuine retained writer retries/progress and reconciler freshness, receipts/limits preserved, measured runtime goals; previous package rollback if needed | Any accepted writes lost/unknown; rollback incompatibility -> safely stop and owner recovery |

No one implements all this as one giant migration PR by default. Later reviewable slices can be separate artifact/implementation PRs only after owner authorizes implementation. Native clients/reconciler remain unchanged; a necessary server-launch packaging adapter is an explicit deployment deliverable, not permission to rewrite Python CLI/reconciler or add a second service.

Adoption preserves existing URL/DB/credentials/approved capacities/native sessions/reconciler scratch and identity/local-board configuration. Do not `deploy init`, rename muster directories, change discovery locator, auto-upgrade DB, or restart unrelated services. Keep authoritative production writer singular throughout. Port running-process lock behavior, log redaction, own-process priority policy and no-missing-DB safety. Rollback means previous executable/package/config on **current compatible data**, never previous backup over active work.

## 11. Prioritized collaborative decisions

| Priority | Owner-review decision | Recommended default / evidence needed |
| --- | --- | --- |
| P0 | Is Go approved in place of decision 13's historical Node target? | Go proposed; explicit recorded decision before implementation |
| P0 | Exact D0 source/schema/overlay and previous package rollback pair | Operator inventory; main/source and authenticated binding are insufficient |
| P0 | Freeze contract versus sanctioned error/encoding differences | Preserve P0/D0 and specific current owner intent; review duplicate/surrogate/framework/proxy cases |
| P0 | Mutation cancellation and unknown-commit/shutdown behavior | Dispatched handler work detached from caller cancel, including admission wait, to match Python; driver proof and service drain limit |
| P0 | Supported SQLite driver/engine/build/hosting platforms | Select after both candidates' minimal proof, not language aesthetics |
| P0 | New-data receipts replayable by previous deployed package | Full validated-body shape and null exception proof, not digest patch assumption |
| P1 | Numeric performance/resource goals and hard-versus-ordinary classification | Agree matched host/manifest and two-round ordinary budget; correctness/rollback hard |
| P1 | Preserve generated docs URLs and static packaging/service adapter | Prefer pinned assets and current URLs; no frontend or client rewrite |
| P1 | Resolve stale docs/current auth/projection nuances D11-D14 | Keep source oracle explicit; separately authorize any tightening |
| P2 | WAL, tenancy enforcement, schema internals redesign | Defer until compatible port is complete; separate requirements and owner review |

## 12. Validation and review provenance

Observed P0 disposable baseline checks in the supplied environment:

| Command | Result |
| --- | --- |
| `python -m unittest discover -s tests -p test_intake.py` | 19 tests, 35.416s, OK; deliberate missing-table fault logged an ASGI error as expected |
| `python -m unittest discover -s tests -p test_contention.py` | 10 tests, 16.380s, OK |
| `python -m unittest discover -s tests -p test_transitions.py` | 8 tests, 0.006s, OK |

37 tests passed, the sum of the three executed modules (19+10+8), not 37 completed migration C cases. Other modules are inspected/reference seeds, not claimed run. No full suite, benchmark, Go code, driver spike, deployed validation or rollback rehearsal was run. Their execution belongs to the future plan.

Independent bounded review was performed by `compatibility-spec-review` (rubber-duck agent), reviewing main specification and selective source/appendix evidence at digest `ed2112782ceef77a408f7f861738074b7ef7e7d77227bbfefcbdd2f13a9a49ee`. Initial disposition: **CHANGES_REQUIRED, wording corrections, no design-level blocker**. Material findings and resolutions:

| Finding | Resolution in this revision |
| --- | --- |
| Receipt replay can re-enter `finish=True` finalization and return current plain view or captured pending fallback | D15, route table, section 8.2, C04/C10 now specify both phases, response-shape variation and logged durable-intent fallback |
| Disconnect does not cancel Python dispatched work waiting for admission | Section 7.3, C16 and owner decision now preserve preadmission as well as admitted continuation; explicit interruption distinguished |
| Pool/quarantine design adds unnecessary complexity | Short-lived connection per transaction is baseline; pooling deferred to measured optimization |
| Sole-instance lock interoperability unspecified | Exact `.service.lock` path, POSIX flock/Windows offset-zero one-byte locking and bidirectional exclusion tests added |
| Hard performance gates/budget could read as approved | Existing safety/rollback obligations separated from proposed metrics; owner classification and budget-by-analogy explicit |
| Route precedence undocumented; manifests/docs feature scope too broad | C24 literal/static/escaped/404/405/redirect cases; manifests optional operator artifact; generated docs remains reviewed choice |

Review is bounded document review, not implementation approval. The reviewer did not validate every generated schema or run a Go comparison; those limits are retained. The author verified the revised source claims and addressed all six findings. A focused follow-up to the same reviewer was unavailable because synchronous review agents cannot receive follow-up messages; no duplicate reviewer was launched. Thus the final document is author-revised after independent review, not independently re-approved at its final digest. The final digest and this limitation are retained in submission evidence; report-review stage verifies that exact final revision.

### 12.1 Repository check-in provenance and source reconciliation

The preceding review and test results belong to the original report. They are not an independent approval of this repository revision or fresh check-in test results.

For this documentation check-in, fetched `origin/main` and checkout `HEAD` both resolved to P0 (`9af29396b37f86816fd274e01632164a47718a57`) on 2026-11-26. There was no source drift at that observation. Appendix A remains the pinned P0/R0 inventory, not a newly generated deployment inventory. Recheck current main before merging and retain any later source-drift assessment with the PR evidence.

The SQL inventory in section 5 now spells out all 30 actual table names from `phalanx/schema.py`; the original shorthand obscured `issue_sources` and `dependency_audit`. This is a documentation correction, not a schema change. The D11 development-guide receipt discrepancy remains explicit: the specific intake exception in `SPEC.md` and `intake.replayable` takes precedence over the guide's general "no old-digest fallback" wording. D13/D14 remain behavior-versus-policy distinctions, not silently tightened authorization or merge gates. The six original review improvements remain intact.

Initial check-in CI rejected historical PR references outside `docs/decisions.md` under the existing history-free documentation guard. Section 2.3's complete historical evidence table was therefore relocated unchanged into that log and linked here; no history was deleted, reference disguised, or guard relaxed. The documentation revision requires fresh exact-head independent review and CI after this organizational correction.

Independent, non-author specification/documentation review must name the exact final Git head, scope, reviewer identity and verdict, with resolved findings; that evidence is recorded separately in the documentation PR and execution artifacts. Required CI and the actual merge are likewise attached to that head rather than retroactively attributed to the original report. Approval is only for preserving a collaborative draft in the repository, not a Go implementation or service adoption.

## Appendix A. Generated P0 request/parameter contract

The appended inventory is generated from `create_app().openapi()` in P0/R0 without running startup or opening any database. It records 66 method/path operations and 51 named schemas. Request-body presence, query/path/header defaults/bounds and documented status/media types are retained. Pydantic cross-field/application checks and role dependencies remain defined by sections 4-9 and source; OpenAPI alone omits those semantics and most successful response object schemas.


### `GET /operator/api/repositories`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `POST /operator/api/items`

```json
{"parameters":[{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Intake"}}}},"responses":{"201":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /operator/api/dispatch/settings`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `PUT /operator/api/dispatch/settings`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/DispatchSettings"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /operator/api/issue-scout/{repository_id}/{number}/decision`

```json
{"parameters":[{"name":"repository_id","in":"path","required":true,"schema":{"type":"string","title":"Repository Id"}},{"name":"number","in":"path","required":true,"schema":{"type":"integer","title":"Number"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Decision"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /health`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `GET /v1/owner/status`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `GET /v1/runner/status`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `GET /v1/repositories`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `POST /v1/repositories`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/phalanx__intake_models__Registration"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items`

```json
{"parameters":[{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Intake"}}}},"responses":{"201":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/items/{item_id}`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/items/{item_id}/title`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `PUT /v1/items/{item_id}/title`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"Idempotency-Key","in":"header","required":true,"schema":{"type":"string","minLength":1,"maxLength":200,"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/DisplayTitle"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `PATCH /v1/items/{item_id}/executions/{execution_id}`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Steering"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/items/{item_id}/executions`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/items/{item_id}/executions/{execution_id}/timeline`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/items/{item_id}/executions/{execution_id}/drafts/{revision}`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"revision","in":"path","required":true,"schema":{"type":"string","pattern":"^[a-f0-9]{64}$","title":"Revision"}}],"responses":{"200":{"description":"Successful Response","content":{"text/plain":{"schema":{"type":"string"}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/cancel`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Cancel"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/close-failure`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Cancel"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/retry-finalization`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Versioned"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/rerun`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Rerun"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/result`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Result"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/stop-confirmed`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/AttemptEvent"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/owner/inbox`

```json
{"parameters":[{"name":"limit","in":"query","required":false,"schema":{"type":"integer","maximum":100,"minimum":1,"default":50,"title":"Limit"}},{"name":"after_time","in":"query","required":false,"schema":{"type":"string","maxLength":40,"default":"","title":"After Time"}},{"name":"after_id","in":"query","required":false,"schema":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"},{"type":"null"}],"title":"After Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/reply`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"Idempotency-Key","in":"header","required":true,"schema":{"type":"string","minLength":1,"maxLength":200,"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Reply"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/github-observation`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Observation"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/local-checks`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/LocalChecks"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/reconcilers/{runner_id}/heartbeat`

```json
{"parameters":[{"name":"runner_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Runner Id"}}],"requestBody":{"content":{"application/json":{"schema":{"anyOf":[{"$ref":"#/components/schemas/phalanx__reconciliation__Heartbeat"},{"type":"null"}],"title":"Body"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `DELETE /v1/reconcilers/{runner_id}`

```json
{"parameters":[{"name":"runner_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Runner Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/dispatch/settings`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `PUT /v1/dispatch/settings`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/DispatchSettings"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/operator/executions`

```json
{"parameters":[{"name":"kind","in":"query","required":false,"schema":{"enum":["open","closed"],"type":"string","default":"open","title":"Kind"}},{"name":"limit","in":"query","required":false,"schema":{"type":"integer","maximum":100,"minimum":1,"default":25,"title":"Limit"}},{"name":"cursor","in":"query","required":false,"schema":{"anyOf":[{"type":"string","maxLength":512},{"type":"null"}],"title":"Cursor"}},{"name":"repository","in":"query","required":false,"schema":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"},{"type":"null"}],"title":"Repository"}},{"name":"label","in":"query","required":false,"schema":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"},{"type":"null"}],"title":"Label"}},{"name":"status","in":"query","required":false,"schema":{"anyOf":[{"$ref":"#/components/schemas/Status"},{"type":"null"}],"title":"Status"}},{"name":"stage","in":"query","required":false,"schema":{"anyOf":[{"$ref":"#/components/schemas/Stage"},{"type":"null"}],"title":"Stage"}},{"name":"outcome","in":"query","required":false,"schema":{"anyOf":[{"enum":["report_stored","draft_published","ready_for_check_in","checked_in","failed","cancelled"],"type":"string"},{"type":"null"}],"title":"Outcome"}},{"name":"q","in":"query","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Q"}},{"name":"closed_after","in":"query","required":false,"schema":{"anyOf":[{"type":"string","format":"date-time"},{"type":"null"}],"title":"Closed After"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/operator/overview`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `GET /v1/operator/items/{item_id}`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/operator/groups/{label}`

```json
{"parameters":[{"name":"label","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$","title":"Label"}},{"name":"limit","in":"query","required":false,"schema":{"type":"integer","maximum":100,"minimum":1,"default":25,"title":"Limit"}},{"name":"cursor","in":"query","required":false,"schema":{"anyOf":[{"type":"string","maxLength":512},{"type":"null"}],"title":"Cursor"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/items/{item_id}/executions/{execution_id}/report`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}}],"responses":{"200":{"description":"Successful Response","content":{"text/plain":{"schema":{"type":"string"}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/operator/executions/{execution_id}/timeline`

```json
{"parameters":[{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"limit","in":"query","required":false,"schema":{"type":"integer","maximum":100,"minimum":1,"default":25,"title":"Limit"}},{"name":"cursor","in":"query","required":false,"schema":{"anyOf":[{"type":"string","maxLength":512},{"type":"null"}],"title":"Cursor"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/captains`

```json
{"parameters":[{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/CaptainRegistration"}}}},"responses":{"201":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/captains`

```json
{"parameters":[{"name":"limit","in":"query","required":false,"schema":{"type":"integer","maximum":100,"minimum":1,"default":25,"title":"Limit"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `PUT /v1/captains/{captain_id}`

```json
{"parameters":[{"name":"captain_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/phalanx__captains__Heartbeat"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/captains/{captain_id}`

```json
{"parameters":[{"name":"captain_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `PUT /v1/captains/{captain_id}/reinforcement`

```json
{"parameters":[{"name":"captain_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/ReinforcementAcknowledgement"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/captain-actions`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"Idempotency-Key","in":"header","required":false,"schema":{"anyOf":[{"type":"string","minLength":1,"maxLength":200},{"type":"null"}],"title":"Idempotency-Key"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Action"}}}},"responses":{"201":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/captains/current`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `GET /v1/session-archives`

```json
{"parameters":[{"name":"session_id","in":"query","required":true,"schema":{"type":"string","minLength":1,"maxLength":200,"title":"Session Id"}}],"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `PUT /v1/session-archives`

```json
{"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/SessionArchive"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/flags`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `PUT /v1/flags`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Flag"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/flags/clear`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/FlagKey"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/writer`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/phalanx__writers__Registration"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `PATCH /v1/items/{item_id}/executions/{execution_id}/writer`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Update"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/writer/withdraw`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Withdrawal"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/writer`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `POST /v1/writer/attempt`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `POST /v1/writer/progress`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Progress"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/writer/capacity`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Capacity"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/writer/usage`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Snapshot"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/items/{item_id}/executions/{execution_id}/writers/{writer_id}/usage`

```json
{"parameters":[{"name":"item_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Item Id"}},{"name":"execution_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Execution Id"}},{"name":"writer_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Writer Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/CaptainObservation"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `GET /v1/muster/status`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `GET /v1/issue-scout`

```json
{"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}}}
```

### `PUT /v1/issue-scout/settings`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Settings"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/issue-scout/claim`

```json
{"requestBody":{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Claim"}}},"required":true},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/issue-scout/passes/{pass_id}/page`

```json
{"parameters":[{"name":"pass_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Pass Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Page"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/issue-scout/passes/{pass_id}/failure`

```json
{"parameters":[{"name":"pass_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Pass Id"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Failure"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### `POST /v1/issue-scout/issues/{repository_id}/{number}/decision`

```json
{"parameters":[{"name":"repository_id","in":"path","required":true,"schema":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Repository Id"}},{"name":"number","in":"path","required":true,"schema":{"type":"integer","title":"Number"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Decision"}}}},"responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}},"422":{"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}}}
```

### Named schemas

Each entry is its exact JSON Schema from the pinned model export; references resolve in this collection.

#### Action

```json
{"properties":{"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"},"action":{"type":"string","maxLength":200,"minLength":1,"title":"Action"},"reason":{"type":"string","maxLength":16000,"minLength":1,"title":"Reason"}},"additionalProperties":false,"type":"object","required":["captain_id","action","reason"],"title":"Action"}
```

#### AttemptEvent

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"},"attempt_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Attempt Id"},"stage":{"$ref":"#/components/schemas/Stage"}},"additionalProperties":false,"type":"object","required":["expected_version","attempt_id","stage"],"title":"AttemptEvent"}
```

#### Cancel

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"},"reason":{"type":"string","maxLength":16000,"minLength":1,"title":"Reason"}},"additionalProperties":false,"type":"object","required":["expected_version","reason"],"title":"Cancel"}
```

#### Capacity

```json
{"properties":{"phase":{"type":"string","enum":["building","delivering"],"title":"Phase"},"expected_revision":{"type":"integer","minimum":0.0,"title":"Expected Revision"},"handoff_head":{"anyOf":[{"type":"string","pattern":"^[0-9a-f]{40}$"},{"type":"null"}],"title":"Handoff Head"}},"additionalProperties":false,"type":"object","required":["phase","expected_revision"],"title":"Capacity"}
```

#### CaptainObservation

```json
{"properties":{"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"},"snapshot":{"$ref":"#/components/schemas/Snapshot"}},"additionalProperties":false,"type":"object","required":["captain_id","snapshot"],"title":"CaptainObservation"}
```

#### CaptainRegistration

```json
{"properties":{"session_ref":{"type":"string","maxLength":16000,"minLength":1,"title":"Session Ref"},"charter_revision":{"type":"string","maxLength":200,"minLength":1,"title":"Charter Revision"},"parent_id":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"},{"type":"null"}],"title":"Parent Id"},"pickup":{"$ref":"#/components/schemas/Pickup"}},"additionalProperties":false,"type":"object","required":["session_ref","charter_revision"],"title":"CaptainRegistration"}
```

#### Claim

```json
{"properties":{"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"}},"additionalProperties":false,"type":"object","required":["captain_id"],"title":"Claim"}
```

#### Decision

```json
{"properties":{"expected_fingerprint":{"type":"string","pattern":"^[a-f0-9]{64}$","title":"Expected Fingerprint"},"decision":{"type":"string","enum":["approved","declined"],"title":"Decision"}},"additionalProperties":false,"type":"object","required":["expected_fingerprint","decision"],"title":"Decision"}
```

#### Deployment

```json
{"properties":{"commit":{"type":"string","pattern":"^[0-9a-f]{40}$","title":"Commit"},"check_name":{"type":"string","maxLength":255,"minLength":1,"title":"Check Name"},"state":{"type":"string","enum":["pending","failed","deployed","disabled"],"title":"State"}},"additionalProperties":false,"type":"object","required":["commit","check_name","state"],"title":"Deployment","description":"Optional check-run signal on the merge commit, not a delivery gate."}
```

#### DispatchSettings

```json
{"properties":{"paused":{"anyOf":[{"type":"boolean"},{"type":"null"}],"title":"Paused"},"max_writers":{"anyOf":[{"type":"integer","maximum":2147483647.0,"exclusiveMinimum":0.0},{"type":"null"}],"title":"Max Writers"},"max_building_writers":{"anyOf":[{"type":"integer","maximum":2147483647.0,"exclusiveMinimum":0.0},{"type":"null"}],"title":"Max Building Writers"}},"additionalProperties":false,"type":"object","title":"DispatchSettings"}
```

#### DisplayTitle

```json
{"properties":{"expected_revision":{"type":"integer","minimum":0.0,"title":"Expected Revision"},"title":{"anyOf":[{"type":"string","maxLength":200,"minLength":1,"pattern":"^[^\\r\\n]+$"},{"type":"null"}],"title":"Title"}},"additionalProperties":false,"type":"object","required":["expected_revision","title"],"title":"DisplayTitle"}
```

#### Failure

```json
{"properties":{"claim_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Claim Id"},"kind":{"type":"string","enum":["github","identity","credential","api"],"title":"Kind"},"http_status":{"anyOf":[{"type":"integer","maximum":599.0,"minimum":100.0},{"type":"null"}],"title":"Http Status"},"retry_after_seconds":{"type":"integer","maximum":604800.0,"minimum":0.0,"title":"Retry After Seconds","default":300}},"additionalProperties":false,"type":"object","required":["claim_id","kind"],"title":"Failure"}
```

#### Flag

```json
{"properties":{"kind":{"type":"string","pattern":"^[a-z][a-z0-9_]{0,63}$","title":"Kind"},"item_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{0,100}$","title":"Item Id","default":""},"execution_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{0,100}$","title":"Execution Id","default":""},"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{0,100}$","title":"Captain Id","default":""},"reason":{"type":"string","maxLength":16000,"minLength":1,"title":"Reason"},"recovery":{"type":"string","maxLength":16000,"minLength":1,"title":"Recovery"},"notify_owner":{"type":"boolean","title":"Notify Owner","default":false}},"additionalProperties":false,"type":"object","required":["kind","reason","recovery"],"title":"Flag"}
```

#### FlagKey

```json
{"properties":{"kind":{"type":"string","pattern":"^[a-z][a-z0-9_]{0,63}$","title":"Kind"},"item_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{0,100}$","title":"Item Id","default":""},"execution_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{0,100}$","title":"Execution Id","default":""},"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{0,100}$","title":"Captain Id","default":""}},"additionalProperties":false,"type":"object","required":["kind"],"title":"FlagKey"}
```

#### GateState

```json
{"type":"string","enum":["satisfied","not_configured","waiting","failed","blocked"],"title":"GateState"}
```

#### HTTPValidationError

```json
{"properties":{"detail":{"items":{"$ref":"#/components/schemas/ValidationError"},"type":"array","title":"Detail"}},"type":"object","title":"HTTPValidationError"}
```

#### Intake

```json
{"properties":{"repository_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Repository Id"},"goal":{"type":"string","maxLength":16000,"minLength":1,"title":"Goal"},"target":{"$ref":"#/components/schemas/Target","default":"checked_in"},"context_links":{"items":{"type":"string","maxLength":16000,"minLength":1},"type":"array","maxItems":50,"title":"Context Links"},"acceptance_criteria":{"items":{"type":"string","maxLength":16000,"minLength":1},"type":"array","maxItems":50,"title":"Acceptance Criteria"},"target_branch":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Target Branch"},"priority":{"type":"string","enum":["low","normal","urgent"],"title":"Priority","default":"normal"},"label":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"},{"type":"null"}],"title":"Label"},"dependencies":{"items":{"$ref":"#/components/schemas/Prerequisite"},"type":"array","maxItems":100,"title":"Dependencies"},"publication_mode":{"$ref":"#/components/schemas/PublicationMode","default":"ready"}},"additionalProperties":false,"type":"object","required":["repository_id","goal"],"title":"Intake"}
```

#### Issue

```json
{"properties":{"number":{"type":"integer","exclusiveMinimum":0.0,"title":"Number"},"author_login":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9-]{0,92}(?:\\[bot\\])?$"},{"type":"null"}],"title":"Author Login"},"author_id":{"anyOf":[{"type":"integer","exclusiveMinimum":0.0},{"type":"null"}],"title":"Author Id"},"state":{"type":"string","enum":["open","closed"],"title":"State"},"title":{"type":"string","maxLength":16000,"minLength":1,"title":"Title"},"body":{"type":"string","maxLength":1000000,"title":"Body"},"updated_at":{"type":"string","format":"date-time","title":"Updated At"}},"additionalProperties":false,"type":"object","required":["number","author_login","author_id","state","title","body","updated_at"],"title":"Issue"}
```

#### LocalChecks

```json
{"properties":{"head":{"type":"string","pattern":"^[0-9a-f]{40}$","title":"Head"},"failures":{"items":{"type":"string","maxLength":4000,"minLength":1},"type":"array","maxItems":50,"title":"Failures"}},"additionalProperties":false,"type":"object","required":["head"],"title":"LocalChecks","description":"Trusted reconciler checks on one observed PR head. Empty failures means they passed."}
```

#### Observation

```json
{"properties":{"observed_at":{"type":"string","format":"date-time","title":"Observed At"},"pr":{"anyOf":[{"$ref":"#/components/schemas/ObservedPull"},{"type":"null"}]},"checks":{"anyOf":[{"$ref":"#/components/schemas/GateState"},{"type":"null"}]},"check_reasons":{"items":{"type":"string","maxLength":4000,"minLength":1},"type":"array","maxItems":20,"title":"Check Reasons"},"feedback":{"items":{"type":"string","maxLength":4000,"minLength":1},"type":"array","maxItems":50,"title":"Feedback"},"merged_branch":{"type":"boolean","title":"Merged Branch","default":false},"target_base":{"anyOf":[{"type":"string","maxLength":255,"minLength":1},{"type":"null"}],"title":"Target Base"},"deployment":{"anyOf":[{"$ref":"#/components/schemas/Deployment"},{"type":"null"}]},"duplicate_count":{"anyOf":[{"type":"integer","minimum":0.0},{"type":"null"}],"title":"Duplicate Count"}},"additionalProperties":false,"type":"object","required":["observed_at"],"title":"Observation"}
```

#### ObservedPull

```json
{"properties":{"number":{"type":"integer","minimum":1.0,"title":"Number"},"state":{"type":"string","enum":["open","closed"],"title":"State"},"merged":{"type":"boolean","title":"Merged"},"draft":{"type":"boolean","title":"Draft"},"head_ref":{"type":"string","maxLength":255,"minLength":1,"title":"Head Ref"},"head_sha":{"type":"string","pattern":"^[0-9a-f]{40}$","title":"Head Sha"},"base_ref":{"type":"string","maxLength":255,"minLength":1,"title":"Base Ref"},"base_sha":{"type":"string","pattern":"^[0-9a-f]{40}$","title":"Base Sha"},"merge_commit":{"anyOf":[{"type":"string","pattern":"^[0-9a-f]{40}$"},{"type":"null"}],"title":"Merge Commit"},"mergeable":{"anyOf":[{"type":"boolean"},{"type":"null"}],"title":"Mergeable"},"merge_method":{"anyOf":[{"type":"string","enum":["merge","squash_or_rebase"]},{"type":"null"}],"title":"Merge Method"},"merged_at":{"anyOf":[{"type":"string","format":"date-time"},{"type":"null"}],"title":"Merged At"},"merged_head_ancestor":{"anyOf":[{"type":"string","pattern":"^[0-9a-f]{40}$"},{"type":"null"}],"title":"Merged Head Ancestor"},"additions":{"anyOf":[{"type":"integer","minimum":0.0},{"type":"null"}],"title":"Additions"},"deletions":{"anyOf":[{"type":"integer","minimum":0.0},{"type":"null"}],"title":"Deletions"}},"additionalProperties":false,"type":"object","required":["number","state","merged","draft","head_ref","head_sha","base_ref","base_sha"],"title":"ObservedPull"}
```

#### Outcome

```json
{"type":"string","enum":["succeeded","changes_required","needs_input","failed"],"title":"Outcome"}
```

#### Page

```json
{"properties":{"claim_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Claim Id"},"repository_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Repository Id"},"page":{"type":"integer","exclusiveMinimum":0.0,"title":"Page"},"verified_login":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$","title":"Verified Login"},"verified_id":{"type":"integer","exclusiveMinimum":0.0,"title":"Verified Id"},"issues":{"items":{"$ref":"#/components/schemas/Issue"},"type":"array","maxItems":30,"title":"Issues"},"has_more":{"type":"boolean","title":"Has More"}},"additionalProperties":false,"type":"object","required":["claim_id","repository_id","page","verified_login","verified_id","issues","has_more"],"title":"Page"}
```

#### Pickup

```json
{"properties":{"repositories":{"patternProperties":{"^[A-Za-z0-9_-]{1,100}$":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}]}},"type":"object","maxProperties":100,"title":"Repositories"}},"additionalProperties":false,"type":"object","title":"Pickup"}
```

#### Prerequisite

```json
{"properties":{"upstream_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Upstream Id"},"milestone":{"$ref":"#/components/schemas/Target","default":"checked_in"}},"additionalProperties":false,"type":"object","required":["upstream_id"],"title":"Prerequisite"}
```

#### Progress

```json
{"properties":{"note":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Note"}},"additionalProperties":false,"type":"object","title":"Progress"}
```

#### PublicationMode

```json
{"type":"string","enum":["ready","draft"],"title":"PublicationMode"}
```

#### Quantity

```json
{"properties":{"amount":{"type":"string","pattern":"^(0|[1-9][0-9]{0,14})(\\.[0-9]{1,9})?$","title":"Amount"},"unit":{"type":"string","maxLength":200,"minLength":1,"title":"Unit"},"basis":{"type":"string","enum":["provider_reported","estimated"],"title":"Basis"},"source":{"type":"string","maxLength":200,"minLength":1,"title":"Source"}},"additionalProperties":false,"type":"object","required":["amount","unit","basis","source"],"title":"Quantity"}
```

#### Question

```json
{"properties":{"prompt":{"type":"string","maxLength":16000,"minLength":1,"title":"Prompt"},"options":{"items":{"type":"string","maxLength":16000,"minLength":1},"type":"array","maxItems":20,"title":"Options"},"proposed_default":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Proposed Default"},"handoff":{"type":"string","maxLength":16000,"minLength":1,"title":"Handoff"}},"additionalProperties":false,"type":"object","required":["prompt","handoff"],"title":"Question"}
```

#### ReinforcementAcknowledgement

```json
{"properties":{"interval":{"type":"integer","minimum":1.0,"title":"Interval"}},"additionalProperties":false,"type":"object","required":["interval"],"title":"ReinforcementAcknowledgement"}
```

#### Reply

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"},"answer":{"type":"string","maxLength":16000,"minLength":1,"title":"Answer"},"answered_by":{"type":"string","enum":["owner","captain"],"title":"Answered By","default":"owner"}},"additionalProperties":false,"type":"object","required":["expected_version","answer"],"title":"Reply"}
```

#### Rerun

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"},"target":{"$ref":"#/components/schemas/Target"}},"additionalProperties":false,"type":"object","required":["expected_version","target"],"title":"Rerun"}
```

#### Result

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"},"attempt_id":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"},{"type":"null"}],"title":"Attempt Id"},"stage":{"$ref":"#/components/schemas/Stage"},"outcome":{"$ref":"#/components/schemas/Outcome"},"retry_after_seconds":{"anyOf":[{"type":"integer","maximum":1000000000000.0,"minimum":0.0},{"type":"null"}],"title":"Retry After Seconds"},"wait":{"anyOf":[{"$ref":"#/components/schemas/WaitInput"},{"type":"null"}]},"question":{"anyOf":[{"$ref":"#/components/schemas/Question"},{"type":"null"}]},"failure_reason":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Failure Reason"},"candidate":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Candidate"},"report_markdown":{"anyOf":[{"type":"string","maxLength":256000,"minLength":1,"pattern":"\\S"},{"type":"null"}],"title":"Report Markdown"},"report_revision":{"anyOf":[{"type":"string","pattern":"^[a-f0-9]{64}$"},{"type":"null"}],"title":"Report Revision"},"report_review_notes":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Report Review Notes"},"reviewed_candidate":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Reviewed Candidate"},"review_notes":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Review Notes"},"pr_url":{"anyOf":[{"type":"string","pattern":"^https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]{0,9}$"},{"type":"null"}],"title":"Pr Url"}},"additionalProperties":false,"type":"object","required":["expected_version","stage","outcome"],"title":"Result"}
```

#### Session

```json
{"properties":{"id":{"type":"string","maxLength":200,"minLength":1,"title":"Id"},"name":{"type":"string","maxLength":200,"minLength":1,"title":"Name"},"writer_nonce":{"anyOf":[{"type":"string","maxLength":200,"minLength":1},{"type":"null"}],"title":"Writer Nonce"},"availability":{"anyOf":[{"type":"string","enum":["present","missing","deleted"]},{"type":"null"}],"title":"Availability"},"runtime":{"anyOf":[{"type":"string","maxLength":200,"minLength":1},{"type":"null"}],"title":"Runtime"}},"additionalProperties":false,"type":"object","required":["id","name"],"title":"Session","description":"A native session the Captain can see, by its runtime ID and display name."}
```

#### SessionArchive

```json
{"properties":{"session_id":{"type":"string","maxLength":200,"minLength":1,"title":"Session Id"},"outcome":{"type":"string","enum":["archived","blocked","refused"],"title":"Outcome"},"reason":{"type":"string","maxLength":16000,"minLength":1,"title":"Reason"},"reported_by":{"type":"string","maxLength":200,"minLength":1,"title":"Reported By"}},"additionalProperties":false,"type":"object","required":["session_id","outcome","reason","reported_by"],"title":"SessionArchive"}
```

#### Settings

```json
{"properties":{"expected_revision":{"type":"integer","minimum":0.0,"title":"Expected Revision"},"enabled":{"type":"boolean","title":"Enabled","default":false},"cadence_minutes":{"type":"integer","enum":[15,30,60,180,360,720,1440],"title":"Cadence Minutes","default":60},"author_login":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$","title":"Author Login"},"author_id":{"type":"integer","exclusiveMinimum":0.0,"title":"Author Id"},"repositories":{"items":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"},"type":"array","maxItems":100,"minItems":1,"title":"Repositories"}},"additionalProperties":false,"type":"object","required":["expected_revision","author_login","author_id","repositories"],"title":"Settings"}
```

#### Snapshot

```json
{"properties":{"runtime":{"type":"string","maxLength":200,"minLength":1,"title":"Runtime"},"provider":{"type":"string","maxLength":200,"minLength":1,"title":"Provider"},"account":{"type":"string","maxLength":200,"minLength":1,"title":"Account"},"origin":{"type":"string","maxLength":200,"minLength":1,"title":"Origin"},"session_id":{"type":"string","maxLength":200,"minLength":1,"title":"Session Id"},"source":{"type":"string","maxLength":200,"minLength":1,"title":"Source"},"source_version":{"type":"string","maxLength":200,"minLength":1,"title":"Source Version"},"epoch":{"type":"string","maxLength":200,"minLength":1,"title":"Epoch"},"model":{"anyOf":[{"type":"string","maxLength":200,"minLength":1},{"type":"null"}],"title":"Model"},"sequence":{"type":"integer","maximum":9007199254740991.0,"minimum":0.0,"title":"Sequence"},"window_start":{"type":"string","format":"date-time","title":"Window Start"},"cutoff":{"type":"string","format":"date-time","title":"Cutoff"},"observed_at":{"type":"string","format":"date-time","title":"Observed At"},"scope":{"type":"string","enum":["item_exclusive","session_lifetime"],"title":"Scope"},"status":{"type":"string","enum":["observed","partial","unavailable","pending_final"],"title":"Status"},"reason":{"type":"string","maxLength":1000,"minLength":1,"title":"Reason"},"tokens":{"$ref":"#/components/schemas/Tokens"},"credits":{"anyOf":[{"$ref":"#/components/schemas/Quantity"},{"type":"null"}]},"money":{"anyOf":[{"$ref":"#/components/schemas/Quantity"},{"type":"null"}]}},"additionalProperties":false,"type":"object","required":["runtime","provider","account","origin","session_id","source","source_version","epoch","sequence","window_start","cutoff","observed_at","scope","status","reason"],"title":"Snapshot"}
```

#### Stage

```json
{"type":"string","enum":["development","self_review","report_drafting","report_review","report_storage","publication","delivery"],"title":"Stage"}
```

#### Status

```json
{"type":"string","enum":["queued","running","waiting_for_input","waiting_for_external","completed","failed","cancelled"],"title":"Status"}
```

#### Steering

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"},"goal":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Goal"},"context_links":{"anyOf":[{"items":{"type":"string","maxLength":16000,"minLength":1},"type":"array","maxItems":50},{"type":"null"}],"title":"Context Links"},"acceptance_criteria":{"anyOf":[{"items":{"type":"string","maxLength":16000,"minLength":1},"type":"array","maxItems":50},{"type":"null"}],"title":"Acceptance Criteria"},"priority":{"anyOf":[{"type":"string","enum":["low","normal","urgent"]},{"type":"null"}],"title":"Priority"},"label":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"},{"type":"null"}],"title":"Label"},"dependencies":{"anyOf":[{"items":{"$ref":"#/components/schemas/Prerequisite"},"type":"array","maxItems":100},{"type":"null"}],"title":"Dependencies"},"target":{"anyOf":[{"$ref":"#/components/schemas/Target"},{"type":"null"}]},"steered_by":{"type":"string","enum":["owner","captain"],"title":"Steered By","default":"owner"}},"additionalProperties":false,"type":"object","required":["expected_version"],"title":"Steering"}
```

#### Target

```json
{"type":"string","enum":["report","ready_for_check_in","checked_in"],"title":"Target"}
```

#### Tokens

```json
{"properties":{"input":{"anyOf":[{"type":"integer","maximum":9007199254740991.0,"minimum":0.0},{"type":"null"}],"title":"Input"},"output":{"anyOf":[{"type":"integer","maximum":9007199254740991.0,"minimum":0.0},{"type":"null"}],"title":"Output"},"cache_read":{"anyOf":[{"type":"integer","maximum":9007199254740991.0,"minimum":0.0},{"type":"null"}],"title":"Cache Read"},"cache_write":{"anyOf":[{"type":"integer","maximum":9007199254740991.0,"minimum":0.0},{"type":"null"}],"title":"Cache Write"},"reasoning":{"anyOf":[{"type":"integer","maximum":9007199254740991.0,"minimum":0.0},{"type":"null"}],"title":"Reasoning"},"total":{"anyOf":[{"type":"integer","maximum":9007199254740991.0,"minimum":0.0},{"type":"null"}],"title":"Total"}},"additionalProperties":false,"type":"object","title":"Tokens"}
```

#### Update

```json
{"properties":{"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"},"writer_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Writer Id"},"session_id":{"anyOf":[{"type":"string","maxLength":200,"minLength":1},{"type":"null"}],"title":"Session Id"},"session_url":{"anyOf":[{"type":"string","pattern":"^[a-z][a-z0-9+.-]{0,31}://\\S{1,2000}$"},{"type":"null"}],"title":"Session Url"},"state":{"anyOf":[{"type":"string","enum":["busy","idle","stopped","error"]},{"type":"null"}],"title":"State"},"abandon":{"type":"boolean","title":"Abandon","default":false}},"additionalProperties":false,"type":"object","required":["captain_id","writer_id"],"title":"Update"}
```

#### ValidationError

```json
{"properties":{"loc":{"items":{"anyOf":[{"type":"string"},{"type":"integer"}]},"type":"array","title":"Location"},"msg":{"type":"string","title":"Message"},"type":{"type":"string","title":"Error Type"},"input":{"title":"Input"},"ctx":{"type":"object","title":"Context"}},"type":"object","required":["loc","msg","type"],"title":"ValidationError"}
```

#### Versioned

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"}},"additionalProperties":false,"type":"object","required":["expected_version"],"title":"Versioned"}
```

#### WaitInput

```json
{"properties":{"reason":{"type":"string","maxLength":16000,"minLength":1,"title":"Reason"},"trigger":{"type":"string","maxLength":16000,"minLength":1,"title":"Trigger"}},"additionalProperties":false,"type":"object","required":["reason","trigger"],"title":"WaitInput"}
```

#### Withdrawal

```json
{"properties":{"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"},"writer_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Writer Id"},"reason":{"type":"string","maxLength":16000,"minLength":1,"title":"Reason"},"stopped":{"type":"boolean","title":"Stopped","default":false}},"additionalProperties":false,"type":"object","required":["captain_id","writer_id","reason"],"title":"Withdrawal"}
```

#### phalanx__captains__Heartbeat

```json
{"properties":{"charter_revision":{"type":"string","maxLength":200,"minLength":1,"title":"Charter Revision"},"wake_count":{"type":"integer","minimum":0.0,"title":"Wake Count"},"compaction_count":{"anyOf":[{"type":"integer","maximum":9007199254740991.0,"minimum":0.0},{"type":"null"}],"title":"Compaction Count"},"state":{"type":"string","enum":["active","ending","ended"],"title":"State"},"focus":{"items":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"},"type":"array","maxItems":100,"title":"Focus"},"sessions":{"items":{"$ref":"#/components/schemas/Session"},"type":"array","maxItems":500,"title":"Sessions"},"handoff_note":{"anyOf":[{"type":"string","maxLength":16000,"minLength":1},{"type":"null"}],"title":"Handoff Note"},"pickup":{"anyOf":[{"$ref":"#/components/schemas/Pickup"},{"type":"null"}]}},"additionalProperties":false,"type":"object","required":["charter_revision","wake_count","state"],"title":"Heartbeat"}
```

#### phalanx__intake_models__Registration

```json
{"properties":{"id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Id"},"remote":{"type":"string","maxLength":16000,"minLength":1,"title":"Remote"}},"additionalProperties":false,"type":"object","required":["id","remote"],"title":"Registration"}
```

#### phalanx__reconciliation__Heartbeat

```json
{"properties":{"deployment_checks":{"patternProperties":{"^[A-Za-z0-9_-]{1,100}$":{"anyOf":[{"type":"string","maxLength":255,"minLength":1},{"type":"null"}]}},"type":"object","title":"Deployment Checks"}},"additionalProperties":false,"type":"object","title":"Heartbeat"}
```

#### phalanx__writers__Registration

```json
{"properties":{"expected_version":{"type":"integer","minimum":1.0,"title":"Expected Version"},"captain_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$","title":"Captain Id"},"runtime":{"type":"string","maxLength":200,"minLength":1,"title":"Runtime"},"nonce":{"type":"string","pattern":"^[A-Za-z0-9_-]{16,100}$","title":"Nonce"},"token_digest":{"type":"string","pattern":"^[a-f0-9]{64}$","title":"Token Digest"},"replaces":{"anyOf":[{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"},{"type":"null"}],"title":"Replaces"},"replaced_stopped":{"type":"boolean","title":"Replaced Stopped","default":false}},"additionalProperties":false,"type":"object","required":["expected_version","captain_id","runtime","nonce","token_digest"],"title":"Registration"}
```

## Appendix B. Executed baseline manifest

37 IDs; SHA-256 of sorted UTF-8 IDs, newline terminated: `29b3c3e9049639e3fd5dad583d3f7627d3cc44eb83cd7718bdd698ae03935179`.

```text
test_contention.ContentionTests.test_admission_timeout_releases_request_threads_and_gate_counts
test_contention.ContentionTests.test_background_finalization_keeps_intent_through_external_lock
test_contention.ContentionTests.test_canceled_transaction_rolls_back_before_same_process_resumes
test_contention.ContentionTests.test_failed_commit_rolls_back_and_releases_admission
test_contention.ContentionTests.test_http_external_sqlite_lock_recovers_without_restarting
test_contention.ContentionTests.test_http_writer_contention_is_retryable_and_recovers_without_restarting
test_contention.ContentionTests.test_interrupted_reader_does_not_leave_phantom_admission
test_contention.ContentionTests.test_refresh_retries_external_sqlite_lock_without_losing_its_audit
test_contention.ContentionTests.test_refresh_retries_real_admission_failure_without_losing_its_audit
test_contention.ContentionTests.test_sqlite_busy_response_does_not_need_a_free_request_worker
test_intake.IntakeTest.test_busy_store_returns_retryable_503
test_intake.IntakeTest.test_cancel_active_rerun_and_preserve_history
test_intake.IntakeTest.test_cancel_before_first_claim
test_intake.IntakeTest.test_cancel_storage_error_rolls_back_and_recovers_after_restart
test_intake.IntakeTest.test_captain_answered_reply_resumes_and_records_who_answered
test_intake.IntakeTest.test_cli_cancel_uses_current_execution_and_version
test_intake.IntakeTest.test_failed_attempt_ends_and_stalled_storage_can_be_retried
test_intake.IntakeTest.test_input_wait_reply_survives_restart_and_queues_same_stage_once
test_intake.IntakeTest.test_intake_restart_idempotency_auth_and_cli
test_intake.IntakeTest.test_label_cli_intake_steering_and_clear
test_intake.IntakeTest.test_pr_intake_defaults_to_checked_in_and_keeps_explicit_targets
test_intake.IntakeTest.test_pr_result_rejects_fabricated_provider_evidence_and_stale_fence
test_intake.IntakeTest.test_receipt_fingerprints_the_complete_validated_intake
test_intake.IntakeTest.test_report_review_loop_and_verified_output_survive_restart
test_intake.IntakeTest.test_retired_null_and_exact_current_receipts_replay_without_rewriting_history
test_intake.IntakeTest.test_retired_null_hash_match_is_intake_only_and_does_not_bypass_auth
test_intake.IntakeTest.test_title_metadata_is_owner_only_fenced_and_does_not_change_active_or_closed_work
test_intake.IntakeTest.test_trusted_success_can_open_a_human_wait_without_worker_question
test_intake.IntakeTest.test_unproven_legacy_receipts_and_invalid_payloads_stay_rejected
test_transitions.TransitionTests.test_delivery_owner_question_retains_scope_and_cannot_turn_a_miss_into_success
test_transitions.TransitionTests.test_draft_delivery_preserves_the_reviewed_head_across_close_and_reopen
test_transitions.TransitionTests.test_exhausted_retry_waits_for_a_reply_at_the_same_stage
test_transitions.TransitionTests.test_open_or_closed_state_cannot_violate_final_output_invariant
test_transitions.TransitionTests.test_report_review_loop_and_storage_retry
test_transitions.TransitionTests.test_running_failure_requires_reconciliation_and_stored_report
test_transitions.TransitionTests.test_unclaimed_cancel_waits_for_disposition_storage
test_transitions.TransitionTests.test_writer_publishes_and_github_observations_deliver
```
