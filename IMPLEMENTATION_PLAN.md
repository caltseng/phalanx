# Phalanx implementation work items

This is the open implementation and acceptance plan, not a release history.
[ROADMAP.md](ROADMAP.md) owns stage ordering; [SPEC.md](SPEC.md) and
[STATE_MACHINE.md](STATE_MACHINE.md) own product and lifecycle contracts.
Completed work, superseded mechanisms and acceptance limitations remain in
[decisions](docs/decisions.md), linked [pilot evidence](docs/pilots/direct-sessions.md)
and Git history. Do not restore retired mechanisms from those records.

The current path is the HTTP API/Scribe server, native Captain chats, registered
writers and an independent GitHub reconciler. Use [the implementation map](docs/architecture.md)
and [operating guide](docs/direct-path.md). Source delivery does not establish installed
package, retained-session or live-service adoption; follow [deployment authority and
verification](docs/deployment.md#standing-authority-for-compatible-upgrades).

## Remaining v1 integration work

Stage 2 remains open until PH-V1b and PH-V1c pass with `main` CI green
([decision 53](docs/decisions.md)). Local dashboard work and PH-26's measurement
report may run alongside it. Hosting, sign-in and multi-user implementation wait
for v1 polish. Fix findings that forced manual intervention, silently stalled work
or produced wrong state before rerunning; queue nonblocking improvements separately.

| Work item | Remaining scope and acceptance |
| --- | --- |
| **PH-V1b Run Phalanx on its own backlog** | Deliver the polish backlog and PH-26 measurement report as ordinary Phalanx items through the live direct muster, with Captains answering routine questions and the owner only watching. Each item must end in a verified PR or stored report without manual state edits. Turn incidents, stuck waits and required raw API calls into fix rows before roadmap stage 3. |
| **PH-V1m Verify the current-base rule** | Add the remaining direct reconciler regression: advancing `main` after the PR's recorded `base_sha` must not raise a conflict flag; an actual GitHub merge conflict must. Use the target's current tip and GitHub mergeability, not equality with the recorded PR base. Do not audit or resurrect retired runner PR stages. |
| **PH-V1c Run one clean batch** | After the current-base regression, the owner files about five real backlog items together, mixing PR and report targets and at least one worker question. Captains/direct writers deliver them with the owner only watching. Deliberately lose one writer launch before confirmation, race a second Captain on an already-bound item, replace a stopped writer with uncommitted changes preserved, and merge one PR each by merge commit, squash and rebase. Record every drill in `docs/pilots/direct-sessions.md`. Every drill must pass with no duplicate writer/PR, every item must end as a merged PR or stored report, and `main` CI must stay green. No manual state edits or raw API calls beyond the drills' deliberate steps. The accepted handoff/keeper evidence does not waive these remaining PH-DS5 cases; see [pilot acceptance](docs/decisions.md#direct-session-pilot-acceptance). |

Dependencies are verified milestones, not publication alone: check in prerequisite
PRs unless a dependent explicitly consumes a pinned reviewed revision. Independent
items may proceed in parallel; reconsider queued items after upstream integration
or steering. The Captain/plan session batches status and ordering updates after merges;
feature PRs do not edit this plan.

Each PR has one acceptance focus. Required CI and applicable independent exact-head
review must pass with no open actionable findings before check-in. Apply the
[canonical merge decision](phalanx/prompts/worker.md#merge-decision-publication-and-delivery),
including current target, draft ceiling, repository protections and actual-main
integration. After two unproductive review/optimization rounds, record the verified
scope and accountable follow-up under [the bounded-work policy](docs/writer-operations.md#bounded-performance-work);
never waive a safety/SLO/resource contract or claim a numeric miss passed.

### Acceptance repositories

Use the public scratch repositories below for remaining live drills, following their
own current instructions and protections. [Pilot validation policy](docs/pilots/validation-policies.md)
records their policy; fresh GitHub facts govern execution. Neither requires a preview.

| Repository | Stack and checks | Required checks / ruleset |
| --- | --- | --- |
| [caltseng/phalanx-pilot-py](https://github.com/caltseng/phalanx-pilot-py) | Python 3.12+, standard-library unittest; `python -m unittest discover -s tests -v` | `test (ubuntu-latest)`, `test (windows-latest)`; `27783095` |
| [caltseng/phalanx-pilot-web](https://github.com/caltseng/phalanx-pilot-web) | Node.js 20+ ESM, no dependencies; `npm ci` then `npm test` | `test (ubuntu-latest)`, `test (windows-latest)`; `27783096` |

Both `main` rulesets require checks non-strictly, forbid deletion and force-push,
and have no bypass actors. Verify registrations, local mappings and prepared checks
environments before a live muster; never substitute another checkout's environment.

## After the local release

The stage-2 exceptions below do not move hosting or multi-user work forward.
Remaining rows preserve their acceptance, with current direct clients replacing
retired relay/slot/journal terminology.

| Item | Dependency / ordering | Deliverable and acceptance |
| --- | --- | --- |
| **PH-26 Raise local write throughput** | Stage 2 measurement report; simple code optional, not a v1-polish gate | Measure against the original 100 ms claim/renewal p95 and 120 s drain of 10,000 due rechecks. Preserve the historical misses and approved timing exceptions in [decisions](docs/decisions.md#ordinary-performance-case-closeout-decision-94) and [the envelope report](docs/pilots/local-operating-envelope.md#findings). Try bounded batching before considering WAL; WAL requires its reset check. Rerun `tests/envelope_probe.py` before/after on matched workload/environment, retaining test IDs, run windows, faults, fairness, recovery and integrity evidence. Report remaining numeric misses and an accountable next action; merge is not proof a target passed. |
| **PH-29 Build the dashboard (D1), local first** | Local parts during stage 2; hosted origin/sign-in remain PH-20/27 | Remaining PH-29d acceptance report seeds Phalanx's own PR history and the large synthetic store, measures performance/accessibility, verifies links/reasons and obtains owner/designated-reviewer phone/laptop visual acceptance under [DASHBOARD.md](DASHBOARD.md#phases). Groups and remaining lifecycle/evidence detail are design scope, not accepted implementation by implication. The compact-header source contract is not proof of live adoption or overall D1 acceptance. Scope the board to workspaces when PH-28 lands. |
| **PH-20 Host the API and dashboard** | After stage 2 | Deploy the same API with HTTPS, persistent backend storage, verified backups via `phalanx server backup` and restore validation. Serve Scribe from the API origin; add separate-origin configuration/CORS only if required. Repeat the v1 muster with local direct clients pointed at the hosted URL. Keep trusted-local board mode off; test that hosted/proxied deployments reject tokenless `/operator/api/` writes. Clients remain off server storage. |
| **PH-27 Add Google sign-in for the dashboard** | After stage 2; paused | Optional client ID/secret and owner-email allowlist enable authorization-code OIDC with `state`, `nonce` and PKCE. Exchange server-side over TLS; check issuer, audience, expiry, nonce, verified email and allowlist without a new dependency. Store hashed expiring session IDs; cookies are HttpOnly, SameSite=Lax and Secure over HTTPS. Authorize only read-only operator GETs; writes, runner and CLI routes retain bearer tokens until explicit D2 extension. Sign-out deletes the session. Fake-provider tests cover invalid claims/state, expiry, allowlist, sign-out and rejected cookie writes/runner access. Document OAuth setup and redirect URI; live sign-in requires operator-provided credentials. |
| **PH-21 Add phone intake** | PH-20 | Verify the supported ChatGPT integration and authorization; file an item by phone and return its durable link. |
| **PH-30 Act from the dashboard (D2)** | PH-29; hosting/sign-in proven before hosted cookie actions | Deliver replies, cancel, retry, rerun, pause and capacity actions through existing API logic, with CSRF protection, server role checks, confirmations and recorded actors. Existing narrow local intake/settings authorization is not general cookie-write authority. |
| **PH-28 Add users and workspaces** | PH-27; before a second hosted user | Implement [workspace contracts](SPEC.md#users-and-workspaces) in separate parts: scope all server records and reads/writes to a workspace, preserving current data in a default workspace; provider-subject users with owner/member/viewer roles and verified-email invitations; hashed revocable personal/per-runner tokens bound to workspace/runner, retaining static single-user bootstrap; requester/actor attribution and requester-routed questions. Prove cross-workspace isolation and repeat the v1 muster with two users in two workspaces. |
| **PH-31 Add dashboard insight (D3)** | PH-29; roadmap stage 4 | Deliver server-computed metrics, repository health, Captain decision review and notification deep links under [DASHBOARD.md](DASHBOARD.md#metrics-d3). |
| **PH-22 Support multi-device execution** | Local execution/envelope acceptance; roadmap stage 5 | Validate cross-machine claims, checkpoint portability, routing, GitHub-write reconciliation and device loss with work in flight. This is P2, not an alternative current local execution path. |
| **PH-23 Support alternate runtimes** | Native execution/envelope acceptance; roadmap stage 5 | Verify runtime-native visible-session capabilities for additional runtimes as needed, preserving the shared direct stage/recovery contract. Do not reintroduce retired bridge adapters. |
| **PH-24 Add campaigns** | Dependency/envelope acceptance; roadmap stage 5 | Group items under a goal; propose parallel/dependent work and revisit after verified check-ins or steering. Preserve history and use versioned work-item operations. |
| **PH-25 Port to TypeScript on Node.js** | After v1 polish; prefer before PH-20 | Port in separately reviewable layers: transition/test fidelity spike, shared models/state machine, server against HTTP contract tests, then direct Captain/writer/reconciler clients and CLI. Preserve API/SQLite boundaries and lifecycle; no relay or Captain CLI scheduler. Use Python test cases as acceptance, keep Python releasable, and pass the v1 muster before replacing it. |
