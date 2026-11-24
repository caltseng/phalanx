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

## Session archival records

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
| `GET /items/{item_id}/executions` | Owner or runner | Read past and current executions. |
| `GET /items/{item_id}/executions/{execution_id}/timeline` | Owner or runner | Read durable transition history and evidence. |
| `GET /items/{item_id}/executions/{execution_id}/drafts/{revision}` | Owner or runner | Retrieve the exact draft for review. |
| `GET /items/{item_id}/executions/{execution_id}/report` | Owner or runner | Retrieve the final stored Markdown. |
| `POST /items/{item_id}/executions/{execution_id}/cancel` | Owner | Request cancellation with `expected_version` and `reason`. |
| `POST /items/{item_id}/executions/{execution_id}/rerun` | Owner | Create a linked execution from the current closed execution with `expected_version` and an explicit `target`. |
| `POST /items/{item_id}/executions/{execution_id}/result` | Writer, or remaining runner-stage caller | Submit `outcome`, stage-specific evidence and the current attempt preconditions. |
| `POST /items/{item_id}/executions/{execution_id}/stop-confirmed` | Runner | Confirm a cancellation under a live lease after accounting for the worker and its effects. |

Intake requires `repository_id` and `goal`; `target` defaults to `checked_in`, preserving explicit `ready_for_check_in` and `report` requests. Optional fields include `context_links`, `acceptance_criteria`, `target_branch` and `priority` (`low`, `normal` or `urgent`). Use an `Idempotency-Key` header when retrying a mutation. The server returns the original response for a replay of the same validated request in its operation scope, or HTTP 409 for a different request under the same key. Registration is keyed by the repository ID and unique remote. Version conflicts also return 409. Versions start at 1 for each execution. A rerun preserves the prior execution and keeps the item's output type.

Results require a matching `attempt_id`, `stage` and `expected_version`, authenticated with
the current writer token. Remaining runner-stage callers instead supply their runner token
and current `Phalanx-Lease`. A successful report-drafting result sends `report_markdown`;
the server persists it and derives a SHA-256 revision. Report review and storage send
`report_revision`; final storage and execution closure are atomic. A `needs_input` result
supplies `question.prompt` and `question.handoff`, with optional `options` and
`proposed_default`. Active cancellation preserves the attempt while effects are reconciled.
Writer stop confirmation uses its scoped credential; runner-stage confirmation needs a live lease.

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

