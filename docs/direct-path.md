# Direct path: Captain and reconciler

On the direct path, a Captain chat launches native worker sessions that post their own results, and a reconciler observes GitHub. The design is in [Direct sessions](direct-sessions.md). Set up the server and tokens first as in [Install Phalanx](install.md).

## Run the GitHub reconciler

The reconciler reads GitHub and git for mapped open items, completed direct ready PRs
awaiting an owner merge, and configured recent deployments. It posts timestamped
observations through the API, without a worker attempt or lease. The server applies those
observations to delivery and later owner-merge enrichment; the reconciler never writes
queue storage directly or performs GitHub writes. Provider failures back off without
blocking unrelated work.

The server derives flags from the latest observation of each open execution: `head_drift`, `pr_closed`, `merge_conflict`, `wrong_base` (the PR's base isn't the item's target branch), `checks_failed`, `review_feedback`, `unpublished_branch_merged` (item branch reachable from its base with no PR) and `pre_pr_checks_failed`. An observation older than 15 minutes keeps its flags, marked with when it was seen, and adds `observation_stale`. While the observed head differs from the execution's recorded candidate, only `head_drift` reports it: `merge_conflict`, `checks_failed` and `pre_pr_checks_failed` describe that superseded head, so they wait for an observation of the candidate. Each reconciler with no heartbeat for 15 minutes raises its own `reconciler_stale`. An incomplete GitHub read posts nothing, so earlier flags stand, and backs that repository off until a later pass.

```powershell
python -m phalanx reconcile --config reconciler.json --state C:\phalanx-reconciler
```

It needs `PHALANX_RUNNER_TOKEN` and `PHALANX_GITHUB_TOKEN`, and a minimal
[reconciler configuration](configuration.md#reconciler-config) with `base_url`,
`reconciler_id`, `repositories` (checkout and `base_branch`), optional `validation_policy`
and optional `notify_command`. It never reads an execution-runner config.

- With a validation policy, it reports each open PR's configured GitHub checks and runs the trusted local checks once per PR head, one at a time, in a scratch worktree under `--state`, heartbeating while they run. Results go to their own `local-checks` route, so they never re-assert an older PR snapshot. Failures raise a flag; they never gate the item.
- Only one reconciler can use a `--state` directory at a time.
- A checkout whose configured `origin` URL isn't the registered remote is refused.
- Owner-notify flags are sent once each to `notify_command`.
- Each pass prints one JSON line; `--once` runs a single pass and exits nonzero on any error.

### Existing muster rollout

Reusable launch behavior is now [versioned deployment tooling](deployment.md); retained
host wrappers should only delegate to it. The service owner must review and coordinate
adoption, retaining current paths, credentials, active writers and capacity. The following
explicit command is an example, not a live-host inventory or permission to restart services.
Use the installation's recorded API URL, existing reconciler identity as `reconciler_id`,
verified mappings and private environment. Keep those machine-specific values in the
operator's private runbook, not tracked documentation.

```powershell
python -m phalanx reconcile --config C:\muster\reconciler.json --state C:\muster\reconciler --once
```

Under the service owner's rollout, stop only the identified previous reconciler before
running `--once`; it takes the same scratch lock as the persistent process. Verify no
errors and a fresh heartbeat through authenticated API status, then start the single
persistent supervisor. `muster status` additionally requires matching configured home
identity. Retire obsolete configs only after checking no launcher references them.
Do not delete the database, artifacts, credentials or worktrees. No client SQLite state
is imported or recreated.

### Post-merge deployment status

For a repository that deploys from its base branch, optionally set `"post_merge_check": "deploy"` in its reconciler mapping. Use the exact check-run name of the post-merge deployment job (for example your GitHub Pages deploy job), not a PR build check. The reconciler then also polls recent executions checked into the mapping's `base_branch` and reads that check on each PR's **merge commit**, never its PR head or the latest base tip.

- During the 24-hour window after check-in, the board shows a derived `deployment_pending`, `deployment_failed` or `deployed` flag. Missing or running checks are pending; only a successful completed run means deployed. Other conclusions, including skipped or cancelled, mean failed. The newest run wins when a job is rerun before success.
- Polling stops on success or at the end of the window, and closed-page enumeration stops at the window boundary instead of walking all history.
- After the window, successful flags disappear, failed flags remain, and a still-pending deployment becomes one owner-notifying `deployment_missing` flag. Closed executions never raise `observation_stale`.
- Each heartbeat reports the mapped repositories' signal configuration. Removing `post_merge_check` retires their previous deployment signals, including expired failures, without GitHub reads or changing execution state.

Unconfigured repositories keep the existing behavior. This is GitHub observation only: check-in still completes on merge, dependencies unblock, and no deployment stage or agent-runtime integration is added. Deployment reads start after check-in is recorded, so an unavailable deployment-check API can't hold up delivery. The design is in [direct-session reconciliation](direct-sessions.md#github-reconciliation).

## Run a Captain on the direct path

A Captain is an agent chat in any runtime that can start visible sessions. It launches one native worker session per item, registers it as the item's writer and coordinates it. It never does item work. The worker posts its own stage results with `phx work`, and the reconciler observes GitHub.

The Captain's commands are `python -m phalanx shift ...`. They need `PHALANX_RUNNER_TOKEN` and a shift config, passed with `--config` (before or after the action) or set in `PHALANX_SHIFT_CONFIG`. Alternatively, the [per-user muster locator](../README.md#direct-muster-discovery-and-status) derives `shift.json`, `charter.toml`, `reconciler.json` and `private.env` from an existing muster directory (`--home`, then `PHALANX_HOME`, then `~/.phalanx/muster.json`). With that layout, `shift status`, `reconcile` and `muster status` need no explicit config paths. Discovery/status never initialize or migrate state.

Example explicit shift config:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "runtime": "copilot-app",
  "charter": "C:\\phalanx\\charter\\charter.toml",
  "worktrees": "C:\\phalanx\\direct\\worktrees",
  "session_workspace": "<runtime workspace ID for the worktrees folder>",
  "work_command": "python -m phalanx work",
  "shift_command": "python -m phalanx shift --config C:\\phalanx\\shift.json",
  "repositories": {
    "phalanx-pilot-py": {
      "checkout": "C:\\phalanx\\checkouts\\phalanx-pilot-py",
      "base_branch": "main",
      "checks_environment": "& \"C:\\phalanx\\checks\\phalanx-pilot-py\\Scripts\\python.exe\""
    }
  }
}
```

- `repositories` maps registered IDs to verified checkouts and base branches, as in the reconciler config.
- Each repository may also set `checks_environment`, a non-empty string naming its prepared checks interpreter or command prefix, with any quoting or shell invocation syntax the writer needs. Use an absolute interpreter path or a prefix that runs from the item's worktree (for example `uv run --no-sync python` with an already prepared environment). Writers append the repository's test or check arguments. This is separate from `work_command`: it supplies the project's check dependencies, not the Phalanx CLI.
- `worktrees` is the root for item worktrees, separate from reconciler scratch worktrees.
- `work_command` is how a worker session runs `phx work` from its worktree. The default is `python -m phalanx work`.
- `shift_command` is the concrete `phx shift` command, including its config and anything that loads the runner token, which `shift handoff` names in the successor's kickoff. The default is `python -m phalanx shift --config <this config's path>`.
- `session_workspace` is the runtime's workspace for the `worktrees` folder, where every worker session opens. In the GitHub Copilot app it is the ID `add_repository` returns for that folder.

`shift launch` includes that repository's exact `checks_environment` in the writer kickoff.
It takes the target from the **current execution's** `state.target`, not the original intake
request, so reruns and target steering are reflected. When the environment is omitted, the
kickoff explicitly says it is not configured; it never guesses a Python or environment.
The worker prompt requires checks to use that environment from the item's worktree,
forbids borrowing another checkout's environment or installing packages, and tells the
writer to report the validation gap and request the correct environment with
`phx work question` if it is missing or unusable.

### Prepared checks environments

Keep each repository's prepared checks environment outside its item worktrees. The
operator records the exact interpreter or command prefix in that repository's shift
mapping. For Windows npm/Playwright checks, the tracked
[`scripts/checks.ps1`](../scripts/checks.ps1) accepts `-EnvironmentRoot`; a private
environment may retain a delegation-only shim. Example mapping:

```json
{"checks_environment": "& \"C:\\muster\\envs\\example\\checks.ps1\""}
```

Writers invoke it **from their item worktree**, for example:

```powershell
& "C:\muster\envs\example\checks.ps1" npm run ci
```

The prefix is a PowerShell command wrapper, not a Python interpreter. Its shim
delegates to the operator-pinned tracked script with `-EnvironmentRoot $PSScriptRoot`
and `@args`. The script preserves the working directory and command exit code,
verifies dependencies and sets `PLAYWRIGHT_BROWSERS_PATH` to the prepared environment's
`ms-playwright` directory. Paths and installed browser/package versions belong in
the private runbook; this example does not attest a particular live environment.

To provision or refresh, the **operator**, not a writer:

1. Copies the intended repository revision's `package.json` and `package-lock.json`
   into the external environment directory, prepares Node/npm matching repository
   requirements, and runs `npm ci` there.
2. Installs the required Playwright browsers there with
   `PLAYWRIGHT_BROWSERS_PATH` set to the external `ms-playwright` directory, using
   that repository's required browser set.
3. Records the lowercase SHA-256 of the provisioned lockfile after normalizing CRLF to
   LF, as UTF-8 without a BOM, in `lock.sha256`; makes any retained `checks.ps1` a shim
   delegating to the pinned tracked `scripts/checks.ps1 -EnvironmentRoot <prepared environment>`
   and puts the exact prefix in the shift config.

Before any check, the wrapper hashes the **worktree's** `package-lock.json` with the same
normalization. A missing or changed lockfile fails visibly.
The writer reports the validation gap with `phx work question`; it never installs,
updates the shared environment, or overwrites the expected hash to bypass the mismatch.
The operator must reprovision for the new lockfile.

When `node_modules` is absent, the wrapper creates an untracked Windows junction at
`<item worktree>\node_modules` pointing to the shared environment's `node_modules`.
It does not replace an existing path or install packages. Keep that link out of commits;
repository/local ignore rules should exclude it. Package resolution then uses the shared
dependencies while builds/tests still operate on the writer's worktree.

### Charter

The charter is the Captain's standing authority. It is a TOML file on the runner host, outside every checkout, worktree and Git repository, so workers can't edit it:

```toml
goal = "Deliver the queued phalanx-pilot-py items"
repositories = ["phalanx-pilot-py"]
allowed_actions = ["launch, confirm, replace and withdraw writer sessions", "answer routine questions"]
escalate = ["scope changes", "destructive actions", "missing credentials or authority"]
stop_when = ["the owner ends the pilot"]
notes = "Optional context for every Captain."

[shift]
max_hours = 4
max_wakes = 48
max_compactions = 5
```

`[shift]` is optional. Its positive-integer settings default to `max_hours = 4`,
`max_wakes = 48` and `max_compactions = 5` (D7, as amended for compaction handoff).
The compaction threshold uses only cumulative successful native telemetry; ordinary
compaction below it is a checkpoint. Missing telemetry stays unknown, with no hidden
first-compaction fallback. Configure concurrency with Scribe's **Writer concurrency**
control or owner-authenticated `PUT /v1/dispatch/settings` (`{"max_writers": 5}`),
not in the charter or `allowed_actions` prose. The persisted limit starts at 5.
Charter `[shift] max_writers` is rejected with guidance to remove it and use
dispatch settings instead; heartbeats and launches never configure the limit.
The server counts registered intents and
confirmed writers on open executions across all repositories and Captains
(including predecessors). Idle or waiting writers still count. Completion, failure,
cancellation, withdrawal and replacement release the old writer's place; replacement
does not consume an extra place. Registration checks the cap atomically, so concurrent
launches cannot overfill it. Replaying a launch remains safe even at the cap.
If the cap is reached, launch refuses with a wait message naming the cap and active
writers; retry when a place opens, without escalating or raising a flag. Lowering the
cap does not stop existing writers. Queued items without registered sessions do not count.
`shift status` prints the server's `active_writers` against
`max_writers`. `shift charter` validates the file and prints its revision, the SHA-256
of its bytes. Each heartbeat re-reads the charter and records that revision.

### Commands

| Command | Does |
| --- | --- |
| `shift prompt captain\|keeper\|worker\|forwarding` | Prints the Captain/keeper prompt (including durable off-shift forwarding), worker template, or just the forwarding section for retained chats. Needs no config |
| `shift charter` | Validates the charter and prints it with its revision |
| `shift start --session-ref S [--parent ID]` | Registers this chat as a Captain. A rerun replays the same registration |
| `shift heartbeat --captain ID [--wake] [--state] [--focus] [--session ID NAME]... [--session-writer ID NONCE]... [--no-sessions] [--handoff-note]` | Re-reads the charter and heartbeats. Reports all Phalanx sessions, including task-first and renamed sessions, with the full verified writer nonce separately. The server flags any that no writer or Captain owns. Omitted inventory keeps its last value, including nonces. Prints `shift.handoff_due` once a shift bound is reached |
| `shift status [--captain ID]` | Flags, Captains, configured `dashboard_url` and the charter's open items with their writers; `--captain` also reads that exact Captain and its cleanup, even outside the recent list |
| `shift current` | Reads all registrations to resolve a unique live active Captain and native `session_ref`, or returns null with the precise reason. Native tools must verify the conversation and return its link |
| `shift launch --captain ID --item ITEM [--replaces WRITER]` | Prepares the item worktree, saves the writer credential in its Git directory and registers the writer intent. Prints a task-first `session_name` (at most 40 characters, ending in `[phx]`), structured `session_identity` (item ID, writer ID and full writer nonce), and the worker's `kickoff`. A rerun reuses the saved nonce; verify it in an existing session's kickoff/metadata before confirming or relaunching |
| `shift writer --captain --item --execution --writer [--session-id] [--session-url] [--state] [--abandon]` | Confirms a launched session or reports its state |
| `shift withdraw --captain --item --execution --writer --reason [--stopped]` | Ends a writer whose session is stopped or never launched |
| `shift action --captain --item --action --reason` | Puts a Captain action on the item's timeline |
| `shift handoff --captain ID --note NOTE` | Starts ending the shift. Prints owned open intents, successor launch `session_name` (predecessor-derived nonce, not the new Captain ID), `kickoff`, registered successors and durable `off_shift_instructions` |
| `shift retire-reconciler RUNNER_ID` | Deletes a decommissioned reconciler's heartbeat, which clears its `reconciler_stale` flag |
| `shift archive-check --session ID --by SESSION (--item ITEM \| --worktree PATH \| --no-worktree)` | Checks whether the session's writer/handoff has ended and local work is preserved. Fetches the actual origin branch (or the item's origin base when its branch is absent) and refuses dirty worktrees or unpushed commits. Does not archive |
| `shift archive-result --session ID --by SESSION --outcome archived\|blocked\|refused --reason REASON` | Records the runtime outcome durably. A block or refusal keeps `orphan_session`; only verified archival clears it. Refusals suppress further attempts |

