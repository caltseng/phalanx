# Phalanx design spec

Phalanx lets Caleb dispatch work across registered repositories and have local agents carry it through to a Markdown report or pull request. The backend is the durable source of truth. Agents run locally on machines with access to the relevant checkouts and credentials. The initial design should favor a small API, recoverable execution, and a flexible work-item format.

## Work items and repositories

- A work item has an ID, repository, goal, output type (`report` or `pull_request`), status, and timestamps. Optional fields include context links, acceptance criteria, a target branch, and a prerequisite work item. An API caller may provide an idempotency key so resubmitting the same request returns the original item.
- A registered repository has a stable ID and expected remote. Each runner maps that ID to a local checkout and verifies the remote before starting. Repository instructions apply to each run. An item waits if no eligible runner can access its repo.
- A **field report** is a Markdown deliverable with findings, sources or evidence where relevant, and open questions. PR items specify a completion target of `ready_for_check_in` or `checked_in`. A check-in target authorizes merging only when the dispatch explicitly requests it or a repo policy enables it. Repository rules still govern the merge.

## Dispatch and execution

1. An authenticated client files an item through the API and receives its ID. The API supports listing and inspecting work, claiming eligible items, renewing claims, posting progress and results, and responding to a question.
2. A runner requests an atomic claim for an eligible repo only when it has a free slot and dispatch is enabled. Agents can run concurrently, including on the same repo when their tasks are independent. A runner uses a separate worktree and topic branch for each PR item.
3. An agent reads the repo instructions, executes the request, and runs appropriate checks. It reports the result or asks a focused question when a material decision remains. A waiting item releases its worker slot and retains the question, prior work, and resume context.
4. A reply makes the item eligible again. A runner can resume from the recorded handoff and branch rather than require the original agent process to stay alive.

Each work item exposes both its lifecycle stage and its execution status. For example, `live_validation / waiting_for_input` preserves where work paused and what must happen next.

## Development lifecycle state machine

The server owns transitions. Each stage has required evidence, allowed next stages, and a recovery path. A worker reports an outcome and evidence for its current stage. The server validates the transition and atomically records the new state and timeline event. Stages that need agent work become eligible for another claim. Stages waiting for GitHub checks, reviews, or approval release their worker slot.

| Stage | Advance when | Recovery or loop |
| --- | --- | --- |
| `development` | Changes are ready for self-review and the item records the candidate revision. | Retry transient execution failures or pause for a focused decision. |
| `self_review` | The implementing agent checks the diff against the request and repo rules, with findings recorded. Advance to `live_validation`. | Return to `development` for findings that require changes. |
| `live_validation` | Required local tests and relevant live or preview checks pass, with evidence attached. Advance to `pr_creation` for initial publication or an update. | Route failures to `development`. Missing access or a check that cannot run needs a visible blocker or an authorized disposition consistent with repo rules. |
| `pr_creation` | Create the PR or update the existing PR for this item. Verify that its head matches the validated revision and advance to `adversarial_review`. | Retry publication after reconciling the branch and existing PR. Route conflicts to `pr_reconciliation`. |
| `adversarial_review` | An independent review pass examines the current PR diff for correctness, regressions, and missing validation. Record findings and advance to `pr_reconciliation`. | Retry an interrupted review on the current head. A newer head invalidates review evidence for the old one. |
| `pr_reconciliation` | Confirm the branch is mergeable, actionable review comments are addressed, the independent review is current, and required checks and approvals are satisfied. Advance to `ready_for_check_in`. | Resolve conflicts and address feedback on the existing PR. Any code change returns to `self_review`, then runs validation and independent review again on the updated head. New feedback or failed checks return here. |
| `ready_for_check_in` | Complete a ready-only item with the verified PR link and head. For a merge-authorized item, proceed to `check_in`. | A stale head, new blocking comment, failed check, or conflict returns the item to `pr_reconciliation` while it is active. If authorization is missing or withdrawn, wait for input or complete as ready-only with explicit disposition. |
| `check_in` | Recheck the expected head, base, required checks, and merge policy immediately before merging. Complete only after GitHub confirms the merge and the item records the resulting commit. | Reconcile ambiguous merge responses before retrying. Return stale or unmergeable PRs to `pr_reconciliation`. Pause for an authorization or policy blocker. |

