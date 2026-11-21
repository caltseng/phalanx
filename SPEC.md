# Phalanx design spec

Phalanx lets Caleb dispatch work across registered repositories and have local agents carry it through to a Markdown report or pull request. The backend is the durable source of truth. Agents run locally on machines with access to the relevant checkouts and credentials. The initial design should favor a small API, recoverable execution, and a flexible work-item format.

## Work items and repositories

- A work item has an ID, repository, goal, output type (`report` or `pull_request`), status, and timestamps. Optional fields include context links, acceptance criteria, a target branch, and a prerequisite work item. An API caller may provide an idempotency key so resubmitting the same request returns the original item.
- A registered repository has a stable ID and expected remote. Each runner maps that ID to a local checkout and verifies the remote before starting. Repository instructions apply to each run. An item waits if no eligible runner can access its repo.
- A report contains findings, sources or evidence where relevant, and open questions. A pull request includes the verified head commit and checks run. An item is complete when its output reference is recorded. Merging a PR is a separate action.

## Dispatch and execution

1. An authenticated client files an item through the API and receives its ID. The API supports listing and inspecting work, claiming eligible items, renewing claims, posting progress and results, and responding to a question.
2. A local runner polls when a worker slot is free and requests an atomic claim for a repo it can access. Agents can run concurrently, including on the same repo when their tasks are independent. A runner uses a separate worktree and topic branch for each PR item.
3. An agent reads the repo instructions, executes the request, and runs appropriate checks. It reports the result or asks a focused question when a material decision remains. A waiting item releases its worker slot and retains the question, prior work, and resume context.
4. A reply makes the item eligible again. A runner can resume from the recorded handoff and branch rather than require the original agent process to stay alive.

The proposed states are **queued**, **running**, **waiting for input**, **completed**, **failed**, and **cancelled**. The first version can represent missing repo access or unmet prerequisites as a reason an item is queued. Keep progress notes, attempts, questions, and output references with the item so a restart does not erase its history.

## Parallelism and recovery

- **Duplicate submissions.** Support idempotency keys on create and warn about similar open work when useful. Similarity alone should not prevent dispatch because separate tasks can touch the same feature.
- **Duplicate execution.** Use one active lease per item. The store assigns the lease atomically, runners renew it, and state changes require a valid lease token. An expired lease allows another attempt. A restarted worker should reconcile the known branch, PR, or report before repeating an external side effect.
- **Overlapping changes.** Separate worktrees protect local changes from concurrent agents. Review intervening changes before opening a PR. Use an explicit dependency or targeted coordination when two items need the same change in a particular order. Avoid a global one-worker-per-repo restriction.
- **Retries.** Retry transient failures with bounded backoff. Escalate persistent failures and material ambiguity. Record enough detail for a human to decide whether to retry, redirect, or cancel.

## First milestone

Build a small API, one durable store, and a local runner with configurable concurrency. Demonstrate two agents picking up different repos in parallel, one report and one PR, safe recovery after a runner stops mid-item, and a question that can be answered and resumed. Start with CLI intake and status. Later, an authenticated ChatGPT action or intermediary can use the same create API to file work from a phone. Its availability and authentication flow are decisions to validate separately.

## Design questions

| Decision | Options | Starting proposal |
| --- | --- | --- |
| Where should the API and store live? | Local service with SQLite, hosted service with a managed relational store, or a managed queue plus a separate metadata store. | Use one relational store for both queue and history. Start local if proving the runner comes first. Host the API early if phone intake or multiple machines are part of the first milestone. |
| Which agent interface should the runner use? | One local agent CLI, or adapters for multiple runtimes. | Start with the agent CLI Caleb already uses. Define a small runner interface that accepts an item and returns progress, a question, or an output. |
| How should input requests reach Caleb? | CLI inbox, email, or a chat/push integration. | Store the question and reply in the API first. Use one notification channel with a link to the item. Decide where replies should be submitted. |
| Who can file work and what can agents publish? | One owner, selected collaborators, or separate submitter and runner roles. PRs may be draft or ready for review. | Start with one owner plus runner credentials. Keep repo and Git credentials local to the runner. Confirm whether PRs should default to draft or ready for review. |
| Where should Markdown reports live? | Local files, backend artifact storage, or a designated Git repo. | Store small reports with the work item so they remain available when the runner goes offline. Revisit storage if reports start carrying large artifacts. |
| How strict should coordination be? | Claim only by work-item ID, warn about likely overlap, or serialize work by repo or declared resource. | Start with per-item claims, isolated worktrees, and explicit dependencies for known overlaps. Add narrower coordination when a real collision shows where it helps. |
