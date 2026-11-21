# Phalanx execution and implementation plan

Implement Phalanx in stages, starting with a real HTTP server running locally and a Captain-launched muster in Codex. The first end-to-end release should produce a Markdown field report and a checked-in PR across two registered repos, recover from an interruption, and resume after a user reply. Read [SPEC.md](SPEC.md) for the design and [STATE_MACHINE.md](STATE_MACHINE.md) for authoritative transitions. The HTML view illustrates those rules.

## 0 Validate the integration assumptions

- **Visible Codex chats.** Launch two worker chats from a Captain in their target projects. Capture chat IDs, a result, and a question answered in the worker chat. Stop the Captain while a worker is active, then test reattaching, collecting a completed result, and handling an unreachable chat. Record which steps use supported interfaces and need user involvement. If automatic reattachment cannot be verified, start with an explicit recovery action in the Captain chat and keep an unknown attempt from being redispatched blindly.
- **GitHub workflow.** Use a disposable branch or test repo to open a draft, confirm which draft-only checks and previews run, mark it ready for review, and observe checks and approvals on the new head. Test an outside push and a base change. Identify the GitHub operations the runner will perform and how its permissions will be scoped.
- **Decisions before implementation.** Choose the server language and storage library, first notification channel, initial aging and retry settings, and how to report a prolonged finalization outage. SQLite and periodic polling are reasonable starting candidates. Keep advanced integrations out of this milestone.

## 1 Build the server and durable state

- Create a standalone HTTP process with a configurable bind address, persistence path or connection string, and authentication. Start with one owner credential and a separate runner credential. Keep Git authentication and local checkout paths on runners. Document local startup and reserve a configuration path for HTTPS, persistent storage, and backups when hosted.
- Persist registered repos, work items and executions, dependency links and required milestones, attempts and leases, revision-bound evidence, questions and replies, output references, and transition history in one store. Add schema migrations and backup and restore instructions. Keep claim and transition transactions in the same store as the work item.
- Expose HTTP operations for creating, inspecting, listing, claiming, renewing, reporting a stage result, replying, cancelling, and rerunning. Include idempotency keys for submissions and retried operations. Return the item ID, stage, status, and reason whenever an item is waiting. Use a thin CLI for basic intake and status so the same API can later accept phone submissions.
- Implement transitions from the Markdown rules as one server-side, testable decision function. Check stage, attempt, lease, and expected state version before accepting results. Persist state transitions and history together. Write tests from the documented event, guard, outcome, and recovery cases. Check diagram and HTML parity when rules change.
- **Exit:** Demonstrate a CLI-created item surviving a server restart and duplicate delivery. Prove that stale results cannot advance the item and that retries produce one recorded result.

## 2 Implement scheduling and workspace reuse

- Register per-device repo access and runtime capabilities. Atomically claim one runnable stage when a runner has capacity. Enforce one active claim per item across devices, dispatch pause, prerequisites, retry delays, and local-only checkpoint affinity. Keep concurrency limits scoped to each runner initially.
- Rank eligible work by aged priority, then oldest `ready_since`, then stable item ID. Start with urgent, normal, and low. Preserve age through ordinary claim contention, start a new clock on each new runnable stage, and back off retries. Return a reason and elapsed wait for ineligible work.
- Support fan-out and fan-in through multiple prerequisites per work item. Validate link updates for cycles. Check that all required upstream milestones are verified before dispatch, and include their outputs in the work packet. Recheck blocked items on upstream completion and allow versioned edits to queued work when the owner steers it.
- Create an isolated worktree and deterministic item branch for PR work. Record runner, branch, and candidate SHA, plus any local-only checkpoint. Reuse on the same device after verifying state. On another device, reconstruct from a durable fetched SHA and the handoff. Preserve unrecorded edits and clean up closed worktrees only after verifying they are safe to remove.
- **Exit:** Two devices compete for one item without double-claiming it, concurrently work different items, and hand off one item after a published checkpoint. A downstream item waits for two parallel upstream items and starts only after the last required milestone, including a merge when specified. A load test with a steady stream of urgent items shows that older normal and low items eventually run. A local-only checkpoint remains pinned and explains its wait.

