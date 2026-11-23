# Runner operations

The runner claims work from the API, prepares isolated worktrees, launches visible worker sessions through the Captain, renews leases, and submits verified results. Runner-only stages such as draft publication, validation and check-in run on the runner itself. This guide assumes a runner set up as in [Install Phalanx](install.md#set-up-a-runner), with its [config](configuration.md#runner-config).

Use one stable runner ID, one private state directory and one config for every command. Keep the runner token in the shell that runs them. Keep the state directory when restarting; expired leases and unverified results require reconciliation.

## Continuous Captain loop

The loop is the normal way to run a muster. Finish any manual `attended` preparation first, then start one loop per state directory:

```powershell
python -m phalanx captain --config runner.json --state C:\phalanx-runner
```

A process lock (`captain-loop.lock`) excludes a second loop on the same directory; it does not block manual status, receipt or recovery commands.

The loop first runs a startup preflight (also run by `attended prepare`). It reports every missing package, Git older than 2.36, a missing `PHALANX_RUNNER_TOKEN`, and an unreachable or rejecting API together, and stops before any claim. `PHALANX_GITHUB_TOKEN` is optional; without it, draft claims are excluded.

By default the loop reaches the worker runtime through the [Captain chat relay](captain-relay.md#captain-chat-relay), because the Captain chat holds the runtime's session tools. To use a trusted subprocess bridge instead, put `--adapter-command BRIDGE [ARGS...]` last; all remaining arguments belong to the bridge. Both transports serve the same [bridge contract](captain-relay.md#bridge-contract).

### Admission and claims

The loop enumerates durable reservations and the server's expired attempts before admitting work. An unavailable recovery listing or unmatched attempt pauses admission with a visible recovery reason. Existing reservations, including slots above a reduced capacity, still run and renew.

New claims route worker stages (`development`, `self_review`, `adversarial_review`, `pr_revision`, `report_drafting`, `report_review`) separately from runner-only `pr_creation`, `report_storage` and, when `validation_policy` is configured, `pre_pr_validation`, `pr_promotion`, `pr_validation`, `pr_reconciliation` and `check_in`. An optional config `stages` array restricts this set. Draft, promotion, published-validation, revision and check-in claims are excluded without `PHALANX_GITHUB_TOKEN`, and validation claims without `validation_policy`. The server's atomic claim still applies pause, capacity, priority, repository, dependency and affinity gates.

Worker claims require verified runtime project mappings; runner-only claims keep every Git-verified mapping even when its runtime is unavailable. Slots 1..`capacity` poll worker stages first and fall back to runner-only stages. Two extra slots, `capacity`+1 and `capacity`+2, form the runner-only lane: they claim and continue only runner-only stages, so promotion and validation never wait behind long workers. The loop preserves each pending claim's exact original filters and secret. One asynchronous discovery pass per poll verifies each distinct host/project/checkout mapping once and shares the result across slots. Failed discovery is retried on the next poll. Every unverified mapping is listed with its project ID, checkout and error in one `mappings` loop status (`attended status --all`) and gets no workers, while other mappings keep claiming.

### Scheduling

Each slot has one work lane. Lease renewals have separate lanes (60 seconds by default), as do bounded native observation batches (eight slots, two-second wait by default). Git, provider requests, a slow native batch or an ambiguous slot cannot block the heartbeat or another slot. `--poll-seconds`, `--heartbeat-seconds`, `--batch-size` and `--wait-seconds` tune the scheduler; `--batch-size 1` isolates individual native waits. If a renewed lease has less than two heartbeat intervals left, the slot reports `renewal_interval_too_long`. Per-attempt cursors and admission rotation stay in memory; they reset on restart and never prove stop or authorize replacement.

### Launch at most once

Every prepared launch is relayed at most once. The loop journals relay intent before invoking the bridge and saves the native or queued receipt immediately. After a timeout, failure or restart, a recorded relay intent is **never** repeated. A prepared packet with no relay intent, receipt, submission or recovery ownership can resume discovery and launch with its original claim and packet, after its clean, accounted worktree and live claim are rechecked.

Discovery must correlate the original marker, assignment, host, project, launch time and any queued ID to exactly one native thread. A stopped turn also needs an explicit attestation that downstream writers stopped. The result collector validates the local result and checkpoint before submitting. Repeated observations of a correlated native ID keep the queued identity and stay idempotent. Saved submissions replay their exact original receipt even after the local lease deadline; the server decides whether they were already accepted.

### Recovery inside the loop

Expired attempts with a known active writer, a verified result or a saved submission are adopted, not released. The runner journals a recovery claim with a fresh lease secret; the server re-leases the same attempt and fences the old secret, so nothing is redrafted. Stopped-attempt recovery releases only verified safe reservations.

A stopping worker assignment with no relay intent, receipt, submission or recovery ownership can use the leased `stop-confirmed` route without waiting for expiry. The loop checks that its worktree is absent or clean and accounted, and records the exact stop request before sending it; retries recheck that proof and replay the original request. Only a verified finalization acknowledgment releases the slot. Dirty checkpoints, partial workspace refs, attempt evidence, ambiguous launches and runner-only publication or storage effects still require explicit reconciliation. Runner-only `report_storage` with no saved submission and `pr_creation` with no draft publication intent or sealed observation use the same no-writer stop path. Draft cancellation checks the publication journals under the item publication lock; a saved intent stays reserved even when the slot has none. An existing clean candidate must match the claimed candidate, not today's origin.

For a launched stopping worker, the stop path requires its exact stopped native turn and verifies the worktree and any result through the recovery verifier; it never submits a worker result as a cancellation acknowledgment. A clean unlaunched checkpoint stays accounted when origin advances to a descendant. Unmatched expired attempts consume capacity rather than stopping all admission. Recovery waits for an observation before starting its uncertainty backoff, and malformed native rows block only their own markers. Rejected submissions stay observable across restart, and stop confirmation archives their evidence once. An expired prepared packet with no relay effects uses the verified no-launch checkpoint without waiting for an observation that cannot exist.

### Long-running workers

A worker still active three hours after launch with no result keeps its reservation and lease; the loop never stops it. The slot reports `worker_overdue` once, and the operator board shows it as the item's wait reason until the worker produces a result. A writer observed stopped without a result file stops renewing its lease, so stopped-attempt recovery reconciles the expired attempt instead of the item staying reserved forever.

If that writer left uncommitted changes or new commits without a result, recovery never releases or discards them. It resumes the same writer through the bridge's [`resume` operation](captain-relay.md#bridge-contract) with a continue prompt, then adopts the same attempt under a fresh lease once the writer is observed active. Each resume gets 180 seconds to show the writer active. After three resumes the slot waits on the owner, naming the attempt, writer, worktree and its dirty state; the board shows the same reason, and the optional `notify_command` receives an `owner_wait` event once. A parked wait is adopted without a recheck if the exact writer is later observed active, and `--recheck` resets the resume count.

### Output and shutdown

The CLI prints JSON activity and actionable errors. Reservation status includes the last loop and recovery reason. Loop status has one durable store and is written and emitted only when its status or reason changes. Ctrl+C stops scheduling and drains in-flight operations without discarding reservations. Restart with the same configuration; see [Resume an existing install](resume.md).

## PR validation and check-in

PR validation runs on the runner. Set `validation_policy` in the runner config and write the policy as described in [the validation policy guide](runner-validation-policy.md).

- After self-review, `pre_pr_validation` runs the configured local checks in the clean item worktree.
- `pr_promotion` verifies draft checks and marks the primary PR ready.
- `pr_validation` records ready CI for the current head. Pending CI waits, and the loop requests due rechecks each poll.
- For a `checked_in` item, `pr_reconciliation` records the merge intent once every gate is current, and `check_in` merges with a merge commit once GitHub's required reviews and checks allow it. Phalanx adds no gate of its own. Set `"auto_merge": false` to deliver the ready PR and leave the merge to the owner.

Pending draft and ready checks are rechecked every `check_recheck_seconds`. After `check_wait_limit_seconds` pending on one head and base, the owner is asked; a reply starts a fresh window. At most `local_check_capacity` validations run local checks at once on the runner host; the rest wait with the reason "Waiting for local check capacity" without using their check timeout. A local check that times out with no identified failure retries validation twice with backoff before returning to development. A missing or broken policy waits for an owner reply that names the problem, and startup preflight reports a malformed file before any claim. See [Configuration](configuration.md#runner-config) for defaults and ranges.

The [runner PR observation API](runner-pr-observations.md) records evidence for publication, promotion and PR validation.

## Manual attended commands

The `attended` commands run one step at a time from a Captain chat. They are useful for debugging and recovery; the loop automates the same steps. All take the same prefix and a slot number, for example:

```powershell
python -m phalanx attended --config runner.json --state C:/phalanx-runner --slot 1 prepare
python -m phalanx attended --config runner.json --state C:/phalanx-runner prepare --draft
python -m phalanx attended --config runner.json --state C:/phalanx-runner status --all
```

Local capacity defaults to 1 and bounds agent worker sessions. The runner holds two more numbered slots for runner-only stages (PR publication, checks, promotion, reconciliation, check-in and report storage), so they run while every worker slot is busy. Use numbered slots from 1 through capacity plus 2. The server still applies dispatch pause, eligibility, its per-runner capacity for worker stages, and a runner-only lane of 2. A local capacity reduction applies to new reservations; existing reservations in higher slots can still be inspected, renewed and submitted, and count toward local capacity. When upgrading the bridge, finish in-flight CLI commands and use the new version for all later commands. An existing journal stays usable and its original reservation is slot 1.

1. Verify the project and host with the runtime's project discovery. Run `prepare`. On `launch_intent`, launch **one** visible worker session from the returned `session` (`project_id`, `title`, `prompt`), as the `launch` operation in the [runtime mapping](captain-relay.md#runtime-mapping) describes. Save the marker. Prepare each additional assignment in a free slot, such as `--slot 2 prepare`. The worker targets its assigned Git worktree explicitly.
2. Run `receipt MARKER HOST --thread-id THREAD` with the worker session's UUID, or `receipt MARKER HOST --client-thread-id CLIENT` for queued setup. Resolve a queued or lost receipt in the runtime and verify the exact marker and assignment before recording the native ID. Use `status` to inspect a reservation or `status --all` to list occupied slots. If the claim response was lost, repeat `prepare` in the original slot to replay the claim. Keep an ambiguous launch reserved and reconcile it with the original marker. Other free slots can keep preparing.
3. Run `renew` for every active claim in its slot, independently of observation and comfortably before the lease expires. Use short observation waits.
4. Read the worker session and verify the exact marker, assignment, and that the worker and any writers it started have stopped. Then run `submit MARKER HOST THREAD --confirmed-stopped`. The runner verifies the result file and current worktree before posting. If the response is lost, repeat the same command to replay it.

Development success requires a new local candidate commit and a clean worktree. Report drafting sends the Markdown to the server as a draft. Worker stages record local checkpoints for questions and failures. Questions wait for an owner reply. A technical failure from a confirmed-stopped worker enters the shared 5, 10, 20 and 40 second retry backoff; exhausting it asks for owner intervention.

Runner-only stages:

- **Draft PR.** With `PHALANX_GITHUB_TOKEN` set, after development succeeds run `prepare --draft` to publish the initial draft. It uses the runner's Git credentials for the push and its GitHub token for draft creation, and returns `draft_submitted` when the API accepts the verified PR. Keep the same state directory to retry an interrupted publication, and use `status` to inspect a blocked reservation.
- **Report review and storage.** Run `prepare` again to launch an independent review of the exact stored draft revision. Review findings return to drafting with the previous draft in the new packet. Run `prepare --report-storage` in a free slot to submit a reviewed revision for server finalization. A `report_storage_submitted` result needs no native chat; if the response is lost, repeat the command in the same slot. The server retries final storage independently and exposes the Markdown through the report endpoint.
- **Validation and check-in stages.** `prepare` has no flag for these; the Captain loop claims and runs them as described in [PR validation and check-in](#pr-validation-and-check-in).

To reconcile a reservation by hand, see [Recovery and troubleshooting](recovery.md#reconcile-a-reservation).
