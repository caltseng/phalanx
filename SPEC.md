# Phalanx design spec

Phalanx lets its users dispatch work across registered repositories and have local agents carry it through to a Markdown report or pull request. Caleb is the first user, and the design must also serve other people and teams (see [Users and workspaces](#users-and-workspaces)). The backend is the durable source of truth. Agents run locally on machines with access to the relevant checkouts and credentials. The initial design should favor a small API, recoverable execution, and a flexible work-item format.

## Work items and repositories

- A work item has an ID, repository, goal, output type (`report` or `pull_request`), status, and timestamps. Optional fields include context links, acceptance criteria, a target branch, and dependencies on other work items. An API caller may provide an idempotency key so resubmitting the same request returns the original item.
- A registered repository has a stable ID and expected remote. Each runner maps that ID to a local checkout and verifies the remote before starting. Repository instructions apply to each run. An item waits if no eligible runner can access its repo.
- GitHub issue intake is an explicit local `ingest-issue` command for a registered `github.com` repository and issue number. The local client reads the issue and all comments with its own GitHub credential, then submits an immutable snapshot and source link through owner-authenticated API intake. The operator chooses the completion target; issue content cannot grant permissions, change runner configuration, or register repositories. A stable per-repository issue idempotency key prevents duplicate retry publication; edited snapshots conflict instead of replacing existing work. Polling, webhooks and ongoing synchronization are not part of this path.
- A **field report** is a Markdown deliverable with findings, sources or evidence where relevant, and open questions. PR items specify a completion target of `ready_for_check_in` or `checked_in`. A `checked_in` target merges automatically once GitHub's required reviews and checks pass on the verified head, unless the runner's auto-merge setting (on by default) is off, in which case the item completes with the PR marked ready. Repository rules still govern the merge.
- The requested output type defines the intended path. Every closed execution records exactly one final output, either a verified PR reference or a stored Markdown report. If work closes without the requested deliverable, store a disposition report explaining the failure or cancellation, what was attempted, and what remains. Include any partial PR as a reference in that report. Work still in progress can have intermediate evidence and no final output yet.

## Dispatch and execution

Use Phalanx terminology in operator-facing copy where it describes a concrete event.

| Term | Meaning |
| --- | --- |
| **Muster** | A batch of work launched from the Captain chat. |
| **Marshaled** | A work item has enough context to enter the queue. Dispatch gates still determine when it can be claimed. |
| **Phalanx advancing** | Work is moving to the next lifecycle stage after the server accepts the evidence. Name the stage and transition condition in the activity entry. |
| **Field report** | A Markdown deliverable, including the disposition report when work closes without the requested result. |
| **Spearheaded by Phalanx** | Attribution footer in the description of every PR Phalanx opens. |

Append `Spearheaded by [Phalanx](https://github.com/caltseng/phalanx)` as its own paragraph at the end of each PR description Phalanx creates. Keep the footer when updating that description and add it only once across retries.

1. An authenticated client files an item through the API and receives its ID. The API supports listing and inspecting work, claiming eligible items, renewing claims, posting progress and results, and responding to a question.
2. A runner requests an atomic claim for an eligible repo only when it has a free slot and dispatch is enabled. Agents can run concurrently, including on the same repo when their tasks are independent. A runner uses a separate worktree and topic branch for each PR item.
3. An agent reads the repo instructions, executes one assigned stage, and returns its outcome and evidence or a focused question. The server applies the transition rules before the item is eligible for another stage. A waiting item releases its worker slot and retains the question, prior work, and resume context.
4. A reply makes the item eligible again. A runner can resume from the recorded handoff and branch rather than require the original agent process to stay alive.

Each work item exposes both its lifecycle stage and its execution status. For example, `pr_validation / waiting_for_external` shows that a published PR is waiting for CI or a preview.

## Development lifecycle state machine

The Markdown specs define lifecycle and execution transitions. The transition tables and rules in [STATE_MACHINE.md](STATE_MACHINE.md) are authoritative. Mermaid and the [HTML visualization](state-machine.html) illustrate those rules. Update the Markdown first when changing a transition, then bring the diagrams and tests into sync.

Workers try to self-resolve within scope and repository rules, escalating when human judgment or authorization is required or further attempts cannot make progress.

The server owns transitions. Each stage has required evidence, allowed next stages, and a recovery path. A worker reports an outcome and evidence for its current stage. The server validates the transition and atomically records the new state and timeline event. Stages that need agent work become eligible for another claim. Stages waiting for GitHub checks, reviews, or approval release their worker slot.

| Stage | Advance when | Recovery or loop |
| --- | --- | --- |
| `development` | Record a candidate commit. Open a draft PR for the first candidate or advance to `self_review` when a PR already exists. | Retry transient execution failures or pause for a focused decision. |
| `pr_creation` | Open and verify a draft PR, then advance to `self_review`. | Reuse a matching PR when found, and adopt an existing PR on the item branch: a foreign head returns to `development` to integrate its commits, and a closed PR asks the owner. After bounded reconciliation, retry uncertain creation so publication is not dropped. |
| `self_review` | The implementing agent checks the diff against the request and repo rules, with findings recorded. Advance to `pre_pr_validation`. | Return to `development` for findings that require changes. |
| `pre_pr_validation` | Run applicable local tests and live checks before promotion by default. Advance to `pr_promotion` after a pass or a recorded bypass allowed by repo policy. | Route actionable failures to `development` with the tail of each failed check's output. Development may return the candidate unchanged when a rerun should pass. For unavailable checks, try to restore validation or record a permitted bypass or blocker. |
| `pr_promotion` | Update and verify the candidate on the existing PR. While it is a draft, mark it ready for review after draft-only checks pass. Advance to `pr_validation`. | Wait for draft-only external checks. Before the first promotion, failed checks, a merge conflict, or an unexpected head return to `development`, which fixes, rebases onto the target or integrates the foreign commits, then revalidates. Phalanx never converts a ready PR back to draft; a ready PR records draft-only checks as skipped. Ask the owner for a closed or retargeted PR, or before re-promoting a PR a human converted to draft. |
| `pr_validation` | Observe CI for the current PR head and run required preview checks once available. Advance to `pr_reconciliation` with the results. | Wait for pending external checks without holding a worker slot. |
| `adversarial_review` | An independent review pass examines the diff since the last reviewed head (the whole PR diff the first time) for correctness, regressions, and missing validation. Record findings against the exact PR head and return to `pr_reconciliation`. | Blocking findings go to `pr_revision`. Retry an interrupted review on the current head. |
| `pr_reconciliation` | Observe the current head's required checks and previews, waiting for pending ones without holding a slot. Confirm the branch is mergeable, actionable review comments are addressed, and the review covers the current head. Then complete a ready-only item, or one whose runner has auto-merge off, with the PR ready; otherwise advance to `check_in`. | An unreviewed diff goes to `adversarial_review`. A merge conflict, failed checks, new actionable GitHub feedback, or an unexpected head go to `pr_revision` as findings. Ask the owner after 10 revision rounds or a third round with identical findings. |
| `pr_revision` | Fix the findings on the open, ready PR. Self-review only the new diff, rerun the failed checks and the local checks covering the changed files, push, verify the head, and advance to `pr_validation`. | A push that loses to a foreign one returns through `pr_reconciliation`. Escalate to `development` when the fix turns into new scope. Never convert the PR to draft. |
| `check_in` | When the runner's auto-merge setting is on (the default), reconcile any earlier merge first, then recheck the expected head and mergeability and merge with a head precondition. GitHub's required reviews, checks and rulesets gate the merge; Phalanx adds no gate. Complete only after GitHub confirms the merge and the item records the resulting commit. | Reconcile ambiguous merge responses before retrying. A conflict or unexpected head goes to `pr_revision` as findings. When GitHub refuses the merge because of repository rules or a disallowed merge method, wait for them to be satisfied, then ask the owner. |

The server durably records report finalization before writing the final artifact. Finalization retries independently of worker capacity or dispatch pause. The owner can retry a stalled finalization after addressing the storage blocker.

The draft author self-reviews the field report before submitting it. A fresh review assignment receives the exact stored draft revision and verifies the report against the request, its supporting evidence, and its links. Findings and the reviewed draft go back to drafting for fixes. The runner submits the approved revision for server finalization without launching a storage worker. Reports share the same pause, retry, and handoff behavior.

Draft revisions are content-addressed and written once, so a review stays bound to the exact Markdown it checked. A completed review or an unaccepted storage submission survives lease expiry or a restart through the recovery claim on the same attempt, without a second review or a redraft. The stored report is served by the API, so it stays readable with the runner offline.

For PR work, the runner binds self-review and local check results to the claimed candidate at the clean item worktree HEAD. Gitignored build output does not make the worktree dirty. Local checks and bypass authorizations come from trusted repository policy supplied by the runner. The result timeline records review notes, each check disposition, and the reason and authorization for a bypass.

The [runner-local validation policy](docs/runner-validation-policy.md) is loaded from an explicit operator-owned file outside worker-writable checkouts, selected by registered repository ID. Commands, bypass reasons, and authority are never loaded from item or worker-controlled content. Unavailable policy is distinct from explicitly empty checks. The loader preserves phase-specific source and revision requirements; preview provider adapters and draft-only recovery remain separate integration requirements and unsupported configuration cannot waive their gates.

### Execution status and recovery

- Use `queued`, `running`, `waiting_for_input`, `waiting_for_external`, `completed`, `failed`, and `cancelled` as execution statuses. Store the stage, status, expected revision, and why any wait is active. A waiting item has a resume trigger, such as a user reply, check result, approval, or scheduled recheck. Reconcile external waits periodically so a missed event cannot strand the item.
- Accept transitions only for the expected state version and, for worker results, the current lease. Persist attempts and transition evidence. A stale result cannot advance the item. Make retryable API operations idempotent and reconcile remote effects before repeating PR creation, updates, or a merge.
- The local notification route is a pollable owner inbox of open questions. An authenticated, versioned reply records the answer with the original question and handoff and queues the same stage. A runner can list due external rechecks in bounded batches and queue that same stage once per wait version. Recheck timing includes jitter and honors a bounded provider retry delay. Provider observation supplies gate evidence separately.
- Cap automatic retries and detect repeated review or conflict cycles that make no progress. After `self_review`, `pre_pr_validation` or a published PR stage, the server allows ten rework rounds and two on identical findings between owner replies; the next one asks the owner instead. Escalate with the blocker, attempted fixes, and a concrete next decision. From a stalled stage the user can retry, supply input, redirect, or cancel. `completed`, `failed`, and `cancelled` close an execution. Rerunning terminal work creates a linked execution and preserves the original history.
- Verify the transition table with tests for the happy path, failed validation, new PR comments, merge conflicts, changed heads, interruptions, and an already-merged PR. Every active state needs a path forward, a resume trigger, or an explicit disposition.

## Runner and agent interface

The runner is the agent’s gateway to Phalanx. It claims and renews work through the API, prepares the workspace, launches the agent, and submits the validated result with its current lease. The agent receives instructions and returns a result through the local runtime interface. Queue credentials and lease tokens stay with the runner.

### Work packet

The runner passes the agent a packet with these fields and instructions.

| Field | Contents |
| --- | --- |
| Work item | ID, goal, context links, acceptance criteria when supplied, and expected output type (`report` or `pull_request`). |
| Stage assignment | Current stage, attempt ID, expected state version and revision when applicable, and the outcome and evidence requirements for this stage. |
| Workspace | Verified repository and local working directory. For PR work, include the base and topic branches and use the item’s isolated worktree. |
| Handoff | Attempt history relevant to resuming, known branch or PR, remaining work, and any answer supplied by the user. |
| Execution instructions | Read applicable repository instructions, stay within the assigned scope, run relevant checks, and ask for input when a missing decision materially affects the outcome. |

### Result contract

The agent returns one JSON result for its assigned stage. It reports an outcome and evidence. The server uses the current stage, outcome, and applicable gates to determine the next stage or execution status. The runner attaches the attempt ID, expected state version, and lease to the submission. Advancing a stage ends that attempt and releases its slot. The next stage gets a new claim, which may reuse the workspace or chat when appropriate.

| Outcome | Required result |
| --- | --- |
| `succeeded` | Evidence the stage passed, such as a candidate revision, validation results, a PR URL and verified head, or final report Markdown, as appropriate. |
| `changes_required` | Findings tied to the reviewed or tested revision, with enough detail to reproduce or act on them. |
| `needs_input` | A focused question, relevant options or tradeoffs, and a handoff describing what is done and how to resume. Include workspace or output references when available. |
| `failed` | Why the attempt could not produce a stage outcome, what was tried, and whether a retry is likely to help. Include references to any partial result. |

The authenticated runner submits GitHub observations through the versioned, leased PR observation route. The runner obtains exact provider reads, resolves repository policy, and correlates gate evidence to the observed PR and revision. The server matches the registered repository and current assignment, evaluates the normalized gate evidence, and records the accepted observation with the transition. See the [runner contract](docs/runner-pr-observations.md).

For example, `self_review` plus `succeeded` with findings and revision evidence advances to `pre_pr_validation`. `self_review` plus `changes_required` returns to `development`. Required external checks can still keep an item waiting even after a successful worker result. Only the server marks the whole item completed when its completion target is satisfied.

On `needs_input`, the runner records the question and handoff, releases the slot, and triggers the notification flow. A user reply requeues the item and becomes part of the next work packet. The Captain triages each question first: it answers when the answer follows from the repository, the issue, its conventions or a proposed default, and escalates to the owner only when human judgment materially changes the outcome or authority is missing. The reply records who answered as `answered_by`, `owner` by default or `captain`, in history and the next packet's handoff. If a process stops before returning a valid result, the runner records the attempt failure. Before retrying, it checks for an existing branch, PR, or stored report so work completed just before the interruption can be recovered.

### Execution adapters

V1 prioritizes a **muster**, a batch launched from a Captain chat in any agent runtime that can run visible worker sessions, such as Codex, GitHub Copilot CLI or the GitHub Copilot app, and Claude Code. Each claimed item runs in a visible worker session in the target repo’s project. Record its session link with the attempt. Verify session launch, observation, and result handoff for each runtime adapter in a pilot. Continuous dispatch into visible worker sessions remains an open integration question.

**Decision (PH-11b):** The Captain chat holds its runtime's session tools, so production requests reach the runtime through a **Captain chat relay**. The runner loop queues each `resolve_project`, `launch`, and `observe` request in its private state directory and waits. The Captain chat takes each request once, runs it with its runtime's tools, and records the reply with `phalanx relay respond`. A request the Captain never took is provably undelivered and may be relaunched. A taken request is never handed out again; an unanswered launch stays reserved until native discovery reconciles it by marker. A trusted subprocess bridge uses the same JSON contract and passes the same contract test. The contract names no runtime; each runtime adapter maps it to that runtime's own tools and session identity.

Persist the worker session ID before relying on its result. When Captain monitoring stops or a lease lapses, reconcile a known worker session before assigning a replacement. Reattach if it is active, collect and verify its result if it finished, or resume from a checkpoint if it stopped. Recheck an unknown state before escalating. Use a new server-authorized recovery claim for any recovered result and keep the prior attempt in history.

Keep the work packet and result contract independent of the runtime. When the runner's validation policy configures a local check command, the packet names it as `checks_command` and its executable as `checks_interpreter`, the environment workers use for tests instead of another checkout's environment or new package installs. An execution adapter starts an item in its workspace, reports progress and a session reference when available, and normalizes the final result or question. The runner handles claims, retries, and handoffs across all adapters. Keep native session IDs on the attempt and preserve enough workspace and handoff context to resume without them. A headless run can expose logs or a native session reference according to its runtime’s capabilities.

Keep one adapter per runtime. A Captain adapter is the procedure a Captain chat in that runtime follows to serve the relay. The README documents the generic procedure and the Codex desktop app adapter; the GitHub Copilot app or CLI and Claude Code serve the same contract. Keep runtime selection in runner configuration and runtime-specific invocation details inside the adapters. Every runtime uses the same backend, queue, work packet, and result contract.

**P2:** Add subprocess bridge adapters for runtimes that can launch and observe visible sessions without a Captain chat.

## Settings and dispatch gates

- **Max concurrency.** Set a per-runner maximum for active worker attempts across all repositories and runtimes. A muster may request fewer slots. A waiting-for-input item releases its slot. Reducing the limit leaves current attempts running and stops new claims until the runner is below the new limit.
- **Dispatch pause.** Allow intake while new claims are paused. Existing attempts can finish and report results. Resume enables claims again.
- **Eligibility.** Claim only work for registered repos the runner can reach and execute with an available runtime. A runner can declare the stages it supports so the server filters them before ranking. An unmet prerequisite keeps an item queued with a visible reason. Independent jobs in the same repo remain eligible.
- **Additional gates.** Keep optional queue-wide concurrency limits and explicit approval before publishing a PR as design choices. Enforce any queue-wide limit in the same atomic server-side claim operation. Start without per-repo limits unless overlap becomes a real problem.

## Worktrees and queue selection

- Design v1 for many concurrent workers on one machine. Keep runner identity, repo mappings, runtime availability, and the concurrency limit in the contract. Multiple devices draining the shared queue and resuming each other’s work are P2. The server assigns one active stage claim per item in either case.
- Use an isolated worktree and an item-specific topic branch for each active PR item. Reuse the worktree on the same device after verifying the branch, checkpoint, and local edits. Keep unpublished changes pinned to the device that holds them. Avoid replacement work while an original worker may still be active.
- When the checkpoint is durable, another eligible device can fetch the recorded branch and commit, recreate its local worktree, and resume from the stored handoff. Opening the draft PR is a natural publication checkpoint. Reports can move after their draft and evidence are stored. Remove a closed item's worktree only after verifying no unrecorded changes remain.
- Atomically claim the highest-ranked eligible stage. Filter by dispatch gates, device capability and affinity, prerequisites, and retry time before ranking. Start with urgent, normal, and low priority. Within a tier, order by oldest `ready_since`, then a stable item ID.
- Age runnable stages toward the highest priority so newly filed urgent work cannot continuously overtake older eligible work. Preserve `ready_since` through claim contention. Start a new clock when a new stage becomes runnable. Apply bounded retry backoff and expose the reason and duration when work cannot run. The aging interval is a setting to tune in the pilot.

## Parallelism and recovery

- **Duplicate submissions.** Support idempotency keys on create and warn about similar open work when useful. Similarity alone should not prevent dispatch because separate tasks can touch the same feature.
- **Duplicate execution.** Use one active lease per item. Record launch intent before starting a chat and renew leases independently of observing chat progress. An expired lease makes the item eligible for recovery, and the board shows the expiry as its wait reason until a runner recovers it. Keep active or uncertain workers counted against local capacity and reserve their worktrees until the runner reconciles them. Reconcile any worker chat, checkpoint, PR, or report before dispatching a replacement or repeating an external side effect.
- **Attended reservations.** One runner ID and one private journal per machine hold numbered local slots. Count every occupied slot from durable claim intent through acknowledged completion, including uncertain claims, launches, and results. New reservations respect local capacity and server admission. Each slot keeps its own claim replay inputs and exact native receipt. Local journal transactions cover only reservation state, so slow Git or API operations in one slot leave other claims and renewals available.
- **Continuous attended scheduling.** The CLI Captain loop enumerates reservations and server recovery before new eligible-stage claims, serializes work per slot, and schedules renewals separately from native observation and runner-only routes. One asynchronous project discovery pass per poll verifies each distinct host/project/checkout mapping once for all slot admission and launch checks. The fixture-backed bridge journals one relay attempt and persists its native or queued receipt immediately. A recorded relay intent is never repeated merely because a receipt is absent, a timeout elapsed, or the loop restarted. Before relay intent exists, a prepared packet with no receipt, submission or recovery ownership resumes its original claim and launch after rechecking the clean, accounted worktree and live claim; stopping instead confirms no-writer stop. Bounded native batches rotate; per-attempt cursors and admission rotation remain in memory and reset on restart. Loop status is written and emitted only on change and has one durable store, separate from reservations. Real native acceptance remains a required integration gate. An expired attempt with a known active writer, a verified result or a saved submission is adopted: the server re-leases the same open attempt under a fresh secret, the old secret stops working, and the Captain resumes renewal, observation and submission. Renewal works only on a live lease and adoption only on an expired one, so the two never race.
- **Attended routing and cancellation.** Project discovery filters only worker-stage claims; runner-only stages remain eligible for Git-verified repositories and the loop alternates the claim groups. Correlated queued/native receipts preserve both IDs across repeated reads. Stopping workers use one leased stop-confirmation path: prove no launch/evidence for an unlaunched worker, or observe the exact launched writer stopped and verify its local outputs through recovery. A clean unlaunched workspace remains accounted when its base advances to a descendant; prepared packets retain their exact checkpoint. Journal the stop request, recheck its proof on replay, and release only after verified acknowledgment; do not submit worker results while stopping. Unmatched expired attempts consume capacity without globally blocking admission, and packet recovery waits for an observation before uncertainty backoff. Malformed native rows block their own markers, while invalid batch freshness, completeness or cursors invalidate the batch. Ambiguous launches, changed checkpoints and runner-only effects remain reserved. The adapter bounds file-backed replies and direct-helper waits without waiting for inherited-pipe EOF or terminating native worker descendants.
- **Runner-only no-intent stop.** Stopping `report_storage` without a saved submission and `pr_creation` without a publication intent or sealed observation use the same no-writer stop-confirmation path. Check both draft journals under the existing item publication lock and account for the claimed candidate in a clean local worktree. Preserve any ambiguous publication or saved submission; release only after the exact stop acknowledgment and archive once.
- **GitHub writes.** Workers prepare commits and evidence. The runner owns pushes, PR creation and promotion, and merge via the current item claim. Persist operation intent, verify authority and expected revision, and use Git or GitHub revision preconditions where supported. Reconcile an unresolved write before a conflicting operation. Prefer a possible duplicate PR to missing publication after bounded lookup. Verify and record one primary PR for review and check-in, and surface any duplicates. Uncertain merges require reconciliation before retrying. The direct GitHub merge-commit path relies on GitHub's expected-head precondition plus the repo's required checks and rulesets. Reconcile the recorded intent against the exact merged primary PR and the merge commit's head parent, which must be the authorized head even if the base advanced. An open PR leaves an ambiguous attempt unresolved until its disposition is established.
- **Overlapping changes.** Separate worktrees protect local changes from concurrent agents. Review intervening changes before opening a PR. Use an explicit dependency or targeted coordination when two items need the same change in a particular order. Avoid a global one-worker-per-repo restriction.
- **Retries.** Reconcile partial effects, then retry transient failures with bounded backoff. Within the work item’s scope and repo policy, try a different recovery path when it can address the blocker. Defer only the affected stage or shared resource and keep unrelated eligible work moving. Ask for input when authorization or a material choice is required, or when available recovery paths are exhausted. Include the recommended next action and the evidence behind it.

## Interdependent work

- V1 supports multiple prerequisites per work item, across repos when needed. Items whose prerequisites are satisfied can run in parallel. Validate self-dependencies and cycles in the same transaction that writes the links. Keep dependencies in the shared store.
- A dependency names the upstream item and the output milestone it needs. A report dependency needs the stored report. A PR dependency defaults to a verified check-in when downstream work needs the merged code. An item can explicitly depend on a PR being ready for check-in if it only needs the reviewed PR. Bind the claim to the exact upstream execution and output revision. A ready-only dependency consumes that pinned reviewed revision. A failed or cancelled upstream item does not satisfy a dependency even if it produces a disposition report.
- Keep an item queued with a visible reason until all its prerequisites are satisfied. Before claim, give the next worker the upstream output references and expected target revision where applicable. Make newly unblocked items eligible on prerequisite completion and reconcile eligibility if an event is missed. A failed upstream prerequisite can be retried or replaced, or the dependent item can be redirected or cancelled.
- Allow the owner to update queued work and its dependencies after upstream changes or new steering. Version those changes and check the version on claim. A change to an active item goes through the existing pause, handoff, or cancellation path so a worker cannot silently continue against revised instructions.
- **Later: campaigns.** A campaign groups related work items around a larger goal. Campaign planning can decompose the goal into items and dependency links, then reconsider remaining work after check-ins or steering. Keep automated decomposition and replanning outside v1. The same work-item and dependency model will support them later.

## Server and deployment

Start with a standalone HTTP API server running on localhost. Run the CLI and workers as clients of that server from day one. The server owns the queue, claims, history, and reports. The runner owns local checkouts, agent processes, and Git credentials. Configure clients with a base URL and credentials so they can switch from the local server to a hosted instance without code changes.

Use one relational store and keep database operations behind the server, with migrations and explicit transactional claim and lease behavior. The first single-server deployment uses FastAPI with plain Uvicorn and Python's standard sqlite3 module in SQLite DELETE journal mode with synchronous=EXTRA and short, serialized write transactions. CPython 3.14 is the deployment baseline and CPython 3.12 or later is supported. The server authenticates owner and runner operations with distinct bearer tokens supplied at startup. A hosted instance could use SQLite on durable storage or move to a managed relational database if hosting or concurrency needs warrant it. Keep storage-specific code localized and verify claim, retry, and resume behavior against any new database before migrating.

Use the same API server in both environments. For a hosted deployment, configure HTTPS, authentication, persistent storage, and backups. Local runners initiate outbound API requests, so hosting the server does not require exposing the runner or its repositories to the internet. Phone intake will use the same authenticated work-item creation endpoint.

## Users and workspaces

Phalanx will be used by people other than Caleb, alone or in teams, on a shared hosted server. Build for that shape now and ship it in small steps. The v1 local release runs one implicit workspace with the existing owner and runner tokens.

- **Workspace.** The tenancy boundary. A workspace owns its repositories, work items, executions, runners, dispatch settings, questions and reports. Every server record belongs to exactly one workspace. The server scopes every query and write by the caller's workspace. Cross-workspace access is a bug, and tests prove isolation. The v1 data belongs to one default workspace, and a migration adds the scope without changing behavior.
- **Users and roles.** A user is a person with a stable identity from a sign-in provider: the provider and its subject ID, with a verified email for display and invitations. Start with Google ([PH-27](IMPLEMENTATION_PLAN.md)). A user can belong to several workspaces, with a role in each:
  - `owner` manages members, repositories, runners and dispatch;
  - `member` files work, replies and acts on items;
  - `viewer` can only read.

  Add finer permissions, such as per repository, only when a team needs them.
- **Attribution.** Every work item records who requested it, and every question, reply, steering change and owner action records who acted. A question goes first to the item's requester and is visible to the workspace's owners. "Needs you" means needs this user.
- **Credentials.**
  - **People** sign in to the dashboard. For the CLI and API they use personal API tokens, which are hashed on the server, scoped to one workspace and revocable.
  - **Runners** register with their workspace and get their own token, bound to their runner ID.
  - **Bootstrap.** The static owner and runner tokens remain the setup path for a single-user local install.
- **Runners and Git stay with their owner.** A runner claims only work for its own workspace's repositories. Checkouts and Git credentials stay on the machine of the user who runs it, and the server never holds them. That boundary is what makes a shared server safe for many users.
- **Fair sharing.** Capacity comes from each workspace's own runners, so one workspace's backlog cannot take another's slots. A hosted server adds per-workspace rate limits and storage quotas when it serves more than one workspace.
- **Captain.** Each workspace runs its own Captain loop with that workspace's credentials. Captain decisions are attributed to the Captain and reviewable by the workspace owners.
- **Not planned yet:** billing, enterprise single sign-on, and cross-workspace sharing.

## Operator view

Provide a read-only work board for the current state of Phalanx. Put work in progress first, with an explicit distinction between running, queued, waiting for input, and waiting for external checks. Show recently closed work below it, newest first, including failures and cancellations with their reports. Keep the full backlog and older history available through paginated views.

- In progress shows the item and repo, current stage, status, when it last advanced, and the specific wait reason or blocker. Put items needing the user’s judgment where they are easy to find. Include links to the visible worker chat and draft PR when available.
- A selected item shows its recorded transition history, current attempt, next action or resume trigger, and prerequisites when relevant. Keep stage and execution status separate. Show the recorded lifecycle rather than percent complete, since an item may loop back after a review or revision change.
- Recently closed shows the outcome, close time, and one link to the verified PR or stored field report. If a merge was authorized and completed, distinguish checked in from ready for check-in.
- Read state from the API. Use bounded, paginated queries for open items and recent closures, and load the detailed timeline only when selected. Refresh periodically and show when the view is stale or disconnected. Terminal outputs and recorded history remain available while the runner is offline.
- The hosted dashboard's goals, UI principles and phases are proposed in [DASHBOARD.md](DASHBOARD.md).

## First milestone

Build a small API, one durable store, and a runner with a concurrency limit and a dispatch pause. Demonstrate two visible worker sessions on one machine handling different repos in parallel, with one field report and one PR that goes through the full lifecycle, including a feedback or conflict loop and an authorized check-in. Demonstrate recovery after an interruption, a question that can be answered and resumed, and the operator view under backlog. Validate the local operating envelope before adding multi-device handoff. Later, an authenticated ChatGPT action or intermediary can use the same create API to file work from a phone. Its availability and authentication flow are decisions to validate separately.

## Design questions

| Decision | Options | Starting proposal |
| --- | --- | --- |
| How should the first hosted instance run? | One hosted service with a persistent disk, or a hosted service with a managed relational database. | Run the standalone server locally first. Choose the host and database once we know the durability, concurrency, and maintenance tradeoffs. |
| How should input requests reach a user? | CLI inbox, email, or a chat/push integration. | Store the question and reply in the API first. Use one notification channel per user, with a link to the item. Decide where replies should be submitted. |
| Who can file work? | One owner, selected collaborators, or separate submitter and runner roles. | **Decided:** many users in workspaces with owner, member and viewer roles (see [Users and workspaces](#users-and-workspaces)). v1 ships one implicit workspace with owner and runner tokens. Repo and Git credentials stay local to each runner. |
| Where should Markdown reports live? | Local files, backend artifact storage, or a designated Git repo. | Store small reports with the work item so they remain available when the runner goes offline. Revisit storage if reports start carrying large artifacts. |
| How strict should coordination be? | Claim only by work-item ID, warn about likely overlap, or serialize work by repo or declared resource. | Start with per-item claims, isolated worktrees, and explicit dependencies for known overlaps. Add narrower coordination when a real collision shows where it helps. |