## 3 Run visible worker stages

- Implement a small Codex execution adapter for a Captain-launched muster. Pass one stage assignment and evidence requirements per claim. Record a durable association between the attempt and visible worker chat. Workers prepare artifacts and return structured outcomes. The runner holds backend claims and interprets the adapter output for the server.
- Track interrupted launch and observation separately. If a known chat is active, reattach. If it finished, verify and submit its result through an authorized recovery claim. If it stopped, resume from the checkpoint. When state is unknown, recheck before assigning a replacement and escalate only if it stays unclear.
- Save questions and responses on the item. Release the worker slot while waiting. Start with one notification route and a canonical API reply, then test whether a reply in Codex can be captured reliably. Validate work packets, outcomes, and revisable evidence independently of the adapter. Keep Claude Code and GitHub Copilot CLI as P2 adapters.
- **Exit:** A visible worker produces a field report, another pauses and resumes after input, and a fresh Captain turn recovers an interrupted attempt without prematurely launching duplicate work.

## 4 Complete the PR lifecycle

- Put GitHub writes behind one runner-owned path per item. Verify the claim and candidate revision before pushing, opening, promoting, or merging. Workers prepare commits and report evidence. Track operation intent and reconcile what GitHub recorded after an uncertain response. Reuse an existing matching PR. After bounded lookup, retry uncertain creation rather than drop the PR. Select and record a primary if duplicates appear, and surface the others. Reconcile uncertain merges before any retry.
- Open a draft after development produces a candidate commit. Use the [required attribution](SPEC.md#dispatch-and-execution). Run self-review and pre-validation. Record any permitted skip or bypass and keep failures visible. Publish updated candidates to the existing PR and mark it ready after draft-only checks pass. Keep an already promoted PR open for review when updating it.
- Observe expected CI and required previews. Distinguish unconfigured CI from missing expected CI. Run the independent review on the current diff. Reconcile feedback, conflicting revisions, stale evidence, and failed checks using the documented loops. Revalidate outside pushes and ask for input if the new scope is unclear. Poll pending external results on a schedule so missed notifications cannot strand an item.
- Check in only when authorized, using GitHub’s required checks and approvals on the current revision. Verify the recorded merge commit. For ready-only work, finish with verified PR identity and head evidence. Exercise cancellation during an in-flight GitHub operation and a merge response that is lost.
- **Exit:** A draft PR goes through promotion, CI, independent review, and a fix or conflict loop. It publishes exactly one verified merge for the authorized test, or yields a verified ready-only PR for a separate test. Confirm the attribution appears once.

## 5 Harden finalization and prepare hosting

- Persist the final PR reference or report and terminal status together. Generate failure and cancellation reports from durable history, including work cancelled before the first claim. Make finalization reviewable and retryable without rerunning the worker. Exercise storage failure before and after an artifact was written. Decide when to notify for a sustained outage and keep output-less work open for recovery.
- Add an operator view or CLI query showing stage, claim owner, wait reason and age, active attempt link, primary PR or field report, and recently failed actions. Validate the backlog and recovery flows with workload above available concurrency. Run the full lifecycle tests against real test checkouts after the simulated fault tests pass.
- Deploy the same HTTP server with configured authentication, HTTPS, persistence, and backups. Verify restore, then point an existing runner at the hosted URL and rerun the first milestone. Add phone intake after validating its supported integration and authentication. Keep runners and their repo credentials on local machines.
- **Exit:** A storage outage cannot silently discard work or close an item without an output. Two local runners can use the hosted API with the same work packet and state rules.

## Later campaign planning

Use the v1 dependency model to group work under a campaign goal. Add decomposition and re-evaluation of remaining work after verified check-ins or owner steering. Keep that planning layer separate from stage dispatch. It can propose or update queued work through the same versioned work-item operations. V1 acceptance depends on interdependent execution, and campaign creation and automatic replanning can follow later.
