# Runner PR observations

Submit `POST /v1/items/{item_id}/executions/{execution_id}/pr-observation` with the runner bearer token and the current claim's `Phalanx-Lease`. Use an `Idempotency-Key` and repeat the same body and lease after an uncertain API response. Successful submissions return the updated execution. They record the observation in the timeline and end the attempt in the same transaction.

The runner credential authorizes the caller to attest facts from its GitHub adapter and repository policy. Keep that credential and the lease in the runner. Populate this route from exact GitHub reads and the runner's own expected refs, policy, and revision evidence. The worker result route remains the path for local stage results.

## Common fields

| Field | Value |
| --- | --- |
| `attempt_id`, `expected_version`, `stage` | From the current assignment. |
| `expected` | `head_ref` from the publication intent, `base_ref` from the work item or provider-resolved default, and `base_sha` for the exact target tip being evaluated. |
| `pull` | Exact read-back fields `number`, `head_repository`, `base_repository`, `head_ref`, `base_ref`, `head_sha`, `base_sha`, `state`, `draft`, and `merged`. Repository names use `owner/name`. Commit SHAs use 40 lowercase hexadecimal characters. |

The server checks the registered github.com remote, same-repository head and base, deterministic item branch, explicit target branch when supplied, expected refs, base tip, and assigned candidate SHA. It constructs the canonical PR URL from the registered repository and observed number. Subsequent successful submissions require the recorded primary PR.

## Stage fields

- `pr_creation` accepts an open, unmerged draft and optional `duplicate_numbers` from publication reconciliation. The runner verifies that duplicates match the publication intent. A successful observation records the primary PR and queues self-review.
- `pr_promotion` supplies `gates` with phase `draft`. Collect applicable draft evidence before requesting ready status. To advance, submit a read-back of the ready PR at that same revision. Failed draft checks can be submitted while the PR is still draft.
- `pr_validation` supplies `gates` with phase `ready`, covering applicable CI and preview checks. Passing advances to independent review. Failed checks schedule reconciliation.

The `gates` object carries `phase`, `number`, `head_sha`, `base_sha`, and an optional `test_merge_sha` verified by the runner to correspond to that head and base. Its `required` list describes applicable checks with `name`, `kind`, optional `app_id`, and optional `use_merge_sha` and `require_execution` booleans. Its `observations` list contains the latest normalized `name`, `kind`, `tested_sha`, `outcome`, and optional `app_id` per source. Kinds are `check_run`, `status`, and `preview`. Outcomes are `passed`, `pending`, `failed`, `neutral`, and `skipped`. Use an empty `required` list only after verifying the phase has no applicable requirements.

Malformed payloads return 422. Invalid claims, mismatched revisions, and unresolved gates return 409 without advancing the item. Pending gates still need scheduled rechecks that release the claim. The independent review, ready, and check-in stages still need evidence submissions that verify reviews and merge authorization against the assigned revision. This endpoint accepts only open, unmerged PR observations.
