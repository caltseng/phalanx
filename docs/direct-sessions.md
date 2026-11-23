# Direct sessions: coordination, not a gate

**Status: decided, partly implemented.** Caleb decided the principle ([decision 68](decisions.md)), the sources of truth with no runner-local database (decision 69), multiple Captains with periodic successors (decision 70) and the design defaults [D1–D8](#decisions) (decision 71). Writer registration and `phx work` (step 2), the reconciler (step 3) and the writer's [PR lifecycle](#pr-publication-and-delivery) (pulled forward from step 5) are implemented. The rest isn't yet. Until it is, [SPEC.md](../SPEC.md) and [STATE_MACHINE.md](../STATE_MACHINE.md) describe the running system. The [gap check](#appendix-gap-check-of-attended-mode) at the end records what attended mode already does.

## Principle

Phalanx adds visibility and coordination. It must not become an extra permission gate between the agent runtime and useful work. The gates that remain belong to the repository and GitHub (required checks, reviews, rulesets) and to trust and credentials. Phalanx records, reconciles and flags. It never blocks starting work.

Merging the Captain and relay chats alone doesn't solve the reliability problem. These parts were the bottlenecks:

- the serial relay (one chat served about one request every 25 s, and one launch held it for about 7.5 min);
- relay SQLite contention;
- the shared slot cap that starved runner-only stages;
- merged PRs that never reach delivered.

## Sources of truth

The Phalanx server and GitHub together are the source of truth (Caleb). Each fact has exactly one owner.

| Owner | Authoritative for |
| --- | --- |
| **GitHub** | PR existence and state (open, draft, merged, closed), head and base SHAs, checks, reviews, merges by any method, deployments, and branch contents |
| **Phalanx server** | Items, goals and acceptance criteria, dependencies, executions, stages, attempts, writer bindings and launch nonces, Captain registrations, questions, replies, decisions, evidence, reports and flags |

Rules:

- **GitHub wins.** The server doesn't keep a copy of a GitHub fact that could drift and then act on it. When it stores a GitHub value (the primary PR URL, an observed head, a merge commit), it stores it as an observation with `observed_at`. Decisions about PR state re-read GitHub or use a fresh observation. If the two disagree, reconciliation updates the server to match GitHub.
- **Nothing else is authoritative.** Chat memory, Captain handoff notes, the charter's notes, local files, worktrees and runner caches are hints or artifacts. A Captain or worker verifies them against the server and GitHub before acting.
- **Applied to the merged-PR defect.** churchtree#55 was squash-merged on GitHub but is still in `pr_validation`/`waiting_for_input`. Under these rules GitHub's `merged` state is the delivery fact. The item becomes delivered from that merge (merge commit, squash or rebase), with no Phalanx merge intent and no second parent. See [GitHub reconciliation](#github-reconciliation).

## Components and state ownership

| Component | Owns | Never does |
| --- | --- | --- |
| **Server** (HTTP API, `phalanx.sqlite3`) | The only Phalanx state store and the [source of truth](#sources-of-truth) for Phalanx facts: items, executions, stages, attempts, each item's registered writer and launch nonce, evidence and timeline, questions and replies, reports, Captain registrations, heartbeats and handoff notes, flags, and timestamped GitHub observations. It is the only writer to its store. | Launch or stop sessions, read GitHub, or override a GitHub fact. |
| **Captain chat** (an agent turn in any runtime; one or more at once) | Launching, messaging and stopping worker sessions with its runtime's session tools, registering them through the API, triaging questions, acting on flags and starting its successor at the end of its [shift](#captain-shifts-and-handoff). It reads the [charter](#captain-continuity-and-goal-adherence) and keeps no other local state. | Keep state that a fresh Captain couldn't rebuild from the API, GitHub, runtime session status and the charter. |
| **Worker session** | The item's work in its worktree and branch, posting progress, evidence and results with its writer token. Git and GitHub writes follow [decision D1](#decisions). | Run without a registered writer, or write for another item. |
| **Reconciler** (background Python on the runner host) | Reading GitHub (PR state, head, checks, reviews, merges by any method, deployments), git (ancestry and merge-base) and the API. It posts observations and flags through the API. | Block work, write the store directly, kill processes by name, launch or stop sessions, or bypass trust. |
| **GitHub** | The [source of truth](#sources-of-truth) for PR state, heads, checks, reviews, merges and deployments. | |

The runner host keeps only configuration, credentials and artifacts: checkout mappings, Git credentials, trusted validation policy, worktrees and result files. The server references any artifact it relies on. The host keeps **no state database**; see [Local SQLite stores removed](#local-sqlite-stores-removed).

## Writer registration and fencing

Each open execution has at most one registered writer, a native worker session. The server enforces that.

1. **Intent.** The Captain generates a writer token and a launch nonce, then writes the token to the item's worktree (see [Evidence submission](#evidence-submission)). It calls `POST /v1/items/{item}/executions/{execution}/writer` with its Captain ID, the expected execution version, the runtime, the nonce and the token's digest, using the nonce as the idempotency key. The server atomically opens an attempt for the current stage in state `launching` and records which Captain owns the intent. A replay returns the same attempt, so a lost response needs no local journal. A concurrent registration by another Captain gets `409 Conflict`. This claims an item by ID, not the queue head, and checks no capacity. Dispatch pause still refuses new writers (D6).
2. **Launch.** The Captain launches the session with the nonce in its name and kickoff.
3. **Confirm.** The Captain records the native session ID and link with `PATCH .../writer`. If the launch reply is lost, the next wake finds the session by its nonce in the runtime's session list and confirms it. Until a Captain has checked native sessions for the nonce, an unconfirmed intent is never relaunched. After 10 min it is flagged `launch_unconfirmed`. A launch a Captain proves never happened is withdrawn, and a new intent gets a new nonce. Only the owning Captain acts on its own unconfirmed intent, unless that Captain has [ended or gone stale](#captain-shifts-and-handoff) or handed the intent off. This keeps the relay's at-most-once launch guarantee without the relay.
4. **Fencing.** Every write carries the attempt ID, expected version and writer token, and a stale token is rejected. A second registration for the same execution gets `409 Conflict` unless it names `replaces: <attempt>`. Replacing a writer requires the Captain to attest the old session is stopped, and the server revokes the old token in the same transaction. Unpushed local changes are never discarded on replacement. The new writer resumes from the worktree, or the stored handoff if the worktree is gone.
5. **No timed lease for writers.** A writer token is valid until the attempt ends or is revoked. Liveness comes from the Captain's session status reports and worker progress, and silence raises a [flag](#deviation-flags) rather than expiring the writer. A busy worker and a hung one look the same from outside, so the Captain decides.

One writer keeps its item across stages. The worker posts each stage's result. The server applies the existing transition rules and records a new attempt per stage under the same writer, so "keep working until you really can't anymore" holds without capacity reservations or continuation offers.

As built in step 2, the writer token is bound to the writer registration and fences each stage attempt that registration opens. It ends when the writer ends, is replaced or is withdrawn, or the execution closes. So `replaces` names the open writer's ID, and the server ends that writer's open attempt. A queued execution can take a writer, and the worker runs `phx work next` when it becomes ready. An execution whose attempt a runner holds can't take a writer until that attempt ends.

Review stages (`adversarial_review`) launch a separate reviewer session, registered as a non-writer reviewer bound to the PR head it reviews.

## Evidence submission

- The Captain writes the writer token and base URL to `$(git rev-parse --git-path phalanx-writer.json)` in the item's worktree. The file lives under the git directory, so it is never committed and never pasted into a chat (D4). It is a credential file, not state: the server holds only its digest.
- The worker runs `phx work progress|result|question`, which reads that file and calls the existing result routes from the worker's own shell. Today those routes require the runner token plus lease. They change to accept the writer token as a scoped `writer` role bound to one attempt. The server's transition rules, idempotency keys and version checks don't change.
- `phx work progress` updates `last_progress_at` and an optional note. The board shows it and the no-progress flag uses it.
- `needs_input` lands in the owner inbox as today. The Captain answers routine questions with `answered_by: captain`.

### PR publication and delivery

A writer-bound PR item goes `development` → `self_review` → `publication` → `delivery`. It never enters the runner-only stages, `adversarial_review` or `pr_revision`.

1. **Publication.** After self-review passes, the worker pushes the item branch (`phalanx/<sha256 of the item ID>`, which the reconciler looks up). It opens one ready PR against the target branch, or reuses the open PR on that branch. For a `checked_in` target it enables GitHub auto-merge with any method the repository allows (D1, D2). Then it posts `{"outcome": "succeeded", "pr_url": "https://github.com/<owner>/<repo>/pull/<n>"}` with `phx work result`. Only a successful publication result carries `pr_url`, and the PR must be in the item's registered repository.
2. **Delivery.** The execution waits as `delivery / waiting_for_external` with no attempt, and the reconciler's observations drive it (see the next section). The writer stays registered. It acts on `checks_failed`, `review_feedback`, conflict and `head_drift` flags by pushing fixes to the open PR, which needs no attempt, and the next observation picks up the new head.
3. **Closed without merge.** The server asks the owner. A reply queues `delivery` for the writer, which reopens the PR or explains, then posts a result to return to the wait.

Pre-PR local checks are flags, not a gate (D3). The reconciler runs the trusted policy's local checks on each new head and posts them as flags. Repository rules and required checks gate the merge. An independent reviewer session for `adversarial_review` isn't part of the direct path yet.

## GitHub reconciliation

The reconciler runs periodically for each open PR item and for due external waits. It holds no lease or slot and posts facts through a runner-authenticated observation route. Each observation is idempotent on (execution, PR, head, observation digest) and carries `observed_at`. GitHub is authoritative, so a newer observation always replaces the server's view, even when it moves an item backward (for example, a reopened PR).

| GitHub shows | Server records |
| --- | --- |
| The item's primary PR **merged by any method** (merge commit, squash or rebase), by Phalanx or a person | Delivered: it completes a `checked_in` target and records the merge commit and final head. It needs no Phalanx merge intent or second parent. A final head that differs from the last recorded one raises an informational `head_drift` flag. A `ready_for_check_in` target also completes when merged, so dependents unblock (fixes decision 58's gap). |
| PR closed without merge | `waiting_for_input` with the question; never a replacement PR. |
| New head, failing checks, new review feedback, conflict | Evidence on the timeline plus a flag. The writer, or the Captain if no writer is active, acts on it. |
| Ready head with required checks and reviews satisfied | `ready_for_check_in`. Merging is GitHub's job (D2). |
| Branch commits reachable from the base with no PR (git ancestry) | A flag only. |

GitHub rate limits back off and never block other items.

For a writer's execution in `publication` or `delivery`, the server applies each PR observation as a state transition (`pr_observed`). The observation sets the primary PR and current head. A merge completes the execution and ends its writer. Every post is reapplied, so a lost apply recovers on the next pass, and an unchanged observation doesn't bump the execution's version. A snapshot older than one already recorded is ignored unless it shows a merge. A `ready_for_check_in` milestone, and the target, need required checks green, so they need the repository's trusted validation policy. Without one the execution keeps waiting, visibly, until merged. Executions on the old path only record observations.

## Liveness

- **Captains:** each Captain posts a heartbeat each wake (see below).
- **Workers:** the Captain reads its runtime's session status and idle notifications each wake, then posts each registered writer's state (`busy`, `idle`, `stopped`, `error`) and the list of native sessions it can see. Workers add `phx work progress`.
- **Reconciler:** it heartbeats through the API. A stale reconciler is flagged and the Captain reports it.

There is no relay polling and no foreground polling loop.

## Captain continuity and goal adherence

1. **Continuity.** A chat turn isn't a daemon. A session automation wakes each Captain every 5 min, and idle notifications wake it sooner. Each wake posts a heartbeat. Today's standing relay serve repeats a command "without ending your turn", the foreground loop this removes. [Shifts](#captain-shifts-and-handoff) bound how long any one chat runs, and [No live Captain](#no-live-captain) covers restarts.
2. **Recoverable.** All Captain state lives in Phalanx and GitHub. A fresh Captain rebuilds everything from the charter, the API, GitHub and runtime session status, with no loss. **Several Captains may run at once** (Caleb). Captains only coordinate: they launch, message, stop and register sessions, answer questions and act on flags. They never do an item's work, so every repository and GitHub write happens in a worker session under that item's fencing. Overlapping Captains therefore can't corrupt work. Safety is per item, not per Captain:
   - writer registration is atomic and allows exactly one writer per execution;
   - replies and steering are versioned;
   - unconfirmed intents belong to the Captain that made them.

   Two Captains can't launch duplicate work for one item: the loser of a race gets `409` and moves on.
3. **Stay on task.** A durable charter file on the runner host, outside worker-writable checkouts, holds the goal, in-scope repositories, allowed actions, escalation rules and stop conditions. Each wake re-reads it, records its revision in the heartbeat and runs a fixed loop:
   1. Reconcile. Read flags, the inbox and writer states, and report session status.
   2. Act on items needing the Captain in priority order: answer, launch, replace or stop. Work in a bounded batch rather than one item per wake, so a 5 min cadence doesn't throttle a large queue.
   3. Record each action on its item's timeline (`captain_action` with the item ID and reason).

   New work must be filed as an item before a session is launched for it. Any visible session without a registered writer, reviewer or Captain registration is flagged `orphan_session`.

   As built in step 4, `python -m phalanx shift` is the Captain's client, and its packaged prompt (`shift prompt captain`) is the fixed loop. The charter is a TOML file whose revision is the SHA-256 of its bytes, and `shift` refuses one inside a checkout, worktree or Git repository. `shift launch` stages the writer token and nonce in the item worktree's Git directory before registering, so rerunning it replays the same intent, and makes them the live credential only once the server accepts the nonce. The README's [Captain section](../README.md#run-a-captain-on-the-direct-path) shows the setup.
4. **Deviation detection.** See [Deviation flags](#deviation-flags). The reconciler and server raise these flags. They never block.
5. **Escalation.** Escalate to the owner only for trust, scope changes, destructive actions (overwriting a foreign head, closing PRs, deleting branches or data), missing credentials or authority, and exhausted retry caps. The Captain decides everything else and logs it (`answered_by: captain`, [decisions.md](decisions.md)).

### Captain shifts and handoff

Long chats degrade as context is compacted. Each Captain works a bounded **shift** and then starts a fresh successor. Shifts can overlap, because safety is per item.

- **Registration.** A new Captain calls `POST /v1/captains` with its native session reference, charter revision and the Captain that spawned it, if any, and receives a Captain ID. Each wake heartbeats `PUT /v1/captains/{id}` with the charter revision, wake count, the items it is acting on and its state (`active`, `ending`, `ended`). The board lists every live Captain, its session link, shift age, last heartbeat and current focus.
- **Shift end.** A shift ends on the first of these:
  - a fixed age (4 h);
  - a wake count (48);
  - the Captain's first observed context compaction;
  - a degradation signal: three failed actions in a row, a deviation flag it raised against itself (`captain_idle_with_work`, `orphan_session` it created), or a missed heartbeat it notices on waking.

  The bounds are charter settings (D7).
- **Handoff.**
  1. The outgoing Captain sets `ending` and posts a handoff note: open decisions, its unconfirmed intents and anything it was midway through. The note is a hint. The successor verifies every point against the server, GitHub and session status and never trusts it as state.
  2. It launches the successor with the charter, the parent Captain ID and a nonce, using the same at-most-once launch rule as writers.
  3. It doesn't wait for the successor to take over. It finishes or explicitly abandons each of its own intents, which the server records, so they become actionable by any Captain.
  4. It sets `ended` and stops acting.
- **Cleanup.** The successor confirms the predecessor is `ended` with no owned intents, then archives the predecessor's chat. A predecessor still `ending` after 30 min is treated as dead (below).
- **Dying mid-handoff.** A Captain whose heartbeat is older than 15 min is `stale`. Its unconfirmed intents become actionable by any live Captain, which resolves them by nonce. If it had already launched a successor, that successor continues. If not, [No live Captain](#no-live-captain) applies.

### No live Captain

The reconciler is a long-running process, so it, not a chat, notices when no Captain is live.

- **Detect.** No Captain is `active` or `ending` with a heartbeat under 15 min while open work exists. The server raises `no_live_captain`, the board shows a banner with the last Captain and its handoff note, and the reconciler runs the runner's `notify_command`.
- **Restart.** A runtime scheduler that **starts a new session** on a fixed cadence, such as a scheduled automation that opens a fresh chat, runs a short "Captain keeper" prompt every 30 min. The keeper checks `GET /v1/captains`. If a live Captain exists, it ends without acting. Otherwise it registers and works as a Captain from the charter. Each runtime adapter documents its scheduler (D8).
- **Owner fallback.** If no runtime scheduler is configured, or the keeper itself fails, the owner sees the banner and notification and starts a Captain from the charter by hand. Nothing else blocks: workers keep running and posting evidence, and the reconciler keeps recording GitHub state. The keeper doesn't depend on the reconciler. If both are down, the keeper still finds no live Captain through the API and starts one, and that Captain restarts the reconciler.

### Deviation flags

A flag names an item or a Captain and carries a reason, `first_seen`, a recovery action and an automatic clear when its condition clears. The board shows flags. `no_live_captain` and owner-needed flags notify the owner.

| Flag | Condition | Recovery |
| --- | --- | --- |
| `captain_stale` | A registered Captain has sent no heartbeat for 15 min | Live Captains take over its intents. The successor or keeper archives it. |
| `no_live_captain` | No live Captain while open work exists | The keeper starts one, or the owner does |
| `captain_idle_with_work` | Live Captains, but a runnable item has had no writer for 30 min | A Captain launches it, or the charter explains why not |
| `launch_unconfirmed` | Writer intent unconfirmed for 10 min | Captain searches native sessions by nonce, then confirms or withdraws |
| `duplicate_writer` | Two visible sessions carry one item's nonce or ID | Captain stops the unregistered one |
| `orphan_session` | A visible session has no registration | Captain registers it to an item or stops it |
| `no_progress` | No progress post or stage advance for 2 h while `busy` | Captain checks the session, then nudges or replaces it |
| `writer_stopped` | Writer reported `stopped`/`error` without a result | Captain resumes the same session or replaces the writer |
| `head_drift`, `checks_failed`, `review_feedback`, `pr_closed` | From GitHub reconciliation | The writer acts, or the Captain relaunches |
| `reconciler_stale` | A reconciler has sent no heartbeat for 15 min (one flag per reconciler) | Captain restarts it, or reports it to the owner |
| `observation_stale` | An open item's latest GitHub observation is over 15 min old | Captain checks the reconciler's pass log for that item's error |

## Failure and recovery

| Failure | Recovery |
| --- | --- |
| Captain dies between intent and confirm | Once it is stale, any live Captain finds the session by nonce, then confirms or withdraws. No blind relaunch. |
| Two Captains pick the same item | Writer registration is atomic. One wins and the other gets `409`. |
| Captain dies mid-handoff | Its successor continues if launched. Otherwise `no_live_captain` and the keeper restarts one. |
| No Captain alive | Workers and the reconciler continue. `no_live_captain` notifies the owner, and the keeper or owner starts one. |
| Duplicate launch | The first confirmed session is the writer. The other is stopped and flagged. |
| Worker stops without a result | Resume the same session with a message, or replace the writer (step 4 above). |
| Server restart | State is durable, and tokens are stored as digests. Writers retry idempotent posts. |
| Reconciler down | Nothing blocks. The Captain notices `reconciler_stale`. |
| Host reboot | The runtime restores sessions. The automations wake the Captains, or the keeper starts one, and they reconcile writers. |
| Human merges or closes the PR | Reconciliation records delivered or asks the owner. |

## What gets removed

- **Relay:** `captain_relay.py`, `captain-relay.sqlite3`, `phalanx relay`, standing relay serve, `relay_stalled` (decision 65), and the README relay sections. The PH-11b decision is superseded.
- **Slot gating and admission:**
  - the attended slot journal and `phx attended prepare|receipt|renew|submit`;
  - `attended-recover`, `captain_loop.py`'s slot scheduling and the bridge adapter;
  - server recovery candidates, `recover-claim`, `recover-stopped` and `attempt_adoptions`;
  - timed worker leases, and continuation offers (`Phalanx-Next-Lease`/`Phalanx-Next-Stages`).

  Runner capacity becomes an advisory target shown on the board. Dispatch pause stays enforced (D6).
- **Runner-only stages:** `pr_creation`, `pr_promotion`, `pr_validation`, `pr_reconciliation` and `check_in` stop being claimed stages. The writer publishes (D1), the reconciler observes, and GitHub merges (D2). The trusted `pre_pr_validation` stage depends on D3.
- **Kept:** the server, intake, items, dependencies, questions and replies, report drafting, review and finalization, the operator board, the transition rules and trusted policy.

Schema changes are made directly, with no migrations or compatibility layers (decision 67). Live data isn't deleted. In-flight attended items either finish on the old path before it is removed or have their existing sessions registered as writers.

### Local SQLite stores removed

Caleb wants no runner-local database, because it duplicates the backend as a source of truth. Every runner-local SQLite store goes. A fact still needed moves to server state behind the API, or to GitHub, which already owns it.

| Local store today | Holds | Under this design |
| --- | --- | --- |
| `captain-relay.sqlite3` ([captain_relay.py:34-40](../phalanx/captain_relay.py)) | Relay requests and server heartbeat | Removed with the relay. Launch intents and nonces live in the server's writer registration. |
| `attended.sqlite3` `slot` and `completed` ([attended.py:127-150](../phalanx/attended.py), [attended_recovery_service.py:32-61](../phalanx/attended_recovery_service.py)) | Slot packets, marker and thread bindings, saved submissions | Removed. The writer binding, nonce and native session are server state. A worker's result goes straight to the API, so there's no saved submission to replay. |
| `attended.sqlite3` `captain` ([captain_loop.py:103](../phalanx/captain_loop.py)) | Loop identity and status | Removed. Captain registrations and heartbeats are server state. |
| `draft-publication.sqlite3` ([draft_publication.py:86-118](../phalanx/draft_publication.py)) | Publication intents and outcomes | Removed. The writer publishes (D1). The primary PR is found on GitHub by branch and recorded as a timestamped observation. |
| `draft-observations.sqlite3` ([draft_stage.py:177-180](../phalanx/draft_stage.py)); read by [captain_loop.py:320-324](../phalanx/captain_loop.py) | Draft-stage receipts for replay | Removed. Observations are idempotent server posts. |
| `published-validation.sqlite3` ([published_validation_stage.py:159-162](../phalanx/published_validation_stage.py)) | Validation submissions for replay | Removed with the runner-only stages. |
| `publication/pr-notes.sqlite3` ([pr_notes.py:33-40](../phalanx/pr_notes.py)) | Outbox for PR deficiency comments and Known gaps | Removed. The comment marker on GitHub already deduplicates notes. Pending notes become server flags, and the writer or reconciler posts them. |
| `attended_drafts.py` reads `completed` ([attended_drafts.py:19-30](../phalanx/attended_drafts.py)) | Development checkpoint lookup | Removed. The server's accepted development result is the checkpoint. |

**Kept on the runner host**, because none of these is a state store:

- configuration and credentials, and the charter;
- worktrees, result files and logs, which the server references as artifacts;
- OS file locks for machine-local exclusion: `phalanx-workspaces.lock` ([workspaces.py:239](../phalanx/workspaces.py)) and `local-check-{i}.lock` ([validation_stages.py:143](../phalanx/validation_stages.py));
- the per-attempt writer token file, which is a credential, not state.

`captain-loop.lock` and `attended-prepare-{slot}.lock` go with the loop and slots. Existing local files are left in place, not deleted, and simply stop being read.

### Active direct work under this design

| Work | Under this design |
| --- | --- |
| Runner-only lane (1b8fcbfe) | Unnecessary once runner-only stages go (step 5). Land it only if it is nearly done, since the old path runs until then. |
| Relay coalescing (44cdf154) | Unnecessary. Recommend stopping it now. |
| Relay SQLite contention (108f6009) | Unnecessary once the relay is retired (step 5). Until then, run one relay server, as the runbook already says. |
| Dirty-stop recovery (a4bcb34f) | Unnecessary once attended slots go. Keep its invariant: writer replacement never discards unpushed changes. |

## Migration order

1. **Merged means delivered.** Accept any GitHub merge of the item's primary PR as delivery in `pr_validation`, `pr_reconciliation` and `check_in`. This is independent of the rest and unsticks churchtree#55 now.
2. **Writer registration and evidence.** Add the writer routes and token, the `writer` role on result routes, and `phx work`. Add Captain registration and heartbeat, flags, and the board's writer, Captain and flag columns.
3. **Reconciler.** Read GitHub and git, post observations, and raise flags, including `no_live_captain`.
4. **Charter, Captain automation and shifts.** Pilot one item end to end on the direct path while the old loop runs other items. An execution with a registered writer isn't queued, so the old loop can't claim it. Run at least one shift handoff and one keeper restart during the pilot.
5. **End-to-end acceptance**, then retire the old path. The writer's [PR lifecycle](#pr-publication-and-delivery) landed early, so a pilot PR item can run fully direct. The acceptance test must show:
   - lost-launch discovery by nonce;
   - exact-writer matching, and no duplicate writers with two Captains racing for one item;
   - writer replacement without losing local changes;
   - merged-to-delivered for all three merge methods;
   - a shift handoff with overlap, and a keeper restart after every Captain is gone.

   Then remove the relay, slots, adoption, runner-only stages and every runner-local SQLite store, and update AGENTS.md (D1, D3), SPEC.md, STATE_MACHINE.md, the HTML diagram and README.

## Decisions

Caleb accepted every default below on 2026-11-23 ([decision 71](decisions.md)). They take effect as the migration steps land. Until then, AGENTS.md's runner-owned GitHub writes and draft-promotion rules still govern the running system, and step 5 updates them.

| # | Decision | Decided |
| --- | --- | --- |
| D1 | Should worker sessions push and open PRs themselves? AGENTS.md currently keeps GitHub writes on the runner-owned path. | Yes. Repository rules and branch protection still apply, and Phalanx reconciles one primary PR. |
| D2 | Who merges `checked_in` items? | GitHub auto-merge, enabled by the writer once the PR is ready (decision 19 already uses it). Accept any merge method. |
| D3 | Should trusted runner-local pre-PR checks stay a gate? | No. The reconciler runs them and flags failures. Repository CI gates the merge. |
| D4 | How does the worker get its writer token? | A per-attempt token in the worktree's git directory, never the runner token or chat text. |
| D5 | Cadence and thresholds | Wake every 5 min, stale at 15 min, flag no progress at 2 h, overdue at 3 h (existing). |
| D6 | Is dispatch pause enforced? | Yes, on writer registration. Capacity becomes advisory. |
| D7 | Shift bounds | 4 h or 48 wakes, the first compaction, or a degradation signal, whichever comes first. Charter settings. |
| D8 | What starts a Captain when none is alive? | A runtime scheduler that opens a new session every 30 min with the keeper prompt, which exits if a live Captain exists. The owner notification is the fallback. |

## Appendix: gap check of attended mode

Main at `40be235`, 2026-11-23.

### (a) Registering an externally launched session as an item's writer

**Exists**

- A claim takes only the queue head, never a named item: `Claim` has no item ID ([dispatch_api.py:28-32](../phalanx/dispatch_api.py)), and `admit` uses `queue_head` ([dispatch.py:86-126](../phalanx/dispatch.py)). Admission enforces pause and per-runner capacity ([dispatch.py:87-101](../phalanx/dispatch.py)).
- The local journal (`attended.sqlite3`) holds one record per slot ([attended.py:113-159](../phalanx/attended.py)). `receipt` binds marker + host to a native or queued thread ID only when a launch packet exists ([attended.py:493-514](../phalanx/attended.py), [attended.py:545-552](../phalanx/attended.py)).
- The server stores only an opaque worker reference and URL, republished on renewal ([dispatch.py:212-225](../phalanx/dispatch.py), [operator_schema.py:66-72](../phalanx/operator_schema.py)). It doesn't store the marker or host, and doesn't link a session at claim time.
- **Fencing:**
  - The lease digest is checked on renewal, with a 300 s default ([dispatch.py:212-218](../phalanx/dispatch.py), [config.py:6](../phalanx/config.py)).
  - Adoption re-leases an *expired* attempt under a fresh secret and records `attempt_adoptions` ([dispatch.py:260-277](../phalanx/dispatch.py), [dispatch_api.py:90-96](../phalanx/dispatch_api.py)).
  - Recovery matches the exact writer before adopting ([attended_recovery.py:224-261](../phalanx/attended_recovery.py)).
- `submit` requires a confirmed stop plus a matching marker, host and thread ([attended.py:554-590](../phalanx/attended.py)).

**Missing**

- A claim of a named item without a queue-head admission or capacity check.
- An API that attaches a native session ID, nonce and link to an attempt as its writer.
- Registration for a session that has no prior launch packet: `receipt` raises "No durable launch intent exists".
- A writer identity that survives without a timed lease.
- The closest path is adoption of an expired attempt, which applies only to sessions the loop launched itself.

### (b) Workers submitting evidence directly

**Exists**

- Two bearer roles, owner and runner ([auth.py:10-26](../phalanx/auth.py), [auth.py:48-56](../phalanx/auth.py)). Result, PR-observation, recheck and stop routes require the runner role plus `Phalanx-Lease` ([intake_api.py:19-22](../phalanx/intake_api.py), [intake_api.py:173-254](../phalanx/intake_api.py)).
- The worker writes a result file that the runner submits after the Captain attests a stop ([attended.py:554-590](../phalanx/attended.py)).

**Missing**

- A per-attempt worker credential.
- A worker-facing progress route.
- Any path that skips the runner. A worker posting today would need the runner token and lease secret.

### (c) GitHub-derived reconciliation and merged to delivered

**Exists**

- Due external waits are listed and requeued by version ([intake_api.py:209-220](../phalanx/intake_api.py), [waits.py:99-149](../phalanx/waits.py)). The Captain loop requeues them ([captain_loop.py:701](../phalanx/captain_loop.py)).
- GitHub is read only inside a claimed runner stage, so there is no background reconciler.

**Gaps that strand merged PRs**

- A merged PR in `pr_validation` is a mismatch and becomes `needs_input` ([published_validation_stage.py:100-106](../phalanx/published_validation_stage.py)). The same happens in `pr_reconciliation` ([validation_stages.py:402-404](../phalanx/validation_stages.py)) and `pr_promotion` ([promotion.py:76](../phalanx/promotion.py)).
- `check_in` accepts only a two-parent merge whose second parent is the ready head ([check_in.py:81-105](../phalanx/check_in.py)), so a squash or rebase merge is `unresolved`.
- The server accepts a verified merge only with a recorded Phalanx merge intent ([pr_observations.py:177-188](../phalanx/pr_observations.py)), so a human merge can never complete an item.
- STATE_MACHINE.md already asks to verify an already-merged PR before recording completion (line 65). The code only does that for its own merge commit. A human-merged `ready_for_check_in` item also can't satisfy dependents (decision 58).

### Captain continuity today

**Exists**

- **Loop:** the loop takes `captain-loop.lock`, which prevents two loops on one journal, and binds runner ID and API ([captain_loop.py:101-119](../phalanx/captain_loop.py), [captain_loop.py:715-721](../phalanx/captain_loop.py)).
- **Status:** loop status is written only on change, with no periodic heartbeat or last-poll time ([captain_loop.py:121-134](../phalanx/captain_loop.py)).
- **Relay stall:** after 180 s, with a request waiting, a relay stall is reported and the owner notified ([captain_loop.py:36-37](../phalanx/captain_loop.py), [captain_loop.py:662-699](../phalanx/captain_loop.py)).
- **Overdue workers:** `worker_overdue` is raised at 3 h ([captain_loop.py:468](../phalanx/captain_loop.py), decision 60).
- **Lease renewal:** renewal is the worker heartbeat ([dispatch.py:212-225](../phalanx/dispatch.py)), and `last_advanced_at` is tracked for the board ([operator_schema.py:55-63](../phalanx/operator_schema.py)).
- **Relay chat:** a session automation wakes the relay chat every 5 min, and its persistence across a reboot is unverified (README "Standing relay serve" and "Runtime mapping"; the runtime runbook). The relay store uses `BEGIN IMMEDIATE` with a 30 s timeout ([captain_relay.py:34-40](../phalanx/captain_relay.py)), and contention is tracked as 108f6009.
- **Captain answers:** Captain answers are attributed (`answered_by: captain`), and decisions go to [decisions.md](decisions.md).

**Missing**

- A heartbeat for the Captain chat or the loop.
- Detection of a dead Captain, or of a Captain idle while runnable work exists.
- A server-side registry of live Captains. Only the local `captain-loop.lock` exists, which can't see chats or other hosts.
- A charter with goal, scope, allowed actions and stop conditions.
- A per-item record of Captain actions.
- Orphan and duplicate-session detection.
- A no-progress flag, apart from `worker_overdue`.

### Shifts, handoff and Captain restart today

**Exists:** nothing beyond the relay chat's 5 min automation. The runbook starts the Captain and relay chats by hand, and nothing autostarts after a reboot or a dead chat.

**Missing**

- Any shift bound, compaction trigger or degradation signal.
- A handoff note, a successor launch or predecessor archiving.
- Ownership of unconfirmed launch intents by a Captain. Today they live in the relay store and attended journal on one host.
- A keeper that starts a Captain when none is alive, and a `no_live_captain` banner.

### Runner-local state today

**Exists:** seven runner-local SQLite stores alongside the server DB (`PHALANX_DB`, [config.py:28](../phalanx/config.py)). They are listed with their replacements in [Local SQLite stores removed](#local-sqlite-stores-removed). Several hold facts the server also tracks, such as attempt bindings and publication outcomes, which is the duplication Caleb wants gone.

**Missing:** server routes and tables for writer bindings and nonces, Captain registrations and handoff notes, and pending PR notes. GitHub observations aren't consistently timestamped as `observed_at`.
