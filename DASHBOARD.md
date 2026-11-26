# Phalanx dashboard (Scribe)

Status: **accepted by Caleb**. Implementation rows go into [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). The [Operator view](SPEC.md#operator-view) in SPEC.md remains the v1 contract. This document sets the goals, the UI principles and the target experience for the hosted dashboard.

The dashboard's visible name is **Scribe**. Its URL (`/operator/`), its API routes (`/v1/operator/*`) and its code keep the `operator` name. The local board implements the home page, row anatomy, inline detail, item/execution deep links, queue/history pages, URL filters and global search described below. It follows the direct-sessions model ([docs/direct-sessions.md](docs/direct-sessions.md)): items and executions, writers, Captains and flags. Hosting and sign-in remain separate work.

Phalanx's tagline is **Push forward**, shown on the connection screen and beside the
Phalanx and Scribe names in the board header. It follows the header's existing subtitle
styling and compact-screen behavior; the logo and hover animation are unchanged.

**Shipped versus planned:** PH-29a's server reads, the local home/inline board, server-owned
Captain liveness, glyphs, merged display label and owner-authenticated `+` intake are also
implemented. So are local item/execution routes, queue/history pages,
URL-backed filters/search and safe Markdown report rendering.
The groups page, hosted sign-in and later phases below remain design goals. Shipped
features are not a claim that PH-29d performance/accessibility or owner visual acceptance
has passed.

Unchanged conditional polls retain the cached board and detail nodes without DOM writes.
Changed responses and failed/recovered reads still refresh the view; the separate health
timer updates freshness and relative ages in place when their displayed spans change.
Optimistic intake rows are reconciled on the next poll without mutating cached snapshots.
Row title, cell and report clicks share one delegated
listener per mount, removed on unmount.

The browser scripts share DOM construction and web-link validation in
`operator-helpers.js`. `operator-http.js` owns only authenticated fetches and conditional
response caching; `operator-shell.js` projects API responses and wires the board,
creation/settings forms and report dialog. The shell loads helpers before their consumers
and the HTTP transport before the shell wiring; the standalone board fixture loads the
same helpers.

Open rows label their clock **active** from the execution's first persisted attempt,
not intake or a conversation title. Before any attempt, **queued** means elapsed time
since execution creation (including prerequisite/dispatch waits), not time actively worked.
Details retain **Execution created**, show **Active since**, and measure the current
stage from the later of first activation and recorded stage entry. Initial queue wait
therefore never appears as development age. Activation is the server's first attempt
allocation, not proof that the reserved native session has launched; launch confirmation
and its separate warning remain visible. Retained/replacement writers, rework, input and
delivery waits do not reset elapsed active age. A rerun starts its own execution clock.
The active-work warning uses that same first attempt and its unchanged strictly-over-90-minute
threshold; queue age alone cannot trigger it. Historical timestamps and closed intake-to-close
durations remain intact. This source change does not activate or reset a live deployment.

## Goal

The dashboard is a website, reachable from the internet, where each Phalanx user can see what Phalanx is doing across their workspace's repositories, what needs them, and what it delivered. They can use it from a phone or a laptop, and later act on it there too. It serves many users across workspaces ([SPEC.md](SPEC.md#users-and-workspaces)), with Caleb as the first. It must stay fast and readable with thousands of items, tens of workers running at once, and years of history.

## Workload and visual reference

The board must represent burst intake, multi-PR goals, small bookkeeping beside features,
long-running work, review rounds, duplicate outputs and prerequisite stacks. Keep each row's
stage, age, writer and next action visible without jumping under the pointer. Detail views
link the primary PR, duplicates, upstream/downstream work and latest findings. Repository
filters separate concurrent workstreams; grouping and a decision-review feed remain planned.
Questions belong in **Needs you**, reports render as Markdown, and interrupted work remains
visible with disconnection/recovery state and preserved history.

The following home-page sketch is illustrative, not a live incident snapshot. Running work uses the selected D inline SVG three-spear formation, with a taller center spear (`[pike]` below). Its 12-by-16px row silhouette uses broad leaf heads and legible shafts; only the spears wave vertically by 2px over 2.2 seconds, staggered by .22 seconds from left to center to right. The icon frame, row and text stay still, refreshes retain the wave phase, and reduced motion leaves a static formation. Captain rows and references use the owner-selected A Roman side-profile helmet (`[helmet]` below): the approved three filled paths in a 64-by-64 viewBox, rendered at the existing em size. Live Captain marks are gold; stale marks retain attention styling, and ended/unavailable marks stay muted, with unchanged accessible state labels. Queued work has an outlined shield and waiting work a shield with two bars (`[shield]` below); both stay quiet, with distinct screen-reader labels. Flags use a red pole and pennant (`[flag]`): solid for owner notification, outlined otherwise, with "notifies owner" / "flag" accessible text. Informational capacity markers stay neutral, and successful deployment flags use success green. Row and detail chips use the same flags. All SVGs are em-sized, use currentColor and need no external assets. Unmerged ready/draft outcomes use a teal handoff arrow, not a green check. Verified merged work and stored reports retain the green `✓`; human attention (`!`) is amber, failures (`✕`) red, and cancellation (`–`) muted. Transition/recovery arrows are unchanged. D1 overall dashboard visual acceptance remains open.

The semantic palette follows the retained Palette A reference, with the owner's latest
gold Captain override (not the superseded purple or teal Active alternative):

| Theme token | Meaning | Light | Dark |
| --- | --- | --- | --- |
| `--captain` | Live Captain | `#886000` | `#ecc46d` |
| `--run` | Active work | `#1d5fbf` | `#79b8ff` |
| `--idle` | Queue, automatic wait, cancelled/ended | `#70675d` | `#ada49a` |
| `--attn` | Human attention | `#9a5700` | `#f3bf62` |
| `--flag` | Problem/owner-notification flags and failure | `#b3261e` | `#ff8985` |
| `--handoff` | Unmerged handoff | `#007477` | `#79d7dc` |
| `--ok` | Verified success | `#187345` | `#a2d7a4` |

These are integrated with Scribe's actual warm light/dark surfaces, not the preview's
backgrounds. The browser fixture checks at least 3:1 icon contrast on the background,
row, raised and hover surfaces and at least 4.5:1 for adjacent status text. It also checks
the exact helmet geometry, distinct handoff/success shapes and native reduced-motion
behavior at normal icon sizes.

```text
Ⓐ PHALANX Scribe  ● Live updated 4s ago   1 needs you  4 active  6 queued  2 flags  1/1 Captains live   ◐ Auto  Refresh
NEEDS YOU 1
 ! Confirm decision 32: OAuth client          phalanx  development     41m  —    idle         Which OAuth client should sign-in use?
FLAGS 2
 [flag] ▸ head drift ×2                       Head moved after validation; revalidate                                         6m ago
CAPTAINS 1/1
 [helmet] Captain 52940744 live  ♥ heartbeat 30s ago  wakes 8  compactions 0  focus: PH-15, PH-12, PH-14
 ▸ Ended (12)
ACTIVE 4
 [pike] PH-15 Add direct GitHub check-in co…   phalanx  development   4m ago  —    busy         running
 [pike] PH-12 Preserve interrupted work…      phalanx  self review   2m ago  —    busy         running
 [pike] PH-14 Publish the reviewed primary…  phalanx  publication   1m ago  PR   busy         running
 [pike] PH-08 Persist waits, replies, rechecks phalanx  delivery      6m ago  PR   busy         CI failed; writer fixes same PR
QUEUED AND WAITING 6
 [shield] PH-14 Collect GitHub observations…  phalanx  development   3m ago —    no writer    Awaiting a direct Captain
 [shield] PH-15 Record PR review evidence…    phalanx  development   9m ago —    no writer    Waiting for 1 prerequisite
 …
RECENTLY CLOSED 25+
 ✓ PH-19 Exercise queue fairness under a st…  phalanx  Merged  closed 27s ago  PR   took 52m
 ✓ PH-17 Add bounded operator queue and tim…  phalanx  Merged  closed 9m ago   PR   took 1h 4m
 …
```

Captain rows show the native session's reported successful compaction count, or
`compactions unavailable` when telemetry is unknown (including historical Captains).
The charter revision remains in the API for enforcement, not in the main row.

Captain helmets and names link to their recorded host-app session. Native links are
restricted to canonical `ghapp://sessions/<UUID>` and `codex://threads/<UUID>` targets;
an existing HTTPS session URL is also usable. Activation is an ordinary browser link,
retaining external-app confirmation, never a server-side app launch or new conversation.
Opaque legacy Captain references without a known host remain unchanged and show
`session unavailable`. New `shift start` registrations encode a known app runtime in
the session reference; existing records are not rewritten or assigned a guessed host.

Work rows and details retain the latest writer's session association across active,
queued/resumed and closed executions, including history and execution permalinks.
An ended writer is not evidence that its app session was deleted. A newly registered
replacement without an identity does not borrow its predecessor's session.
Recorded links say existence is unverified unless native inspection explicitly reported
the session present; present means last reported, not a live availability guarantee.
Explicit missing/deleted reports disable the link with that reason. An omitted session
in a partial inventory is not evidence of deletion. Unsafe or conflicting identities
remain unlinked.

Long queue waits remain visible after 30 minutes. **Queue backlogged** uses a neutral
information marker and **Waiting for writer capacity**, including occupied/maximum
assigned-writer counts from canonical dispatch settings. An active Captain hold does
not hide full-capacity backlog. **Dispatch overdue** keeps warning styling only for
unassigned runnable work with free capacity, dispatch enabled and no active hold
(the API retains `captain_idle_with_work` for compatibility). Dependency waits are
not missed launches. Flags retain first-seen ages and execution/item links; paused
dispatch is informational, not evidence that a Captain stopped. **Pickup blocked**
uses the same 30-minute budget to notify the owner about scope, uninitialized authority,
affinity or repository/trust blockers, carrying the shared pickup reason and recovery.
It is not a Captain failure or handoff signal.

## Who uses it

- **v1 hosted:** a single workspace with an operator-configured owner, not a hard-coded person or account. The owner signs in with Google ([PH-27](IMPLEMENTATION_PLAN.md)) against a configured email allowlist. The owner token still works as a fallback, and for the CLI and API.
- **Multi-user:** people sign in and see the workspaces they belong to, with a workspace switcher, and each workspace's own repositories, runners and items. A user's role decides what they can do:
  - viewers can only read;
  - members file work, reply and act on items;
  - owners also manage members, repositories, runners and dispatch.

  **Needs you** shows the questions routed to the signed-in user, and every action shows who took it.
- **Devices:** phone and laptop are equally important. The phone matters most for **Needs you** and replies, and the laptop for **Active**, **Queued and waiting** and **History**.

## Goals

1. **Attention first.** Within five seconds of opening the dashboard, the user knows whether anything needs them and what it is.
2. **Is it moving?** Every open item shows its stage, its status, how long since it last advanced, and either its next action or its wait reason.
3. **A trustworthy record.** Every closed execution shows exactly one outcome and one link, to the verified PR or the stored report.
4. **Scale.** Targets:
   - 10,000 open items, 200 running and 1,000,000 closed executions.
   - The first screen renders in under a second on the hosted server.
   - Every list is paginated and bounded, and the client never loads everything.
5. **Hosted.** HTTPS, served from the same origin as the API, reachable from the internet, with stable deep links.
6. **Act from the dashboard (phase D2).** Reply, cancel, retry, rerun, pause dispatch and change capacity, through the same API routes the CLI uses.
7. **Honest freshness.** The dashboard always shows how old its data is, and never presents stale data as live.

**Non-goals:**

- Replacing GitHub's PR review or the worker chat. Link to them instead.
- Editing code or requests in place, beyond the documented steering routes.
- Multi-tenant billing, enterprise single sign-on, and sharing items across workspaces.
- An analytics warehouse. Phase D3 computes a few metrics from work history.
- Real-time streaming in D1. Polling is enough.

## UI principles

1. **Attention, then motion, then history.** Order every page this way: what needs you, then what is moving, then what is done.
2. **No row without a reason.** Every open row answers "what happens next, and why". A blank wait reason is a bug. This applies the "never fail silently" rule in [AGENTS.md](AGENTS.md).
3. **Recorded lifecycle, not percent complete.** Show the stages Phalanx actually recorded and the loops it went through, such as `self_review` returning to `development`, as [SPEC.md](SPEC.md#operator-view) requires. Retired stage names may appear in immutable historical events, never as new dispatch choices.
4. **Calm by default.** Keep color for states that need action: needs you, failed, stale and disconnected. Everything else stays neutral. Never use color alone; pair it with an icon and text.
5. **Stable under refresh.** A refresh never reorders rows under the pointer, moves focus, or resets scroll. New or changed rows are highlighted briefly.
6. **Dense and scannable.** On a laptop, each item takes one line, and about 40 rows fit on screen. The detail view holds the rest.
7. **Link out; don't duplicate.** Diffs and CI logs live on GitHub, and conversation lives in the worker chat. The dashboard shows a summary and a link.
8. **Everything has a URL.** Items, executions, reports, groups and filtered views all have shareable URLs, and a notification link lands on its item.
9. **Times are honest.** Show relative times, with the absolute local time and UTC on hover. Always show the age of the data.
10. **Neutral wording.** Say "worker chat", "runner" and "Captain", never the name of a specific agent runtime or operating system. This follows the runtime and platform neutrality rules in [AGENTS.md](AGENTS.md).
11. **Accessible.** Full keyboard navigation, visible focus, WCAG 2.2 AA contrast, screen-reader labels, and respect for reduced motion. On the board, ↑/↓ or j/k move between rows, Enter expands a row and Escape collapses it.
12. **Recognizably Phalanx.** Keep the phalanx visual identity as a restrained accent: the crimson and gold mark and wordmark. Readability comes first. Light and dark themes follow the system, and a header toggle overrides it (auto, dark, light) in local storage. Motion is small and thematic and never moves row text: the shield is raised on load and the wordmark glints, the Live dot and attention counts pulse like beacons, running work's three spears wave in formation, waiting shields stay steady, a live Captain's heart beats, changed rows get a stroke of gold ink and a seal, details unroll, and a theme change spills like ink. Looping motion stays in phase across refreshes, and none of it runs under reduced motion.
13. **Nothing secret or third-party in the browser.** No tokens in storage, and no third-party scripts, fonts or analytics. Keep the strict CSP.

Reading layouts keep expanded wait reasons, questions and approval text full-width below
compact metadata, preserving multiline text and wrapping long links. Field reports scroll
inside a separate body beneath a persistent heading and labeled 44px close button.
Recently closed ages start at the same column edge regardless of relative-time length.

## Pages

Every page reads state through the API. Filters live in the URL: repository, label, status, stage, outcome, time range and a text query. A global search accepts an item ID, an execution ID, a PR number or URL, or text from the title.

| Route | Page |
| --- | --- |
| `/operator/` | Home: top bar, **Needs you**, **Flags**, **Captains**, **Active**, **Queued and waiting**, **Recently closed**. |
| `/operator/items/{item_id}` | Item detail, showing the current execution, with earlier executions one click away. |
| `/operator/executions/{execution_id}` | A specific execution, including closed ones and reruns. |
| `/operator/queue` | The full open backlog, paginated and filterable. |
| `/operator/history` | Closed executions, paginated and filterable, with counts for the filter. |
| `/operator/groups/{label}` | Every item with that label, with a rollup. |

The first five routes are available on the **local** board. The groups page remains a
proposal. Item links resolve the current execution on each refresh; execution links keep
the exact execution, including a closed execution before a rerun. Both reuse the inline
detail renderer, with links to the current item, the execution permalink and the previous/
next execution when present. Missing records show an explicit error with a refresh action.

Queue and history use the existing bounded keyset list API (100 open / 25 closed per page).
Their URL query parameters are `repository`, `label`, `status`, `stage`, `outcome`,
`closed_after`, `q` and `cursor`; outcome/closure time apply only to history. Home also accepts
these filters and keeps independent `open_cursor` and `closed_cursor` parameters. Changing
filters starts at the first page; paging updates the URL and preserves filters. A bookmarked
later page can return to the first page, and a filtered empty page with a continuation still
shows More. Counts beside headings describe the loaded page, not a server-wide total.
Home's top-bar queue totals come from the global overview, independent of filters and
pagination. Empty sections explicitly describe this page, not the whole queue. Running
rows retain the server's next action. Live appears only after every enabled initial read
succeeds; failed reads remain Partial/Disconnected. Polls retain bounded in-memory
snapshots and send `If-None-Match`, reusing the snapshot on a bodyless 304.

The global search form always searches both open and closed work on Home, using PH-29a's
`q` matching (item ID, execution ID, observed PR number/URL or goal/title text). The filter
form preserves that query. Status filters show only the matching open/closed kind; an
outcome or closure-time filter shows only closed work.

The sticky header is one wrapping toolbar: branding, honest freshness, Home/Queue/History,
an explicitly labeled **+ New** entry point, search/filter icon disclosures, theme and refresh
icons, and the existing summary/attention counts. Icon buttons have accessible names and
hover/keyboard-focus descriptions; navigation marks the current page. Search and filters
start collapsed, open one panel at a time, and focus their first control. Escape closes a
panel and returns focus to its button (an open filter dropdown closes first). These are
nonmodal disclosures, not menus: normal Tab navigation remains available. Applied search
and filters remain visible as text and counted button badges even with both panels closed;
unapplied edits survive closing/reopening without being presented as applied state.
When the host supplies writer-concurrency settings, the toolbar reparents that same native
disclosure/form behind one settings icon. It does not duplicate settings, create a second
write path or change the host's dashboard authorization lifecycle.

Filters use compact, initially closed dropdowns. Each trigger shows its label and current
value (or **Any**), including edits before Apply filters; opening a dropdown exposes the
existing text field, so repository IDs, labels and ISO closure times retain their exact
matching behavior. Only one dropdown is open at a time; Escape closes it and returns
focus to its trigger. Apply filters preserves search and resets pagination, while Clear
resets the page's filters and search. Triggers, fields, Apply filters and Clear retain
44px minimum touch targets, wrapping onto short rows on narrow screens. This layout
does not constitute D1 acceptance or owner visual approval.

Complete default header/control-area measurements, from board top to section container
top, using the real renderer and stylesheet in a 900px-high browser viewport:

| Viewport width | Before toolbar | After toolbar | Reduction |
| --- | --- | --- | --- |
| 1280px | 184.66px | 86.90px | 53% |
| 390px | 402.72px | 182.53px | 55% |
| 320px | 501.76px | 182.53px | 64% |

Both measurements use the same unfiltered Home data: five active, five queued, 19 flags,
one of 20 Captains live, and intake available. Default panels are closed. Toolbar/navigation
targets remain at least 44 by 44px without horizontal overflow. The route browser fixture
checks the complete header height, disclosure/focus/Escape behavior, applied and pending
state, theme cycle, refresh, global search, URL filters and pagination. The separate intake
fixture exercises the real modal and owner-authentication client flows.

Current-item and execution details show **Acceptance criteria** and **Supporting context**
from the stored brief, alongside the goal, Observed usage, facts, actions and history.
Lists retain their complete text and wrap notes, paths and long URLs. Lengthy lists use
native keyboard-accessible disclosures; absent lists say **None provided.** Only explicit
HTTP(S) URLs accepted by the existing safe URL helper become links; all content is rendered
as text, never HTML. Queue rows stay compact and do not fetch full briefs. Owner-only
display-title metadata can clarify an item's caption without editing its goal or lifecycle;
there is no general editor.

Stored reports render in the existing report dialog as a local, dependency-free Markdown
subset: headings, paragraphs, emphasis, links, lists, quotes, tables and fenced/inline code.
Raw HTML tags/comments are stripped, images become alt text, and code stays literal.
Only generated DOM elements are inserted; links accept credential-free HTTP(S) URLs only.
No report HTML, scripts or external images are loaded, and the shell's CSP is unchanged.

### Home

Every section is a list of one-line rows. A section shows its count in its heading, and an empty section says so in one line.

1. **Top bar** (sticky): the Scribe mark, data health (Live, Partial, Stale or Disconnected, with the age of the oldest displayed read), counts for each section, the theme toggle and Refresh. A failed or stale read turns the health pill amber or red and keeps the last loaded rows on screen. Later: dispatch on or paused, the age of the oldest queued item, and default-branch CI for each repository.
2. **Needs you.** Items waiting for input, failed items, and items with a flag that notifies the owner, plus owner-notify flags that name no visible item. Expanding a question row shows the prompt, the options, the proposed default and the exact `reply` command. D2 replaces the command with a reply form. Later, it also takes PRs that need a human merge and duplicate PRs to review.
3. **Flags.** The remaining derived flags from `GET /v1/flags`, grouped by kind. Each group shows its count, its reason (or the number of distinct reasons) and the age of its oldest flag, and expands to one line per flag with its recovery. Each item row also carries its own flags as chips.
4. **Captains.** Live Captains always appear expanded first with the gold A Roman side-profile helmet and "live" label, then stale Captains with the same helmet in the attention color and a "stale" label. Muted ended rows retain the helmet and live below them in a native, keyboard-accessible `Ended (N)` disclosure, closed by default; its state survives board refreshes and opening/closing item details. Liveness is the server's `captains.is_live`, already exposed as `live` / `stale` by `GET /v1/captains`, never recomputed in the browser. Captain references in flags and item details use the same helmet, state colors and labels (or explicit "status unavailable" if outside the bounded read). Rows show `heartbeat 3m ago`, wake count, compaction count and handoff/focus.
5. **Active.** Items that are running or have a writer.
6. **Queued and waiting.** Everything else that is open: queued stages, external waits and prerequisite waits, with the open-work pager.
7. **Recently closed.** Newest first, with the closed-work pager. Each row shows the outcome and links to its PR or report.

### New work item (local board)

The board-level header has a `+` button named **New work item**, reusable by later queue
and history pages. It opens a compact native modal with registered repository, Markdown
goal, acceptance criteria (one per line), target, priority, optional label, publication mode,
and optional dependencies (one item ID and optional API milestone per line). Target defaults
to intake's `checked_in`, displayed as **Merged** using the board's display-label mapping.

Creation uses `/operator/api/items`, a narrow adapter to existing intake validation and
idempotency, not another intake service. With explicit `PHALANX_LOCAL_BOARD=1`, a verified
direct loopback peer and local Host can load `/operator/api/repositories` and create work
without entering a credential. The form opens directly, with no unlock step. Forwarding
headers cannot grant local authority. Nonlocal boards require an owner connection and
reuse its in-memory credential; `/v1` writes still require their existing bearer roles.
No server credential is injected into the browser or placed in storage, URLs or cookies.
Tokenless intake requires an exact same Origin (scheme, host and port)
and, when supplied, `Sec-Fetch-Site: same-origin`; opaque, cross-site, and same-site-but-not-
same-origin requests are rejected. Non-browser bearer clients without browser headers
remain supported. There is no CORS grant or cookie-based write path.

Every submission generates an idempotency key and locks double submission. A lost response
or server failure retains the exact request and key for retry, including across modal close/
reopen; fields unlock only after a definitive rejection. Intake errors appear inline and
identify invalid fields. Success inserts the acknowledged queued row without remounting,
changing URL filters, resetting pagination or moving scroll; normal polling reconciles it.
Native dialog focus containment, Escape/Close, named controls, visible focus and 44px touch
targets support keyboard, screen-reader and narrow mobile use. Closing returns focus to `+`.

### Row anatomy

Open rows, from left to right, in aligned columns:

- a status glyph, with its status text for screen readers; a red edge marks rows that need the owner;
- the short title, derived on the server from the first sentence of the goal, followed by flag chips;
- the repository;
- the stage;
- time in stage;
- the primary PR number, or a report link;
- the writer: busy, idle, `unconfirmed` while its launch is unconfirmed, stopped, error, or `no writer`;
- the reason: the wait reason, the highest-priority flag, a scheduled retry, or the status.

Closed rows show the outcome (`Merged`, `ready for check-in`, `draft published`, `report stored`, `failed` or `cancelled`) instead of the stage, `closed 7m ago`, the PR or report and `took 17m` from intake to close (also on mobile). A completion target appears only for a shortfall, e.g. `wanted: merged`; it comes from the execution's actual `state.target`, not the original request. A ready completion whose reconciler-observed `pr.state` is `merged` displays `Merged` and meets a merged target, without changing persisted state. The full target remains in details. One JS display-label mapping renders `checked_in` as `Merged` everywhere, including history stage text; API values, query parameters, identifiers and stored data stay unchanged. All relative ages say `ago`; absolute local and UTC timestamps remain in tooltips and screen-reader labels.

Later: the label, the revision round if more than 1, the PR state (draft, ready or merged) with a CI dot, and the size of the change.

Below 760px wide, each row wraps to two or three lines: the title and age, then repository, stage, PR and writer, then the reason.

### Item detail

Clicking a title, or pressing Enter on it, expands the detail in place under its row, so the list keeps its position. Only one row is open at a time, and a refresh keeps it open.

Built now:

- **Request:** the full goal.
- **Facts:** status or outcome, stage and time in it, target, version, age, last progress, wait reason and since when, resume trigger, retry time, attempt, writer and its session, PR or report, and IDs. Native session references come from writer bindings, not separate historical attempt-worker records.
- **Flags** on this item, with their recovery and age.
- **Question** with its options, proposed default and the exact `reply` command.
- **Prerequisites** with their milestone and reason.
- **Stage path:** the stages on the loaded history page, each with its duration. Returns to an earlier stage are marked ↺ and counted.
- **History:** a paginated table of events with their from and to stages, status and reason. Backward transitions are highlighted.

The target detail also includes:
- **Header:** the title, repository, label, status or outcome, the completion target (ready or Merged), the primary PR and any duplicates, and the worker chat.
- **Lifecycle rail:** the stage sequence from [STATE_MACHINE.md](STATE_MACHINE.md) with the current stage highlighted. Returns to development or report drafting appear as counts; older timelines may retain retired stages.
- **Now:** the next action or resume trigger, the wait reason, retry time, and the registered writer and current fenced attempt (if any), not an expiring runner lease.
- **Evidence by revision:** the local checks, CI, review findings and approvals, each tied to the head SHA it covered.
- **Conversation:** each question with its options, the reply, and who replied.
- **Timeline and attempts:** paginated, with a worker link for each attempt.
- **Prerequisites and dependents.**
- **Output:** the PR and its merge commit, or the stored report rendered as Markdown.
- **Request:** the original goal and each steering version.

### Groups

An optional label on intake, such as `PH-14a` or `PH-V1`, groups related items. The group page shows every item with that label and rolls up counts by outcome, the size of the changes, and the time span. Campaigns ([SPEC.md](SPEC.md)) can reuse this later.

### Metrics (D3)

Computed on the server from work history and shown on History and group pages:

- items delivered per day;
- cycle time, median and 90th percentile, by stage;
- revision rounds;
- failure and cancellation rates;
- time in queue.

## Hosting and security

The hosted sign-in, workspace roles, cookie writes, hashed static assets and rate limits
below are **target design**, not shipped local authentication. Current local-board reads
and owner-authenticated intake are described explicitly here and in [work items](docs/work-items.md#operator-work-board).

- **Origin:** the hosted API origin serves the dashboard ([PH-20](IMPLEMENTATION_PLAN.md)), so it needs no CORS and no separate API base URL.
- **Sign-in:** Google sign-in from PH-27. The session cookie is HttpOnly, Secure and SameSite=Lax. Sessions expire, and sign-out deletes the session on the server.
- **Local board:** a directly accessed server started with `PHALANX_LOCAL_BOARD=1` serves board reads and narrow `/operator/api/` actions without a token to verified loopback peers with local Host. Creation and canonical settings writes additionally require exact same-origin browser checks; forwarding headers cannot grant local authority. `/v1` writes keep their existing bearer roles. A hosted server, proxy or tunnel leaves the flag unset. See the [local-dashboard amendment](docs/decisions.md).
- **Workspace scope:** every dashboard request is scoped to the signed-in user's selected workspace and checked against their role on the server. The browser only hides controls for convenience; it never enforces access.
- **Write actions (D2):** the cookie authorizes owner write routes only with CSRF protection, using an Origin check plus a required custom header. Destructive actions ask for confirmation. Each action records the signed-in email in the item timeline, the same way reply authors are recorded today. Runner routes never accept the cookie.
- **Caching:** static assets have hashed names and cache for a long time. Conditional board reads use `private, no-cache`; other API responses are `no-store`.
- **Rate limits:** sign-in and write routes are rate-limited.

## Performance and data

- The server computes the summary rows, and the client renders pages. Lists use the existing keyset cursors with a limit of at most 100.
- A small overview endpoint returns the section counts and the top-bar health. That way the home page doesn't need to count rows on the client.
- Polling runs every 15 seconds with a change token, so an unchanged board returns `304`. It pauses while the tab is hidden. Server-sent events can come later if polling proves too slow.
- Filters and search use bounded, indexed queries on the server.
- The server learns PR and CI facts only from reconciler GitHub observations ([AGENTS.md](AGENTS.md): GitHub access stays local). If the reconciler hasn't observed a fact, the dashboard shows it as unknown and doesn't guess.

## API additions

The `/v1/operator` routes are read-only; the local `+` form uses a narrowly authorized
`/operator/api` adapter to existing intake, and later D2 actions will also reuse existing logic.
The D1 server reads
(PH-29a) target the local board at `/operator/` only, with existing authentication unchanged:
no hosting, sign-in or new authentication. [API reference](docs/api.md#operator-board-data)
lists the routes. Exactly two persisted dispatch limits are enforced atomically:
`max_building_writers` (Building) and `max_writers` (Total).
The **Writer concurrency** disclosure shows Building used/limit, Delivering count and Total
used/limit from registered allocations. Both controls save as one atomic pair, with
Building <= Total (initially 5/10 for new installs; existing installations activate separately).
Trusted local boards save through `/operator/api/dispatch/settings` without
another token gate. Nonlocal boards reuse the owner connection. This adapter uses the same
canonical settings implementation and trusted-local helper as creation, not another setting
or authentication mechanism. Saves preserve pause/aging settings and survive reload/restart.
Lowering below usage preserves existing sessions and blocks affected admissions. Building-full
and Total-full waits are distinct; spare Total never implies another build can start.
Eligible waiting PR fixes precede new builds; Delivering includes registered rework waiters
that have not yet acquired Building. Refresh capacity
reads current server values; charter settings cannot undo dashboard edits.

- An optional `label` on work items, set at intake (`phalanx intake --label`) and by steering. Steering `label: null` clears it. Labels match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
- New summary fields:
  - `label`;
  - `revision_round`: 1, plus one each time the execution re-enters development or report drafting (and PR revision in retained historical events);
  - `duplicate_count`: other open PRs on the item branch in the latest reconciler observation, or `null` when not observed;
  - `pr`: the PR in the latest reconciler GitHub observation, with `url`, `number`, `state` (`open`, `closed` or `merged`), `draft`, `head_sha`, `head_ci`, `additions` and `deletions`. `head_ci` is the gate result observed on that head. Each field is `null` when not observed; `pr` is `null` before an observation or when the latest observation found no PR. Writer-supplied PR URLs and lifecycle evidence never populate these facts.
  - `pickup`: the shared server decision for an unbound queued item, including eligibility and a concrete reason for exclusion. Normal authorized intake in a charter repository is eligible without naming its ID in charter notes. Scope/readiness snapshots, affinity, persisted dependencies, reasoned holds, retry, pause and canonical Building/Total capacity supply the decision; uninitialized and stale observations never imply readiness.
  - `next_action`: set on every open row without a `wait_reason`, so each open row has one or the other. An eligible queued item calls for Captain dispatch; an excluded item shows the same specific pickup reason as `shift status`. Eligible unlaunched items use the existing 30-minute **Dispatch overdue** flag; paused/capacity-bound work uses **Queue backlogged**, without another budget.

    Already shipped: a short one-line `title`, the full `goal`, `target` and the execution's `created_at`.
- `GET /v1/operator/overview`: counts for needs you (waiting for input), running, waiting on checks, queued and blocked by prerequisites; outcomes closed in the last 24 hours; dispatch `paused` (false means on); the oldest eligible queued timestamp and age; and `generated_at` for data age.
- `GET /v1/operator/items/{item_id}`: the current execution summary in `item`, or 404.
  Uses the same local-board/owner/runner read authentication and conditional ETags.
  The existing execution timeline also includes nullable `previous_execution_id` and
  `next_execution_id` for navigating reruns without reading the full execution list.
- Draft-completed executions use `draft_published` consistently in summary rows, the outcome filter, overview and label-group rollups. They count as delivered in the overview, not as ready for check-in.
- Filters on `GET /v1/operator/executions`: `repository`, `label`, `status`, `stage`, `outcome`, `q` and `closed_after`. `outcome` and `closed_after` apply to `kind=closed`, and `status` must match the kind; otherwise the server returns 400. `q` matches goal text, an item or execution ID, or an observed PR URL or number. Filtered cursors are bound to their filters. A filtered page inspects at most 2,000 indexed candidate executions, so it can return fewer rows than requested (even zero) with a cursor to continue.
- `GET /v1/operator/groups/{label}`: a bounded page of current executions with that label and a rollup **of that page only** (`scope: "page"`; items, open, outcomes, summed size with a count of PRs whose size is unknown, first created, oldest open and last closed). Follow `next_cursor` for subsequent pages; no request aggregates the whole label. Returns 404 when no item has the label.
- Execution lists, overview and groups return a weak `ETag` and accept `If-None-Match`. An unchanged poll returns `304` before reading rows or rollups. A durable change token and indexed retry/queue/unconfirmed-launch/24-hour-closure boundaries invalidate the tag. Ticking display ages alone do not: retain the last snapshot and calculate elapsed age from its timestamps and `generated_at`. Authentication is checked before conditional reads.
- Reports render as sanitized Markdown on the local board, with raw HTML stripped and no
  browser dependency or change to the plain-text report API.

## Phases

| Phase | Scope | Depends on |
| --- | --- | --- |
| **D1 Local board** | Routes and deep links; the home layout; row anatomy; item detail with the lifecycle rail; labels and groups; filters and search; rendered reports; the overview endpoint; freshness and stale states; mobile layout; accessibility. Hosting and sign-in are separate. | |
| **D2 Act from the dashboard** | Reply, cancel, retry, rerun, pause dispatch and change capacity, with CSRF protection and recorded actors. | D1 |
| **D3 Insight** | Metrics; repository health; a review feed of Captain decisions; notification links into the dashboard. | D1 |

**Acceptance for D1:**

- Seed a store with Phalanx's own 111 PRs as historical items, plus a synthetic fixture with 10,000 open and 1,000,000 closed executions.
- The home page renders in under a second.
- Every row has a reason.
- Every link resolves.
- The repository owner or their designated reviewer reviews screenshots at phone and laptop sizes.

## Design questions

| Question | Options | Starting proposal |
| --- | --- | --- |
| Should the dashboard take write actions? | Read-only forever, or owner actions with CSRF protection. | Add owner actions in D2, after hosting and sign-in are proven. |
| Where is it hosted, and on what domain? | Decided together with PH-20. | Same origin as the hosted API. |
| Who else can see it? | Only the owner, or other users. | **Decided:** many users in workspaces with owner, member and viewer roles ([SPEC.md](SPEC.md#users-and-workspaces)). |
| How do notifications reach a user? | Email, push or chat (see [SPEC.md](SPEC.md#design-questions)). | One channel per user, whose messages deep-link to the item page. |
| Should Captain decisions become data? | Keep them in `docs/decisions.md`, or record them as reviewable records through the API. | Keep them in the doc for now, and revisit in D3. |
| How does the dashboard know default-branch CI health? | The runner observes it during polls it already makes, or the server calls GitHub. | The runner observes it, which keeps GitHub access on the runner. |