### Intake routing and charter lanes

Intake defaults to `checked_in`; explicit ready-only and report targets are preserved.
Historical `runner_affinity: "direct-hold"` excluded the old runner before PH-DS5
part 1; there is now no queue claim scheduler or special direct-hold gate.
Named writer registration does not filter by affinity.
Unbound queued work, including intake with no affinity, visibly awaits a direct Captain.

Lane order and mutual exclusion belong to the operator's charter and the Captain's
selection, not a separate server lane setting. For each runnable item deliberately deferred
by lane order, overlap or the writer cap, the Captain records
`shift action --captain ID --item ITEM --action held --reason "<concrete charter reason>"`.
Refresh each wake: a hold expires after 30 minutes, a version change, or its Captain
ending/becoming stale. It explains the board wait and suppresses dispatch-overdue warnings;
it does not hide informational backlog when the canonical writer limit is full or dispatch is paused.
A hold never reserves capacity or prevents another Captain launching the item.

### Creator-owned session cleanup

The runtime's session tools, not Phalanx core, CLI or API, archive sessions. Each Captain archives
the writer sessions it **created**, after the execution is completed, failed or cancelled, or an
abandoned/replaced writer has ended and its runtime session is confirmed stopped. Idle, waiting,
stale and trust-blocked sessions are not finished. `shift status` includes each Captain's
`ended_writers`, including writers on closed items absent from the open-item list, and its
`cleanup_pending` count.

