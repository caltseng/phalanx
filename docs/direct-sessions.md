# Direct sessions: coordination, not a gate

Native Captains, registered writers and an independent GitHub reconciler are the only
execution path. Phalanx records, reconciles and flags; it adds no permission gate beyond
repository checks/reviews/rulesets, trust and credentials. See the [implementation map](architecture.md)
for code ownership and [operating guide](direct-path.md) for commands.

## Sources of truth

| Owner | Authoritative for |
| --- | --- |
| GitHub | PR identity/state/head/base, checks, reviews, merges, deployments and branch contents |
| Phalanx API | Items, dependencies, executions, attempts, writer bindings/nonces, Captain registrations, questions/replies, evidence, reports, flags and timestamped observations |
| Execution host | Native sessions, configuration, credentials, worktrees, artifacts and logs |

Only the server writes queue storage. Clients use authenticated API requests, not local
SQLite queues/publication journals. GitHub wins over cached PR facts. Chat/handoff notes
are hints to verify against current API, GitHub and native state, never another queue.

## Writer registration and fencing

1. The Captain prepares an isolated item worktree, creates a token/full launch nonce, saves
   the credential in its Git directory and registers an intent through the API. Expected
   versions, one open writer per execution, dispatch pause and persisted Building/Total
   limits are checked atomically. Intents/waits consume Total; replacement adds no extra slot.
2. `shift launch` returns a task-first name (at most 40 characters, suffix `[phx]`), item/writer
   IDs, full nonce and kickoff. Names redact recognized credentials/URLs and normalize
   whitespace; identical goals may share names. Identity is the registered session ID/full
   nonce, never title text. The kickoff distinguishes the Phalanx control command from the
   repository's prepared checks prefix. Missing/unusable checks environments require a
   visible question, not borrowing environments or installing packages.
3. Launch one native session and confirm its ID and returned link. Complete
   reserve/create/confirm before reserving another item. A reservation's `running` state
   does not prove a native session ran. After interruption, inspect **all** native inventory,
   including renamed sessions, and verify the full nonce in kickoff/metadata. Confirm a
   match. Only after proving absence and no create in flight may the owning Captain replay
   the same reservation, verify unchanged writer ID/nonce and create/confirm once.
   Inconclusive inventory or missing replay credentials retain the intent with an explicit
   recovery blocker. The ten-minute alert is notification, not permission to delay recovery.
4. Replacement names the current writer and attests its exact session stopped after local
   work is preserved. The server ends the old attempt and revokes its token atomically.
   Only the owning Captain handles an unconfirmed intent until it ends, goes stale or
   abandons it. Silence/progress age alone never proves a writer stopped.

