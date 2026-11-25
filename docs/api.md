# API reference

The HTTP API is the only path to queue state. Owner routes use the owner bearer token,
Captain/reconciler routes use the trusted runner token, and writer routes use a scoped writer
bearer token. All routes below are under `/v1` unless shown in full.

## Direct muster status

`GET /v1/muster/status` requires an owner or runner bearer token, even with the local board
enabled. It returns `status`, `identity.home` (the API's resolved `PHALANX_HOME`, or null when
not configured), live `captains`, registered open `writers`, `reconcilers` with `last_seen`,
`age_seconds` and `stale`, `dispatch_paused`, and `observed_at`. It is a read-only snapshot,
contains no credentials and performs no local client database reads. See
[muster discovery and status](../README.md#direct-muster-discovery-and-status).

## Current Captain routing

`GET /captains/current` uses the same board-read authentication as Captain detail/list reads.
It resolves across all registrations, independent of the recent-history page limit, and returns
`observed_at`, `candidates` (live `active` registrations as `{id, session_ref}`), `captain` (the
unique candidate, or null), and an explicit `reason`. Ending, ended and stale registrations
are excluded; multiple active candidates or an absent native binding cannot yield a destination.
This is a routing snapshot, not an exclusive Captain lease or a native link verifier. The caller
must freshly inspect the exact `session_ref` in its native provider, obtain its conversation URL
there, and re-read this snapshot before forwarding. No URL is synthesized by the API.

## Captain compaction telemetry

`PUT /captains/{id}` accepts optional `compaction_count`: the native session's cumulative
successful context compactions, a nonnegative integer no greater than 9007199254740991.
The reporting Captain must read an explicit native counter, not infer one from wakes,
tokens, checkpoints or failed attempts. Omit it when telemetry is unavailable or incomplete;
only report zero when explicitly known. Existing heartbeat callers need no changes.

Captain detail/list and muster status reads include `compaction_count` (`null` until known).
The server stores the greatest reported total, so duplicate/out-of-order reports never count
a compaction twice or reduce the count. Omission or `null` preserves a known value, including
across service restarts. A successor has its own unknown count, not its parent's total.
Charter revision, wake counts and liveness retain their existing contracts.

`phx shift heartbeat --captain <id> --compaction-count <count>` reports the same counter;
normal dashboard refreshes display it as `compactions N` or `compactions unavailable`.
`shift heartbeat` returns `compaction_count` and `wakes` as telemetry only. `handoff_due`
compares elapsed time from the registration's `created_at` with charter `[shift].max_hours`
(default 24), never wake or compaction counts. Compaction is a checkpoint, with no count-based
handoff or first-compaction fallback. Five-minute reconciliation continues through queue
completion and dependency/external waits. Explicit owner stop and actual degradation/failure
safeguards remain independent; the server's telemetry contract is unchanged.

## Captain workflow reinforcement

Captain detail/list and heartbeat responses include `reinforcement`: `interval_minutes` (90),
the current registration-relative `interval`, `acknowledged_interval` and `due`. Only an
active Captain with a newer interval is due; server UTC determines elapsed time, not the
client clock. Negative elapsed time is clamped to interval zero.

`shift wake --captain <id>` counts one wake and returns the fresh charter and compact
briefing. When due it includes the current canonical workflow and acknowledgement CLI
arguments. Read the workflow before using those arguments.
`PUT /captains/{id}/reinforcement` takes `{"interval": N}` under coordinator authorization.
N must be a positive strict integer and not a future unacknowledged interval (409).
The acknowledged interval only increases, so duplicate/late acknowledgements, including
retries after clock rollback, cannot undo progress. Heartbeats never acknowledge reminders.
Failed or unread output remains due; missed intervals coalesce on the next wake. No
additional runtime session, local timing file or scheduler is involved.

## Session archival records

Captain heartbeat `sessions` entries contain native `id`, display `name` and optional
`writer_nonce`, the full launch nonce verified from the session's kickoff/metadata, never its
credential/token. Registered session IDs and full nonces identify writers independently of
task-first or renamed titles. Without `writer_nonce`, legacy names still match full nonces or
item ID prefixes. Unknown structured nonces do not fall back to potentially misleading titles.
Captain reads preserve these fields and the server's `first_seen`.

The runtime session tools perform archival. The API only records and exposes evidence:

| Route | Access | Purpose |
| --- | --- | --- |
| `GET /session-archives?session_id=ID` | Owner or runner | Returns `record` (or null), registered `writers`, and `active`: whether a writer, Captain handoff or creator cleanup is still unfinished |
| `PUT /session-archives` | Owner or runner | Records `session_id`, `outcome` (`archived`, `blocked`, `refused`), `reason` and `reported_by` (the reporting native session ID) |

An `archived` write is refused while the registered writer is active, or while its Captain has
not ended, still owns intents/writer cleanup, or has neither a registered successor nor a charter
stop note. The caller verifies creator authority, runtime termination and local-work preservation
immediately before archival with `shift archive-check`; the API does not access local Git or call
any runtime. An existing `archived` record is immutable. A `refused` record cannot be overwritten
by another block/refusal, only by a separately verified `archived` confirmation. Blocks and refusals
derive a persistent `orphan_session` flag with the reason, even without a current session inventory.

Captain reads (`GET /captains` and `GET /captains/{id}`) include `cleanup_pending` and up to 100
`ended_writers` needing cleanup, untried then least recently checked first, with their item, execution and session IDs and any
local-work block. Archived/refused writers leave this actionable list; refusals remain visible
through their flags and individual archival record. These records do not advance work or clear
other execution flags. See [creator-owned cleanup](direct-path.md#creator-owned-session-cleanup).

## Work items and executions

| Route | Access | Purpose |
| --- | --- | --- |
| `POST /repositories` | Owner | Register an ID and expected remote. Repeating the same registration returns it. |
| `GET /repositories` | Owner or runner | Read registered repositories. |
| `POST /items` | Owner | File a new item with an initial queued execution. |
| `GET /items/{item_id}` | Owner or runner | Read the request and current execution, including its version, stage, status, wait and output. |
| `GET /items/{item_id}/title` | Owner | Read display-title metadata, including its independent revision (initially 0). |
| `PUT /items/{item_id}/title` | Owner | Set or clear only the display title with `expected_revision`, `title` and a required `Idempotency-Key`. |
| `GET /items/{item_id}/executions` | Owner or runner | Read past and current executions. |
| `GET /items/{item_id}/executions/{execution_id}/timeline` | Owner or runner | Read durable transition history and evidence. |
| `GET /items/{item_id}/executions/{execution_id}/drafts/{revision}` | Owner or runner | Retrieve the exact draft for review. |
| `GET /items/{item_id}/executions/{execution_id}/report` | Owner or runner | Retrieve the final stored Markdown. |
| `POST /items/{item_id}/executions/{execution_id}/cancel` | Owner | Request cancellation with `expected_version` and `reason`. |
| `POST /items/{item_id}/executions/{execution_id}/rerun` | Owner | Create a linked execution from the current closed execution with `expected_version` and an explicit `target`. |
| `POST /items/{item_id}/executions/{execution_id}/result` | Writer | Submit `outcome`, stage-specific evidence and the current attempt preconditions. |
| `POST /items/{item_id}/executions/{execution_id}/stop-confirmed` | Writer | Confirm a cancellation under the current writer binding after accounting for the worker and its effects. |

Intake requires `repository_id` and `goal`; `target` defaults to `checked_in`, preserving explicit `ready_for_check_in` and `report` requests. Optional fields include `context_links`, `acceptance_criteria`, `target_branch` and `priority` (`low`, `normal` or `urgent`). Use an `Idempotency-Key` header when retrying a mutation. The server returns the original response for a replay of the same validated request in its operation scope, or HTTP 409 for a different request under the same key. Registration is keyed by the repository ID and unique remote. Version conflicts also return 409. Versions start at 1 for each execution. A rerun preserves the prior execution and keeps the item's output type.

The display-title route accepts a single-line trimmed title of 1-200 characters, or `null`
to restore the goal-derived title. Its own `expected_revision` prevents concurrent title
edits; stale revisions return 409. It returns `item_id`, `revision`, `title`, `changed_by`
(`owner`) and `created_at`. Successful writes append an audit row, and exact retries replay
the original receipt. No execution/attempt/writer/scope/output is changed, including for
running or completed work. This is not queued-only goal Steering. It remains bearer-owner
only even on a tokenless local board; browser writes require same origin.

Scribe current-item and execution-timeline detail responses include only the stored
`acceptance_criteria` and `context_links` lists beside the existing projected item. Missing
lists are `[]`. Queue/group summaries omit those lists. The board does not read arbitrary
request/result fields to construct a brief. Display titles apply to all item executions,
including history, search and prerequisites; absent/cleared metadata uses the derived goal.

Results require a matching `attempt_id`, `stage` and `expected_version`, authenticated with
the current writer token. A successful report-drafting result sends `report_markdown`;
the server persists it and derives a SHA-256 revision. Report review and storage send
`report_revision`; final storage and execution closure are atomic. A `needs_input` result
supplies `question.prompt` and `question.handoff`, with optional `options` and
`proposed_default`. Active cancellation preserves the attempt while effects are reconciled.
Writer stop confirmation uses its scoped credential.

### Prerequisites and queued steering

An owner can include `dependencies` in `POST /v1/items`. Each entry names an `upstream_id` and a `milestone` of `report`, `ready_for_check_in` or `checked_in` (the default). Prerequisites may belong to other repositories. Item inspection includes configured links and the execution's `unmet_dependencies`, with an explanation for each wait.

An owner can send `PATCH /v1/items/{item_id}/executions/{execution_id}` with `expected_version` and changes to `goal`, `context_links`, `acceptance_criteria`, `priority`, `runner_affinity` or `dependencies`. Omitted fields keep their values. An empty list clears a list field, and `null` clears runner affinity. The patch accepts `Idempotency-Key`. Stale versions and executions outside the queued state return HTTP 409.

The same patch accepts `target` changes between `ready_for_check_in` and `checked_in`, with
`steered_by: "owner"` (default) or `"captain"` recorded alongside the before/after values in the
timeline. A target-only change is also allowed on active PR work, including direct `delivery`
and its GitHub wait. Switching to or from `report` is refused. A completed ready item still requires
`rerun`. A target change does not itself call GitHub; the writer reconciles auto-merge with the
latest target on its next delivery pass.

A successful claim includes a `dependencies` snapshot with each upstream execution ID, output reference and exact revision. For a reviewed PR it includes the reviewed head and base; for a check-in it also identifies the verified merge commit. Use these revisions in the work packet. The snapshot is recorded on the attempt's claim event and returned on claim replay even if an upstream execution is rerun.

Milestone changes refresh up to 32 dependents in the upstream transaction. Larger fan-outs and restart audits are processed in indexed batches across short transactions, so queued dependents in later batches may become eligible after the upstream transaction commits. The claim transaction always checks current durable evidence before assigning work.

### Questions and external waits

See [Answer a waiting item](work-items.md#answer-a-waiting-item) for the question and reply flow.
Delivery waits retain the writer without an open attempt. The reconciler posts GitHub
observations; the writer follows checks and feedback on its primary PR. There is no separate
runner-scheduled wait or recheck API.

## Direct writers and dispatch

Optional [observed usage](usage.md) uses `POST /writer/usage` with the current writer token
or `POST /items/{item}/executions/{execution}/writers/{writer}/usage` with a live in-scope
Captain's trusted owner/runner transport, including bounded late observations. These routes
never advance work or affect capacity. The existing operator timeline response includes
`usage.execution`, `usage.item`, per-session provenance/gaps and provider capabilities.

| Method and path | Request and behavior |
| --- | --- |
| `GET /v1/dispatch/settings` | Board-authenticated read of `paused`, `aging_seconds`, canonical `max_building_writers` (Building, initially 5), `max_writers` (Total, initially 10 on new installs), `building_writers`, `delivering_writers` and `active_writers` (Total usage). Existing stores retain their approved Total until activation. Local read-only board access also permits this read. |
| `PUT /v1/dispatch/settings` | Owner changes any settings above except derived counts; omitted fields are preserved. Both limits must be strict integers 1..2147483647, with Building <= Total against the saved pair (409 for an incoherent pair). Set both in one request for an atomic pair update. Null, unknown fields and aliases are rejected. Browser writes must be same-origin. Returns saved settings and usage. Pause stops new registrations/attempts/acquisitions; existing progress/results remain accepted. Lowering never ends writers or revokes existing allocations. |
| `POST /v1/items/{item}/executions/{execution}/writer` | Captain registers a named writer intent, expected version, nonce and token digest. Atomically enforces Total and, for building work, Building; no caller-supplied limit. Replacement requires the current writer ID and stopped attestation; transfers its allocation and pending request, revoking the old identity. |
| `PATCH /v1/items/{item}/executions/{execution}/writer` | Captain confirms the native session ID/link for its registered intent. |
| `GET /v1/writer` | Scoped writer reads its binding, execution, open flags and latest persisted reply. |
| `POST /v1/writer/attempt` | Scoped writer claims/resumes its own next runnable stage, acquiring Building when required. On full Building, returns a binding with no attempt and visible capacity wait; the same writer retains Total. |
| `POST /v1/writer/progress` | Scoped writer records progress without a timed lease. |
| `POST /v1/writer/capacity` | Scoped writer posts `{"phase":"building","expected_revision":N}` before rework, or phase `delivering` with `handoff_head` (the full reviewed commit SHA) after local work. The CLI reads HEAD automatically. Read N from `writer.capacity_revision`. Inspect returned `writer.building`; false means wait/retry, not permission to build. Binding includes durable `building_requested_at`, `capacity_wait` and `handoff_head`. Repeated requests do not double-allocate; stale revisions fail 409 without releasing later work. Eligible waiting PR fixes precede new builds. Building stages cannot release. A mismatched open head waits visibly with `head_drift`; review/hand off a superseding head or stop/withdraw the writer to clear it. Verified merges remain authoritative and release the writer. |

`phx shift` registers and manages Captains; `phx work` uses the scoped credential in the item's
Git directory. The server enforces one open writer per execution. A result advances the same
writer to the next runnable stage with a new attempt; the writer retains ownership through
waits. It has no timed expiry. See the [complete fencing and evidence contract](direct-sessions.md#writer-registration-and-fencing).

Writer bindings include `last_reply`, scoped to the bound execution: `null` before a reply,
otherwise `{version, created_at, answer, answered_by}`, the same shape as owner inspection.
Both `phx work status` and `next` expose it; claiming an attempt does not change the reply.

Unbound queued items include `pickup` (`eligible`, `code`, `reason`, `trigger`, and eligible
`captain_ids`). `wait_reason` is the same concrete exclusion reason, or null when eligible;
`next_action` then calls for Captain dispatch. Decisions use live active Captain scope/readiness,
persisted dependencies/holds/retry, affinity and canonical dispatch capacity.
No live scope and an uninitialized Captain snapshot have distinct explicit wait reasons,
never scope inferred from a charter hash.

Captain registration and heartbeat accept `pickup` with `repositories` mapping charter
repository IDs to null (ready) or a concrete readiness blocker, and optional `runner_id`.
Heartbeat omission preserves the snapshot; normal `shift` commands republish it from the charter
and local verification. An empty snapshot grants no scope. Snapshots expire with Captain liveness.
Only trusted coordinator callers can publish them. Item IDs in charter prose are not required.
Captains select named eligible items, taking dependencies, priority aging and retry time into
account. Views expose `ready_since_ms`, `retry_at_ms` and `retry_count`. A stopped writer
is replaced only after its native session and local work are accounted for. No timed expiry
alone authorizes replacement.

## Operator board data

Both owner and runner tokens can call these routes. With `PHALANX_LOCAL_BOARD=1`, loopback requests can read them without a token.

Dashboard-only owner actions use these narrow adapters, not implicit owner authorization
for `/v1`:

| Route | Contract |
| --- | --- |
| `GET /operator/api/repositories` | Existing registered-repository list for the creation form. |
| `POST /operator/api/items` | Same body, validation, target/publication rules, response and `Idempotency-Key` receipt as `POST /v1/items`. |
| `GET /operator/api/dispatch/settings` | Canonical dispatch settings and current active-writer usage. |
| `PUT /operator/api/dispatch/settings` | Same validated partial update and response as owner-authenticated `PUT /v1/dispatch/settings`; omitted fields are preserved. |

These adapters require an owner bearer token unless the server explicitly enables local-board
mode and verifies a direct loopback peer and local Host. Tokenless writes additionally require
an exact same Origin; null/untrusted origins, non-same-origin Fetch Metadata and forwarding
headers are rejected. A supplied credential is always validated, never replaced with local
authority. Authenticated nonbrowser clients without browser headers continue to work.
Do not enable local mode behind proxies/tunnels or on shared/hosted deployments.
The [local-board setup](work-items.md#operator-work-board) covers the service-owner checks.

| Route | Response |
| --- | --- |
| `GET /v1/operator/executions?kind=open` | `items` and `next_cursor`. Input waits appear first. Each group is ordered by execution creation time, oldest first. |
| `GET /v1/operator/executions?kind=closed` | `items` and `next_cursor`. Closures appear newest first, including outputs from earlier executions of rerun items. |
| `GET /v1/operator/items/{item_id}` | The current execution summary in `item`, or 404. Uses the same conditional-read contract as execution lists. |
| `GET /v1/operator/overview` | Section counts, 24-hour outcomes, dispatch paused/on, oldest eligible queued age and `generated_at` for data age. |
| `GET /v1/operator/groups/{label}` | Current executions with the label, a paginated item list and an outcome/size/time rollup of that page only (`scope: "page"`). Follow `next_cursor` to continue; no unbounded label aggregate is computed. |
| `GET /v1/operator/executions/{execution_id}/timeline` | The execution summary in `item`, the current attempt ID in `attempt`, newest-first transition `events`, and `next_cursor`. Events include `attempt_id`, recorded `pr_url` and full applied `candidate` SHA when available. A result without a persisted `reason` derives a credential-redacted cause from its details, bounded to 400 characters; see the [correlated timeline example](#read-one-items-correlated-timeline). A `reply` event carries `answered_by`. |

Paginated routes default to 25 entries and accept `limit` from 1 through 100. Pass the returned `next_cursor` as `cursor` on the same route and list kind until it is null. Pages read live state: an open execution entering or leaving the input-wait group can cross the cursor between requests, so refresh from the first page to see the current order. New timeline events and closures normally precede the current cursor. The timestamp and version fields describe the persisted state.

Execution lists accept `repository`, `label`, `status`, `stage`, `outcome`, `q` and `closed_after`. Outcome and closure-time filters apply only to closed lists. Filtered pages inspect at most 2,000 indexed candidates and may return an empty page with a continuation cursor. Lists, overview and groups support weak `ETag`/`If-None-Match` conditional reads (`private, no-cache`); unchanged state returns 304. Display ages alone do not invalidate the token: calculate elapsed time from the retained snapshot's timestamps. State changes, retry/queue deadlines, unconfirmed-launch deadlines and 24-hour closure boundaries invalidate it.

Intake accepts `label` (also `phalanx intake --label`); steering can change or clear it with `null`. Summaries add `label`, `revision_round`, `duplicate_count`, `pr`, and a `next_action` whenever an open row has no wait reason. The summary `target` is the execution's `state.target`, including reruns and steered targets, not the item's original request. PR fields and duplicate counts come only from the latest reconciler GitHub observation, never from a writer's claim, and are null when unobserved. The local-board contract is detailed in [DASHBOARD.md](../DASHBOARD.md#api-additions). It adds no runners endpoint or capacity fields.

`last_advanced_at` records entry into the current stage, and `last_progress_at` the latest
execution transition. Wait details include the reason, resume trigger and age. Scheduled
retries appear as `retry_at_ms`. `primary_pr_url` identifies the primary PR. Writer fields
expose its native session and progress.
`version` is the version a reply must name. Timelines list blocking `prerequisites` and the
current `question`. Report output URLs use the authenticated report route.
Timelines also include nullable `previous_execution_id` and `next_execution_id` for rerun
navigation. Local `/operator/items/{item_id}` and `/operator/executions/{execution_id}`
pages reuse that timeline; `/operator/queue` and `/operator/history` use the existing
filtered lists, preserving filters and keyset cursors in the browser URL.

### Read one item's correlated timeline

Use the authenticated API, not direct database queries. Inspect the item to find its
current `execution.id` (`GET /v1/items/ITEM_ID`); use
`GET /v1/items/ITEM_ID/executions` to find earlier executions after a rerun.
Then page that execution's existing operator timeline:

```powershell
$base = "http://127.0.0.1:8000"
$headers = @{ Authorization = "Bearer $env:PHALANX_OWNER_TOKEN" }
$item = Invoke-RestMethod "$base/v1/items/ITEM_ID" -Headers $headers
$route = "$base/v1/operator/executions/$($item.execution.id)/timeline?limit=100"
do {
    $page = Invoke-RestMethod $route -Headers $headers
    $page.events | Select-Object version, attempt_id, from_stage, stage, reason, pr_url, candidate
    $route = "$base/v1/operator/executions/$($item.execution.id)/timeline?limit=100&cursor=$($page.next_cursor)"
} while ($page.next_cursor)
```

The response's `item.item_id` and `item.id` correlate the work item and execution.
Events are newest first; `attempt_id` identifies the fenced stage attempt when recorded
(otherwise null). `from_stage` and `stage` show a stage change.
`pr_url` is the PR recorded on that event, and `candidate` is its full applied
candidate/head SHA, not the execution's possibly newer revision.

For example, the paginated backward-transition fixture in
`tests/test_operator.py::test_backward_result_reasons_correlate_the_recorded_attempt_and_revision`
includes this event (other response fields omitted):

```json
{
  "version": 4,
  "attempt_id": "result-attempt-1",
  "from_stage": "self_review",
  "stage": "development",
  "reason": "changes_required; merge_conflict; at bbbbbbbbbbbb",
  "pr_url": "https://github.com/a/b/pull/42",
  "candidate": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

An existing `details.reason` or state `wait.reason` takes precedence, including
on forward results that enter an external wait. Otherwise result reasons derive
only from persisted outcome, issue, failed local check names and reasons, failure
reason, and applied SHA. Derived reasons are at most 400 characters, with bounded
check/failure excerpts and a reserved short-SHA suffix. Recognizable GitHub tokens,
bearer credentials and named secret assignments are redacted before truncation.
Task bodies, reports, review notes and other arbitrary evidence are not copied
into these reasons. Existing pause/retry waits are unchanged; no slot, capacity or
rank-position wait reasons are inferred on the direct path.

## GitHub observations

The trusted reconciler posts timestamped facts to
`POST /items/{item_id}/executions/{execution_id}/github-observation`, local-check facts to
`POST /items/{item_id}/executions/{execution_id}/local-checks`, and its heartbeat to
`POST /reconcilers/{id}/heartbeat`. These require the trusted service token, not a writer
credential or stage attempt. The result API rejects fabricated provider observations.
See [reconciliation](direct-path.md#run-the-github-reconciler).
