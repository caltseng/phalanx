# Phalanx dashboard (Scribe)

The accepted dashboard goals and current UI contracts live here.
[SPEC.md#operator-view](SPEC.md#operator-view) defines the product,
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) tracks open work, and
[decisions](docs/decisions.md) owns decision history and superseded alternatives.

**Current source:** the local board implements Home, inline details, item/execution
deep links, Queue/History, URL filters/search, sanitized reports, intake and writer
concurrency settings. The HTTP API/Scribe server remains the supported direct-session
architecture. Groups UI, hosted sign-in, multi-user roles and later phases are proposals.
Source features do not certify D1 performance/accessibility or owner visual acceptance.
Installed-package/service adoption requires separate [deployment evidence](docs/deployment.md#standing-authority-for-compatible-upgrades).
The compact-header source contract below is not installed/live acceptance.

## Goal

Users should see what is moving across their workspace's repositories, what needs them
and what was delivered, from phone or laptop. The eventual hosted website must remain
fast with thousands of items, many simultaneous workers and years of history.
It must not replace GitHub review, worker conversation or become an analytics warehouse.

## Who uses it

Current local operation uses configured owner/runner authority, not a hard-coded identity.
Hosted v1 proposes Google sign-in with a configured owner-email allowlist and bearer-token
fallback. Later users select their workspace: viewers read, members file/reply/act,
owners also manage members, repositories, runners and dispatch. The server enforces
roles and workspace scope; every action records its actor and **Needs you** routes
questions to the responsible user. Phones prioritize attention/replies; laptops motion/history.

## Goals

- Within five seconds, users know whether anything needs them.
- Every open row has a recorded stage/status, age and next action or wait reason.
- Every closed execution has exactly one verified PR or stored-report outcome.
- Target scale: 10,000 open items, 200 running and 1,000,000 closed executions;
  first screen under one second, all reads bounded/paginated.
- Hosted HTTPS and same-origin stable deep links; D2 actions reuse canonical API logic.
- Show honest freshness, never stale data as live. No D1 streaming requirement.

## UI principles

Attention precedes motion, then history. Rows remain dense (about 40 on a laptop),
calm, stable under refresh and explicit about recovery. Never reorder under the pointer,
move focus/reset scroll or reset looping animation phase. Use actual lifecycle loops,
not percentage complete; retired stages belong only to immutable history.
Link to GitHub and worker chats rather than duplicate their content.
All views/filters have URLs. Relative timestamps expose absolute local/UTC times.
Use runtime/platform-neutral language and icons plus text, never color alone.

Full keyboard navigation, visible focus, WCAG 2.2 AA contrast and reduced motion apply.
Up/down or j/k navigate rows, Enter expands, Escape collapses. Controls have 44px
minimum touch targets. Narrow reading layouts place complete multiline questions,
wait/approval text and wrapping links below metadata. Reports scroll under a persistent
heading and labeled 44px close button; closed ages align regardless of string length.
No third-party scripts/fonts/analytics or secrets in browser storage; preserve strict CSP.

Light/dark follow the system, with Auto/Dark/Light override in local storage.
Restrained thematic motion must not move row text/layout and stops under reduced motion.
Changed rows receive brief gold-ink/seal emphasis; Live/attention beacons, a live Captain's
heartbeat, unrolling details and theme-change ink remain small accents. Looping row motion
stays in phase across refreshes; branding follows its separate interaction-only contract below.

## Workload and visual reference

Rows represent bursts, small bookkeeping beside features, multi-PR goals, revision loops,
prerequisites, duplicates and interruptions. Stage, age, writer and next action remain
visible; details link outputs, dependencies and findings.

Branding is PHALANX, **Push forward**, and a distinct **Scribe** label even on compact
screens. The engraved-italic lockup places tagline/wordmark beside the circular shield.
`phalanx/static/phalanx-brand.svg` owns shared geometry and the README's ivory static
lockup; the dashboard references its parts. Local Palatino/Georgia fallbacks need no fonts.
Fine-pointer hover or keyboard focus parts the upper rim and raises the existing chevron
through it with a subtle formation trail: no idle/sideways animation, spear morph or
layout shift. Touch needs no hover; reduced motion retains resting mark and focus ring.

Running work uses D's inline three-spears (12x16px, taller center, broad leaf heads):
only spears move vertically 2px over 2.2 seconds, staggered .22 seconds left/center/right.
Captain references use A's Roman side-profile helmet, the three approved filled paths
in a 64x64 viewBox at existing em size. Queued uses outlined shield; waiting adds two bars.
Flags use a red pole/pennant, solid for owner notification, outlined otherwise, with
distinct accessible labels. Capacity stays neutral; deployment success is green.
Unmerged ready/draft uses a teal handoff arrow, verified merges/reports a green check,
human attention amber, failure red and cancellation muted. Glyphs use currentColor;
row/detail chips share semantics. Historical palette alternatives belong in decisions.

| Token | Meaning | Light | Dark |
| --- | --- | --- | --- |
| `--captain` | Live Captain | `#886000` | `#ecc46d` |
| `--run` | Active | `#1d5fbf` | `#79b8ff` |
| `--idle` | Queue/automatic wait/ended | `#70675d` | `#ada49a` |
| `--attn` | Human attention | `#9a5700` | `#f3bf62` |
| `--flag` | Problem/notification/failure | `#b3261e` | `#ff8985` |
| `--handoff` | Unmerged handoff | `#007477` | `#79d7dc` |
| `--ok` | Verified success | `#187345` | `#a2d7a4` |

Browser fixtures verify 3:1 icon contrast on actual background/row/raised/hover surfaces,
4.5:1 adjacent text, exact helmet geometry, handoff/success distinction and reduced motion.

## Pages

| Route | Current page |
| --- | --- |
| `/operator/` | Home |
| `/operator/items/{item_id}` | Current execution, previous executions linked |
| `/operator/executions/{execution_id}` | Exact execution, including before a rerun |
| `/operator/queue` | Open backlog |
| `/operator/history` | Closed executions |
| `/operator/groups/{label}` | Proposed grouped UI |

Item links re-resolve current execution; execution permalinks stay pinned.
Missing records show an error and refresh action. Details reuse one inline renderer.
Queue/history page limits are 100 open/25 closed. URL fields are `repository`, `label`,
`status`, `stage`, `outcome`, `closed_after`, `q`, `cursor`; outcome/time are history-only.
Home has separate `open_cursor`/`closed_cursor`. Applying filters resets pagination,
preserves search; Clear resets both. Later pages can return to first; empty filtered
pages with continuation retain More. Heading counts describe loaded pages; global
overview counts ignore page/filter. Search on Home covers open/closed goal/title text,
item/execution IDs and observed PR number/URL. A filter limiting one kind hides the other.

The sticky wrapping toolbar includes brand, freshness, navigation/current-page marker,
**+ New**, search/filter disclosures, theme/refresh and counts. Icon buttons have
accessible names and hover/focus descriptions. One nonmodal panel opens at a time,
focuses its first control, and Escape restores trigger focus (dropdown first).
Applied state remains visible in text/badges; unapplied edits survive panel closure.
Filter triggers show value/Any and expose existing exact-match text fields.
Writer settings reparent the host's same native disclosure, never duplicate its write path.
The toolbar retains top clearance for the upward rim break while removing bottom
padding, row spacing and the inline-heading baseline gap. Its complete default header
keeps the 100px budget at 1280px without scaling the shield or engraved typography.
This source contract does not certify live adoption or overall D1 acceptance.

### Home

Sections are Needs you, Flags, Captains, Active, Queued and waiting, Recently closed.
Each heading describes its page count; empty sections do not claim the whole queue empty.
The top bar retains rows when reads fail and shows Live/Partial/Stale/Disconnected with
age of oldest displayed read. Live requires every enabled initial read to succeed.

Needs you contains questions, failures and owner-notify flags, including those without
visible items. Question expansion shows prompt/options/default and exact reply command;
D2 proposes forms. Other flags group by kind, reason/count and oldest first-seen age,
expanding to recovery/item links; rows also carry flag chips.

Captain liveness uses server `captains.is_live`, never browser inference. Live gold
helmets precede stale attention-colored ones; muted ended entries live in a default-closed
native disclosure whose state survives refresh/detail changes. Show heartbeat, wakes,
reported successful compactions or **compactions unavailable**, and focus/handoff.
Charter revision is API enforcement metadata, not main-row clutter. Out-of-page Captain
references explicitly show status unavailable.

Active means running or writer-bound; other open work is queued/waiting.
Recently closed is newest first with output links and its own pager.
After 30 minutes, neutral **Queue backlogged** exposes occupied/maximum writer counts;
active holds do not hide full-capacity backlog. **Dispatch overdue** at 60 minutes
requires unassigned runnable work, free capacity, dispatch enabled and no active hold.
Dependencies are not missed launches; pause is informational.
**Pickup blocked** uses the 30-minute owner-notify budget for scope/authority/routing/
repository/trust blockers with shared reason/recovery, not Captain failure/handoff.

### New work item (local board)

**New work item** opens a native modal for registered repository, Markdown goal,
line-separated acceptance criteria, target, priority, optional label, publication mode
and dependencies (item ID/optional API milestone). Target defaults to `checked_in`,
displayed **Merged**. Named controls, focus containment, Escape/Close, 44px targets and
return focus to `+` support phone, keyboard and screen readers.

`/operator/api/items` adapts canonical intake validation/idempotency.
Explicit `PHALANX_LOCAL_BOARD=1`, direct loopback peer and local Host permit repository
reads and narrow intake without unlock/token. Nonlocal boards reuse an owner connection's
in-memory bearer credential. `/v1` retains its bearer roles; never inject/store server
credentials, put them in URLs/cookies, grant CORS or infer authority from forwarding.
Tokenless writes require exact same Origin (scheme/host/port) and, if supplied,
`Sec-Fetch-Site: same-origin`. Reject opaque/cross-site/same-site-but-different-origin.
Non-browser bearer clients without browser headers remain supported.

Lock double submission. Lost responses/server errors retain exact body/key across
close/reopen; only definitive rejection unlocks fields. Inline errors identify fields.
Success inserts the acknowledged row without remount, filter/pager/scroll changes;
polling reconciles it without mutating cached snapshots.

### Row anatomy

Open columns: accessible status glyph, short server-derived title/flags, repository,
stage/time, primary PR/report, writer state and reason/next action. Writer states include
busy, idle, unconfirmed launch, stopped/error and no writer. Below 760px rows wrap title/
age, metadata, then reason without truncating it.

Open **active** age starts at first persisted attempt, not intake/chat title.
Before activation, **queued** is elapsed execution age including prerequisites/dispatch.
Details show Execution created and Active since; stage age starts at later activation/
stage-entry. Allocation is not proof of native launch; unconfirmed warning stays separate.
Waits/rework/replacements do not reset active age; reruns have a new clock.
The strictly-over-90-minute active warning uses that first attempt, never queue age.

Closed outcomes are Merged, ready for check-in, draft published, report stored, failed
or cancelled, with closed age, output and intake-to-close duration on mobile too.
Show wanted target only for shortfall, using execution `state.target`.
Observed owner merge can display Merged without rewriting completed state.
One display mapping translates `checked_in` to Merged; API/filter/storage values stay exact.
Historical timestamps/durations stay intact.

### Item detail

Keep one row expanded in place across refreshes. Show complete goal, acceptance criteria
and supporting context (**None provided** when absent); long lists use native disclosures.
Fetch briefs only for authorized details, never compact queue rows. Display-only owner
titles do not edit goal/lifecycle. Render text safely; only accepted HTTP(S) URLs link.

Facts include status/outcome, target, version, stage/active age, progress, wait/since,
resume/retry, fenced attempt, IDs, writer/session, output and observed usage with explicit
unknown/final gaps. Show flags/recovery, question/options/default/reply, prerequisites,
loaded stage path/durations/return counts and paginated event history.

Captain/session links permit canonical `ghapp://sessions/<UUID>`,
`codex://threads/<UUID>` or safe HTTPS, never server-side launches. Unknown hosts remain
unlinked. Retain latest writer association across waits/history/closure; ended is not
deleted. Unidentified replacements do not borrow predecessor identity. Links say existence
unverified unless native inspection reported present; presence is last reported, not a
live guarantee. Explicit missing/deleted disables; partial inventory omission proves nothing.
Unsafe/conflicting identities remain unlinked.

Target detail extends to full lifecycle rail, revision-bound checks/review evidence,
conversation actor history, dependents, duplicates, merge commit and steering versions.
These are remaining UI scope, not implied acceptance.

Reports use dependency-free Markdown headings, emphasis, links, lists, quotes, tables
and fenced/inline code in the existing dialog. Strip raw HTML/comments, render image alt
text only, keep code literal and insert generated DOM only. Links accept credential-free
HTTP(S); no report scripts/images/dependency or CSP change. Plain-text report API remains.

### Groups

Labels group work; proposed UI shows rollups/size/span and can support future campaigns.
The existing bounded group API reports page-only rollups, never full-label aggregates.

### Metrics (D3)

Server history supplies delivery/day, median/p90 cycle time by stage, revision rounds,
failure/cancellation rates and queue time, for History/groups. Repository health,
decision review and notification deep links remain later work.

## Hosting and security

Hosted design is not current local authentication. Serve Scribe from HTTPS API origin,
no separate API URL/CORS. Google sessions are expiring server-side hashed IDs, HttpOnly,
Secure, SameSite=Lax; sign-out revokes. PH-27 initially permits read-only operator GETs,
not writes or runner routes. D2 cookie writes require Origin plus custom-header CSRF
protection, server roles/workspace checks, destructive confirmation and actor audit.
Rate-limit sign-in/writes. Proposed hashed static names cache long; conditional reads
are `private, no-cache`, other API responses `no-store`.

Local-board mode must stay off on hosted/proxied/tunneled services. Narrow local settings/
intake share trusted-local authorization; settings writes also require exact same-origin.
Review/merge alone never authorizes service activation.

## Performance and data

Server projects bounded rows; client renders only the visible page with shared DOM/link
helpers (`operator-helpers.js`), conditional authenticated transport (`operator-http.js`)
and shell wiring (`operator-shell.js`), loaded in that order.
Unchanged cached responses cause no DOM writes; changed/failing/recovered reads refresh.
A health timer changes freshness/relative spans only when displayed values change.
One delegated listener per mount handles row/title/report clicks and is removed on unmount.
Poll every 15 seconds, pause hidden tabs, reuse bounded snapshots on bodyless 304.
Learn GitHub facts only through reconciler observations; unobserved means unknown.

## API additions

[API reference](docs/api.md#operator-board-data) owns route details.
`/v1/operator` reads keep existing local/owner/runner authentication;
narrow `/operator/api` adapters reuse canonical intake/settings logic.
Summary fields include label, revision round, duplicate count, observed PR and pickup.
Revision round starts at one and increments on reentry to development/report drafting
(including PR revision in retained historical events).
Labels match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; steering null clears them.
PR fields (URL/number/state/draft/head/CI/size) and duplicates are nullable observations,
never populated from writer claims. Every open row has wait reason or next action.
Shared pickup uses scope/readiness, routing, dependencies, holds, retry, pause/capacity;
stale/uninitialized evidence never implies readiness.

Overview gives global counts, 24-hour outcomes, pause, oldest eligible queue and generated
time. Item GET resolves current execution or 404; timelines link previous/next reruns.
Execution filters enforce open/closed kind and bind cursor to filters (invalid combinations
400). At most 2,000 indexed candidates per filtered page can yield an empty continuation.
Group GET returns bounded current executions and `scope: page` rollup, unknown-size count
and dates; absent label is 404. Draft outputs count as delivered, not ready.
Weak ETags are authenticated before reads; unchanged returns 304 before rows/rollups.
Durable change tokens and indexed retry/queue/unconfirmed/24-hour boundaries invalidate;
ticking ages alone do not. Calculate ages from timestamps/generated time.

Exactly two persisted limits: `max_building_writers` and `max_writers`.
**Writer concurrency** displays Building used/limit, Delivering and Total used/limit;
saves one atomic Building <= Total pair (new installs 5/10; existing adoption separate).
Save preserves pause/aging and persists reload/restart. Reductions preserve sessions,
block admissions; Building-full/Total-full differ, spare Total never grants a build.
Waiting PR fixes precede new builds. Delivering includes rework awaiting Building.
Refresh reads canonical server values; charter cannot undo edits.

## Phases

| Phase | Remaining acceptance |
| --- | --- |
| **D1 Local board** | Complete groups/lifecycle detail and acceptance; seed 111 historical Phalanx PRs plus 10,000 open/1,000,000 closed synthetic executions. Home under one second, every row reason/link correct, mobile/accessibility verified, owner/designated reviewer phone/laptop screenshots accepted. Local functionality/source merges do not waive these gates. |
| **D2 Actions** | After D1 and proven hosted sign-in: replies/cancel/retry/rerun/pause/capacity, canonical routes, CSRF/roles/actors. |
| **D3 Insight** | After D1: metrics, repository health, Captain decision review and notification links. |

## Design questions

Hosting/domain belongs to PH-20. Notification channel selection remains open.
Select one channel per user whose messages deep-link to the affected item.
Captain decisions stay in [decisions.md](docs/decisions.md) until D3 makes reviewable API
records useful. Default-branch CI must be observed locally, never inferred or fetched with
server-held Git credentials. Later hosted actions do not broaden current local authority.