Tokens have no timed expiry; ended/replaced writers cannot submit. `work next` resumes
eligible work. The canonical worker [work loop](../phalanx/prompts/worker.md#work-loop)
and [outage protocol](../phalanx/prompts/worker.md#control-plane-outages) govern continuation.

## Evidence submission

The scoped credential is at `git rev-parse --git-path phalanx-writer.json`, never chat text
or a committed file. Writers run `work status|next|progress|result|question` from the item
worktree. Results bind the live writer, attempt, stage and expected version in the write
transaction; retries are idempotent. Progress updates the visible note/timestamp.
Questions preserve prompt/options/default/handoff; Captain replies record
`answered_by: captain`, while owner is the default.

`status` and `next` return `last_reply` (or null) with the latest exact answer, version,
timestamp and attribution. Opening another attempt does not change that reply version.
Reports use drafting/review/storage of the exact content-addressed draft; the server retries
final storage without redrafting. [STATE_MACHINE.md](../STATE_MACHINE.md) is authoritative
for transitions and final-output guards.

### PR publication and delivery

Stages are `development -> self_review -> publication -> delivery`. The writer commits and
reviews the exact candidate, pushes the deterministic item branch (`phalanx/<sha256 of item ID>`)
and opens/reuses one PR at the explicit target base. Publication supplies its primary URL;
a PR merged by an earlier execution cannot be reused. Every created description ends with
`Spearheaded by [Phalanx](https://github.com/caltseng/phalanx)`.

Apply the worker's [merge decision](../phalanx/prompts/worker.md#merge-decision-publication-and-delivery)
and [stage instructions](../phalanx/prompts/worker.md#stage-results) for current target/mode.
These are writer policy, not new reconciler predicates.

Owner/Captain target steering is audited and cannot switch PR/report. Draft delivery records
`draft_published`, ends the writer and satisfies no ready/check-in dependency. A verified
merge remains authoritative for either PR target, including draft-mode work. Ready-only
completion may later be enriched by an owner merge without restarting its writer.
An open ready PR under draft mode raises `draft_required`; changed heads queue self-review.

## GitHub reconciliation

The reconciler reads git/GitHub and posts timestamped idempotent observations, without
launching/stopping native sessions, making GitHub writes or opening queue storage.
Incomplete reads post no false snapshot and back off independently by repository.

| Observed fact | Server behavior |
| --- | --- |
| Primary PR merged into target by anyone using merge/squash/rebase | Verify delivery; record final head, merge commit, time and method. |
| Closed without merge, or merged into the wrong base | Ask the owner; never silently replace the primary PR. |
| Ready primary head on target with required checks green, no feedback/conflict | Complete ready-only work, or retain checked-in work awaiting merge. |
| Head drift, conflicts, failed checks or actionable feedback | Raise evidence/flags for the retained writer. A moved target tip alone is not a conflict. |
| Item branch reachable from base without a PR | Flag only. |

Older snapshots do not replace newer ones except an observed merge; unchanged observations
do not bump versions. Missing trusted readiness policy remains a visible wait, but cannot
invalidate a verified merge. Failures for one execution are reported with its ID while
the pass continues for others. See [reconciler operation](direct-path.md#run-the-github-reconciler).

Completed ready PRs are paged until observed merged, independent of deployment opt-in/window.
Their verified `{head, base}` comes from `output_revision`, not the compact `{kind, url}`
output reference. A different merged head needs GitHub ancestry proof matching the verified
head. Unrelated heads, wrong-base and closed-unmerged PRs do not enrich completion.
One audited transition updates the output and releases check-in dependents atomically;
repeated observations do not bump versions. This grants no authority to merge ready-only work.

Optional `post_merge_check` observes the exact delivered **merge commit** for up to 24 hours,
stopping on success. Latest runs derive pending/failed/deployed flags; after the window,
success clears, failure stays and pending becomes owner-notifying `deployment_missing`.
Changing/removing configuration retires old signals without provider reads. These observations
match delivered PR/head/merge commit and never change state, output or version. Closed
executions never raise `observation_stale`. See [deployment signals](direct-path.md#post-merge-deployment-status).

## Captain continuity and goal adherence

The operating guide is the single reference for [Captain shifts and handoff](direct-path.md#captain-shifts-and-handoff),
[creator-owned cleanup](direct-path.md#creator-owned-session-cleanup) and
[retained-chat forwarding](direct-path.md#retained-chats-and-owner-message-forwarding).
Runtime prompts implement that workflow; historical pilot/decision records are evidence,
not competing operating instructions.

### Deviation flags

Flags carry identity, reason, first-seen time and recovery, clearing when their condition
clears. Owner-needed flags notify; informational flags do not block work.

| Flag | Condition / recovery |
| --- | --- |
| `captain_stale`, `no_live_captain` | Captain liveness/recovery follows the [continuity rules](direct-path.md#captain-shifts-and-handoff). |
| `captain_idle_with_work` | Runnable unassigned work for 60 minutes with admission available, dispatch on and no deliberate hold; launch or refresh a justified hold. |
| `queue_backlogged` | Unassigned work waiting 30 minutes on Building/Total, PR-fix preference or pause; informational, including with a hold. |
| `pickup_blocked` | Unassigned work waiting 30 minutes on scope/authority or checkout/trust; notify with the shared pickup reason/recovery, not a Captain-failure signal. |
| `launch_unconfirmed` | Unconfirmed for ten minutes; recover the same reservation by full nonce as above. |
| `duplicate_writer` | Two sessions carry one item's ID/nonce; stop the unregistered one. |
| `orphan_session` | Unregistered session or blocked/refused archival; creator preserves work, records outcome and escalates refusals once. |
| `no_progress`, `worker_overdue` | No progress/stage advance for two hours while busy, or writer overdue after three hours; inspect before nudging/replacing. |
| `writer_stopped` | Native stopped/error without result; resume the same session or safely replace. |
| `head_drift`, `checks_failed`, `review_feedback`, `pr_closed` | Writer addresses fresh GitHub evidence. |
| `reconciler_stale`, `observation_stale` | Reconciler heartbeat/open-item observation older than 15 minutes; inspect pass errors and restore the exact owned service or escalate. |

## Failure and recovery

Server restarts preserve bindings/token digests; writers retry identical requests.
Stopped-writer replacement preserves local work and reconciles external effects.
Stale native/provider observations never authorize discarding work. See [Recovery](recovery.md).
Only API/server-host administration opens queue storage; upgrades leave unused runtime
files untouched and never delete live records.

## Migration order

The relay, numbered slots, runner-only stages and client SQLite journals are retired.
Their implementation/removal history belongs in the [decision log](decisions.md) and
[pilot evidence](pilots/direct-sessions.md), not a current rollout checklist.
For existing stores, retain the explicit operator-owned
[offline conversion contract](development.md#existing-store-conversion) and
[reviewed adoption procedure](deployment.md#existing-runtime-reviewed-adoption-not-a-rename).
Startup creates/checks the canonical schema; it does not convert arbitrary deployed data.

## Decisions

The historical D1-D8 record and subsequent amendments live in [decisions.md](decisions.md).
Current writer behavior is specified above and in [writer operations](writer-operations.md);
current Captain behavior is in the [operating guide](direct-path.md#captain-shifts-and-handoff).
