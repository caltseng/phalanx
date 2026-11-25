# Direct sessions: coordination, not a gate

**Status: live.** Caleb accepted the principle and defaults in decisions 68-71.
Captains, writers, shifts and GitHub reconciliation are the only execution path. PH-DS5
removes the previous transport, admission machinery, separate PR-stage executor and client
SQLite stores. See [the operating guide](direct-path.md) and
[pilot evidence](pilots/direct-sessions.md).
The [implementation map](architecture.md) diagrams component ownership, one writer's
stage/result sequence and deployment boundaries. Removing the relay did not remove the
HTTP API server or Scribe.

## Principle

Phalanx adds visibility and coordination, not an extra permission gate between the runtime
and useful work. Repository checks, reviews, rulesets, trust and credentials still apply.
Phalanx records, reconciles and flags.

## Sources of truth

| Owner | Authoritative for |
| --- | --- |
| GitHub | PR identity, state, heads, base, checks, reviews, merges, deployments and branch contents |
| Phalanx server | Items, dependencies, executions, stage attempts, writer bindings/nonces, Captain registrations, questions, replies, evidence, reports, flags and timestamped observations |

GitHub wins when observations disagree with cached PR facts. Chats, handoff notes, local
artifacts and worktrees are context, not another queue database. A fresh Captain verifies
them against the API, GitHub and native session state.

## Components and state ownership

The server alone writes its Phalanx store. Captain chats use native runtime tools to launch,
message and stop sessions and register those actions through the API. Writers do the item's
work in isolated worktrees and submit scoped evidence. The background reconciler reads git,
GitHub and the API and posts observations; it never launches or stops sessions. GitHub owns
merging. Config, credentials, worktrees, result artifacts and logs stay on the host.

## Writer registration and fencing

1. The Captain creates a writer token and launch nonce, writes the credential in the item's
   Git directory, and registers the intent with `POST /v1/items/{item}/executions/{execution}/writer`.
   Registration checks the expected version and atomically enforces one open writer per
   execution and the owner's persisted dispatch Building/Total limits across all repositories.
   Intents and waiting writers count; replacing a writer consumes no extra place. A full
   limit is a normal wait, not an escalation. Dispatch pause refuses new registrations.
2. The Captain launches one native session with a concise goal-derived name and the full nonce
   in its kickoff. Task words come first; a short `[phx]` suffix aids discovery within
   40 characters. Names redact recognized credentials and URLs before truncation, normalize
   whitespace/control characters, and fall back to "Phalanx task" for absent text.
   `shift launch` returns structured item ID, writer ID and full writer nonce separately.
   Duplicate goals produce identical names, never shared writer identity. The
   kickoff includes the repository's exact prepared `checks_environment`, distinct from the
   Phalanx command. A missing or unusable environment is a visible validation gap; writers
   never borrow another checkout's environment or install packages.
3. The Captain confirms the native ID and link with `PATCH .../writer`. If the launch reply
   was lost, search the full native session list and inspect kickoffs/metadata for the exact full
   nonce before launching anything. Legacy names containing the nonce remain discoverable.
   If inspection is unavailable or inconclusive, retain the intent and surface the blocker. Only
   the owning Captain acts on an unconfirmed intent until it ends, goes stale or abandons it.
   After ten minutes an unconfirmed launch is flagged, not automatically repeated.
4. Replacement names the current writer ID in `replaces` and requires `replaced_stopped`.
   Verify the exact session stopped and preserve all local changes first. The server
   atomically ends its attempt and revokes its token. A stale writer cannot post.
5. Writer credentials have no timed expiry. Liveness comes from native status reports and
   progress. A busy and a hung worker can look alike; silence alone never authorizes stopping.

One writer retains the item across stages, with a new fenced attempt per stage. A result
that leaves runnable work opens the next attempt under that writer. Input waits, external
waits, retry delays and dependency waits retain the writer; `phx work next` resumes when ready.

## Evidence submission

