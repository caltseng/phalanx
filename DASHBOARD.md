# Phalanx dashboard

Status: **proposal for Caleb's review.** Until it's accepted, the [Operator view](SPEC.md#operator-view) in SPEC.md remains the v1 contract. This document sets the goals, the UI principles and the target experience for the hosted dashboard. Implementation rows go into [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) after review.

## Goal

The dashboard is a website, reachable from the internet, where Caleb can see what Phalanx is doing across every repository, what needs him, and what it delivered. He can use it from a phone or a laptop, and later he can act on it there too. It must stay fast and readable with thousands of items, tens of workers running at once, and years of history.

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
| Burst intake | #4 to #10 opened within 7 minutes. | A running list with each item's stage and time in stage, plus slots used out of total. Rows don't jump around when new work arrives. |
| One goal, many PRs | PH-13 shipped as #15, #22, #31, #40 and #61. PH-14a and PH-15a were each split into numbered parts. | Grouping by label, such as a plan row or campaign, with a rollup: delivered, open, and age of the oldest open item. |
| Small bookkeeping next to features | About 25 plan and decision PRs, such as #59, #84, #99, #103, #106 and #109. | Filters and grouping so one-line docs PRs don't bury feature work. |
| Long-running outliers | #80, #81 and #82 were each open 8 to 11 hours. #43 took 2.6 hours and changed 2,000 lines. | Age that's visible and sortable. Work that is stale past a threshold rises into **Needs you**. |
| Review fix rounds | #105 (the 503 mapping) and #108 (the unbounded writer wait) each needed one fix round. | The revision round count and the latest findings, on the row and in the detail view. |
| Duplicates | #104 duplicated #103. | The primary PR with a "1 duplicate" chip that links to the duplicate. |
| Stacks and prerequisites | #111 stacked on #110, and PH-19b had to finish before PH-V1. | "Blocked by" chips on the row. The detail view lists upstream and downstream items. |
| Breakage found after delivery | Main went red after PH-19a (#105), which led to #107 and #108. | A health strip with default-branch CI for each repository. Later, links from a fix to the item that caused the breakage. |
| Questions for Caleb | Confirming decision 32, and the Google OAuth client. | **Needs you** at the top: the question, the options, the proposed default and how long it has waited. |
| Decisions made on his behalf | 38 of the 46 decisions were made by the Captain. | Later, a review feed of Captain decisions waiting for Caleb's confirmation. |
| Field reports | Scheduled reflections, and the PH-V1 acceptance report. | Reports rendered as Markdown on the page, not raw text. |
| Interrupted work | PH-V1 gate 4 stopped the server mid-stage and held workers past lease expiry. | A disconnected or stale banner while the server is down. After a restart, recovering items stay visible and show "recovering". History stays complete. |
| Several repositories | phalanx, phalanx-pilot-py and phalanx-pilot-web. | A repository column and filter, with counts for each repository. |

This is roughly how the home page would have looked at the peak. The stages and times are illustrative.

```text
PHALANX          ● Live · updated 4s ago   Dispatch on · Runner 1/1 · Slots 4/8 · Oldest queued 9m
──────────────────────────────────────────────────────────────────────────────────────────────
NEEDS YOU (0)    Nothing needs you.
──────────────────────────────────────────────────────────────────────────────────────────────
NOW (10)                                                   Group: label ▾   Repo: all ▾   🔍
 Running (4)
  PH-15  Add direct GitHub check-in coordinator    development         4m   worker ↗
  PH-12  Reconcile reservations after restart      self_review         2m   worker ↗
  PH-14  Promote the verified primary PR           pr_promotion        1m   #30 draft ↗
  PH-08  Persist waits, replies, rechecks          pr_revision  r2     6m   #24 ✕ CI ↗
 Waiting on checks (4)
  PH-14  Collect GitHub validation checks          pr_validation       3m   #26 ● CI, recheck 1m
  PH-11  Connect Captain to runner claims          pr_reconciliation  12m   #18 ● CI, recheck 30s
  PH-18  Connect board to authenticated reads      check_in            1m   #28 merging
  PH-07  Enforce prerequisites at claim            adversarial_review  2m   #20 review
 Queued (2)
  PH-13  Run draft publication under the claim     queued              1m   next to run
  PH-15  Record PR review and check-in evidence    blocked             9m   blocked by PH-15 #29
──────────────────────────────────────────────────────────────────────────────────────────────
DELIVERED (last 24h: 25)                                                             History →
  ✓ checked in   PH-19  Exercise queue fairness under a stalled backlog    #25 +99/-0   27s
  ✓ checked in   PH-17  Add bounded operator queue and timeline reads      #21 +468/-1  9m
  ✓ checked in   PH-13  Journal and reconcile runner draft publication     #22 +657/-19 4m
  …
```

## Who uses it

- **v1 hosted:** the owner only. Caleb signs in with Google ([PH-27](IMPLEMENTATION_PLAN.md)) against an email allowlist. The owner token still works as a fallback, and for the CLI and API.
- **Later:** read-only viewers, such as collaborators. This is a [design question](#design-questions).
- **Devices:** phone and laptop are equally important. The phone matters most for **Needs you** and replies, and the laptop for **Now** and **History**.

## Goals

1. **Attention first.** Within five seconds of opening the dashboard, Caleb knows whether anything needs him and what it is.
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
- Multi-tenancy, or more than one owner per server.
- An analytics warehouse. Phase D3 computes a few metrics from work history.
- Real-time streaming in D1. Polling is enough.

## UI principles

1. **Attention, then motion, then history.** Order every page this way: what needs Caleb, then what is moving, then what is done.
2. **No row without a reason.** Every open row answers "what happens next, and why". A blank wait reason is a bug. This applies the "never fail silently" rule in [AGENTS.md](AGENTS.md).
3. **Recorded lifecycle, not percent complete.** Show the stages Phalanx actually recorded and the loops it went through, such as `pr_revision ×2`, as [SPEC.md](SPEC.md#operator-view) requires.
4. **Calm by default.** Keep color for states that need action: needs you, failed, stale and disconnected. Everything else stays neutral. Never use color alone; pair it with an icon and text.
5. **Stable under refresh.** A refresh never reorders rows under the pointer, moves focus, or resets scroll. New or changed rows are highlighted briefly.
6. **Dense and scannable.** On a laptop, each item takes one line, and about 40 rows fit on screen. The detail view holds the rest.
7. **Link out; don't duplicate.** Diffs and CI logs live on GitHub, and conversation lives in the worker chat. The dashboard shows a summary and a link.
8. **Everything has a URL.** Items, executions, reports, groups and filtered views all have shareable URLs, and a notification link lands on its item.
9. **Times are honest.** Show relative times, with the absolute local time and UTC on hover. Always show the age of the data.
10. **Neutral wording.** Say "worker chat", "runner" and "Captain", never the name of a specific agent runtime or operating system. This follows the runtime and platform neutrality rules in [AGENTS.md](AGENTS.md).
11. **Accessible.** Full keyboard navigation, visible focus, WCAG 2.2 AA contrast, screen-reader labels, and respect for reduced motion.
12. **Recognizably Phalanx.** Keep the phalanx visual identity from #67 as a restrained accent. Readability comes first.
13. **Nothing secret or third-party in the browser.** No tokens in storage, and no third-party scripts, fonts or analytics. Keep the strict CSP.

## Pages

Every page reads state through the API. Filters live in the URL: repository, label, status, stage, outcome, time range and a text query. A global search accepts an item ID, an execution ID, a PR number or URL, or text from the title.

| Route | Page |
| --- | --- |
| `/operator/` | Home: health strip, **Needs you**, **Now**, **Delivered**. |
| `/operator/items/{item_id}` | Item detail, showing the current execution, with earlier executions one click away. |
| `/operator/executions/{execution_id}` | A specific execution, including closed ones and reruns. |
| `/operator/queue` | The full open backlog, paginated and filterable. |
| `/operator/history` | Closed executions, paginated and filterable, with counts for the filter. |
| `/operator/groups/{label}` | Every item with that label, with a rollup. |
| `/operator/runners` | Runners, their capacity, heartbeats and active attempts. |

### Home

1. **Health strip:**
   - server status and data age;
   - dispatch on or paused;
   - runners online, and slots in use out of total;
   - the age of the oldest queued item;
   - later, default-branch CI for each repository.
   
   A disconnected or stale state turns the whole strip into a banner.
2. **Needs you.** Each card shows the item, what it needs, how long it has waited, and the way to respond. In D1, that is the exact `reply` command. In D2, it is a reply form with the offered options and the proposed default. A card appears for:
   - questions (`waiting_for_input`);
   - owner waits;
   - PRs that are ready but need a human merge;
   - duplicate PRs to review;
   - items stale past their threshold.
3. **Now.** Sections for running, waiting on external checks, queued, and blocked by prerequisites. Each section can collapse and shows its count. Rows can be grouped by nothing, by repository, or by label.
4. **Delivered.** The last 24 hours, newest first. Each row shows the outcome and links to all executions in History.

### Row anatomy

From left to right:

- the label;
- the title;
- the repository;
- the stage;
- the revision round, if more than 1;
- time in stage;
- the wait reason or next action;
- the PR number with its state (draft, ready or merged) and a CI dot;
- the worker chat link.

Closed rows show the outcome instead of the stage:

- `checked in`, `ready for check-in`, `report stored`, `failed` or `cancelled`;
- the size of the change;
- total duration.

### Item detail

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
- **Write actions (D2):** the cookie authorizes owner write routes only with CSRF protection, using an Origin check plus a required custom header. Destructive actions ask for confirmation. Each action records the signed-in email in the item timeline, the same way reply authors are recorded today. Runner routes never accept the cookie.
- **Caching:** static assets have hashed names and cache for a long time. API responses are `no-store`.
- **Rate limits:** sign-in and write routes are rate-limited.

## Performance and data

- The server computes the summary rows, and the client renders pages. Lists use the existing keyset cursors with a limit of at most 100.
- A small overview endpoint returns the section counts and the health strip. That way the home page doesn't need to count rows on the client.
- Polling runs every 15 seconds with a change token, so an unchanged board returns `304`. It pauses while the tab is hidden. Server-sent events can come later if polling proves too slow.
- Filters and search use bounded, indexed queries on the server.
- The server learns PR and CI facts only from runner observations ([AGENTS.md](AGENTS.md): GitHub access stays on the runner). If the runner hasn't observed a fact, the dashboard shows it as unknown and doesn't guess.

## API additions (sketch)

Each phase row specifies these in detail. All are read-only except the D2 actions, which reuse existing routes.

- An optional `label` on work items, set at intake and by steering.
- New summary fields:
  - `label`;
  - a `pr` object with number, title, state, draft, head CI, additions and deletions;
  - `revision_round`;
  - `duplicate_count`;
  - `target`.
- `GET /v1/operator/overview`: counts for each section, dispatch and runner health, and data age.
- Filters on `GET /v1/operator/executions`: `repository`, `label`, `status`, `stage`, `outcome`, `q` and `closed_after`.
- `GET /v1/operator/runners` and `GET /v1/operator/groups/{label}`.
- Reports rendered as sanitized Markdown, with raw HTML stripped.

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
| Who else can see it? | Only the owner, or allowlisted read-only viewers. | Only the owner, until a collaborator needs it. |
| How do notifications reach Caleb? | Email, push or chat (see [SPEC.md](SPEC.md#design-questions)). | One channel, whose messages deep-link to the item page. |
| Should Captain decisions become data? | Keep them in `docs/decisions.md`, or record them as reviewable records through the API. | Keep them in the doc for now, and revisit in D3. |
| How does the dashboard know default-branch CI health? | The runner observes it during polls it already makes, or the server calls GitHub. | The runner observes it, which keeps GitHub access on the runner. |
