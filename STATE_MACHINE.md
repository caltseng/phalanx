# Phalanx work item state machine

The transition tables and rules in this Markdown document define Phalanx’s behavior. The diagrams and [HTML visualization](state-machine.html) are visual references. If a diagram differs from the transition rules, correct the diagram against the rules.

Each execution has a **stage**, which describes what must happen next, and a **status**, which describes whether it can happen now. For example, `pr_validation / waiting_for_external` can mean the current PR head is awaiting CI. The server owns both values. A worker attempts one stage and reports its outcome and evidence. The server determines the transition and releases or reassigns the slot.

Phalanx tries to self-resolve within the work item’s scope and the repository’s rules. Fix failing tests, reconcile routine conflicts, and address actionable review feedback independently when the expected result is clear. Ask for human judgment when competing choices would materially change the requested behavior, an approval is required, or continued attempts cannot make progress. Record material assumptions and how they were verified.

## Lifecycle stages

```mermaid
flowchart TD
    Start([New execution]) --> Kind{Output type}
    Kind -->|Pull request| Dev[Development]
    Kind -->|Field report| Draft[Report drafting]
    Dev -->|First candidate and no existing PR| Publish[Open draft PR]
    Dev -->|Candidate for an existing PR| Self[Self review]
    Publish -->|Draft head verified| Self
    Publish -->|Existing PR with a foreign head| Dev
    Self -->|Changes required| Dev
    Self -->|Pass| Pre[Pre-PR validation]
    Pre -->|Validation fails| Dev
    Pre -->|Checks pass or permitted bypass recorded| Promote[PR promotion]
    Promote -->|Draft-only check fails or unexpected head| Dev
    Promote -->|Merge conflict| Revise
    Promote -->|PR ready on the verified head| Post[PR validation]
    Post -->|Current CI and preview results recorded| Reconcile[PR reconciliation]
    Reconcile -->|Diff since the last review is unreviewed| Review[Adversarial review]
    Review -->|No blocking findings| Reconcile
    Review -->|Blocking findings| Revise[PR revision]
    Reconcile -->|Conflict, failed check, feedback or unexpected head| Revise
    Revise -->|Fix pushed and head verified| Post
    Revise -->|Fix pushed to a draft PR| Promote
    Revise -->|New scope| Dev
    Reconcile -->|Ready-only target or auto-merge off| Done([Completed])
    Reconcile -->|Auto-merge on| Merge[Check-in]
    Merge -->|Conflict or unexpected head| Revise
    Merge -->|Merge verified| Done
    Draft --> ReportReview[Report review]
    ReportReview -->|Changes required| Draft
    ReportReview -->|Evidence verified| Store[Store field report]
    Store -->|Artifact recorded| Done
```

The arrows below apply only when their guards pass. A blocked guard keeps the stage and changes the execution status. Record evidence against the candidate revision. After PR publication, use the PR identity, head, and base revision where relevant. A changed head invalidates prior head-bound evidence. Recheck base-sensitive evidence when the base moves. Draft-only check evidence in `pr_promotion` binds to the PR's head and the base GitHub records for it, which GitHub doesn't advance when the target branch moves. An ordinary target advance therefore doesn't stall promotion; a resulting conflict takes the merge-conflict path. Ready evidence in `pr_validation` still binds to the live target tip, and a repo that requires an up-to-date branch enforces it through GitHub's rules at `check_in`.

Before the PR is first marked ready, every fix goes back to `development`, which builds the candidate. After that, the PR stays ready and fixes go through the revision loop: `pr_reconciliation`, `pr_revision`, `pr_validation`, and `adversarial_review` when the new diff needs it. Each pass validates and reviews only what changed. `ready_for_check_in` remains a dependency milestone, reached when reconciliation finds every gate current.

