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
| PH-05 | Server-owned transition engine in [PR #5](https://github.com/caltseng/phalanx/pull/5). | Wire phase decisions to durable intake, history, and versioned results. |
| PH-10 | Local worktree allocation, reuse, and safe cleanup in [PR #9](https://github.com/caltseng/phalanx/pull/9). | Connect workspace reservations and cleanup to the runner and queue. |
| PH-11 | Codex work packets and bounded attempt evidence ingestion in [PR #10](https://github.com/caltseng/phalanx/pull/10). | Wire live Captain launch receipts and observation to runner claims, results, and recovery. |
| PH-13 | Runner-side GitHub client primitives in [PR #6](https://github.com/caltseng/phalanx/pull/6) and conditional Git ref publication in [PR #15](https://github.com/caltseng/phalanx/pull/15). Draft workflow and recovery in [PR #22](https://github.com/caltseng/phalanx/pull/22), trusted observation API in [PR #23](https://github.com/caltseng/phalanx/pull/23), and a runner stage that checks the current claim and submits verified drafts idempotently. | Route attended claims to the stage, persist the development base checkpoint, and wire non-success outcomes and recovery. |
| PH-17 | Authenticated keyset pages for open and recently closed executions, plus the selected execution timeline. | Wire the board adapter and map prerequisite and worker chat data when their read contracts are available. |
| PH-18 | Operator board component with browser fixtures in [PR #8](https://github.com/caltseng/phalanx/pull/8). | Connect it to the authenticated PH-17 read API and package its static assets. |

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
- The attended single-slot bridge prepares development and report-drafting launches and relays verified results. Captain resolves the app project, records native receipts, and attests that the writer stopped. Dispatch filters claims by supported stage. Report review and storage, wider PR stage coverage, and a two-worker desktop run remain.
- When PH-08 lands, send the worker question, options and handoff as its structured `question` result, carry the recorded question and owner answer into the next claim and work packet, and verify a question, reply and resumed attempt through the attended CLI. Until that integration, the attended runner submits the question and handoff in the owner wait reason.
- Launch and observe visible worker chats from a user-started muster. Pass one stage assignment, the workspace and handoff, and evidence requirements. Normalize the result into the shared schema. Keep leases and GitHub writes with the runner.
- **Done when:** Two visible workers run in separate slots and return independently validated stage outcomes. The adapter exposes each chat reference and releases a slot when the stage ends or pauses.

### PH-12 Recover interrupted sessions and enforce real capacity

- **Depends on:** PH-08, PH-10, PH-11. **Output:** PR.
- Record launch intent before spawning, attach the chat ID, and keep active or uncertain local sessions counted while reconciling. Reserve their worktrees until stopped or accounted for. Keep heartbeat scheduling independent of chat observation. Adopt a verified finished result through a recovery claim.
- **Done when:** Interrupting the Captain on either side of launch or stalling the API never lets a replacement write in the same worktree or oversubscribe the machine. A reply and a finished worker result survive restarting the Captain.

## PR and report delivery

The independent restart core consumes the original attended journal row, fresh native observations, verified local checkpoint evidence, and the server's expired-attempt list. It keeps uncertain reservations visible, retries temporary uncertainty at 2, 4, 8, and 16 seconds, and records an owner action when further automatic checks cannot help. Stopped-attempt requests retain their exact body and idempotency key until an authenticated response matches the execution and next version, or confirms the following version with its stored cancellation or failure report.

After the attended runner lands, call the core before admitting replacement work. Gather Codex and Git evidence outside the journal transaction, then compare the original reservation and receipt inside it before applying the decision. Persist the stop request before sending it, archive only after verified acknowledgment, and expose retries, owner waits, and unmatched server attempts in runner status. Resolve definite version conflicts against server history before changing a recorded stop request. The runner still needs fresh-claim adoption for verified expired results, operator notification, and the live restart and concurrent capacity pilot.

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
- **API follow-up:** Wire independent review, feedback, approvals, and mergeability to the verified primary PR and revision before assigning check-in. Verify a merge against the recorded pre-merge revision and durable write intent.
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

## After the local release

| Item | Depends on | Deliverable and acceptance |
| --- | --- | --- |
| **PH-20 Host the API** | PH-19 | Deploy the same server with HTTPS, persistence, backups, and restore validation. Point a local runner at the hosted URL and repeat the v1 muster through the same API. |
| **PH-21 Add phone intake** | PH-20 | Verify the supported ChatGPT integration and authorization. File an item by phone and return its durable link. |
| **PH-22 Support multi-device execution** | PH-19 | Validate claims across machines, checkpoint portability, affinity, GitHub write reconciliation, and device loss with work in flight. |
| **PH-23 Add alternate runtimes** | PH-11, PH-19 | Verify and add Claude Code and GitHub Copilot CLI adapters as needed. Pass the shared stage contract and recovery tests for each supported runtime. |
| **PH-24 Add campaigns** | PH-07, PH-19 | Group items under a goal. Propose parallel and dependent items, then revisit queued work after verified check-ins or steering. Preserve history and apply updates through versioned work-item operations. |
