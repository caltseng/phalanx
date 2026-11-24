# Phalanx design spec

Phalanx lets users dispatch work across registered repositories and carry it through to a
Markdown report or pull request. Caleb is the first user; the design must also serve other
people and teams. Agents run on machines with the relevant checkouts and credentials.
Favor a small API, recoverable execution and flexible work items.

**Running architecture:** [direct sessions](docs/direct-sessions.md), native Captain chats,
registered writers and an independent GitHub reconciler (decisions 68-71). Phalanx adds
visibility and coordination, not a permission gate. GitHub owns PR facts and merges;
Phalanx owns items, attempts, questions, evidence, flags and observations. Native sessions
are the only execution path. Clients keep configuration, credentials, artifacts, worktrees
and OS locks, not SQLite queues or publication journals.

Direct clients discover the existing muster via `--home`, `PHALANX_HOME`, then the single
per-user `~/.phalanx/muster.json` locator. Discovery is read-only: never initialize or migrate
state to repair discovery. Authenticated `muster status` reads identity, Captains, writers,
reconciler freshness and pause. See [layout and precedence](README.md#direct-muster-discovery-and-status).

Separate, explicit `deploy init` writes credential-free new-install templates under
`~/.phalanx_muster` by default, refusing an existing directory. Versioned foreground
service entrypoints preserve explicitly configured legacy paths and require an existing
database unless new-database initialization is explicitly requested. Adoption never moves
live state or restarts services; the service owner coordinates rollout after review.
See [deployment tooling and migration](docs/deployment.md).

## Work items and repositories

- An item has ID, registered repository, goal, output type (`report` or `pull_request`),
  timestamps, and one current execution. Optional fields include context, acceptance
  criteria, target branch, grouping label, priority, affinity and dependencies.
- A repository has a stable ID and expected remote. Local mappings verify the checkout and
  base before work. Repository instructions apply. Missing mappings or prepared checks
  environments are visible blockers, not permission to improvise another environment.
- PR target defaults to `checked_in`; explicit `ready_for_check_in` and `report` are
  preserved. Writers enable GitHub auto-merge only for checked-in work. Owner/Captain
  steering can change an open PR target, including delivery, but cannot switch PR/report.
  The writer reconciles GitHub auto-merge with the latest target on each delivery pass.
- A completed ready execution stays completed when its primary PR later merges into the
  target at the verified head or a proven descendant. Reconciliation enriches its final
  output once with final revision, merge commit and `merged_at`, and releases check-in
  dependents without a rerun, writer or merge authorization. Closed-unmerged PRs, unrelated
  heads and wrong-base merges do not advance it.
- `publication_mode: "draft"` (default `"ready"`) is a ceiling for either PR target. Writers
  never promote/merge it. Delivery verifies the primary PR open, draft, unmerged, at the
  reviewed head/item branch/target base. It records `draft_published`, satisfies no
  ready/check-in dependency and ends the writer. A changed head queues self-review.
- Every closed execution records exactly one verified PR or stored Markdown report.
  Failure/cancellation without the requested output records a disposition report with
  evidence, partial artifacts and recovery context. Open executions may have intermediate
  evidence without a final output.
- GitHub issue intake is explicit `ingest-issue` for a registered github.com repo and issue
  number. The local client reads the issue/comments with its credential, then posts an
  immutable owner-authenticated snapshot. Stable per-repo issue idempotency prevents
  duplicate retry intake; edited snapshots conflict. Issue content cannot grant authority
  or edit config. Polling, webhooks and ongoing synchronization are not part of this path.
- Optional `post_merge_check` observes deployment on the delivered merge commit for up to
  24 hours, stopping on success. Pending/failed/deployed/missing are flags, not stages or
  delivery gates. Rechecks replace the latest signal; changed/removed config retires old
  signals without extra provider reads.

## Dispatch and execution

| Term | Meaning |
| --- | --- |
| Muster | A batch of work launched from the Captain chat. |
| Marshaled | Context is sufficient to enter the queue; eligibility still applies. |
| Phalanx advancing | Accepted evidence moves work to the named next lifecycle stage. |
| Field report | Markdown deliverable, including a disposition when the requested output is unavailable. |
| Spearheaded by Phalanx | Footer on every PR Phalanx creates. |

End each created PR description with
`Spearheaded by [Phalanx](https://github.com/caltseng/phalanx)`, exactly once.

1. Authenticated owner intake files the item. Missing prerequisites keep it queued with
   a visible reason. Otherwise an unbound item visibly **awaits a direct Captain**; unpinned
   intake must not disappear behind an unavailable scheduler.
2. The Captain re-reads its charter and API/GitHub/native session state. It chooses named
   eligible work, records a writer intent/nonce and scoped token digest before launching
   one native session. Registration atomically enforces one open writer per execution and
   the charter's `max_writers` across its repositories. Dispatch pause stops new admissions.
3. The writer uses an isolated item worktree, reads repository instructions and performs
   its stage. `phx work` submits progress, result/evidence or a focused question using the
   credential in the item's Git directory. Never expose it in chat or commits.
4. Accepted results end one attempt and open the next runnable stage for the same writer.
   The writer remains registered across input, external, retry and dependency waits.
   After a reply or recheck, `work next` resumes when eligible. Keep working until a real
   blocker or verified completion, not merely a stage boundary.
5. The reconciler independently observes GitHub and posts timestamped facts/flags. It
   does not launch/stop sessions, mutate GitHub, or require a worker attempt.

## Development lifecycle state machine

[STATE_MACHINE.md](STATE_MACHINE.md) is authoritative; keep
[the HTML diagram](state-machine.html) in sync.

| Stage | Advance when | Recovery |
| --- | --- | --- |
| `development` | Candidate committed with repository checks performed. | Retry transient failures or ask a focused question; preserve local work. |
| `self_review` | Exact candidate reviewed against scope and repository rules. | Findings return to development; bound repeated no-progress cycles. |
| `publication` | Writer pushes/opens or reuses one PR at the explicit base, honoring target and draft ceiling; submits primary URL. | Reconcile uncertain effects before repeating writes. Never reuse a PR merged by an earlier execution. |
| `delivery` | Trusted reconciler verifies requested draft, ready or merged output. | Writer fixes failing checks, feedback/conflicts on the same PR; closed without merge asks for input. |
| `report_drafting` | Content-addressed Markdown draft stored. | Preserve evidence and handoff. |
| `report_review` | Exact stored draft and supporting evidence verified. | Return findings and reviewed revision to drafting. |
| `report_storage` | Reviewed artifact and terminal reference committed atomically. | Server retries storage independently, without redrafting. |

A ready primary head on the target base with required checks green, no feedback and no
conflict satisfies ready-only work. Checked-in work records that milestone and waits for
GitHub merging. A verified merge by anyone, via merge/squash/rebase, into the target completes
either target, including draft-mode work. Record exact final head, merge commit, time and
verified method. An owner merge after ready-only completion releases check-in dependents.
A merge into another base asks the owner. Never replace a closed primary PR silently.

**D1:** writers perform Git/GitHub writes under repository rules and credentials.
**D3:** trusted local checks are reconciler flags, not a Phalanx gate. Writers still run
repository checks; GitHub checks, reviews and rulesets gate merging. Provider failures produce
no false observations and back off independently by repo.

The reconciler uses its own minimal `reconciler.json`, not an execution-runner config.
One appended data migration normalizes retired stage names on closed execution snapshots
to `delivery`. It preserves outputs, versions, timestamps, attempts and immutable history,
so existing reports, board pages and later owner-merge reconciliation remain readable.

### Execution status and recovery

- Status is `queued`, `running`, `waiting_for_input`, `waiting_for_external`, `completed`,
  `failed` or `cancelled`, separate from stage. Every open state has an action, resume
  trigger or recovery path. Persist wait reasons and recheck due external waits with jitter.
- Results require the current writer binding, attempt, stage and expected version. A
  writer token has no timed expiry. Ended/replaced writers and stale attempts cannot submit.
  Retryable mutations are idempotent; reconcile uncertain remote effects first.
- Questions save prompt/options/default/handoff. A versioned reply records its author and
  queues the same stage. Captains answer routine questions from scope/repo evidence and
  record `answered_by: captain`; owner is the default.
- Lost launch response: discover the nonce through complete native session inventory
  before repeating. Confirm native ID/link in the registration. Stale/unknown session
  status is a flag, never proof it stopped.
- Replacement names the current writer and attests the exact session stopped after
  preserving local changes, checkpoints and external effects. Revocation and replacement
  are atomic. Never discard work or start a second writer to repair a lost response.
- Retry transient failures with bounded backoff; bound repeated identical findings and
  ask for input with evidence when recovery cannot progress. Keep unrelated work moving.
- Cancellation stops new work and reconciles in-flight effects before finalization.
  Rerun creates a linked execution with explicit target, preserving all prior history.
  Report finalization is durable, retryable and independent of dispatch pause.

## Runner and agent interface

Captains use their runtime's native tools to create visible sessions. The core assumes no
runtime's tool names or behavior. It records session IDs/links and nonce-based discovery
evidence. The writer kickoff contains goal, context, acceptance criteria, exact stage/result
contract, verified workspace/base/topic, dependency output pins, prior handoff and any reply.
It distinguishes the installed Phalanx control command from the repo's prepared checks
environment. Missing/unusable checks environments are explicit gaps, not permission to
borrow another checkout or install packages.

Writers submit JSON outcomes: `succeeded` with stage evidence, `changes_required` with
revision-bound findings, `needs_input` with question/handoff, or `failed` with reason and
partial artifacts. Only the server applies transitions and closes the execution. Report
review and storage always reference the exact immutable draft revision.

### Captain shifts and continuity

An operator-owned charter defines goal, repo scope, authority, writer budget, escalation and
stop conditions. Captain wakes every five minutes and hands off after four hours, 48 wakes,
first compaction or degradation by default. Overlap is bounded to thirty minutes. The
predecessor finishes/abandons uncertain intents and performs creator-owned cleanup before
ending, without waiting forever for a successor. Only the owner grants repository trust.

A runtime keeper wakes every thirty minutes to start a Captain if none is live, respecting
recent handoff and charter stop conditions. Existing writers and the reconciler continue
without a Captain. Stale Captain/reconciler, no progress, unconfirmed launches, stopped
writers and orphan sessions are visible flags with recovery actions. Archival requires
preserved local work and runtime creator authority; server records do not perform archival.

## Settings and dispatch gates

- Charter `max_writers` bounds new registered writers across its repositories. Intents and
  waiting writers count. Replacements add no extra place; reductions do not end writers.
- Pause blocks new registrations/attempts while existing progress/results continue.
- Captains consider repo mappings, charter scope, affinity, dependencies and retry time.
  Rank eligible work by urgent/normal/low priority aged toward urgent, then oldest ready
  time and stable item ID. A new stage gets a new clock; blocked/retrying work does not
  accrue runnable age prematurely.
- Never infer server admission from board counts. The server checks current durable
  prerequisites and fencing atomically at registration/attempt time.

## Worktrees and queue selection

Use isolated worktrees and deterministic item branches, allowing independent jobs in the
same repo. Reuse an existing worktree only after verifying branch/checkpoint/local edits.
Keep unpublished changes on their host; reconcile session and outputs before replacement.
Once a checkpoint is durable, another authorized host can fetch it and resume with the
handoff. Multi-device execution and handoff remain P2.

## Parallelism and recovery

Idempotency prevents duplicate posts, not all similar work. Similarity alone should not
block independent items. Preserve one primary PR and surface duplicates after bounded
publication reconciliation; prefer possible duplicates to silently losing the deliverable.
Reconcile uncertain merges before retrying. Never overwrite foreign pushes. Coordinate
known overlaps through explicit dependencies rather than a global one-writer-per-repo rule.

## Interdependent work

Dependencies can cross repos and name upstream `report`, `ready_for_check_in` or `checked_in`
milestones (check-in is the default). Validate self-links/cycles atomically. Bind each attempt
to exact upstream execution/output revisions. Ready-only output cannot satisfy check-in;
failed/cancelled disposition reports cannot satisfy successful report dependencies.

Keep unmet reasons visible. Refresh dependents in bounded batches on upstream change and
check durable evidence again at attempt assignment. Owner steering of queued context and
dependencies is versioned; active work cannot silently receive revised instructions.
Campaign planning/decomposition and automated replanning remain later work.

## Server and deployment

One standalone HTTP API owns the relational queue, attempts, history and reports; clients
never write its store directly. Local config, checkouts, credentials and worktrees remain
on their host. Local deployment is FastAPI/plain Uvicorn and standard sqlite3, DELETE journal,
synchronous EXTRA, short serialized writes and fair reads. CPython 3.14 is the deployment
baseline; 3.12+ is supported. Server credentials distinguish owner/runner roles; scoped
writer credentials fence item work.

Use existing `MIGRATIONS` for one-time schema changes; decision 67 permits the appended
drop-only change for the retired tables, not new migration machinery or live-data resets.
Keep storage-specific code localized. Hosted deployment adds HTTPS, persistent storage,
authentication and backups. Clients initiate outbound requests, so their repos need not be
exposed. Phone intake will use the same authenticated create endpoint.

## Users and workspaces

Build for people alone or in teams on a shared server. V1 runs one implicit workspace with
static owner/runner credentials; ship multi-user features in separate slices.

- **Workspace:** tenancy boundary owning repos, items, executions, runners, settings,
  questions and reports. Future queries/writes must scope to caller workspace and prove
  isolation in tests; introducing tenancy preserves existing data in a default workspace.
- **Users/roles:** provider identity and subject with verified email for display/invitations;
  start with Google (PH-27). Owners manage membership/repos/runners/dispatch, members file
  work/reply/act, viewers read. Add finer permissions only for a concrete need.
- **Attribution:** record requester, reply author, steering actor and Captain decisions.
  Questions go to requester and workspace owners; "Needs you" means the current user.
- **Credentials:** people use sign-in or hashed, revocable workspace-scoped personal API
  tokens; runners register with their own ID-bound token. Keep static tokens for bootstrap.
- **Local ownership:** checkouts/Git credentials stay with their owner, never the server.
  Captains use their own workspace's credentials and authority.
- **Fair sharing:** writer budgets belong to each workspace; add server-side workspace
  rate/storage limits when hosting multiple workspaces.
- Billing, enterprise SSO and cross-workspace sharing are not yet planned.

## Operator view

Read-only paginated board: open work first (running/queued/input/external distinct), recently
closed newest first, full history available. Show stage/status, repo, age, progress, wait
reason/action, writer/session, PR and flags. Highlight actionable input. Every unbound queued
item shows its Captain wait; do not infer capacity or rank-position blockers.

- Local Scribe serves `/operator/items/{item_id}` (current execution),
  `/operator/executions/{execution_id}` (exact execution), `/operator/queue` and
  `/operator/history`, reusing the existing inline detail. Rerun links navigate adjacent
  executions. Queue/history filters and opaque keyset cursors live in the URL; global search
  reads both open and closed lists using the existing item/execution ID, observed PR number/
  URL and goal/title text filter. Lists remain bounded, and missing/failed detail reads are
  visible. `GET /v1/operator/items/{item_id}` resolves the current summary under existing
  board-read authentication. No hosting, sign-in, runners or capacity view is introduced.
  Stored reports render a safe local Markdown subset using generated DOM only: strip raw
  HTML, render code literally, allow only credential-free HTTP(S) links and omit external
  images. The plain-text report API and strict CSP are unchanged.
- Scribe's header `+` (**New work item**) adds owner-authorized intake to the otherwise
  read-only local board through the existing `POST /v1/items` contract. The form reads registered
  repositories with an owner token, defaults to `checked_in` (displayed as **Merged**), and
  supports goal, acceptance criteria, target, priority, label, publication mode and dependencies.
  Tokens stay in memory; the anonymous loopback read path never authorizes intake. Browser
  creation requires a matching Origin and rejects non-same-origin Fetch Metadata. Bearer API
  clients without browser headers are unchanged. Retain one key and exact payload across an
  uncertain submission until an idempotent retry settles it. Inline validation and queued-row
  insertion preserve the current board view. See [the form contract](DASHBOARD.md#new-work-item-local-board).

Timeline events correlate their recorded attempt/worker, stage from/to, PR URL and full SHA,
never newer evidence. Prefer persisted reasons; otherwise derive bounded (400-character)
credential-redacted causes from outcome/issue/check failures/revision. Do not copy arbitrary
task/report/review content. Read PR facts/duplicate counts only from reconciler observations;
unknown remains null. Distinguish draft, ready and checked-in outcomes.

Use bounded queries, labels/groups, filters, conditional weak ETags and on-demand timeline
pages. Show freshness/disconnection. Ticking ages alone do not invalidate ETags; transitions
and temporal boundaries do. Records/reports remain available offline from the writer.
See [DASHBOARD.md](DASHBOARD.md#api-additions) for hosted UI proposals.

## First milestone

Demonstrate concurrent visible sessions across repos, report and PR delivery, feedback/conflict
recovery, authorized merging, interruption/question recovery and backlog visibility. Validate
the local operating envelope before multi-device handoff. Current native-session evidence
is in [the direct pilot](docs/pilots/direct-sessions.md).

## Design questions

| Decision | Starting proposal |
| --- | --- |
| Hosted service/storage | Local first; choose hosting from durability/concurrency/maintenance evidence. |
| User notifications | API inbox first, then one channel per user linking the item. |
| Intake authority | Many users/workspaces with owner/member/viewer roles; implicit workspace first. |
| Report storage | Small immutable reports in the backend; revisit for large artifacts. |
| Coordination strictness | Per-item fencing, isolated worktrees and explicit dependencies; narrower coordination only for real collisions. |