The credential is `$(git rev-parse --git-path phalanx-writer.json)`, never chat text or a
committed file. The worker runs `phx work status|next|progress|result|question` from its item
worktree. Each result names the attempt, stage and expected version and authenticates with
the writer token, not the runner token. Accepted results and uncertain retries remain
idempotent. `needs_input` saves a focused question and handoff; Captain answers use
`answered_by: captain`. Progress updates the writer's visible note and timestamp.

`phx work status` and `phx work next` include `last_reply`: the latest persisted reply
for this writer's execution, or `null` before any reply. It contains the exact `answer`,
reply `version`, `created_at` and `answered_by` (`owner` or `captain`), matching owner
inspection. Read it when resuming after input; no duplicate session message is needed.
The reply version stays unchanged when `next` opens a new attempt.

### PR publication and delivery

PR stages are `development` -> `self_review` -> `publication` -> `delivery`.

The writer commits and reviews the candidate, then pushes the deterministic item branch
`phalanx/<sha256 of item ID>`. It opens one PR with the explicit target base or reuses the
branch's open PR. For ready publication it marks a draft ready; it enables GitHub auto-merge
only for `checked_in`. For `ready_for_check_in` it leaves merging to the owner. Every PR
description ends with `Spearheaded by [Phalanx](https://github.com/caltseng/phalanx)`.
The successful publication result supplies `pr_url`; it must belong to the registered repo
and not have been merged by an earlier execution. The server records one primary PR.

PR intake defaults to `checked_in`; explicit `ready_for_check_in` and `report` targets are
unchanged. An owner or Captain can steer an open PR item's target, including in direct
`delivery`, between `ready_for_check_in` and `checked_in`. Only queued work accepts other edits
alongside the target change. The timeline records the actor
and before/after targets. The writer reads the latest target on each delivery pass and enables
GitHub auto-merge only for `checked_in`, disabling it if retargeted to `ready_for_check_in`.
GitHub's required reviews, checks and rulesets still gate a merge (D2); no new setting is needed.
Steering never changes to or from `report`. Completed ready items cannot be steered, but the
reconciler records a later owner merge of their verified head or a descendant without a rerun.
Delivery waits for reconciler observations without an open attempt. The writer remains
registered and fixes failing checks, feedback and conflicts on that PR.

**Draft-only publication:** intake `publication_mode: "draft"` is a ceiling for either PR
target. The writer opens a draft with the explicit base, disables auto-merge and converts a
reused ready PR to draft. It never promotes or merges. Delivery completes only when the
trusted reconciler observes the exact primary PR open, unmerged, draft, on the item branch,
at the self-reviewed candidate and target base. It requires no ready-only CI or mergeability.
A new head queues self-review, a ready PR raises `draft_required`, and a wrong base waits.
Draft delivery records `draft_published`, ends the writer and satisfies no ready/check-in
dependency. An actual merge remains a GitHub delivery fact, never a draft outcome.

Trusted local checks are flags, not a gate (D3). Writers still follow repository checks.
Reports use drafting, review and storage of the exact content-addressed draft; the server
retries final storage independently.

## GitHub reconciliation

The reconciler posts timestamped, idempotent observations with the primary PR and exact head.
The server applies observations to delivery and records flags. Incomplete provider reads
post nothing and back off; one failing repository does not block unrelated work.

| GitHub fact | Server behavior |
| --- | --- |
| Primary PR merged into target by anyone, by merge, squash or rebase | Complete either PR target, record final head, merge commit, time and verified method. No Phalanx merge intent is required. A completed ready item's later owner merge also releases `checked_in` dependents. |
| Primary PR closed without merge | Ask the owner; never silently open a replacement. A reply queues delivery for the writer. |
| Ready primary head with required checks green, no feedback or conflict | Complete ready-only work, or record the ready milestone while checked-in work awaits GitHub merging. |
| Changed head, conflict, failed check or review feedback | Record evidence and flags for the writer or Captain. A moved target tip alone is not a conflict. |
| Item branch reachable from base without a PR | Flag only. |

GitHub rate limits back off and never block other items. An execution's unexpected
observation failure is recorded with its item ID in the pass's errors; remaining
executions are still processed.