See [Answer a waiting item](work-items.md#answer-a-waiting-item) for the question and reply flow. Runners can post `/external-wait` under an execution with the live attempt lease, expected version and an idempotency key. Include the attempt and stage, a reason, a gate ID, the expected head and base, and an optional provider retry delay in seconds. Poll `GET /v1/runner/due-rechecks?limit=50` for due waits and post `/recheck` under each execution with its listed version and a stable idempotency key. Both use the runner token. A recheck queues the same stage for verification.

## Direct writers and dispatch

| Method and path | Request and behavior |
| --- | --- |
| `PUT /v1/dispatch/settings` | Owner sets `paused` and optionally `aging_seconds` (default 300). Pause stops new writer registrations and attempts; existing results and progress remain accepted. |
| `POST /v1/items/{item}/executions/{execution}/writer` | Captain registers a named writer intent, expected version, nonce and token digest. Optional `writer_limit` enforces its charter across the named repositories atomically. Replacement requires the current writer ID and stopped attestation. |
| `PATCH /v1/items/{item}/executions/{execution}/writer` | Captain confirms the native session ID/link for its registered intent. |
| `POST /v1/writer/attempt` | Scoped writer claims/resumes its own execution's next runnable stage. |
| `POST /v1/writer/progress` | Scoped writer records progress without a timed lease. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/renew` | Remaining runner-stage callers renew the original live `Phalanx-Lease`; optional `worker` reference/link and `blocker` update board evidence. Returns `lease_expires_at`. This route retires in part 2. |

`phx shift` registers and manages Captains; `phx work` uses the scoped credential in the item's
Git directory. The server enforces one open writer per execution. A result advances the same
writer to the next runnable stage with a new attempt; the writer retains ownership through
waits. It has no timed expiry. See the [complete fencing and evidence contract](direct-sessions.md#writer-registration-and-fencing).

Unbound queued items show **Awaiting a direct Captain**, even without `runner_affinity`.
Captains select named eligible items, taking dependencies, priority aging and retry time into
account. Views expose `ready_since_ms`, `retry_at_ms` and `retry_count`. A stopped writer
is replaced only after its native session and local work are accounted for. No timed expiry
alone authorizes replacement.

## Operator board data

Both owner and runner tokens can call these routes. With `PHALANX_LOCAL_BOARD=1`, loopback requests can read them without a token.

| Route | Response |
| --- | --- |
| `GET /v1/operator/executions?kind=open` | `items` and `next_cursor`. Input waits appear first. Each group is ordered by execution creation time, oldest first. |
| `GET /v1/operator/executions?kind=closed` | `items` and `next_cursor`. Closures appear newest first, including outputs from earlier executions of rerun items. |
| `GET /v1/operator/overview` | Section counts, 24-hour outcomes, dispatch paused/on, oldest eligible queued age and `generated_at` for data age. |
| `GET /v1/operator/groups/{label}` | Current executions with the label, a paginated item list and an outcome/size/time rollup. |
| `GET /v1/operator/executions/{execution_id}/timeline` | The execution summary in `item`, the current attempt ID in `attempt`, newest-first transition `events`, and `next_cursor`. Events include their attempt's `worker_reference`, recorded `pr_url` and full applied `candidate` SHA when available. A result without a persisted `reason` derives a credential-redacted cause from its details, bounded to 400 characters; see the [correlated timeline example](#read-one-items-correlated-timeline). A `reply` event carries `answered_by`. |

Each route defaults to 25 entries and accepts `limit` from 1 through 100. Pass the returned `next_cursor` as `cursor` on the same route and list kind until it is null. Pages read live state: an open execution entering or leaving the input-wait group can cross the cursor between requests, so refresh from the first page to see the current order. New timeline events and closures normally precede the current cursor. The timestamp and version fields describe the persisted state.

Execution lists accept `repository`, `label`, `status`, `stage`, `outcome`, `q` and `closed_after`. Outcome and closure-time filters apply only to closed lists. Filtered pages inspect at most 2,000 indexed candidates and may return an empty page with a continuation cursor. Lists, overview and groups support weak `ETag`/`If-None-Match` conditional reads (`private, no-cache`); unchanged state returns 304. Display ages alone do not invalidate the token: calculate elapsed time from the retained snapshot's timestamps. State changes and retry, lease and 24-hour closure boundaries invalidate it.

Intake accepts `label` (also `phalanx intake --label`); steering can change or clear it with `null`. Summaries add `label`, `revision_round`, `duplicate_count`, `pr`, and a `next_action` whenever an open row has no wait reason. The summary `target` is the execution's `state.target`, including reruns and steered targets, not the item's original request. PR fields and duplicate counts come only from the latest reconciler GitHub observation, never from a writer's claim, and are null when unobserved. The local-board contract is detailed in [DASHBOARD.md](../DASHBOARD.md#api-additions). It adds no runners endpoint or capacity fields.

`last_advanced_at` records entry into the current stage, and `last_progress_at` the latest
execution transition. Wait details include the reason, resume trigger and age. Scheduled
retries and external rechecks appear as `retry_at_ms` and `recheck_at_ms`. `primary_pr_url`
identifies the primary PR. Writer fields expose its native session and progress, while
`worker_reference` and `worker_url` retain remaining runner-stage attempt evidence.
`version` is the version a reply must name. Timelines list blocking `prerequisites` and the
current `question`. Report output URLs use the authenticated report route.

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
    $page.events | Select-Object version, attempt_id, worker_reference, from_stage, stage, reason, pr_url, candidate
    $route = "$base/v1/operator/executions/$($item.execution.id)/timeline?limit=100&cursor=$($page.next_cursor)"
} while ($page.next_cursor)
```

The response's `item.item_id` and `item.id` correlate the work item and execution.
Events are newest first; `attempt_id` and that attempt's `worker_reference` identify
the worker when recorded (otherwise null). `from_stage` and `stage` show a stage change.
`pr_url` is the PR recorded on that event, and `candidate` is its full applied
candidate/head SHA, not the execution's possibly newer revision.

For example, the paginated backward-transition fixture in
`tests/test_operator.py::test_backward_result_reasons_correlate_the_recorded_attempt_and_revision`
includes this event (other response fields omitted):

```json
{
  "version": 4,
  "attempt_id": "result-attempt-1",
  "worker_reference": "worker-1",
  "from_stage": "pr_promotion",
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

## Runner PR observations

The [runner PR observation API](runner-pr-observations.md) records evidence for publication, promotion and PR validation. The result API rejects caller-supplied PR observations.
