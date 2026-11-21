# Phalanx implementation work items

Each item produces a reviewable PR or a Markdown field report. Dependencies below are required milestones. A prerequisite PR must be checked in unless the dependent explicitly consumes a pinned reviewed revision. Independent items can proceed in parallel. Revisit queued items after an upstream check-in or new steering. [SPEC.md](SPEC.md) defines the product and [STATE_MACHINE.md](STATE_MACHINE.md) defines transitions.

V1 targets a large queue and many workers on one local machine with visible Codex chats, backed by a real HTTP server running locally. It is complete when one muster across two repos produces a field report and a checked-in PR, recovers an interrupted worker, resumes after user input, and passes the single-machine load and fault gates. Cloud hosting is a follow-on deployment of the same API. Multi-device execution and handoff, campaigns, and alternate runtimes are P2.

## Implementation checkpoints

| Work item | Merged deliverable | Remaining verification or integration |
| --- | --- | --- |
| PH-01 | [Codex recovery field report](docs/pilots/codex-recovery.md) in [PR #3](https://github.com/caltseng/phalanx/pull/3). | Exercise interrupted launch and reply recovery against the integrated runner. |
| PH-02 | [GitHub lifecycle field report](docs/pilots/github-lifecycle.md) in [PR #2](https://github.com/caltseng/phalanx/pull/2). | Check real repo preview and CI triggers and race handling in the integrated lifecycle. |
| PH-03 | [Local operating envelope](docs/pilots/local-operating-envelope.md) in [PR #4](https://github.com/caltseng/phalanx/pull/4). | Confirm the real worker capacity and repeat the synthetic benchmark against the full service. |
| PH-04 | Standalone FastAPI and SQLite server, migrations, bearer roles, and verified backups in [PR #7](https://github.com/caltseng/phalanx/pull/7). | Use the same API process for subsequent deployment. |
| PH-05 | Transition engine and durable intake, history, and stage results in [PR #5](https://github.com/caltseng/phalanx/pull/5) and [PR #13](https://github.com/caltseng/phalanx/pull/13). | Verify both outputs through the integrated runner. |
| PH-06 | Atomic indexed claims, aging, pacing and lease reservations in [PR #17](https://github.com/caltseng/phalanx/pull/17) and [PR #18](https://github.com/caltseng/phalanx/pull/18). | Measure mixed HTTP claim, renewal and read latency with the final stage selection. |
| PH-07 | Exact milestone pins, queued steering, and durable bounded dependency refresh in [PR #20](https://github.com/caltseng/phalanx/pull/20). | Exercise a large, interrupted fan-out with unrelated eligible traffic. |
| PH-08 | Durable waits, owner inbox and replies, and versioned external rechecks in [PR #24](https://github.com/caltseng/phalanx/pull/24). | Drive due rechecks and owner notifications from the Captain loop. Use [PR #39](https://github.com/caltseng/phalanx/pull/39) for ready-phase gate wait evidence. |
| PH-09 | Durable report and disposition finalization in [PR #19](https://github.com/caltseng/phalanx/pull/19). | Exercise finalization while the runner is offline and under store interruptions. |
| PH-10 | Worktree allocation and reuse in [PR #9](https://github.com/caltseng/phalanx/pull/9), wired to attended claims in [PR #18](https://github.com/caltseng/phalanx/pull/18). | Reconcile reservations before cleanup following interruption. |
| PH-11 | Stage packets in [PR #10](https://github.com/caltseng/phalanx/pull/10), attended adapter in [PR #18](https://github.com/caltseng/phalanx/pull/18), and numbered local slots in [PR #34](https://github.com/caltseng/phalanx/pull/34). | Add the ongoing Captain loop, independent renewal and the remaining stage routing. Verify two native chats. |
| PH-12 | Exact stopped-attempt decisions in [PR #27](https://github.com/caltseng/phalanx/pull/27) and journal/API recovery in [PR #38](https://github.com/caltseng/phalanx/pull/38). | Drive native observation and reattach automatically. Adopt saved valid results under server authority after expiry. |
| PH-13 | Recoverable draft creation and authenticated observation in [PR #22](https://github.com/caltseng/phalanx/pull/22), [PR #23](https://github.com/caltseng/phalanx/pull/23) and [PR #31](https://github.com/caltseng/phalanx/pull/31). Attended draft dispatch pins the development base in [PR #40](https://github.com/caltseng/phalanx/pull/40). | Route base drift, conflicts and uncertain writes to the lifecycle, and publish updated candidates to the primary PR. |
| PH-14 | Validation policy, exact checks, promotion, and candidate-local operations in [PR #12](https://github.com/caltseng/phalanx/pull/12), [PR #26](https://github.com/caltseng/phalanx/pull/26), [PR #30](https://github.com/caltseng/phalanx/pull/30), [PR #35](https://github.com/caltseng/phalanx/pull/35), and ready-phase validation in [PR #39](https://github.com/caltseng/phalanx/pull/39). | Connect trusted repo policy, local validation, draft promotion, and ready-phase dispatch through the Captain loop. |
| PH-15 | Direct check-in coordinator in [PR #29](https://github.com/caltseng/phalanx/pull/29) and exact review, disposition and merge evidence API in [PR #32](https://github.com/caltseng/phalanx/pull/32). | Add the independent review, feedback and conflict loop, and runner-side durable merge intent. |
| PH-16 | Attended report drafting, independent review and runner-only storage with localhost interruption coverage in [PR #37](https://github.com/caltseng/phalanx/pull/37). | Verify native report delivery during the two-repo muster and adopt interrupted review evidence. |
| PH-17 | Keyset pages and a bounded timeline in [PR #21](https://github.com/caltseng/phalanx/pull/21), with prerequisite blockers in [PR #36](https://github.com/caltseng/phalanx/pull/36). | Add verified native chat links and detailed provider recheck status to the operator read contract. |
| PH-18 | Operator board and authenticated API connection in [PR #8](https://github.com/caltseng/phalanx/pull/8) and [PR #28](https://github.com/caltseng/phalanx/pull/28). | Follow a running muster with waits, worker links and final outputs. |
| PH-19 | Deterministic regression with 2,000 parked items, 24 claimants, eight slots and continued urgent intake in [PR #25](https://github.com/caltseng/phalanx/pull/25). | Run mixed authenticated load and fault checks with the integrated runner. |

## Integration pilots

### PH-01 Prove visible Codex execution and recovery

- **Depends on:** Nothing. **Output:** Field report.
- Launch two visible workers in their target projects and collect an outcome plus a question and reply. Interrupt Captain observation before and after saving a chat ID. Determine which supported operations can find, reattach to, and recover a worker. Identify how the worktree is selected.
- **Done when:** The report identifies an attended v1 path, the smallest explicit recovery action if automatic recovery is unavailable, and how to keep a possibly live worker counted and prevent a replacement writer. Include evidence or a concrete blocker for each capability.

### PH-02 Map real GitHub draft and check-in gates

- **Depends on:** Nothing. **Output:** Field report.
- In a disposable repo or branch, verify draft creation, draft-only checks, push-dependent previews, promotion, required CI, and authorized check-in. Test an outside push and a base change. Check which Git and GitHub writes accept revision preconditions and which outcomes can be recovered by inspection.
- **Done when:** The report distinguishes checks before upload, checks on the draft candidate, and checks after promotion. Record how the runner can verify the expected head and merge candidate, and how it should handle a provider guarantee it cannot establish.

### PH-03 Define the local operating envelope

- **Depends on:** Nothing. **Output:** Field report.
- Measure pilot hardware, worker startup and memory, representative build and disk use, and queue operations with representative blocked and historical rows. Propose concurrency, aging, retry, and polling defaults, plus a load-test workload. Select the initial server stack and SQLite library with a short decision record.
- **Done when:** The report includes measurable targets for dispatch and renewal latency, oldest eligible wait, backlog recovery time, and resource headroom. Separate synthetic load from the observed number of viable real Codex workers.

## Server and queue

### PH-04 Bootstrap the HTTP service and store

- **Depends on:** PH-03. **Output:** PR.
- Create the runnable HTTP service, config for bind address and storage, owner and runner authentication, schema migrations, and local startup instructions. Keep Git credentials and checkout paths on the runner. Add a minimal health endpoint and backup and restore procedure.
- **Done when:** A second process can call the server over HTTP, authorization boundaries are tested, and data persists through restart and backup restore.

### PH-05 Implement work intake and stage transitions

- **Depends on:** PH-04. **Output:** PR.
- Add registered repos, work items, executions, attempts, revision-bound evidence, and an append-only transition history. Implement create, inspect, stage result, cancel, and rerun operations. Define one server decision function from the authoritative Markdown rules. Use request idempotency and expected versions for state-changing operations.
- **Done when:** A repeated intake returns the same item, stale results cannot advance it, and transition tests cover PR and report routes, including loops, pauses, and durable recovery. The CLI can file and inspect an item.

### PH-06 Implement leases, capacity, and fair queue claims

- **Depends on:** PH-05. **Output:** PR.
- In a short transaction, claim the highest-ranked eligible stage and assign one lease. Enforce capacity and dispatch pause. Rank by aged priority, oldest eligible `ready_since`, then stable ID. Preserve age through claim contention and record retry delays separately. Index candidates so claims and lease renewals do not scan retained history or the blocked backlog.
- **Done when:** Competing local slots cannot claim the same item or exceed the configured limit. Old eligible items run during sustained urgent intake. Measure claim and heartbeat latency under the PH-03 workload.

### PH-07 Add prerequisites and steering

- **Depends on:** PH-05, PH-06. **Output:** PR.
- Store multiple dependencies and required milestones across repos with indexed reverse links. Reject cycles in the transaction that changes links. Bind claims to exact upstream execution and output revisions. Treat a ready-only milestone as evidence for a pinned reviewed revision and carry that reference forward. Reconcile missed prerequisite events and support versioned edits to queued work.
- **Done when:** Two upstream items can run in parallel, a dependent waits for the last required milestone, and a failed upstream report does not satisfy the dependency. Concurrent cycle edits and a prerequisite rerun racing a claim are covered.

### PH-08 Implement waits, notifications, and replies

- **Depends on:** PH-05. **Output:** PR.
- Persist a focused question and handoff before releasing the slot. Provide a canonical owner reply through the API and CLI, plus one notification route. Index external recheck times, process due work in bounded batches with jitter and provider-directed backoff, and coalesce duplicate events by gate and revision. Surface why an item is waiting and what will resume it.
- The durable slice provides the owner inbox, versioned replies, and runner endpoints for due waits and same-stage rechecks. Remaining integration includes active runner polling, provider observations and event coalescing by gate and revision, and the overdue-wave load check against lease renewals.
- **Done when:** Replying queues the correct stage exactly once, a missed event is recovered by a recheck, and duplicate events or a wave of overdue waits neither occupy worker slots nor drown out lease renewals.

### PH-09 Make finalization durable

- **Depends on:** PH-05. **Output:** PR.
- Use a retryable finalization operation keyed by execution ID. Persist each terminal outcome and final output together. Generate failure and cancellation reports from durable history when necessary. Keep finalization retries independent of worker slots. Define when a prolonged outage calls for operator attention.
- **Done when:** Storage failure and restart do not rerun agent work, duplicate outputs, or close any execution without a PR or report. Include cancellation before first claim and a merge that succeeded before the local response was lost.
- **Report finalization implemented:** Durable server-side finalization for reviewed reports, cancellation, and explicit failure. Provider-verified merge reconciliation and delivery across a lost merge response remain in the GitHub integration.

## Local execution

### PH-10 Isolate and reuse local worktrees

- **Depends on:** PH-04, PH-06. **Output:** PR.
- Verify registered remotes, allocate an item-specific topic branch and worktree, and record recoverable checkpoints. Reuse a verified worktree across stages, preserve unrecorded changes, and keep local-only checkpoints associated with their owner. Clean up only after verifying a closed worktree is safe.
- **Done when:** Unrelated items can work in the same repo with separate worktrees. Recovery preserves dirty work, and low disk or an unavailable checkout produces a visible reason without blocking eligible work in other repos.

### PH-11 Build the visible Codex adapter

- **Depends on:** PH-01, PH-06, PH-10. **Output:** PR.
- The attended bridge uses independent numbered slots and prepares development and report-drafting launches and relays verified results. Captain resolves the app project, records native receipts, and attests that the writer stopped. Dispatch filters claims by supported stage. Report review and storage, wider PR stage coverage, and a two-worker desktop run remain.
- The attended runner submits questions and handoffs through PH-08 and includes the recorded question and owner answer in the resumed work packet. Verify the roundtrip in a live Captain chat during the attended pilot.
- Launch and observe visible worker chats from a user-started muster. Pass one stage assignment, the workspace and handoff, and evidence requirements. Normalize the result into the shared schema. Keep leases and GitHub writes with the runner.
- **Done when:** Two visible workers run in separate slots and return independently validated stage outcomes. The adapter exposes each chat reference and releases a slot when the stage ends or pauses.

### PH-12 Recover interrupted sessions and enforce real capacity

- **Depends on:** PH-08, PH-10, PH-11. **Output:** PR.
- Record launch intent before spawning, attach the chat ID, and keep active or uncertain local sessions counted while reconciling. Reserve their worktrees until stopped or accounted for. Keep heartbeat scheduling independent of chat observation. Adopt a verified finished result through a recovery claim.
- **Done when:** Interrupting the Captain on either side of launch or stalling the API never lets a replacement write in the same worktree or oversubscribe the machine. A reply and a finished worker result survive restarting the Captain.

## PR and report delivery

The independent restart core consumes the original attended journal row, fresh native observations, verified local checkpoint evidence, and the server's expired-attempt list. It keeps uncertain reservations visible, retries temporary uncertainty at 2, 4, 8, and 16 seconds, and records an owner action when further automatic checks cannot help. Stopped-attempt requests retain their exact body and idempotency key until an authenticated response matches the execution and next version, or confirms the following version with its stored cancellation or failure report.

Integrate the restart core from [PR #27](https://github.com/caltseng/phalanx/pull/27) per occupied slot before admitting replacement work. Match expired attempts across all local slots, including replaying an uncertain claim in its original slot to learn the attempt identity. Gather Codex and Git evidence outside the journal transaction, then compare the original slot's lease, attempt and receipt inside it before applying the decision. Slot 1 keeps the original journal layout and pending claims retain their original request filters. Persist the stop request before sending it, archive only after verified acknowledgment, and expose retries, owner waits, and unmatched server attempts in runner status. Resolve definite version conflicts against server history before changing a recorded stop request. The runner still needs fresh-claim adoption for verified expired results, operator notification, and the live restart and concurrent capacity pilot.

### PH-13 Publish drafts and reconcile GitHub writes

- **Depends on:** PH-02, PH-10, PH-11. **Output:** PR.
- Record operation intent and expected remote state. After development records a candidate commit, push it and open a draft with the required Phalanx attribution. Reuse a matching PR. If bounded reconciliation cannot confirm creation, retry so the work gets a PR. Record one primary and surface duplicates. Use revision preconditions where supported and reconcile unresolved writes before issuing a conflicting operation.
- **Done when:** A lost response to draft creation produces a primary PR and repeated publication keeps one attribution footer. Test a writer paused after its claim check, a lease expiry, and an outside push. The old attempt cannot silently overwrite newer work and any unresolved write remains visible for reconciliation.

- **API slice:** The [runner observation contract](docs/runner-pr-observations.md) records verified draft publication and surfaced duplicates. Connect the runner's durable publication intent and reconciliation to this route.

### PH-14 Validate and promote the current candidate

- **Depends on:** PH-02, PH-08, PH-13. **Output:** PR.
- Run self-review and candidate-local pre-validation, recording allowed bypasses. Publish any updated candidate to the primary PR before checks that require that head. Run draft-only gates and then mark the draft ready for review. Observe required CI and previews for the current revision. Distinguish CI that is unconfigured from expected CI that is missing.
- **Done when:** Revised draft previews run on the intended head and promotion does not wait for checks that only start after it. A pre-validation bypass leaves required CI intact. If a promoted PR later needs a draft-only check that cannot run, follow the repo policy or expose the decision rather than wait indefinitely.
- Candidate-local runner operations prepare and collect self-review handoffs and run trusted repository checks in the claimed item's clean worktree. The result route stores review notes and individual check dispositions against the candidate. Dispatch wiring, local policy loading, and journal integration remain.
- Runner collection reads the primary PR and target tip around the check snapshot, verifies test-merge parents, and evaluates bounded, phase-specific provider evidence. The caller supplies complete phase policy, including whether a check uses the head or test-merge SHA, and an adapter for the configured preview provider and served revision. Policy discovery, provider-specific preview reads, promotion orchestration, and scheduled rechecks remain integration work.

- **API slice:** Draft and ready check evidence is bound to the primary PR and exact revision. Remaining integration includes scheduled external rechecks and runner policy and provider discovery.

### PH-15 Review, reconcile, and check in

- **Depends on:** PH-09, PH-12, PH-14. **Output:** PR.
- **API slice:** The [runner observation contract](docs/runner-pr-observations.md) records independent review and current gates against the primary PR, finalizes ready-only work, and records trusted merge verification against a persisted authorization and pre-merge revision. Runner integration still needs to supply provider correlation with its durable write intent, handle pending gates and candidate-changing fixes, and reconcile uncertain writes across claims.
- Run an independent review on the current diff, fix actionable feedback and routine conflicts, and loop through validation on changed revisions. Preserve outside changes and revalidate their scope. Respect ready-only and authorized merge targets, verify required checks and approvals and the expected head and base, and reconcile any uncertain merge before retrying.
- When evidence depends on the base, use the repo’s verified mechanism for checking the merge candidate. Revalidate or report a blocker if that assurance is unavailable.
- **Coordinator slice:** Verify exact primary identity and current gates, finish ready-only without a write, or reserve and request one authorized direct merge commit. Reconcile by reading the same PR and verifying the ordered base and head parents. Keep an ambiguous attempt reserved even if the PR still appears open.
- **Remaining integration:** Persist and retire reservations under the runner claim, wire trusted check and review observations and repository-specific policy, record verified results through the server lifecycle, and pilot a live feedback or conflict loop. Add separate adapters for other merge methods or required merge queues.
- **Done when:** A real PR goes through a feedback or conflict loop, required revision changes invalidate stale evidence, and a verified merge is recorded exactly once. A separate ready-only case completes with a verified PR and no merge.

### PH-16 Deliver field reports

- **Depends on:** PH-09, PH-11. **Output:** PR.
- Draft, review, and store Markdown reports with sources or evidence, limitations, and open questions where applicable. Persist reviewed draft checkpoints and use the same stage outcome and finalization rules as PR work.
- **Done when:** A report request completes end to end without a PR and the verified report can be retrieved with the runner offline. Retrying report storage does not redraft the report.

## Operator visibility and local release

### PH-17 Expose bounded queue and history reads

- **Depends on:** PH-05, PH-06, PH-09. **Output:** PR.
- Add cursor-paginated queries for open work and recent closures, and a bounded item timeline query. Include repo, stage, execution status, wait reason and age, last progress, attempt reference, and verified output. Load detailed events on demand.
- **Done when:** The API serves a large backlog and history without reading it all for each refresh, and results remain accurate across pagination while items advance and close.

### PH-18 Build the operator work board

- **Depends on:** PH-08, PH-17. **Output:** PR.
- Implement the [operator view](SPEC.md#operator-view). Show active stages, actionable waits, and recently closed outcomes with final PR or report links. Select an item to inspect its transition history and attempt. Refresh from the API and expose stale or disconnected state.
- **Done when:** Users can follow a stage loop, find an item needing input, and open a recent success or failure output. A closed item appears in recent history with its last outcome, and persisted results remain browsable when the runner is offline.

### PH-19 Load-test and harden one-machine execution

- **Depends on:** PH-03, PH-07, PH-12, PH-15, PH-16, PH-18. **Output:** PR with test results.
- Sweep synthetic backlog and capacity separately from real Codex sessions. Test mostly blocked queues, steady urgent intake, dependency fan-out, a wave of CI waits, provider throttling, interrupted sessions, and finalization outages. Measure scheduling latency, age of eligible work, memory, disk, and restart catch-up. Restart during a batch of dependent wakeups and verify that no items remain incorrectly blocked. Count live builds and previews until they stop. Isolate writable build paths or preview ports where they collide and add narrower resource limits only where measurements justify them.
- **Done when:** The tested operating envelope is documented, starvation and overbooking tests pass, pending work recovers without silent loss, and the full v1 muster passes on one machine. Any limit or known degradation has a visible reason.
- During the load run, stall a PR on provider throttling, leave another item waiting on a failed prerequisite, and interrupt a worker. Verify that ready reports and independent PR work still advance. Lift the throttle, satisfy the prerequisite through recovery or replacement, and reconcile the interrupted session. Verify each affected item resumes from its recorded trigger and checkpoint during the active muster.

## Remaining v1 integration work

The merged components can be tested in isolation. The release still needs an attended muster that connects them through the same API and verifies delivery across two repositories. The acceptance criteria below refine the work items above and use the transitions in [STATE_MACHINE.md](STATE_MACHINE.md).

Execute the rows in table order. PH-04a and PH-03a come first so later slices merge on verified CI rather than local evidence. PH-11b then runs the first native muster before the remaining PR stages are wired, so desktop gaps surface early. Rows with numbered parts ship one part per PR.

- Merge a PR only when required CI passes on its exact head and an exact-head review has no open actionable findings. Live desktop and GitHub pilots add their evidence to the PR.
- Give each PR one acceptance focus. If review is still finding defects after two rounds, merge the verified part and turn the remaining findings into a follow-up row.
- Before PH-14a part 3, record the two acceptance repositories, their registration and their validation policies, including any preview gate. Phalanx can be one of them once CI is required on `main`.

| Work item | Implementation and acceptance |
| --- | --- |
| PH-04a Restore a green, gated baseline | Make the suite hermetic. Address bare test remotes with `git --git-dir`, because Git's `safe.bareRepository=explicit` makes the five `test_git_publication` errors exit with 128. Replace wall-clock timing in `test_input_wait_reply_survives_restart_and_queues_same_stage_once`, which passes alone and intermittently fails in full runs. Land the CI workflow from [PR #44](https://github.com/caltseng/phalanx/pull/44) and require its checks on `main` with a ruleset. The full suite must pass in CI on every matrix entry, and 10 reruns of the affected modules must pass. |
| PH-03a Make runner bootstrap reproducible without PyPI | Add hashes to `requirements.lock` and install with `--require-hashes`. Have CI build a hash-checked wheelhouse for each supported platform and Python version, including the editable-install build backend, and publish it as an artifact. Document an offline install that keeps TLS verification enabled. Add a runner preflight to the `attended` and `captain` commands. Before claiming work, it must report missing packages, required Git version and settings, credentials and API reachability as actionable blockers. On a machine that cannot reach PyPI, a fresh environment installed from the wheelhouse must pass the full suite. A missing token or package must stop the runner before it claims work. |
| PH-11a Dispatch and observe several native workers | Drive the existing numbered slots and isolated worktrees from a user-started Captain. Maintain exact native chat receipts, rotate bounded observation batches, and add lease renewal independent of chat observation and stage operations. Two occupied slots, a blocked third, and a completed slot must produce the same observed capacity in the runner and server. [PR #43](https://github.com/caltseng/phalanx/pull/43) implements this with a fixture desktop boundary. Merge it once its localhost tests pass in CI. |
| PH-11b Bundle the desktop bridge and run the first native muster | PH-01 found that only the Captain chat can call the Codex app's thread tools. PR #43 reaches them through a subprocess JSON boundary that has only a fixture. Record in SPEC how production requests reach the app. Prefer a Captain chat relay: the loop queues launch and observation requests, and the Captain chat runs them and records receipts through the CLI. Use an App Server subprocess only after its sidebar placement and identity equivalence are verified. Ship the bridge and run it and the fixture against one shared contract test. Acceptance also closes the remaining PH-01 checks. A single user-started muster must run two visible workers concurrently in the two acceptance repositories. A report item must reach a stored report, and a PR item must reach a published draft. One worker asks a question, which reaches Caleb in the Captain chat, and resumes with the reply. A Captain restart before and after a receipt keeps one slot and starts no replacement writer. Record the evidence in [the Codex recovery report](docs/pilots/codex-recovery.md). |
| PH-12a Reattach and adopt | Connect the local recovery decision to the Captain and the recovery API. Replay lost receipts, reattach to a known active writer, and preserve ambiguous reservations until verified. Adopt a stopped worker's valid checkpoint under server authority and test restart both before and after the launch receipt. A verified result must survive without redrafting or overwriting another attempt. |
| PH-13a Advance the draft stage | Drive the attended draft handler from the muster. Route base drift before publication back to development, route verified external movement to reconciliation, and recover uncertain writes under the exact item intent. Test a lost write or response without starting another development worker. |
| PH-14a Execute local and published validation | Run self-review and configured local checks on the candidate, capture permitted bypass dispositions, and publish reviewed updates to the primary PR. Collect draft policy and gate observations, promote, then collect ready-phase checks. Turn pending gates into durable due rechecks. Test fallback when draft-only evidence is missing on a ready PR. Ship it in four parts: (1) the trusted policy loader in [PR #42](https://github.com/caltseng/phalanx/pull/42); (2) self-review and local checks dispatched by the Captain loop with bypass dispositions; (3) publishing reviewed updates, draft gates and promotion; and (4) ready-phase checks, due rechecks and the missing draft-only fallback. The fallback records an owner decision wait rather than an automatic policy. Build a preview provider adapter in v1 only if an acceptance repository requires a preview. Otherwise preview requirements still fail closed and the adapter moves to P2. |
| PH-15a Connect review, reconciliation and check-in | Run a separate review stage and record the current revision. Resolve actionable feedback and conflicts, revalidate a changed revision, and derive merge authorization and base assurance from repo policy. Persist merge intent before the provider call and verify the resulting merge or report the unresolved state. Exercise ready-only disposition and the merge loop separately. Ship it in three parts: (1) the revision-bound review stage with ready-only disposition; (2) the feedback and conflict loop with revalidation; and (3) durable merge intent, the direct merge under the claim and reconciliation. |
| PH-16a Complete the field report path | PH-11b covers the native report route. Preserve a completed review across expiry or restart. Confirm revisions are immutable, storage retries do not redraft, and the final report remains readable with the runner offline. |
| PH-18a Surface waits and native chats | Show prerequisite blockers, scheduled rechecks, worker references, current PRs and final outputs in the board. Confirm that the owner can see the prompt and respond through the existing authenticated inbox or API, and that the resumed worker receives the answer and preserved context. |
| PH-19a Verify the integrated local operating envelope | Supplement [the scheduler probe](docs/pilots/local-operating-envelope.md) with mixed authenticated HTTP traffic and the dependency/wait/recovery fault cases above. Record latency and fairness separately from live worker capacity. Run the two-repository acceptance muster once the components are connected. |

## After the local release

| Item | Depends on | Deliverable and acceptance |
| --- | --- | --- |
| **PH-20 Host the API** | PH-19 | Deploy the same server with HTTPS, persistence, backups, and restore validation. Point a local runner at the hosted URL and repeat the v1 muster through the same API. |
| **PH-21 Add phone intake** | PH-20 | Verify the supported ChatGPT integration and authorization. File an item by phone and return its durable link. |
| **PH-22 Support multi-device execution** | PH-19 | Validate claims across machines, checkpoint portability, affinity, GitHub write reconciliation, and device loss with work in flight. |
| **PH-23 Add alternate runtimes** | PH-11, PH-19 | Verify and add Claude Code and GitHub Copilot CLI adapters as needed. Pass the shared stage contract and recovery tests for each supported runtime. |
| **PH-24 Add campaigns** | PH-07, PH-19 | Group items under a goal. Propose parallel and dependent items, then revisit queued work after verified check-ins or steering. Preserve history and apply updates through versioned work-item operations. |