| Stage | Event or evidence | Next stage or disposition |
| --- | --- | --- |
| `development` | Scoped changes have a recoverable candidate commit. | `pr_creation` for a new draft. If the item already has a PR, proceed to `self_review` on the new candidate. A retry whose findings need no change, such as a local check that passes when rerun, may return the recorded candidate unchanged to rerun `self_review` and `pre_pr_validation`. |
| `pr_creation` | Publish the candidate as a draft against the current target branch, record the PR, and verify its head. | `self_review`. An existing PR on the item branch is adopted: on the candidate head it continues to `self_review`, a foreign head returns to `development` to integrate its commits, and a closed PR asks the owner. Reuse a matching PR after interruption. If a bounded search cannot confirm publication, retry creation rather than drop the deliverable. If duplicates appear, select a primary and surface the others for review. |
| `self_review` | Candidate diff has been checked against the work item and repo rules. | `pre_pr_validation` if it passes. `development` if fixes are needed, with the findings carried into the next development packet. |
| `pre_pr_validation` | Before requesting review, run applicable local tests and available live checks. Record the revision and results, or the skipped checks and reason for a permitted bypass. | `pr_promotion` after passing or recording a permitted bypass. Actionable failures return to `development` with the failed checks and the tail of their output as findings. A pure timeout with no identified failure retries `pre_pr_validation` twice with backoff (`retry_at`) before returning. When every local check slot on the runner host is busy, the stage waits as external gate `local_check_capacity` and is rechecked. After 10 rounds of `self_review` or `pre_pr_validation` findings, or a third round with identical findings, since the owner's last reply, ask the owner instead. |
| `pr_promotion` | Update the existing PR to the validated candidate if needed and verify its head. While the PR is a draft, complete its draft-only checks, then mark it ready for review. | `pr_validation`. Wait for draft-only external checks without holding a slot. Failed draft-only checks or an unexpected head return to `development` with findings. A merge conflict goes to `pr_revision` as findings, like after promotion. A PR that is already ready skips draft-only checks and records them as skipped; Phalanx never converts a ready PR back to draft. If a human converted a promoted PR to draft, ask the owner before marking it ready again. A head Phalanx did not verify is only fast-forwarded, never overwritten; if that fails, development integrates its commits. |
| `pr_validation` | Observe CI and perform required preview validation for the current PR head. Wait for pending checks or previews with a scheduled recheck. | `pr_reconciliation` with the results, including failures. |
| `adversarial_review` | An independent reviewer records findings and severity against the diff since the last reviewed head, or the whole PR diff on the first review. | `pr_reconciliation` when nothing blocks. Blocking findings go to `pr_revision`. An interrupted review can retry on the current revision. |
| `pr_reconciliation` | Confirm CI, previews, review, and mergeability for the current PR head and base against the completion target. Reconciliation observes CI and previews on the current head itself and waits for pending checks without holding a slot. | An unreviewed diff since the last reviewed head goes to `adversarial_review`. A merge conflict, failed checks, new actionable GitHub feedback (requested changes or unresolved current review threads, each handed off once), or an unexpected head go to `pr_revision` as findings. GitHub's required approvals gate the merge at `check_in`. When all evidence is current, complete a ready-only item, or one whose runner has auto-merge off, with the PR marked ready; otherwise advance to `check_in`. After 10 revision rounds, or a third round with identical findings, since the owner's last reply, ask the owner instead. |
| `pr_revision` | Fix the findings on the open PR: integrate the target branch or foreign commits, resolve conflicts, fix failures, and address feedback and review findings. Self-review only the new diff, rerun the failed checks and the local checks covering the changed files, then push and verify the head. | `pr_validation`, or `pr_promotion` while the PR is still a draft. If a push outside Phalanx wins the race on a ready PR, the revision is recorded and `pr_reconciliation` sends the foreign head back to `pr_revision`. Never change the PR's draft or ready state. A fix that turns into new scope escalates to `development`. |
| `check_in` | Reconcile first: if GitHub reports the PR merged with the verified head as its final head, record it, even if the base advanced. Merge, squash and rebase merges all verify this way. Otherwise recheck the claim, PR identity, expected head and mergeability, then request a direct merge commit pinned to the head. GitHub's required reviews, checks and rulesets gate the merge; Phalanx adds no gate. | Complete with PR link and merge commit. A conflict or unexpected head goes to `pr_revision` as findings. An unknown merge outcome or pending mergeability retries with backoff and reconciles before any new write. A merge GitHub refuses (HTTP 405), because of repository rules or a disallowed merge method, waits for them to be satisfied, then asks the owner. An unattributable merge asks the owner. |
| `report_drafting` | Produce a recoverable Markdown draft. | `report_review` |
| `report_review` | Check the draft against the request and verify supporting evidence and links. | `report_storage` when it passes. Return to `report_drafting` for fixes. |
| `report_storage` | Store the reviewed Markdown idempotently and record its reference. | Complete with the stored report. Retry storage failures without redrafting. |

If GitHub reports the PR already merged, verify the merge matches this work item and the expected candidate before recording completion. An unexpected merge or a PR closed without merge requires a decision if the action’s intent cannot be established. Never open a replacement PR just because an existing one is closed.

Record the trusted runner's independent review against the PR revision assigned by passing gates. Before queuing check-in, persist the runner's authorization and operation ID against the primary PR and exact ready revision. Accept merge completion only when the current check-in claim supplies the verified provider merge commit of a merged PR whose final head is the candidate.