Field reports use a shorter path. Draft and self-review the report, verify its supporting evidence and links, then store the Markdown and complete the item. Findings return it to drafting. Reports share the same pause, retry, and handoff behavior.

### Execution status and recovery

- Use `queued`, `running`, `waiting_for_input`, `waiting_for_external`, `completed`, `failed`, and `cancelled` as execution statuses. Store the stage, status, expected revision, and why any wait is active. A waiting item has a resume trigger, such as a user reply, check result, approval, or scheduled recheck. Reconcile external waits periodically so a missed event cannot strand the item.
- Accept transitions only for the expected state version and, for worker results, the current lease. Persist attempts and transition evidence. A stale result cannot advance the item. Make retryable API operations idempotent and reconcile remote effects before repeating PR creation, updates, or a merge.
- Cap automatic retries and detect repeated review or conflict cycles that make no progress. Escalate with the blocker, attempted fixes, and a concrete next decision. From a stalled stage the user can retry, supply input, redirect, or cancel. `completed`, `failed`, and `cancelled` close an execution. Rerunning terminal work creates a linked execution and preserves the original history.
- Verify the transition table with tests for the happy path, failed validation, new PR comments, merge conflicts, changed heads, interruptions, and an already-merged PR. Every active state needs a path forward, a resume trigger, or an explicit disposition.

## Runner and agent interface

The runner is the agent’s gateway to Phalanx. It claims and renews work through the API, prepares the workspace, launches the agent, and submits the validated result with its current lease. The agent receives instructions and returns a result through the local runtime interface. Queue credentials and lease tokens stay with the runner.

### Work packet

The runner passes the agent a packet with these fields and instructions.

| Field | Contents |
| --- | --- |
| Work item | ID, goal, context links, acceptance criteria when supplied, and expected output type (`report` or `pull_request`). |
| Workspace | Verified repository and local working directory. For PR work, include the base and topic branches and use the item’s isolated worktree. |
| Handoff | Attempt history relevant to resuming, known branch or PR, remaining work, and any answer supplied by the user. |
| Execution instructions | Read applicable repository instructions, stay within the assigned scope, run relevant checks, and ask for input when a missing decision materially affects the outcome. |

### Result contract

The agent returns one JSON result with a `status` and fields appropriate to that status. A successful process exit alone does not complete an item.

| Status | Required result |
| --- | --- |
| `completed` | For a report, the Markdown content. For a PR, the URL and head commit. Include checks run and their outcomes, plus known limitations. |
| `needs_input` | A focused question, relevant options or tradeoffs, and a handoff describing what is done and how to resume. Include workspace or output references when available. |
| `failed` | What failed, what was attempted, and whether a retry is likely to help. Include references to any partial result. |

On `needs_input`, the runner records the question and handoff, releases the slot, and triggers the notification flow. A user reply requeues the item and becomes part of the next work packet. If a process stops before returning a valid result, the runner records the attempt failure. Before retrying, it checks for an existing branch, PR, or stored report so work completed just before the interruption can be recovered.

### Execution adapters

V1 prioritizes a **muster**, a batch launched from a Captain chat in Codex. Each claimed item runs in a visible worker chat in the target repo’s Codex project. Record its chat link with the attempt. Verify the supported chat creation, observation, and result handoff in a pilot. Continuous dispatch into visible Codex chats remains an open integration question.

Keep the work packet and result contract independent of the runtime. An execution adapter starts an item in its workspace, reports progress and a session reference when available, and normalizes the final result or question. The runner handles claims, retries, and handoffs across all adapters. Keep native session IDs on the attempt and preserve enough workspace and handoff context to resume without them. A headless run can expose logs or a native session reference according to its runtime’s capabilities.

**P2:** Add Claude Code and GitHub Copilot CLI adapters if needed. Keep runtime selection in runner configuration and runtime-specific invocation details inside the adapters. Use the same backend, queue, work packet, and result contract. Start with the Codex path and use it to validate the boundary before building additional adapters.

## Settings and dispatch gates

