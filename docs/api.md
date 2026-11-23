# API reference

The HTTP API is the only path to queue state. Owner routes use the owner bearer token and runner routes the runner token. In the local deployment, the runner token identifies a trusted runner role; use one stable runner ID per runner across restarts, which scopes capacity and recovery within that role. All routes below are under `/v1` unless shown in full.

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
| `POST /items/{item_id}/executions/{execution_id}/result` | Runner | Submit `outcome`, stage-specific evidence and the current attempt preconditions. |
| `POST /items/{item_id}/executions/{execution_id}/stop-confirmed` | Runner | Confirm a cancellation under a live lease after accounting for the worker and its effects. |

Intake accepts `repository_id`, `goal` and `target`, plus optional `context_links`, `acceptance_criteria`, `target_branch` and `priority` (`low`, `normal` or `urgent`). Use an `Idempotency-Key` header when retrying a mutation. The server returns the original response for a replay of the same validated request in its operation scope, or HTTP 409 for a different request under the same key. Registration is keyed by the repository ID and unique remote. Version conflicts also return 409. Versions start at 1 for each execution. A rerun preserves the prior execution and keeps the item's output type.

Results and stop confirmations require a matching `attempt_id`, `stage` and `expected_version`, plus the current secret in the `Phalanx-Lease` header. A successful report-drafting result sends `report_markdown`; the server persists it and derives a SHA-256 revision. Report review and storage send `report_revision`, and the server copies the reviewed draft into final storage in the same transaction that closes the execution. A `needs_input` result supplies `question.prompt` and `question.handoff`, with optional `options` and `proposed_default`. Active cancellation preserves the attempt while awaiting reconciliation. An expired lease or uncertain provider effect requires recovery before confirmation.

### Prerequisites and queued steering

An owner can include `dependencies` in `POST /v1/items`. Each entry names an `upstream_id` and a `milestone` of `report`, `ready_for_check_in` or `checked_in` (the default). Prerequisites may belong to other repositories. Item inspection includes configured links and the execution's `unmet_dependencies`, with an explanation for each wait.

An owner can send `PATCH /v1/items/{item_id}/executions/{execution_id}` with `expected_version` and changes to `goal`, `context_links`, `acceptance_criteria`, `priority`, `runner_affinity` or `dependencies`. Omitted fields keep their values. An empty list clears a list field, and `null` clears runner affinity. The patch accepts `Idempotency-Key`. Stale versions and executions outside the queued state return HTTP 409.

A successful claim includes a `dependencies` snapshot with each upstream execution ID, output reference and exact revision. For a reviewed PR it includes the reviewed head and base; for a check-in it also identifies the verified merge commit. Use these revisions in the work packet. The snapshot is recorded on the attempt's claim event and returned on claim replay even if an upstream execution is rerun.

Milestone changes refresh up to 32 dependents in the upstream transaction. Larger fan-outs and restart audits are processed in indexed batches across short transactions, so queued dependents in later batches may become eligible after the upstream transaction commits. The claim transaction always checks current durable evidence before assigning work.

### Questions and external waits

