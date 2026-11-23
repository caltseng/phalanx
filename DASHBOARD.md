# Phalanx dashboard (Scribe)

Status: **accepted by Caleb** (#112). Implementation rows go into [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). The [Operator view](SPEC.md#operator-view) in SPEC.md remains the v1 contract. This document sets the goals, the UI principles and the target experience for the hosted dashboard.

The dashboard's visible name is **Scribe**. Its URL (`/operator/`), its API routes (`/v1/operator/*`) and its code keep the `operator` name. The local board at `/operator/` implements the home page, row anatomy and inline detail described below. It follows the direct-sessions model ([docs/direct-sessions.md](docs/direct-sessions.md)): items and executions, writers, Captains and flags. It doesn't design around slots or the relay.

## Goal

The dashboard is a website, reachable from the internet, where each Phalanx user can see what Phalanx is doing across their workspace's repositories, what needs them, and what it delivered. They can use it from a phone or a laptop, and later act on it there too. It serves many users across workspaces ([SPEC.md](SPEC.md#users-and-workspaces)), with Caleb as the first. It must stay fast and readable with thousands of items, tens of workers running at once, and years of history.

## Worked example: Phalanx built by Phalanx

Phalanx's own build is the reference workload. It produced 111 PRs in 35 hours, from 21 Nov 01:15 to 22 Nov 12:32 UTC:

- **Outcomes:** 110 merged, and one closed as a duplicate (#104 duplicated #103).
- **Size:** about 28,800 lines changed.
- **Time from open to merge:** median 8 minutes, 90th percentile 67 minutes. The longest was #81, open for 11.4 hours.
- **Peak concurrency:** 8 open PRs at once, at 21 Nov 02:13 (#18, #20, #24, #26, #27, #28, #29 and #30).
- **Around the PRs:**
  - live musters on two pilot repositories;
  - scheduled reflections;
  - 46 logged decisions, 38 of them made by the Captain on Caleb's behalf.

If every one of those had been a Phalanx work item, the dashboard would have needed to show these situations:

| Situation | Example from the build | What the dashboard must show |
| --- | --- | --- |
| Burst intake | #4 to #10 opened within 7 minutes. | A running list with each item's stage, time in stage and writer. Rows don't jump around when new work arrives. |
| One goal, many PRs | PH-13 shipped as #15, #22, #31, #40 and #61. PH-14a and PH-15a were each split into numbered parts. | Grouping by label, such as a plan row or campaign, with a rollup: delivered, open, and age of the oldest open item. |
| Small bookkeeping next to features | About 25 plan and decision PRs, such as #59, #84, #99, #103, #106 and #109. | Filters and grouping so one-line docs PRs don't bury feature work. |
| Long-running outliers | #80, #81 and #82 were each open 8 to 11 hours. #43 took 2.6 hours and changed 2,000 lines. | Age that's visible and sortable. Work that is stale past a threshold rises into **Needs you**. |
| Review fix rounds | #105 (the 503 mapping) and #108 (the unbounded writer wait) each needed one fix round. | The revision round count and the latest findings, on the row and in the detail view. |
| Duplicates | #104 duplicated #103. | The primary PR with a "1 duplicate" chip that links to the duplicate. |
| Stacks and prerequisites | #111 stacked on #110, and PH-19b had to finish before PH-V1. | "Blocked by" chips on the row. The detail view lists upstream and downstream items. |
| Breakage found after delivery | Main went red after PH-19a (#105), which led to #107 and #108. | The top bar shows default-branch CI for each repository. Later, links from a fix to the item that caused the breakage. |
| Questions for Caleb | Confirming decision 32, and the Google OAuth client. | **Needs you** at the top: the question, the options, the proposed default and how long it has waited. |
| Decisions made on his behalf | 38 of the 46 decisions were made by the Captain. | Later, a review feed of Captain decisions waiting for Caleb's confirmation. |
| Field reports | Scheduled reflections, and the PH-V1 acceptance report. | Reports rendered as Markdown on the page, not raw text. |
| Interrupted work | PH-V1 gate 4 stopped the server mid-stage and held workers past lease expiry. | A disconnected or stale banner while the server is down. After a restart, recovering items stay visible and show "recovering". History stays complete. |
| Several repositories | phalanx, phalanx-pilot-py and phalanx-pilot-web. | A repository column and filter, with counts for each repository. |

This is roughly how the home page would have looked at the peak. The stages and times are illustrative. Each line is one row; `!`, `▶`, `◷`, `○`, `✓` and `✕` always come with their status text for screen readers.

```text
Ⓐ PHALANX Scribe  ● Live updated 4s ago   1 needs you  4 active  6 queued  2 flags  1/1 Captains live   ◐ Auto  Refresh
NEEDS YOU 1
 ! Confirm decision 32: OAuth client          phalanx  development     41m  —    idle         Which OAuth client should sign-in use?
FLAGS 2
 ◷ ▸ head drift ×2                            Head moved after validation; revalidate                                         6m
CAPTAINS 1/1
 ▶ Captain 52940744 live        ♥ 30s  wakes 8  charter e04f548  focus: PH-15, PH-12, PH-14
ACTIVE 4
 ▶ PH-15 Add direct GitHub check-in coordin…  phalanx  development      4m  —    busy         running
 ▶ PH-12 Reconcile reservations after resta…  phalanx  self review      2m  —    busy         running
 ▶ PH-14 Promote the verified primary PR      phalanx  pr promotion     1m  #30  unconfirmed  running
 ▶ PH-08 Persist waits, replies, rechecks     phalanx  pr revision      6m  #24  busy         ↺ CI failed on head 7caa5c2
QUEUED AND WAITING 6
 ◷ PH-14 Collect GitHub validation checks     phalanx  pr validation    3m  #26  no writer    Waiting for CI; recheck in 1m
 ◷ PH-15 Record PR review and check-in evid…  phalanx  development      9m  —    no writer    Waiting for 1 prerequisite
 …
RECENTLY CLOSED 25+
 ✓ PH-19 Exercise queue fairness under a st…  phalanx  checked in      27s  #25  52m   target merged
 ✓ PH-17 Add bounded operator queue and tim…  phalanx  checked in       9m  #21  1h 4m target merged
 …
```

## Who uses it

- **v1 hosted:** a single workspace, with Caleb as its owner. He signs in with Google ([PH-27](IMPLEMENTATION_PLAN.md)) against an email allowlist. The owner token still works as a fallback, and for the CLI and API.
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
3. **Recorded lifecycle, not percent complete.** Show the stages Phalanx actually recorded and the loops it went through, such as `pr_revision ×2`, as [SPEC.md](SPEC.md#operator-view) requires.
4. **Calm by default.** Keep color for states that need action: needs you, failed, stale and disconnected. Everything else stays neutral. Never use color alone; pair it with an icon and text.
5. **Stable under refresh.** A refresh never reorders rows under the pointer, moves focus, or resets scroll. New or changed rows are highlighted briefly.
6. **Dense and scannable.** On a laptop, each item takes one line, and about 40 rows fit on screen. The detail view holds the rest.
7. **Link out; don't duplicate.** Diffs and CI logs live on GitHub, and conversation lives in the worker chat. The dashboard shows a summary and a link.
8. **Everything has a URL.** Items, executions, reports, groups and filtered views all have shareable URLs, and a notification link lands on its item.
9. **Times are honest.** Show relative times, with the absolute local time and UTC on hover. Always show the age of the data.
10. **Neutral wording.** Say "worker chat", "runner" and "Captain", never the name of a specific agent runtime or operating system. This follows the runtime and platform neutrality rules in [AGENTS.md](AGENTS.md).
11. **Accessible.** Full keyboard navigation, visible focus, WCAG 2.2 AA contrast, screen-reader labels, and respect for reduced motion. On the board, ↑/↓ or j/k move between rows, Enter expands a row and Escape collapses it.
12. **Recognizably Phalanx.** Keep the phalanx visual identity from #67 as a restrained accent: the crimson and gold mark and wordmark. Readability comes first. Light and dark themes follow the system, and a header toggle overrides it (auto, dark, light) in local storage. Motion is small and thematic and never moves row text: the shield is raised on load and the wordmark glints, the Live dot and attention counts pulse like beacons, running rows march in step, waiting rows tick like a sundial, a live Captain's heart beats, changed rows get a stroke of gold ink and a seal, details unroll, and a theme change spills like ink. Looping motion stays in phase across refreshes, and none of it runs under reduced motion.
13. **Nothing secret or third-party in the browser.** No tokens in storage, and no third-party scripts, fonts or analytics. Keep the strict CSP.

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
| `/operator/runners` | Old runner path: runners, heartbeats and active attempts. Drop it with direct sessions step 5. |

### Home

Every section is a list of one-line rows. A section shows its count in its heading, and an empty section says so in one line.

1. **Top bar** (sticky): the Scribe mark, data health (Live, Partial, Stale or Disconnected, with the age of the oldest displayed read), counts for each section, the theme toggle and Refresh. A failed or stale read turns the health pill amber or red and keeps the last loaded rows on screen. Later: dispatch on or paused, the age of the oldest queued item, and default-branch CI for each repository.
2. **Needs you.** Items waiting for input, failed items, and items with a flag that notifies the owner, plus owner-notify flags that name no visible item. Expanding a question row shows the prompt, the options, the proposed default and the exact `reply` command. D2 replaces the command with a reply form. Later, it also takes PRs that need a human merge and duplicate PRs to review.
3. **Flags.** The remaining derived flags from `GET /v1/flags`, grouped by kind. Each group shows its count, its reason (or the number of distinct reasons) and the age of its oldest flag, and expands to one line per flag with its recovery. Each item row also carries its own flags as chips.
4. **Captains.** One line per Captain: live or stale, heartbeat age, wake count, charter revision, and its handoff note or focus.
5. **Active.** Items that are running or have a writer.
6. **Queued and waiting.** Everything else that is open: queued stages, external waits and prerequisite waits, with the open-work pager.
7. **Recently closed.** Newest first, with the closed-work pager. Each row shows the outcome and links to its PR or report.

### Row anatomy

Open rows, from left to right, in aligned columns:

- a status glyph, with its status text for screen readers; a red edge marks rows that need the owner;
- the short title, derived on the server from the first sentence of the goal, followed by flag chips;
- the repository;
- the stage;
- time in stage;
- the primary PR number, or a report link;
- the writer: busy, idle, `unconfirmed` while its launch is unconfirmed, stopped, error, or `no writer`;
- the reason: the wait reason, the highest-priority flag, a scheduled retry or recheck, or the status.

Closed rows show the outcome (`checked in`, `ready for check-in`, `report stored`, `failed` or `cancelled`) instead of the stage, the time since close, the PR or report, the total duration from intake to close, and the completion target.

Later: the label, the revision round if more than 1, the PR state (draft, ready or merged) with a CI dot, and the size of the change.

Below 760px wide, each row wraps to two or three lines: the title and age, then repository, stage, PR and writer, then the reason.

### Item detail

Clicking a title, or pressing Enter on it, expands the detail in place under its row, so the list keeps its position. Only one row is open at a time, and a refresh keeps it open.

Built now:

- **Request:** the full goal.
- **Facts:** status or outcome, stage and time in it, target, version, age, last progress, wait reason and since when, resume trigger, retry and recheck times, attempt, writer and its session, PR or report, and IDs. Old runner-path facts (worker reference, worker chat and slot note) are isolated in one function for removal in direct sessions step 5.
- **Flags** on this item, with their recovery and age.
- **Question** with its options, proposed default and the exact `reply` command.
- **Prerequisites** with their milestone and reason.
- **Stage path:** the stages on the loaded history page, each with its duration. Returns to an earlier stage are marked ↺ and counted.
- **History:** a paginated table of events with their from and to stages, status and reason. Backward transitions are highlighted.

The target detail also includes:
- **Header:** the title, repository, label, status or outcome, the completion target (ready or checked in), the primary PR and any duplicates, and the worker chat.
- **Lifecycle rail:** the stage sequence from [STATE_MACHINE.md](STATE_MACHINE.md) with the current stage highlighted. Loops back to `development` or `pr_revision` appear as counts.
- **Now:** the next action or resume trigger, the wait reason, retry and recheck times, and the runner and attempt holding the lease.
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

- **Origin:** the hosted API origin serves the dashboard ([PH-20](IMPLEMENTATION_PLAN.md)), so it needs no CORS and no separate API base URL.
- **Sign-in:** Google sign-in from PH-27. The session cookie is HttpOnly, Secure and SameSite=Lax. Sessions expire, and sign-out deletes the session on the server.
- **Local board:** until PH-27, a local server started with `PHALANX_LOCAL_BOARD=1` serves the read-only board routes without a token to loopback clients with a loopback `Host` ([decision 57](docs/decisions.md)). A hosted server leaves the flag unset.
- **Workspace scope:** every dashboard request is scoped to the signed-in user's selected workspace and checked against their role on the server. The browser only hides controls for convenience; it never enforces access.
- **Write actions (D2):** the cookie authorizes owner write routes only with CSRF protection, using an Origin check plus a required custom header. Destructive actions ask for confirmation. Each action records the signed-in email in the item timeline, the same way reply authors are recorded today. Runner routes never accept the cookie.
- **Caching:** static assets have hashed names and cache for a long time. API responses are `no-store`.
- **Rate limits:** sign-in and write routes are rate-limited.

## Performance and data

- The server computes the summary rows, and the client renders pages. Lists use the existing keyset cursors with a limit of at most 100.
- A small overview endpoint returns the section counts and the top-bar health. That way the home page doesn't need to count rows on the client.
- Polling runs every 15 seconds with a change token, so an unchanged board returns `304`. It pauses while the tab is hidden. Server-sent events can come later if polling proves too slow.
- Filters and search use bounded, indexed queries on the server.
- The server learns PR and CI facts only from runner observations ([AGENTS.md](AGENTS.md): GitHub access stays on the runner). If the runner hasn't observed a fact, the dashboard shows it as unknown and doesn't guess.

## API additions

All are read-only except the D2 actions, which reuse existing routes. The D1 server reads (PH-29a) are implemented as follows; [README.md](README.md#read-the-operator-board-data) lists the routes.

- An optional `label` on work items, set at intake (`phalanx intake --label`) and by steering. Steering `label: null` clears it. Labels match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
- New summary fields:
  - `label`;
  - `revision_round`: 1, plus one each time the execution re-enters development, PR revision or report drafting;
  - `duplicate_count`: duplicate PRs found at the latest publication;
  - `pr`: the latest PR the runner observed, with `url`, `number`, `state` (`open`, `closed` or `merged`), `draft`, `head_sha`, `head_ci`, `additions` and `deletions`. `head_ci` is the last gate result for the current head. Size stays known only while the head is unchanged. Each field is `null` when not observed, and `pr` is `null` before the first observation. The PR title is not observed yet.
  - `next_action`: set on every open row without a `wait_reason`, so each open row has one or the other. Queued rows without another reason show, in order: dispatch paused, a scheduled retry, no runner online, waiting for a free slot, or next to run.

    Already shipped: a short one-line `title`, the full `goal`, `target` and the execution's `created_at`.
- `GET /v1/operator/overview`: counts for needs you (waiting for input), running, waiting on checks, queued and blocked by prerequisites; outcomes closed in the last 24 hours; dispatch paused; runners online and known; slots used and total; the oldest eligible queued age; and `generated_at` for data age. A runner is online when it polled or renewed within the lease window.
- Filters on `GET /v1/operator/executions`: `repository`, `label`, `status`, `stage`, `outcome`, `q` and `closed_after`. `outcome` and `closed_after` apply to `kind=closed`, and `status` must match the kind; otherwise the server returns 400. `q` matches goal text, an item or execution ID, or a PR URL or number. Filtered cursors are bound to their filters. A filtered closed page inspects at most 2,000 closures, so it can return fewer rows than requested with a cursor to continue.
- `GET /v1/operator/runners`: each runner's presence, offered slots, capacity and open attempts. Runner IDs appear only here.
- `GET /v1/operator/groups/{label}`: a rollup of the current executions with that label (items, open, outcomes, summed size with a count of PRs whose size is unknown, first created, oldest open and last closed) and an item page. Returns 404 when no item has the label.
- Reports rendered as sanitized Markdown, with raw HTML stripped (later in D1).

## Phases

| Phase | Scope | Depends on |
| --- | --- | --- |
| **D1 Hosted-ready board** | Routes and deep links; the home layout; row anatomy; item detail with the lifecycle rail; labels and groups; filters and search; rendered reports; the overview and runners endpoints; freshness and stale states; mobile layout; accessibility. | PH-20, PH-27 |
| **D2 Act from the dashboard** | Reply, cancel, retry, rerun, pause dispatch and change capacity, with CSRF protection and recorded actors. | D1 |
| **D3 Insight** | Metrics; repository health; a review feed of Captain decisions; notification links into the dashboard. | D1 |

**Acceptance for D1:**

- Seed a store with Phalanx's own 111 PRs as historical items, plus a synthetic fixture with 10,000 open and 1,000,000 closed executions.
- The home page renders in under a second.
- Every row has a reason.
- Every link resolves.
- Caleb reviews screenshots at phone and laptop sizes.

## Design questions

| Question | Options | Starting proposal |
| --- | --- | --- |
| Should the dashboard take write actions? | Read-only forever, or owner actions with CSRF protection. | Add owner actions in D2, after hosting and sign-in are proven. |
| Where is it hosted, and on what domain? | Decided together with PH-20. | Same origin as the hosted API. |
| Who else can see it? | Only the owner, or other users. | **Decided:** many users in workspaces with owner, member and viewer roles ([SPEC.md](SPEC.md#users-and-workspaces)). |
| How do notifications reach a user? | Email, push or chat (see [SPEC.md](SPEC.md#design-questions)). | One channel per user, whose messages deep-link to the item page. |
| Should Captain decisions become data? | Keep them in `docs/decisions.md`, or record them as reviewable records through the API. | Keep them in the doc for now, and revisit in D3. |
| How does the dashboard know default-branch CI health? | The runner observes it during polls it already makes, or the server calls GitHub. | The runner observes it, which keeps GitHub access on the runner. |
