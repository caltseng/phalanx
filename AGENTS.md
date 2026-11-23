# Phalanx repository instructions

## Start here

- Read [README.md](README.md) for the project goal and [SPEC.md](SPEC.md) for the current design. Treat options in the design questions as proposals. Record decisions in the spec as they are made.
- Implement against the current Markdown spec in small, independently reviewable PRs. Track completed slices and remaining integration in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
- Feature PRs don't edit IMPLEMENTATION_PLAN.md. The Captain or plan session batches status and ordering updates after merges.
- Branch new work from `main`. Stack only when the code truly depends on an unmerged PR.
- The Captain answers owner and design questions on Phalanx development with a best guess and logs each in [docs/decisions.md](docs/decisions.md) for Caleb's review. Record who decided each row, and don't change a decision Caleb made explicitly without asking him. Ask Caleb only for missing authority or credentials.

## Design priorities

- Prioritize reliable delivery and simplicity. Choose the smallest design that keeps state correct and lets interrupted work resume. Design and test for a large queue and many concurrent workers on one machine. Multi-device execution and handoff are P2.
- Prioritize simplicity. Don't add database migrations, legacy-schema support or backwards-compatibility machinery unless Caleb asks for it: change the schema, API or format directly and update its callers. This doesn't permit deleting or resetting live data, and checks, trust and authorization still apply.
- Add abstractions, services, settings, and gates when they solve a concrete problem. Keep constraints scoped to the operation that needs them, and leave teams and runtimes room to work within their own repo’s requirements.
- Aim for end-to-end autonomy from dispatch through delivery. Set up standing permissions and credentials for routine, in-scope operations so they can run without interactive permission prompts. Respect repository rules and external approval gates. Surface missing access as an actionable blocker when it prevents delivery.
- Default to self-resolution. Run checks, diagnose failures, retry transient errors, fix routine conflicts, and address actionable review feedback independently. Escalate when human judgment materially affects the outcome, authority is genuinely missing, or further attempts cannot make progress.
- Keep working until you really can't anymore. Keep working on the same item through its runnable delivery stages until it is done or genuinely can't continue: an explicit blocker or external wait, a safe stop or cancel, or a failure that needs recovery. Retain its capacity reservation across stage handoffs while each stage keeps its own attempt, lease and fencing, and release capacity correctly on wait, stop, completion or recovered expiry. This never waives checks, trust, permissions, fencing or cancellation.
- Keep the muster moving when one item is blocked. Try a different scoped recovery when it can help, isolate the affected work, and continue dispatching unrelated eligible stages within capacity.
- Never fail silently. Give every wait or failure a visible reason and a recovery path. Notify Caleb when a blocker needs his attention. Every closed execution records exactly one verified PR or stored Markdown report, including a failure or cancellation report when the requested deliverable could not be produced.
- Two PRs are better than zero. Reuse a matching PR when found. After a bounded search, retry uncertain PR creation rather than lose delivery. Record one primary PR and surface any duplicates. Reconcile an uncertain merge before retrying and check in only the verified primary PR.

## Architecture

- Run a standalone HTTP API server locally first. Keep its API contract usable when hosted. The CLI and runner connect using a configurable base URL and credentials. The work queue and the operator dashboard will be server-hosted, so keep the server a separate component from runners and clients: they reach queue storage only through the API, which lets storage move to the backend without re-architecture.
- Keep queue state, claims, work history, and reports behind the API. Keep checkout mappings and Git credentials local to the runner. The runner talks to the backend and holds queue credentials and leases.
- Stay runtime-neutral. Phalanx must work with any agent runtime that can run visible worker sessions, such as Codex, GitHub Copilot CLI or the GitHub Copilot app, and Claude Code. Don't encode a preferred runtime, or one runtime's tools, names or behavior, in the core, specs, plan, branch names or tests. Keep runtime specifics behind the bridge, work packet and result contracts, with one adapter per runtime. Preserve enough handoff context to resume with a fresh agent after a reply or interruption.
- Stay platform-neutral on the client side. The CLI, runner, Captain loop, bridge, and local checks must work on Windows, macOS, and Linux. Use portable paths, processes, signals, file locking and shells, and keep OS-specific behavior behind small, tested branches. CI must cover each supported OS. The server only needs to run where it is hosted.
- Build for many users. Phalanx will serve people other than Caleb, in workspaces on a shared server ([SPEC.md](SPEC.md#users-and-workspaces)). Don't assume a single owner in the API, data model, dashboard or copy:
  - Scope new server state to a workspace.
  - Attribute requests and actions to a user or runner identity.
  - Keep each user's Git credentials on their own runner.

  Ship the single-workspace path first, and add multi-user features in their own plan rows.
- Allow independent work to run in parallel, including within one repo. Use atomic per-item claims and isolated worktrees for PR tasks. Check for previously created outputs before retrying publication.
- Reuse the item’s worktree across stages on the same machine. Support interdependent work in v1, with explicit upstream milestones and parallel execution when dependencies allow. Campaign grouping, task decomposition, and automatic replanning can come later.
- Make max concurrency and dispatch pause configurable. Rank eligible stages with priority aging so older work gets a turn during sustained intake. Keep the wait reason visible when an item cannot run.
- Keep lifecycle transitions explicit and server-owned. Every active state needs a next action, a resume trigger, or a clear recovery path. Bind validation and review evidence to the revision checked, and reconcile GitHub state before retrying publication or check-in.
- Assign each worker one stage. Keep GitHub writes on the runner-owned path. Open the draft after development, then promote it when pre-validation and draft-only checks are satisfied. Allow documented pre-validation bypasses where repo policy permits. Check required CI and approvals before check-in.
- Define transitions in the authoritative Markdown specs, with the detailed rules in [STATE_MACHINE.md](STATE_MACHINE.md). Treat HTML as a visual reference and sync it to the Markdown when transitions change.

## Working in repositories

- Verify the checkout and its working-tree state before editing. Preserve existing changes. For work dispatched to another repo, follow that repo’s applicable instructions and keep the work scoped to the item.
- Use `phalanx/` for new topic branches unless Caleb or the target repo specifies otherwise. Run checks appropriate to the change and report any gaps.
- Before you open or update a Phalanx PR, run only the tests that cover the change, one module at a time with `python -m unittest discover -s tests -p test_x.py`. The full suite runs in GitHub CI and gates the merge. Run:
  - every changed test module;
  - every test module that imports a changed `phalanx/` module;
  - for a server-side change (`store`, `transitions`, `dispatch_api`, `intake*`, `operator_api` or the app), also the modules that start a server and drive the affected routes.
  - Some changes always need their safety tests as well:
    - store, claims, leases, fencing or dispatch: `test_dispatch`, `test_stage_continuation`, `test_transitions` and `test_bootstrap`
    - runner, Captain loop, relay or recovery: `test_captain_loop`, `test_bridge_contract` and `test_attended_recovery_service`
    - workspaces, Git publication or check-in: `test_workspaces`, `test_git_publication`, `test_draft_publication` and `test_check_in`
  - Run the full suite locally when you change shared test fixtures (`tests/test_bootstrap.py`, `tests/timing.py`, `tests/shard.py`, `tests/envelope_probe.py`), dependencies or the CI workflow.
- For Phalanx work items, record the report or PR and its verification. Check in only when authorized by the work item or repository policy and after the current PR head satisfies the required checks and approvals.
- End PR descriptions that Phalanx creates with `Spearheaded by [Phalanx](https://github.com/caltseng/phalanx)`. Preserve the footer across updates and retries.