A merged primary PR stops further stages, whoever merged it. Check-in, the owner, GitHub and earlier Phalanx writes share one verifier: GitHub's merged flag, the PR, item branch and target, and a final head Phalanx recorded (the candidate, the ready head or a reviewed head). The runner checks before admitting a claimed stage, and the Captain loop's control lane rereads every current execution with a primary PR at least every 120 s, outside `check_in`. Forge errors back off (doubling to 15 minutes, or GitHub's retry hint) and never count as a merge. The detection-latency target is one watch interval plus one heartbeat, about 3 minutes while GitHub answers.

- A verified merge closes a queued or waiting execution as `completed` with the PR, head and merge commit, without cancellation. A running execution fences its attempt (`waiting_for_external`, stopping `completed`); late results are rejected, and the runner's stop confirmation closes it and frees the slot once. A `checked_in` dependent then clears.
- A merge of an unrecorded head never delivers: a running worker is stopped the same way, and the execution waits for the owner (`waiting_for_input`). A reply reruns the stage, which blocks again until the owner cancels or replaces the item.
- Repeated reads of the same merge return the current execution unchanged. The timeline event `merge_observed` records the reason, detector (`admission` or `watch`), GitHub's merge time and the observation time. See the [runner PR observation contract](docs/runner-pr-observations.md) for required evidence.

## Repository validation policy

- Each repo can define applicable pre-validation commands, when they may be skipped, draft-only checks, expected CI, and required previews. Start from repository instructions and GitHub’s required checks. If no CI is expected or required, advance. If expected CI is absent, recheck and surface a blocker if it stays missing.
- Run candidate-local pre-validation before uploading an update. Run checks that need a published candidate after the candidate reaches the PR. Check draft-only gates before the first promotion and post-promotion CI afterward. Draft-only gates apply only while the PR is a draft. Phalanx never converts a ready PR back to draft, so later revisions record those checks as skipped.
- Pre-validation runs before promotion by default. A work item can request a bypass for urgency, or the runner can skip checks that repo policy marks inapplicable to the change. Record the candidate revision, affected checks, reason, and basis for authorization. Record a bypass as skipped, and retain any earlier failures. Draft-only gates, CI, required previews, and check-in rules still apply.
- A changed PR head invalidates candidate-bound checks and CI and preview evidence for the old head, and requires review of the diff since the last reviewed head. A changed base invalidates mergeability and checks tied to the old merge candidate, and requires review of the new diff if it changes. Head-only evidence may carry forward when its inputs are unchanged and repo policy allows it. Recheck the provider’s latest approval and merge gates before check-in.
- If someone pushes outside Phalanx, reconcile the new head and revalidate it. Continue when the changes fit the work item. Ask for judgment if scope or intent is unclear. Preserve the external change rather than overwriting it.

## Execution status

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running: Eligible claim
    queued --> waiting_for_external: Cancellation or failure finalization
    queued --> waiting_for_input: User decision needed
    queued --> completed: Primary PR merge verified
    running --> queued: Stage advances or bounded retry
    running --> waiting_for_input: Human judgment required
    running --> waiting_for_external: External result pending
    running --> completed: Completion evidence verified
    waiting_for_input --> queued: Reply or retry
    waiting_for_input --> waiting_for_external: Cancellation or failure finalization
    waiting_for_input --> completed: Primary PR merge verified
    waiting_for_external --> queued: Event or recheck makes work runnable
    waiting_for_external --> waiting_for_input: Human action required
    waiting_for_external --> completed: Reviewed report stored or completion verified
    waiting_for_external --> failed: Failure report stored
    waiting_for_external --> cancelled: Cancellation report stored
    completed --> [*]
    failed --> [*]
    cancelled --> [*]
```

| Event | Server behavior |
| --- | --- |
| Eligible claim | Check dispatch pause, capacity, repo and runtime eligibility, and all upstream milestones in the claim operation. Assign one current lease and a new attempt only when all prerequisites are satisfied. Bind the claim to their exact executions and output revisions. A blocked item retains the unmet prerequisite and reason. A failed or cancelled prerequisite prompts recovery, redirection, or cancellation of the dependent work. |
| Prerequisite satisfied | Recheck all prerequisites and make the dependent item eligible when its last required output is verified. Keep unrelated items runnable. Reconcile missed notifications by checking durable milestone records before claims and on recheck. |
| Valid stage result | Check attempt ID, current lease, state version, stage, and evidence. Atomically record the outcome and timeline event. Queue the next runnable stage or persist the appropriate wait or final result. If the runner offered a fresh lease for that stage and it is runnable now, claim it for the same runner in the same transaction (a `claim` event with `continued_from`) so the item keeps its slot; otherwise record the `capacity_release` reason on the result. Apply the final-output guard before any terminal transition. |
| Retryable failure or expired lease | Reconcile the known worker session, checkpoint, and outputs. If the writer is still running or has a verified result, adopt the same open attempt under a recovery claim. The claim replaces the expired lease secret and is accepted only after expiry, so it cannot race a renewal. Retry a stopped attempt with bounded backoff. Unknown session state warrants a recheck before replacement. A late result cannot advance the item on an expired lease. Before repeating a GitHub operation, reconcile what happened. |
| Human judgment required | Save one focused question, the options, any proposed default, and the handoff. Release the slot and notify the user. A reply queues the stage again with that context. |
| External gate pending | Save the gate and expected revision, release the slot, and recheck periodically as well as on events. A result schedules the work needed to continue. Escalate persistent stalls or conflicting evidence. |
| Repeated lack of progress | Stop repeating the same ineffective attempt. Try a scoped recovery with a different path when evidence supports it. Otherwise ask for a decision with prior attempts and a recommended next action. Release the slot and keep unrelated eligible work running. Reservations for live or uncertain processes still apply. The user can reply, retry, redirect, or close the execution. |
| Runner relay stalled | The state and lease stay unchanged. When the runner's runtime relay has taken no waiting request for the bounded stall time, the runner attaches the stall reason to each affected attempt's lease renewal. The server shows it as each item's wait reason, and the runner notifies the owner. When the relay takes requests again, the next renewal clears the reason. Work continues without a restart or a new attempt. |
| Primary PR merged | Verify the PR, item branch, target and a recorded final head. Close a queued or waiting execution as delivered with the merge commit. Fence a running attempt with a merge stop; its stop confirmation closes the execution and releases the slot once, and late results are rejected. An unrecorded head waits for the owner instead. Deduplicate repeated reads of the same merge. |
| Cancel requested | Stop new work. For active work, record the cancellation request and reconcile any in-flight publication before marking it cancelled. If the merge already succeeded, record that verified outcome. |
| Finalize a report | Record the terminal intent and release the attempt before storage. The server retries independently of dispatch. Store or reuse the exact report and close the execution in one transaction. |
| Rerun a closed execution | Create a linked execution with an explicit target. Preserve history and reconcile any existing branch, PR, or report before repeating effects. |

Before yielding a slot, record the workspace, candidate revision or local checkpoint, relevant validation evidence, and remaining work. Keep the item on the same runner while a required checkpoint exists only there. If that runner cannot recover, ask whether to rebuild from the last durable checkpoint or close the execution.

## Final output guard

- A `completed`, `failed`, or `cancelled` execution always has exactly one final output. A running, queued, or waiting execution may have progress notes, a draft report, or a PR link without a final output. The requested output type remains in the work item even if its final output differs.
- On successful PR work, the final output is the verified PR link and head, with the merge commit when the completion target is `checked_in`. On successful report work, the final output is the stored reviewed Markdown report.
- On failure or cancellation without the requested deliverable, the final output is a Markdown report with the disposition, last stage, what was attempted, the blocker or reason for stopping, partial artifacts, and any useful next action. A partial PR can be linked from the report. The server can produce the report from the execution history even when the worker has exited or work was cancelled before the first claim.
- Store the final output reference and terminal status together. If output storage fails, keep the execution open with a retryable finalization step. Reuse an existing matching artifact and leave a conflicting artifact intact for investigation. The server retries after 2, 4, 8, and 16 seconds. After five recorded failures, the execution wait and timeline show the blocker. The owner can restore database access or resolve the report integrity issue, then POST the current expected version to the execution’s `retry-finalization` endpoint. A full database outage leaves the intent due and appears in server logs while the API cannot record retry history.

## Transition tests

Cover a report and a PR through completion, ready-only without merge, auto-merge off, pre-PR checks passing or explicitly bypassed, a required CI failure after bypass, PR-only preview checks, a revision loop that fixes a conflict, a failed check and new comments without returning to draft or to development, changed heads or bases, and a retry that discovers an existing PR or successful merge. Exercise duplicate submissions and events, a stale worker result, a lost external notification, a failed prerequisite, interruption during cancellation, and a review loop with no progress. Verify that failure and cancellation produce a report, including cancellation before the first claim, and that a storage outage leaves finalization retryable. For every active state, verify a next action, a persisted resume trigger, or an explicit way to close the execution.

For dependency coverage, test parallel prerequisites across repos, a downstream item waiting for the last prerequisite, ready-only evidence held until a required merge, a failed upstream item that produces a report, cycle rejection, and eligibility changes after steering.
