# Phalanx repository instructions

## Start here

- Read [README.md](README.md) for the project goal and [SPEC.md](SPEC.md) for the current design. Treat options in the design questions as proposals. Record decisions in the spec as they are made.
- Implement against the current Markdown spec in small, independently reviewable PRs. Track completed slices and remaining integration in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
- Feature PRs don't edit IMPLEMENTATION_PLAN.md. The Captain or plan session batches status and ordering updates after merges.
- Branch new work from `main`. Stack only when the code truly depends on an unmerged PR.
- The Captain answers owner and design questions on Phalanx development with a best guess and logs each in [docs/decisions.md](docs/decisions.md) for the repository owner's review. Record who decided each row, and don't change an explicit owner decision without asking the owner. Ask the owner only for missing authority or credentials.

## Design priorities

- Prioritize reliable delivery and simplicity. Choose the smallest design that keeps state correct and lets interrupted work resume. Design and test for a large queue and many concurrent workers on one machine. Multi-device execution and handoff are P2.
- Prioritize simplicity. Don't add database migrations, legacy-schema support or backwards-compatibility machinery unless the owner asks for it: change the schema, API or format directly and update its callers. This doesn't permit deleting or resetting live data, and checks, trust and authorization still apply.
- Keep Phalanx usable by others, not coupled to any individual's identity. Use roles in operating instructions and operator-configured identities, credentials, paths and repository mappings rather than personal defaults. Preserve identity verification, authorization, privacy and ownership checks, including the configured issue-scout author identity. Historical attribution and canonical project URLs are not personal configuration.
- Add abstractions, services, settings, and gates when they solve a concrete problem. Keep constraints scoped to the operation that needs them, and leave teams and runtimes room to work within their own repo’s requirements.
- Aim for end-to-end autonomy from dispatch through delivery. Set up standing permissions and credentials for routine, in-scope operations so they can run without interactive permission prompts. Respect repository rules and external approval gates. Surface missing access as an actionable blocker when it prevents delivery.
- Default to self-resolution. Run checks, diagnose failures, retry transient errors, fix routine conflicts, and address actionable review feedback independently. Escalate when human judgment materially affects the outcome, authority is genuinely missing, or further attempts cannot make progress.
- Any qualified reviewer independent of the author may provide final exact-head review; no native session or person is a universal review dependency. Reuse valid exact-head approval covering the change without unresolved findings. Preserve genuine GitHub required reviewers/protections.
- Bound ordinary performance optimization to two rounds, retaining baseline/round measurements, remaining numeric miss and concrete follow-up. Explicit safety/SLO/resource-contract gates stay hard and must be named; use supported versioned controls for owner-approved classification, never silently waive criteria or claim a missed metric met. See [bounded performance work](docs/writer-operations.md#bounded-performance-work).
- Retain test-set identity with performance evidence. Already-ordinary goals need no redundant approval; explicit current owner check-in direction supersedes contradictory historical instructions, not hard contracts or merge safeguards. Unrelated service/control upgrades are not delivery prerequisites. Genuine owner decisions go to Needs-you with reason, options, actor and resume trigger, including attempt-free delivery questions; never hide them in generic external waits.
- Reconcile/refill on material native/stage/Building-release/merge/deployment events as well as scheduled wakes, preferring waiting PR fixes and preserving atomic Building/Total admission and sole-writer fencing. No new scheduler/service/polling framework, quota or blanket serialization.
- Routine compatible reviewed/checks-green upgrades use standing owner deployment authority with fresh validated backup, complete migration rehearsal, exact scoped API service ownership, continuity checks and recoverable rollback. Destructive/data-loss/schema-downgrade/new-service/credential-scope changes remain explicit owner decisions. [Source merge is not live adoption](docs/deployment.md#coordination-policy-adoption-boundary).
- Keep working until you really can't anymore. Keep the registered writer on its item through runnable delivery stages until completion or a genuine blocker, external wait, safe stop or cancel. Each stage has its own fenced attempt. Waits retain the writer and context; completion ends it. This never waives checks, trust, permissions, fencing or cancellation.
- API transport alone must not gate already-authorized development, validation, publication or check-in under the same live writer's verified held allocation. Make best-effort updates, distinguish actual effects from missing receipts, and reconcile through existing fenced/idempotent controls. Never infer new admission/capacity, ignore known stop/401/ended credentials or lift ready-only/report/draft ceilings. See [control-plane outages](docs/writer-operations.md#control-plane-outages).
- Keep the muster moving when one item is blocked. Try a different scoped recovery when it can help, isolate the affected work, and continue dispatching unrelated eligible stages within capacity.
- Never fail silently. Give every wait or failure a visible reason and a recovery path. Notify the owner when a blocker needs their attention. Every closed execution records exactly one verified PR or stored Markdown report, including a failure or cancellation report when the requested deliverable could not be produced.
- Two PRs are better than zero. Reuse a matching PR when found. After a bounded search, retry uncertain PR creation rather than lose delivery. Record one primary PR and surface any duplicates. Reconcile an uncertain merge before retrying and check in only the verified primary PR.

## Architecture

- Run a standalone HTTP API server locally first. Keep its contract usable when hosted. Captain, writer, reconciler and CLI clients use a configurable base URL and credentials and reach queue storage only through the API, so hosting does not require re-architecture.
- Keep queue state, attempts, work history and reports behind the API. Keep checkout mappings and Git credentials local. Captains register native writers; each writer uses its scoped credential from the item's Git directory, never the Captain's runner token.
- Stay runtime-neutral. Phalanx must work with any agent runtime that can run visible worker sessions, such as Codex, GitHub Copilot CLI or the GitHub Copilot app, and Claude Code. Don't encode a preferred runtime, or one runtime's tools, names or behavior, in the core, specs, plan, branch names or tests. Captains use their runtime's native session tools and the shared writer registration and result contracts. Preserve enough handoff context to resume with a fresh agent after a reply or interruption.
- Stay platform-neutral on the client side. The CLI, Captain sessions, reconciler and local checks must work on Windows, macOS, and Linux. Use portable paths, processes, signals, file locking and shells, and keep OS-specific behavior behind small, tested branches. CI must cover each supported OS. The server only needs to run where it is hosted.
- Build for many users, alone or in teams, in workspaces on a shared server ([SPEC.md](SPEC.md#users-and-workspaces)). Don't assume a single owner in the API, data model, dashboard or copy:
  - Scope new server state to a workspace.
  - Attribute requests and actions to a user or runner identity.
  - Keep each user's Git credentials on their own runner.

  Ship the single-workspace path first, and add multi-user features in their own plan rows.
- Allow independent work to run in parallel, including within one repo. Use atomic per-item claims and isolated worktrees for PR tasks. Check for previously created outputs before retrying publication.
- Reuse the item’s worktree across stages on the same machine. Support interdependent work in v1, with explicit upstream milestones and parallel execution when dependencies allow. Campaign grouping, task decomposition, and automatic replanning can come later.
- Make max concurrency and dispatch pause configurable. Rank eligible pickup by intake priority, oldest execution first within each priority. Keep the wait reason visible when an item cannot run.
- Keep lifecycle transitions explicit and server-owned. Every active state needs a next action, a resume trigger, or a clear recovery path. Bind validation and review evidence to the revision checked, and reconcile GitHub state before retrying publication or check-in.
- Keep one registered writer on its item across stages, with a fenced attempt per stage. **D1:** writers own development, self-review, publication and fixes during delivery; no separate PR-stage executor or client SQLite journal exists. The reconciler observes one primary PR. Merge or enable GitHub auto-merge only for the current `checked_in` target after the [merge decision gates](docs/writer-operations.md#merge-decision-before-auto-merge) pass; never for draft-only work or `ready_for_check_in`. Publication may finish earlier with auto-merge off and the same writer retained in delivery. **D3:** trusted local checks run serially in reconciler scratch worktrees and raise flags, not a Phalanx gate; writers still run the repository's required checks and GitHub's checks, reviews and rulesets gate merging.
- Define transitions in the authoritative Markdown specs, with the detailed rules in [STATE_MACHINE.md](STATE_MACHINE.md). Treat HTML as a visual reference and sync it to the Markdown when transitions change.
- For architecture changes, trace the implementation and keep [the implementation map](docs/architecture.md), active operating guides and diagrams consistent. The API/Scribe server remains; the relay, Captain CLI loop, numbered slots and client journals are retired. Label historical evidence and undeployed proposals explicitly rather than turning either into current guidance.

## Working in repositories

- Verify the checkout and its working-tree state before editing. Preserve existing changes. For work dispatched to another repo, follow that repo’s applicable instructions and keep the work scoped to the item.
- Use `phalanx/` for new topic branches unless the owner or the target repo specifies otherwise. Run checks appropriate to the change and report any gaps.
- Before you open or update a Phalanx PR, run only the tests that cover the change, one module at a time with `python -m unittest discover -s tests -p test_x.py`. The full suite runs in GitHub CI and gates the merge. Run:
  - every changed test module;
  - every test module that imports a changed `phalanx/` module;
  - for a server-side change (`store`, `transitions`, `dispatch_api`, `intake*`, `operator_api` or the app), also the modules that start a server and drive the affected routes.
  - Some changes always need their safety tests as well:
    - store, claims, leases, fencing or dispatch: `test_dispatch`, `test_writers`, `test_transitions` and `test_bootstrap`
    - Captain sessions or recovery: `test_shift`, `test_writers` and `test_reconciler`
    - workspaces, Git publication or check-in: `test_workspaces`, `test_writers`, `test_reconciler` and `test_transitions`
  - Run the full suite locally when you change shared test fixtures (`tests/test_bootstrap.py`, `tests/timing.py`, `tests/shard.py`, `tests/envelope_probe.py`), dependencies or the CI workflow.
- For Phalanx work items, record the report or PR and its verification. Check in only when authorized by the work item or repository policy and after the current PR head satisfies the required checks and approvals.
- End PR descriptions that Phalanx creates with `Spearheaded by [Phalanx](https://github.com/caltseng/phalanx)`. Preserve the footer across updates and retries.