- **Max concurrency.** Set a per-runner maximum for active worker attempts across all repositories and runtimes. A muster may request fewer slots. A waiting-for-input item releases its slot. Reducing the limit leaves current attempts running and stops new claims until the runner is below the new limit.
- **Dispatch pause.** Allow intake while new claims are paused. Existing attempts can finish and report results. Resume enables claims again.
- **Eligibility.** Claim only work for registered repos the runner can reach and execute with an available runtime. An unmet prerequisite keeps an item queued with a visible reason. Independent jobs in the same repo remain eligible.
- **Additional gates.** Keep optional queue-wide concurrency limits and explicit approval before publishing a PR as design choices. Enforce any queue-wide limit in the same atomic server-side claim operation. Start without per-repo limits unless overlap becomes a real problem.

## Parallelism and recovery

- **Duplicate submissions.** Support idempotency keys on create and warn about similar open work when useful. Similarity alone should not prevent dispatch because separate tasks can touch the same feature.
- **Duplicate execution.** Use one active lease per item. The store assigns the lease atomically, runners renew it, and state changes require a valid lease token. An expired lease allows another attempt. A restarted worker should reconcile the known branch, PR, or report before repeating an external side effect.
- **Overlapping changes.** Separate worktrees protect local changes from concurrent agents. Review intervening changes before opening a PR. Use an explicit dependency or targeted coordination when two items need the same change in a particular order. Avoid a global one-worker-per-repo restriction.
- **Retries.** Retry transient failures with bounded backoff. Escalate persistent failures and material ambiguity. Record enough detail for a human to decide whether to retry, redirect, or cancel.

## Server and deployment

Start with a standalone HTTP API server running on localhost. Run the CLI and workers as clients of that server from day one. The server owns the queue, claims, history, and reports. The runner owns local checkouts, agent processes, and Git credentials. Configure clients with a base URL and credentials so they can switch from the local server to a hosted instance without code changes.

Use one relational store and keep database operations behind the server, with migrations and explicit transactional claim and lease behavior. SQLite is a candidate for the first single-server deployment. A hosted instance could use SQLite on durable storage or move to a managed relational database if hosting or concurrency needs warrant it. Keep storage-specific code localized and verify claim, retry, and resume behavior against any new database before migrating.

Use the same API server in both environments. For a hosted deployment, configure HTTPS, authentication, persistent storage, and backups. Local runners initiate outbound API requests, so hosting the server does not require exposing the runner or its repositories to the internet. Phone intake will use the same authenticated work-item creation endpoint.

## First milestone

Build a small API, one durable store, and a runner with a concurrency limit and a dispatch pause. Demonstrate two visible Codex chats handling different repos in parallel, with one field report and one PR that goes through the full lifecycle, including a feedback or conflict loop and an authorized check-in. Demonstrate recovery after an interruption and a question that can be answered and resumed. Start with CLI intake and status and a Captain-launched muster. Later, an authenticated ChatGPT action or intermediary can use the same create API to file work from a phone. Its availability and authentication flow are decisions to validate separately.

## Design questions

| Decision | Options | Starting proposal |
| --- | --- | --- |
| How should the first hosted instance run? | One hosted service with a persistent disk, or a hosted service with a managed relational database. | Run the standalone server locally first. Choose the host and database once we know the durability, concurrency, and maintenance tradeoffs. |
| How should input requests reach Caleb? | CLI inbox, email, or a chat/push integration. | Store the question and reply in the API first. Use one notification channel with a link to the item. Decide where replies should be submitted. |
| Who can file work and what can agents publish? | One owner, selected collaborators, or separate submitter and runner roles. PRs may be draft or ready for review. | Start with one owner plus runner credentials. Keep repo and Git credentials local to the runner. Confirm whether PRs should default to draft or ready for review. |
| Where should Markdown reports live? | Local files, backend artifact storage, or a designated Git repo. | Store small reports with the work item so they remain available when the runner goes offline. Revisit storage if reports start carrying large artifacts. |
| How strict should coordination be? | Claim only by work-item ID, warn about likely overlap, or serialize work by repo or declared resource. | Start with per-item claims, isolated worktrees, and explicit dependencies for known overlaps. Add narrower coordination when a real collision shows where it helps. |