See [Answer a waiting item](work-items.md#answer-a-waiting-item) for the question and reply flow. Runners can post `/external-wait` under an execution with the live attempt lease, expected version and an idempotency key. Include the attempt and stage, a reason, a gate ID, the expected head and base, and an optional provider retry delay in seconds. Poll `GET /v1/runner/due-rechecks?limit=50` for due waits and post `/recheck` under each execution with its listed version and a stable idempotency key. Both use the runner token. A recheck queues the same stage for verification.

## Dispatch and leases

| Method and path | Request and behavior |
| --- | --- |
| `PUT /v1/dispatch/settings` | Owner sets `paused` and optionally `aging_seconds` (default 300). A pause stops new assignments while existing leases, results and recovery continue. |
| `PUT /v1/dispatch/runners/{runner_id}/capacity` | Owner sets `capacity` (1–256, default 4 per runner) for agent worker stages. Runner-only stages use a separate lane of 2 per runner. Reductions take effect for the next claim and leave all reservations in place. |
| `POST /v1/dispatch/claim` | Runner sends `runner_id`, `repository_ids` (1–100 local checkout mappings), optional `slots` to lower the capacity for this muster, and optional `stages` to claim only supported stages. Send a fresh cryptographically random secret, for example Python's `secrets.token_urlsafe(32)`, in `Phalanx-Lease`. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/renew` | Runner sends the original `Phalanx-Lease` before expiry. The response gives `lease_expires_at` in Unix seconds. Renew independently of worker observation, by default every 60 seconds. The optional body carries `worker` (`reference` and HTTPS `url`) and `blocker`, the runner's current blocker text; a renewal without `blocker` clears it. |
| `GET /v1/dispatch/runners/{runner_id}/recovery` | Runner lists expired reservations for its ID with the item, execution, attempt, stage, current execution version and reconciliation action. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/recover-stopped` | Runner supplies `item_id`, `expected_version`, and a `reconciliation` note describing how it verified the worker stopped and accounted for downstream effects. Accepts `Idempotency-Key`. A live, replaced or changed attempt is rejected. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/recover-claim` | Adopts an expired open attempt under the new `Phalanx-Lease` secret, with the same body as `recover-stopped`. The attempt, version and stage stay the same, and the old secret stops working. Accepts `Idempotency-Key`. A live lease (renew it instead), an ended attempt or a changed execution is rejected. The adoption appears in both timelines as a `recover_claim` event at the same version. |

A claim returns HTTP 200 with one of `claimed`, `paused`, `at_capacity` or `no_eligible_work`. A successful assignment includes the work request, execution and version, attempt ID, and lease expiry (`PHALANX_LEASE_SECONDS` after the claim, 300 by default). Pass its attempt ID, execution version and stage to the result route with the same lease header. Keep the bearer token and lease secret with the runner. After a lost claim response, replay the same body and lease secret to retrieve the original assignment. Deduplicate by attempt ID and verify that the lease and execution are still current before starting work on a replayed assignment. Empty polls leave no receipts. The server stores only the lease-secret digest.

An intake item can set `runner_affinity` to require a specific runner ID. For matching repositories and affinity, urgency rises one band per aging interval. Within the effective band, the server chooses the oldest eligible timestamp and breaks ties by item ID. Execution views report `ready_since_ms` and `retry_at_ms` as Unix milliseconds, and `retry_count` as the retries scheduled in the current stage. A reconciled retry starts accruing age when its delay elapses. A newly queued stage gets its own clock. The claim index excludes blocked stages; a stage begins to age once its last prerequisite clears and any retry delay elapses.

An expired lease prevents stale results and renewals, but its attempt still holds the slot and item reservation. Leave uncertain or live workers reserved. After confirming that a worker stopped and that repeating the stage is safe, submit stopped recovery. It records the reconciliation and uses delays of 5, 10, 20 and 40 seconds for the first four retries. A fifth stopped attempt waits for owner intervention. If cancellation is already pending, stopped recovery closes it with a stored cancellation report. Recovery claims extend this to launch intent, live session reattachment and collection of finished results; see [Runner operations](runner-operations.md#recovery-inside-the-loop).

## Operator board data

Both owner and runner tokens can call these routes. With `PHALANX_LOCAL_BOARD=1`, loopback requests can read them without a token.

| Route | Response |
| --- | --- |
| `GET /v1/operator/executions?kind=open` | `items` and `next_cursor`. Input waits appear first. Each group is ordered by execution creation time, oldest first. |
| `GET /v1/operator/executions?kind=closed` | `items` and `next_cursor`. Closures appear newest first, including outputs from earlier executions of rerun items. |
| `GET /v1/operator/executions/{execution_id}/timeline` | The execution summary in `item`, the current attempt ID in `attempt`, newest-first transition `events`, and `next_cursor`. A `reply` event carries `answered_by`, and a `recover_claim` event records an adoption. |

Each route defaults to 25 entries and accepts `limit` from 1 through 100. Pass the returned `next_cursor` as `cursor` on the same route and list kind until it is null. Pages read live state: an open execution entering or leaving the input-wait group can cross the cursor between requests, so refresh from the first page to see the current order. New timeline events and closures normally precede the current cursor. The timestamp and version fields describe the persisted state.

`last_advanced_at` records entry into the current stage, and `last_progress_at` the latest execution transition. Wait details include the stored reason, resume trigger and age of the current wait. A scheduled retry appears as `retry_at_ms` and a due external gate recheck as `recheck_at_ms`, both in Unix milliseconds. `primary_pr_url` is the recorded primary PR. `worker_reference` is the latest attempt's opaque worker ID, which the runner republishes on each renewal, and `worker_url` is the bridge's optional HTTPS chat link. When the runner is blocked on an item it would otherwise show as plainly running, such as an unreachable runtime or an ambiguous worker, it sends the reason as `blocker` with each renewal, and the item's `wait_reason` shows it until a renewal omits it. `version` is the execution version a reply must name. The timeline lists blocking `prerequisites` and, while the item waits for input, the current `question`. The draft state has no current read mapping. Report output URLs use the authenticated report route.

## Runner PR observations

The [runner PR observation API](runner-pr-observations.md) records evidence for publication, promotion and PR validation. The result API rejects caller-supplied PR observations.