A PR on another base never qualifies as ready; a merge into another base asks the owner.
Older snapshots do not replace newer ones except an observed merge. Unchanged observations
do not bump the version. Required ready checks come from trusted policy; absent policy
keeps readiness waiting visibly but cannot invalidate an actual verified merge.

Post-merge deployment is an optional observation, not a lifecycle stage or
completion gate. A repository mapping's `post_merge_check` names its
post-merge deployment check run. For configured repositories the reconciler
observes executions checked into the mapping's `base_branch` for at most 24 hours
after check-in, stopping earlier on success and never enumerating older closed pages.
It reads that check on the exact delivered merge commit. The latest run derives a board flag: missing or
unfinished is `deployment_pending`, completed success is `deployed`, and
any other completed conclusion is `deployment_failed`. Rechecks replace the
flag after retries before success. After the window, successes have no flag,
failures remain visible, and still-pending signals become one owner-notifying
`deployment_missing` flag with the deadline as its stable first-seen time.
Closed executions never raise `observation_stale`. Heartbeats carry each mapped
repository's current check name (or none); removing or changing it records a
disabled signal for previously observed deployments without GitHub reads, even
outside the polling window.
Checked-in executions accept deployment observations matching their
delivered PR, head and merge commit; those observations do not change state, output or version.
Repositories without this setting have no deployment signal or
extra post-merge polling. GitHub owns the workflow; no runtime runs a
deployment stage and a pending or failed deploy never reopens delivery.

Closed executions never raise observation-stale.

The same reconciler also pages completed direct ready outcomes, without the deployment window
or deployment opt-in, until their primary PR is observed merged. Operator summaries expose the
verified final output's `{head, base}` as `output_revision` (null without a PR output); `output`
remains the compact `{kind, url}` reference. The reconciler reads the verified head from
`output_revision`, not the compact reference. It does not rerun checks or
reactivate their ended writers. A merge into the target at the completed output's verified head
enriches that output with the final revision, merge commit, method and GitHub's `merged_at`.
For a different head the reconciler compares the verified head to that exact merged head using
GitHub ancestry and includes `merged_head_ancestor` only when it is reachable. The server requires
that proof to match the verified head. Closed-unmerged PRs, unrelated heads and wrong-base merges
do not advance completed executions. One `pr_observed` transition records the enrichment and
refreshes dependents in the same transaction; repeated observations do not bump the version.
Status remains `completed` and the target is preserved, while the board outcome becomes
`checked_in` and `checked_in` dependencies clear. This grants no authority to merge a ready-only
item and adds no runner hook or polling service.

## Liveness

Captains heartbeat each wake and report native session state and inventory. Writers post
progress. The reconciler heartbeats independently. Stale observations and processes are
visible flags, never proof that local work can be discarded.

## Captain continuity and goal adherence

The operator-owned charter defines goal, repositories, authority, escalation and stop
conditions. Each wake re-reads it, inspects flags, reconciles sessions and answers routine
questions. Repository trust belongs only to the owner; keep a blocked writer/session rather
than relaunching it. Use one shared trusted worktrees workspace where the runtime supports it.

### Captain shifts and handoff

Long chats degrade as context is compacted. Each Captain works a bounded **shift** and then starts a fresh successor. Shifts can overlap, because safety is per item.

- **Registration.** A new Captain calls `POST /v1/captains` with its native session reference, charter revision and the Captain that spawned it, if any, and receives a Captain ID. Each wake heartbeats `PUT /v1/captains/{id}` with the charter revision, wake count, the items it is acting on and its state (`active`, `ending`, `ended`). The board lists every live Captain, its session link, shift age, last heartbeat and current focus.
- **Shift end.** A shift ends on the first of these:
  - a fixed age (4 h);
  - a wake count (48);
  - the Captain's cumulative successful-compaction count reaching the charter's
    `[shift].max_compactions` (default 5);
  - a degradation signal: three failed actions in a row, a deviation flag it raised against itself (`captain_idle_with_work`, `orphan_session` it created), or a missed heartbeat it notices on waking.

  The bounds are positive-integer charter settings (D7, amended for compaction handoff).
  Ordinary compaction below the threshold is a checkpoint, not a mandatory handoff.
  Only trustworthy cumulative successful native telemetry counts. Repeated reports are
  idempotent and omissions preserve known totals. Unavailable telemetry remains unknown,
  never a fabricated count or a first-compaction fallback; age/wake and actual
  degradation/failure safeguards still apply independently.