Session archival records shipped with schema step 18. This is not a request to drain or
restart a live installation; use the [service-owner adoption procedure](deployment.md#existing-runtime-reviewed-adoption-not-a-rename)
for an upgrade, preserving active writers and existing state.

Immediately before the runtime archive call, the creator runs `shift archive-check` with the
writer's `--item`. The check inspects the verified item worktree, fetches its origin branch and
requires every local commit to be on that remote branch. If origin confirms the branch is absent,
it instead freshly fetches the item's target branch (or configured base branch) and requires every
local commit to be on that origin base. Report-only writers need not push a branch. Tracked or
staged changes, untracked non-ignored files, hidden index changes, in-progress Git operations and
unavailable Git evidence all prevent archival. Gitignored build output does not block archival.
Nothing is discarded or reset. For a helper or Captain
with a Git worktree use `--worktree`; use `--no-worktree` only after runtime inspection confirms
there is no Git worktree or persistent work that archival would remove. `can_archive: true` is
preservation evidence, not runtime authority: the creator must also verify ownership and that the
session has ended/stopped.

After one runtime archive call, record its confirmation with `shift archive-result`. If refused
or uncertain, leave the session alone, record the exact reason as `refused`, and notify the owner
once. Subsequent checks suppress retries; the owner/creator may record `archived` after separately
verifying actual archival. The durable `orphan_session` flag survives a Captain ending or omitting
the session from its next inventory. A local-work block may be checked again after its writer
preserves/pushes the work. Neither archival records nor these flags change execution state.

An ending Captain finishes or stops its own helpers after accounting for their work, then cleans
up those helpers and its ended writers, never its live successor. If its writers are still active,
it retains only a cleanup wake after marking itself
`ended`, with no dispatch or coordination, until `cleanup_pending` is zero. Refusals have already
been escalated and do not keep that wake alive. Its creator, including a keeper run that launched
it, archives the Captain after a clean handoff: `ended`, no open intents, no pending writer cleanup,
and a registered successor or an explicit charter stop condition. Staleness alone is insufficient.
A successor does not inherit creator-only runtime authority.

### In the GitHub Copilot app

1. Start a new chat. Ask it to run `python -m phalanx shift prompt captain --config <shift config>` and follow the printed prompt. Its first wake registers it with `shift start --session-ref <its native session ID>`. Use the ID listed by the runtime's session tools, not an app project-session/workspace ID; confusing them produces an incorrect registration even if the command succeeds.
2. Have the Captain wake every 5 minutes (D5) with a session automation on its own chat (`save_session_automation`, interval `minutes`, `every_minutes` 5). Use the prompt "Wake: run `python -m phalanx shift prompt captain --config <shift config>` and follow its Every wake steps." Idle notifications from its worker sessions wake it sooner.
3. To launch a worker, the Captain runs `shift launch`, then creates one session with `create_session`:
   - `project_id` set to the returned `session_workspace`: the shift config's folder project for its `worktrees` folder. One project covers every item worktree, so the owner trusts it once. Registering each worktree as its own project would hold every launch on a trust prompt, and autopilot denies tools until it is answered;
   - `name` set to `session_name`;
   - kickoff `prompt` set to `kickoff`, which names the item worktree, with mode `autopilot`.

   It confirms the session with `shift writer --session-id <session ID>` and, when the
   runtime returns a link, `--session-url <that exact link>`. Do not construct a session
   URL from its ID.
   To recover a lost launch, list the full inventory, including task-first and legacy names, then
   inspect candidate kickoffs/metadata for the exact `session_identity.writer_nonce`. Names and
   goals may collide and titles may change; neither is identity. If inspection is unavailable or
   inconclusive, retain the intent and report the blocker instead of launching twice.
4. Run the keeper (D8) as an app automation that starts a new session every 30 minutes (`save_workflow`, `cron_expression` `*/30 * * * *`, mode `autopilot`) with the prompt "Run `python -m phalanx shift prompt keeper` and follow it. `phx shift` means `python -m phalanx shift --config <shift config>`. Create the Captain session in project `<session_workspace>`." Before any early exit, it checks cleanup of Captain sessions that this keeper session created. It starts no new Captain when a live one exists, the most recent ended less than 10 minutes ago without a successor yet, or it stopped with `Stop condition met:` under the current charter revision. Otherwise it starts a Captain. An automation run that can't schedule itself creates the Captain with `project_id` set to the shift config's `session_workspace`, the name `phx captain keeper` and a 5-minute `schedule`. That same keeper creator must retain a cleanup wake or child-ended notification until its Captain hands off cleanly; a fresh scheduled keeper session has no creator authority over it.
5. The app may let only a session's creator archive it. Captains clean up their own writers and helpers; keeper runs clean up their own Captain children. All use the preservation check and durable result above, not repeated attempts by successors. Missing authority is recorded and escalated once, with `orphan_session` left visible.

A Captain's shift ends when the charter's `[shift]` age, wake or successful-compaction
bound is reached, or independently on actual degradation/failure (three failed actions
in a row, a self-raised deviation flag, or a missed heartbeat noticed on waking).
`shift heartbeat` returns the effective `max_compactions` and compares it with the
server's cumulative known total; repeated/omitted reports do not reset or add to it.
It runs `shift handoff`, which marks it `ending` (at most 30 minutes) and returns a successor name, `phx captain <id prefix>`, and its kickoff. The Captain starts that chat, finishes or abandons its unconfirmed launches, cleans up its ended children, and heartbeats `--state ended` without waiting for the successor. It then stops coordinating, retaining only creator cleanup if necessary. Captain states only move forward: `active`, `ending`, `ended`. A Captain that stops heartbeating for 15 minutes, or stays `ending` past 30 minutes, is stale, and any live Captain or the keeper takes over its unconfirmed launches; this does not authorize archival of unfinished work.

### Retained chats and owner-message forwarding

Each new Captain, including a handoff successor, opens the dashboard in the runtime's available
browser canvas by default. `shift status` returns `dashboard_url` from the configured API base URL
plus `/operator/`, preserving the installation's host, port and path. Reuse/focus an existing
dashboard canvas rather than duplicating it each wake. If browser canvas support or opening fails,
state that condition and provide the configured link; never claim it opened or pass credentials
in the browser URL/page.

An off-shift Captain explicitly says it is no longer on shift; a keeper identifies itself as keeper.
It does not continue as the live Captain. On every new owner message or forwarded interaction,
`shift current` resolves the unique live `active` registration across all history. Do not follow
only the immediate successor: it may already be off shift too. Inspect the returned `session_ref`
in its native provider, verify the unarchived conversation and use only its returned clickable
conversation URL. Re-read `shift current` immediately before forwarding if inspection took place.
No unique active registration, a stale/missing/archived native binding, a missing conversation URL,
an unavailable lookup or an unsupported provenance path is reported precisely, never replaced with
a guessed ID/link or duplicate dispatch.

Forward the original input verbatim through the runtime's supported original-message provenance
path, with guidance separately attributed. In the Copilot app, supply the relevant current
`source_user_message` for a coordinated user relay when required; use the matching provider's IDs.
An expired/unavailable source is not reconstructed from history or silently replaced by agent
guidance: direct the owner to send fresh input at the verified conversation. Report queue acceptance,
failure or uncertainty honestly; inspect uncertain receipt before retrying. Forwarding grants no
new permissions and does not change repository trust or scoped credentials.

After registration, use the new Captain's own ID in its display name where native rename is
supported; label its `parent_id` as predecessor. An ended chat can be named
`Off-shift <own ID prefix> [phx]`. When renaming is unavailable, explicit role wording in replies
is required. Preserve the predecessor-derived launch nonce in kickoff/history for recovery.
Never detach or reparent chats as a naming fix. Existing creator cleanup, writer bindings,
five-minute Captain wakes, thirty-minute keeper recovery and retirement rules are unchanged.

**Deployment boundary:** forwarding is tracked in `phalanx/prompts/forwarding.md`, composed into
`shift prompt captain` and `shift prompt keeper`, and included in generated handoffs.
Deploy both API and client from the reviewed revision through the normal service-owner rollout;
an old server will not serve `shift current`. No private muster instruction patch is required.
Existing chats do not retroactively receive new prompts: explicitly relay the changed section
verbatim from `shift prompt forwarding` to retained Captain/keeper chats via supported messaging.
Do not claim that merge/deployment updated those chats or that the relay was delivered without
runtime evidence. This item does not itself grant authority to edit unrelated retained chats.

Watch the run on the [operator board](work-items.md#operator-work-board), which shows each item's writer, the live Captains and the flags. `GET /v1/captains` and `GET /v1/flags` return the same data.
