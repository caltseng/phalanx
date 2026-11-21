# Phalanx repository instructions

## Start here

- Read [README.md](README.md) for the project goal and [SPEC.md](SPEC.md) for the current design. Treat options in the design questions as proposals. Record decisions in the spec as they are made.
- Keep the project in design until Caleb asks to start implementation. Keep design changes focused on the current decisions.

## Architecture

- Run a standalone HTTP API server locally first. Keep its API contract usable when hosted. The CLI and runner connect using a configurable base URL and credentials.
- Keep queue state, claims, work history, and reports behind the API. Keep checkout mappings and Git credentials local to the runner. The runner talks to the backend and holds queue credentials and leases.
- Prioritize visible Codex worker chats for v1. Keep the work packet and result contract independent of the execution runtime so Claude Code and GitHub Copilot CLI can be added in P2. Preserve enough handoff context to resume with a fresh agent after a reply or interruption.
- Prefer the smallest design that meets the current need. Allow independent work to run in parallel, including within one repo. Use atomic per-item claims and isolated worktrees for PR tasks. Check for previously created outputs before retrying publication.
- Make max concurrency and dispatch pause configurable. Apply gates before claiming new work and keep the reason visible when an item cannot run.
- Keep lifecycle transitions explicit and server-owned. Every active state needs a next action, a resume trigger, or a clear recovery path. Bind validation and review evidence to the revision checked, and reconcile GitHub state before retrying publication or check-in.

## Working in repositories

- Verify the checkout and its working-tree state before editing. Preserve existing changes. For work dispatched to another repo, follow that repo’s applicable instructions and keep the work scoped to the item.
- Use `codex/` for new topic branches unless Caleb or the target repo specifies otherwise. Run checks appropriate to the change and report any gaps.
- For Phalanx work items, record the report or PR and its verification. Check in only when authorized by the work item or repository policy and after the current PR head satisfies the required checks and approvals.