- **Handoff.**
  1. The outgoing Captain sets `ending` and posts a handoff note: open decisions, its unconfirmed intents and anything it was midway through. The note is a hint. The successor verifies every point against the server, GitHub and session status and never trusts it as state.
  2. It launches the successor with the charter, the parent Captain ID and a nonce, using the same at-most-once launch rule as writers.
  3. It doesn't wait for the successor to take over. It finishes or explicitly abandons each of its own intents, which the server records, so they become actionable by any Captain.
  4. It sets `ended` and stops acting.
- **Creator-owned cleanup.** Each Captain archives the writer sessions it created once their
  executions are completed, failed or cancelled, or their abandoned/replaced writers have ended
  and their runtime sessions are confirmed stopped. An ending Captain also cleans up its own
  ended/stopped helpers, never its live successor. Closed items' writers remain discoverable as
  `ended_writers` on the Captain, with a `cleanup_pending` count. After ending its shift, a Captain
  with pending writers keeps only a cleanup wake, not dispatch/coordination, until this count is
  zero. Refusals are escalated once and leave that actionable count.
  The Captain's creator (including a keeper run that launched it), not its successor, archives
  its chat after a clean handoff: `ended`, no owned intents or pending writer cleanup, and a
  registered successor or explicit charter stop condition. A keeper checks its own previous
  Captain before any live-Captain early exit and retains a cleanup wake/child-ended notification
  in that same creator session; a fresh keeper run cannot assume its creator authority.
  A predecessor still `ending` after 30 min is stale, not automatically safe to archive.
  Immediately before any archive, `shift archive-check` verifies local work is preserved:
  the worktree is clean and its commits are on its freshly fetched origin branch, or on the
  item's freshly fetched origin base if its branch is absent. Unpushed commits, local changes
  or unavailable evidence skip archival and raise `orphan_session`.
  The runtime's session tools perform archival; the core, CLI and API stay runtime-neutral.
  `shift archive-result` records success, block or refusal with the reason. A refused archive
  leaves the session untouched and keeps its flag even after the reporter ends; it is never
  retried in a loop. Only separately verified archival clears it. See the
  [commands and runtime protocol](direct-path.md#creator-owned-session-cleanup).

  As built in step 4:
  - `shift handoff` sets `ending` with the note. It returns the Captain's open intents and a successor name, `phx captain <parent ID prefix>`, which the server derives and which serves as the successor's launch nonce, plus its kickoff.
  - Off-shift chats disclose their role and route each new owner/forwarded interaction through
    `shift current` (`GET /v1/captains/current`), not a remembered immediate successor. That
    read examines all registrations and requires one live `active` Captain. Native tools verify
    its exact `session_ref` and supply the clickable conversation link; a second read before
    forwarding detects intervening succession. Missing, stale, ambiguous or unavailable evidence
    is reported explicitly, never repaired by guessed links, duplicate dispatch or fabricated
    owner provenance. Original-message forwarding preserves permission boundaries.
    On first registration a Captain's name uses its own ID where native renaming is supported.
    After a completed handoff (`ended`, no open intents, verified registered successor), only
    the outgoing Captain renames its own verified conversation with exactly one `[retired] `
    prefix, preserving the substantive title and normalizing repeated leading retirement tags.
    The successor notifies the verified predecessor binding once after registering, so the
    outgoing chat can finish its own presentation change even after its scheduled wakes stop.
    Native identity inspection and a fresh `shift current` check protect the incoming/active
    Captain and unrelated sessions; premature, failed or uncertain handoff leaves names alone.
    Retries re-read state/identity/title and skip an already correct name. Missing/restricted
    rename support or title limits are reported, never bypassed by truncation or storage edits.
    Retirement is presentation only, not election/lease state, archival or writer cleanup;
    off-shift replies still disclose their role even if renaming fails.
    The predecessor-derived launch nonce remains recovery context, not
    current identity. No runtime nesting or archive ownership changes.
    The tracked forwarding section is composed into Captain/keeper prompts and generated
    handoffs; retained chats require an explicit verbatim relay after normal deployment.
    See [forwarding and rollout](direct-path.md#retained-chats-and-owner-message-forwarding).
  - States only move forward (`active`, `ending`, `ended`), and `ending_at` is set once. `ended` isn't refused while intents remain open: they become actionable by any Captain, and the Captain view lists them.
  - Each Captain action is posted with `shift action` to `POST /v1/items/{id}/captain-actions` and appears on both timelines.
    The reserved action `held` requires a reason describing the charter lane order, writer cap or
    mutual exclusion. Refresh it for each deliberately unlaunched runnable item every wake. It is
    active only while it is the latest Captain action on the queued execution's current version,
    is less than 30 minutes old, and its Captain is live and not ended. The board and item view show
    its reason as the wait reason (behind existing dispatch, retry and prerequisite waits). Holds
    suppress dispatch-overdue warnings and exclude normal Captain pickup while active, but reserve no capacity.
    Full-capacity backlog remains visible even with an active hold.
  - The record step reports every Phalanx runtime session: task-first names ending in `[phx]`, legacy `phx …` names, registered session IDs and renamed sessions identified by kickoff/metadata. Reports contain ID, display name and, for writers, a separately verified full `writer_nonce` (`--session-writer ID NONCE`). The server keeps each session's first sighting and derives `orphan_session` and `duplicate_writer` by registered session ID and full nonce, falling back to legacy item-prefix/nonce name words only when structured identity is absent. Titles never establish ownership for task-first sessions. A pending successor's name is exempt while its parent is `ending`.
  - The keeper prompt is `shift prompt keeper`. A decommissioned reconciler's heartbeat is retired with `DELETE /v1/reconcilers/{runner_id}` (`shift retire-reconciler`).
  - Worker sessions open in one runtime workspace for the whole worktrees folder (`session_workspace` in the shift config), which the owner trusts once.
- **Dying mid-handoff.** A Captain whose heartbeat is older than 15 min is `stale`. Its unconfirmed intents become actionable by any live Captain, which resolves them by nonce. If it had already launched a successor, that successor continues. If not, [No live Captain](#no-live-captain) applies.

### No live Captain

The keeper starts a new Captain every thirty minutes only when none is live, respecting
recent handoffs and charter stop conditions. Captain wakes occur every five minutes.
Existing writers and the reconciler continue without a Captain; `no_live_captain` notifies
the owner. Runtime schedules must be checked after a reboot.

### Deviation flags

A flag names an item or a Captain and carries a reason, `first_seen`, a recovery action and an automatic clear when its condition clears. The board shows flags. `no_live_captain` and owner-needed flags notify the owner.

| Flag | Condition | Recovery |
| --- | --- | --- |
| `captain_stale` | A registered Captain has sent no heartbeat for 15 min | Live Captains take over its intents. Its creator cleans up only after handoff and preservation checks. |
| `no_live_captain` | No live Captain while open work exists | The keeper starts one, or the owner does |
| `captain_idle_with_work` | **Dispatch overdue**: live Captains, dispatch enabled, both admission limits permit the work, and runnable work unassigned for 30 min with no active deliberate hold | A Captain launches it, or records/refreshes a `held` action with the charter reason |
| `queue_backlogged` | **Queue backlogged**: unassigned work waiting 30 min while Building or Total admission is exhausted, eligible waiting PR fixes take preference, or dispatch is paused. Counts and precise affected limit included; informational even with a Captain hold, never a stopped/failed Captain signal | Wait for affected capacity, service eligible PR fixes or resume dispatch. Existing writer-health flags remain independent |
| `pickup_blocked` | Unassigned work waiting 30 min on scope/uninitialized authority, affinity or repository/trust readiness; not a Captain failure or handoff signal | The shared pickup decision supplies the concrete reason and recovery; notify the owner |
| `launch_unconfirmed` | Writer intent unconfirmed for 10 min | Captain searches native sessions by nonce, then confirms or withdraws |
| `duplicate_writer` | Two visible sessions carry one item's nonce or ID | Captain stops the unregistered one |
| `orphan_session` | A visible session has no registration, or archival was blocked/refused | Its creator preserves local work and archives only when safe; a refusal records the reason and needs owner resolution, never a retry loop |
| `no_progress` | No progress post or stage advance for 2 h while `busy` | Captain checks the session, then nudges or replaces it |
| `writer_stopped` | Writer reported `stopped`/`error` without a result | Captain resumes the same session or replaces the writer |
| `head_drift`, `checks_failed`, `review_feedback`, `pr_closed` | From GitHub reconciliation | The writer acts, or the Captain relaunches |
| `reconciler_stale` | A reconciler has sent no heartbeat for 15 min (one flag per reconciler) | Captain restarts it, or reports it to the owner |
| `observation_stale` | An open item's latest GitHub observation is over 15 min old | Captain checks the reconciler's pass log for that item's error |

Writers also flag `worker_overdue` after three hours. Informational flags do not block work.

## Failure and recovery

Discover a lost launch by nonce. Resume an interrupted writer or replace it only with stopped
attestation and preserved work. Server restart retains bindings and token digests; workers
retry identical posts. A dead reconciler or Captain raises flags while other work continues.
See [Recovery](recovery.md).

## Durable state

Only the API and explicit server-host backup/restore administration open the queue database.
Execution clients keep configuration, credentials, the charter,
worktrees, artifacts and OS file locks, not local SQLite stores. Existing unused files are
left untouched; upgrades never delete live queue records.

## Migration order

This is the historical implementation/removal sequence, not a current deployment checklist.
Both removals shipped in [#209](https://github.com/caltseng/phalanx/pull/209) and
[#214](https://github.com/caltseng/phalanx/pull/214). The pilot records distinguish landed
code from live acceptance evidence; this list certifies no additional pilot cases.
For an existing installation follow [reviewed deployment adoption](deployment.md#existing-runtime-reviewed-adoption-not-a-rename),
not the retired recovery machinery.

1. Record merged means delivered, including owner merges releasing dependencies.
2. Register direct writers and accept their scoped evidence.
3. Observe GitHub with the independent reconciler.
4. Pilot shifts, overlap and keeper restart.
5. Verify lost-launch discovery, racing Captains, safe writer replacement, all merge methods,
   handoff and keeper restart in the [pilot](pilots/direct-sessions.md). Retire the previous
   transport and reservations in part 1, then runner-only stages/local stores in part 2.
   Each removal updates the specs and operational docs and runs full CI and the envelope probe.
   Dropped schema objects use one appended `MIGRATIONS` entry (decision 67), not compatibility
   layers or new migration machinery. The part 2 entry also normalizes removed stage names
   only on closed execution snapshots to `delivery`; immutable history, attempts, outputs,
   versions and timestamps are preserved.

## Decisions

Caleb accepted these defaults on 2026-11-23 (decision 71).

| # | Decision |
| --- | --- |
| D1 | Writers push and open their own PRs. Repository rules apply; Phalanx reconciles one primary PR. |
| D2 | Writers enable GitHub auto-merge only for checked-in targets. Accept any merge method. |
| D3 | Trusted local checks are reconciler flags, not a Phalanx gate. Repository CI gates merging. |
| D4 | Scoped writer token in the worktree's Git directory, never chat text or the runner token. |
| D5 | Wake five minutes, stale fifteen, no progress two hours, overdue three hours. |
| D6 | Enforce dispatch pause and persisted workspace Building (`max_building_writers`, initially 5) and Total (`max_writers`, initially 10 on new installs) atomically. Publication handoff releases Building, not Total; publication/delivery fixes must reacquire Building before editing or substantial tests. Eligible waiting PR fixes precede new builds. Owner edits persist through reload/restart; existing installations activate separately. |
| D7 | Shift bounds: four hours, 48 wakes, five successful compactions or actual degradation/failure. Age, wakes and compaction threshold are configurable in charter. The original first-compaction default is superseded by the compaction-handoff work item. |
| D8 | Runtime keeper every thirty minutes; owner notification is the fallback. |
