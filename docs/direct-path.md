# Direct path: Captain and reconciler

On the direct path, a Captain chat launches native worker sessions that post their own results, and a reconciler observes GitHub. The design is in [Direct sessions](direct-sessions.md). Set up the server and tokens first as in [Install Phalanx](install.md).

## Run the GitHub reconciler

The reconciler reads GitHub and git for every open item whose repository the runner configuration maps, and posts timestamped observations through the API. It holds no lease or slot, never changes an item's state and never blocks work.

The server derives flags from the latest observation of each open execution: `head_drift`, `pr_closed`, `merge_conflict`, `wrong_base` (the PR's base isn't the item's target branch), `checks_failed`, `review_feedback`, `unpublished_branch_merged` (item branch reachable from its base with no PR) and `pre_pr_checks_failed`. An observation older than 15 minutes keeps its flags, marked with when it was seen, and adds `observation_stale`. While the observed head differs from the execution's recorded candidate, only `head_drift` reports it: `merge_conflict`, `checks_failed` and `pre_pr_checks_failed` describe that superseded head, so they wait for an observation of the candidate. Each reconciler with no heartbeat for 15 minutes raises its own `reconciler_stale`. An incomplete GitHub read posts nothing, so earlier flags stand, and backs that repository off until a later pass.

```powershell
python -m phalanx reconcile --config runner.json --state C:\phalanx-reconciler
```

It needs `PHALANX_RUNNER_TOKEN` and `PHALANX_GITHUB_TOKEN`, and reuses the [runner configuration](configuration.md#runner-config)'s `base_url`, `runner_id`, `repositories` (checkout and `base_branch`), optional `validation_policy` and optional `notify_command`.

- With a validation policy, it reports each open PR's configured GitHub checks and runs the trusted local checks once per PR head, one at a time, in a scratch worktree under `--state`, heartbeating while they run. Results go to their own `local-checks` route, so they never re-assert an older PR snapshot. Failures raise a flag; they never gate the item.
- Only one reconciler can use a `--state` directory at a time.
- A checkout whose configured `origin` URL isn't the registered remote is refused.
- Owner-notify flags are sent once each to `notify_command`.
- Each pass prints one JSON line; `--once` runs a single pass and exits nonzero on any error.

### Post-merge deployment status

For a repository that deploys from its base branch, optionally set `"post_merge_check": "deploy"` in its runner mapping. Use the exact check-run name of the post-merge deployment job (for example your GitHub Pages deploy job), not a PR build check. The reconciler then also polls recent executions checked into the mapping's `base_branch` and reads that check on each PR's **merge commit**, never its PR head or the latest base tip.

- During the 24-hour window after check-in, the board shows a derived `deployment_pending`, `deployment_failed` or `deployed` flag. Missing or running checks are pending; only a successful completed run means deployed. Other conclusions, including skipped or cancelled, mean failed. The newest run wins when a job is rerun before success.
- Polling stops on success or at the end of the window, and closed-page enumeration stops at the window boundary instead of walking all history.
- After the window, successful flags disappear, failed flags remain, and a still-pending deployment becomes one owner-notifying `deployment_missing` flag. Closed executions never raise `observation_stale`.
- Each heartbeat reports the mapped repositories' signal configuration. Removing `post_merge_check` retires their previous deployment signals, including expired failures, without GitHub reads or changing execution state.

Unconfigured repositories keep the existing behavior. This is GitHub observation only: check-in still completes on merge, dependencies unblock, and no deployment stage or agent-runtime integration is added. Deployment reads start after check-in is recorded, so an unavailable deployment-check API can't hold up delivery. The design is in [direct-session reconciliation](direct-sessions.md#github-reconciliation).

## Run a Captain on the direct path

A Captain is an agent chat in any runtime that can start visible sessions. It launches one native worker session per item, registers it as the item's writer and coordinates it. It never does item work. The worker posts its own stage results with `phx work`, and the reconciler observes GitHub.

The Captain's commands are `python -m phalanx shift ...`. They need `PHALANX_RUNNER_TOKEN` and a shift config, passed with `--config` (before or after the action) or set in `PHALANX_SHIFT_CONFIG`. Alternatively, the [per-user muster locator](../README.md#direct-muster-discovery-and-status) derives `shift.json`, `charter.toml`, `runner.json` and `private.env` from an existing muster directory (`--home`, then `PHALANX_HOME`, then `~/.phalanx/muster.json`). With that layout, `shift status`, `reconcile` and `muster status` need no explicit config paths. Discovery/status never initialize or migrate state.

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

- `repositories` uses the runner configuration's mapping.
- Each repository may also set `checks_environment`, a non-empty string naming its prepared checks interpreter or command prefix, with any quoting or shell invocation syntax the writer needs. Use an absolute interpreter path or a prefix that runs from the item's worktree (for example `uv run --no-sync python` with an already prepared environment). Writers append the repository's test or check arguments. This is separate from `work_command`: it supplies the project's check dependencies, not the Phalanx CLI.
- `worktrees` is the root for item worktrees, and must be separate from any runner's.
- `work_command` is how a worker session runs `phx work` from its worktree. The default is `python -m phalanx work`.
- `shift_command` is the concrete `phx shift` command, including its config and anything that loads the runner token, which `shift handoff` names in the successor's kickoff. The default is `python -m phalanx shift --config <this config's path>`.
- `session_workspace` is the runtime's workspace for the `worktrees` folder, where every worker session opens. In the GitHub Copilot app it is the ID `add_repository` returns for that folder.

`shift launch` includes that repository's exact `checks_environment` in the writer kickoff. When omitted, the kickoff explicitly says it is not configured; it never guesses a Python or environment. The worker prompt requires checks to use that environment from the item's worktree, forbids borrowing another checkout's environment or installing packages, and tells the writer to report the validation gap and request the correct environment with `phx work question` if it is missing or unusable.

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
max_writers = 4
```

`[shift]` is optional. Its positive-integer settings default to `max_hours = 4`,
`max_wakes = 48` (D7), and `max_writers = 4`. Configure the concurrent-writer cap only
with `max_writers`, not in `allowed_actions` prose. It counts registered intents and
confirmed writers on open executions in the charter's repositories, across Captains
(including predecessors). Idle or waiting writers still count. Completion, failure,
cancellation, withdrawal and replacement release the old writer's place; replacement
does not consume an extra place. Registration checks the cap atomically, so concurrent
launches cannot overfill it. Replaying a launch remains safe even at the cap.
If the cap is reached, launch refuses with a wait message naming the cap and active
writers; retry when a place opens, without escalating or raising a flag. Lowering the
cap does not stop existing writers. `shift status` prints `active_writers` against
`max_writers`. `shift charter` validates the file and prints its revision, the SHA-256
of its bytes. Each heartbeat re-reads the charter and records that revision.

### Commands

| Command | Does |
| --- | --- |
| `shift prompt captain\|keeper\|worker` | Prints the Captain prompt (the fixed wake loop), the keeper prompt, or the worker prompt template, which a Captain quotes when a writer's kickoff predates a change. Needs no config |
| `shift charter` | Validates the charter and prints it with its revision |
| `shift start --session-ref S [--parent ID]` | Registers this chat as a Captain. A rerun replays the same registration |
| `shift heartbeat --captain ID [--wake] [--state] [--focus] [--session ID NAME]... [--no-sessions] [--handoff-note]` | Re-reads the charter and heartbeats. `--session` reports each of the runtime's `phx ` sessions, and the server flags any that no writer or Captain owns. Omitted fields keep their last value. Prints `shift.handoff_due` once a shift bound is reached |
| `shift status [--captain ID]` | Flags, Captains and the charter's open items with their writers; `--captain` also reads that exact Captain and its cleanup, even outside the recent list |
| `shift launch --captain ID --item ITEM [--replaces WRITER]` | Prepares the item worktree, saves the writer credential in its Git directory and registers the writer intent. Prints `session_name`, which holds the nonce, and the worker's `kickoff` prompt. A rerun reuses the saved nonce, so a lost reply never launches twice |
| `shift writer --captain --item --execution --writer [--session-id] [--session-url] [--state] [--abandon]` | Confirms a launched session or reports its state |
| `shift withdraw --captain --item --execution --writer --reason [--stopped]` | Ends a writer whose session is stopped or never launched |
| `shift action --captain --item --action --reason` | Puts a Captain action on the item's timeline |
| `shift handoff --captain ID --note NOTE` | Starts ending the shift. Prints the open intents still owned, the successor's `session_name` and `kickoff`, and any registered successors |
| `shift retire-reconciler RUNNER_ID` | Deletes a decommissioned reconciler's heartbeat, which clears its `reconciler_stale` flag |
| `shift archive-check --session ID --by SESSION (--item ITEM \| --worktree PATH \| --no-worktree)` | Checks whether the session's writer/handoff has ended and local work is preserved. Fetches the actual origin branch and refuses dirty worktrees or unpushed commits. Does not archive |
| `shift archive-result --session ID --by SESSION --outcome archived\|blocked\|refused --reason REASON` | Records the runtime outcome durably. A block or refusal keeps `orphan_session`; only verified archival clears it. Refusals suppress further attempts |

### Creator-owned session cleanup

The runtime's session tools, not Phalanx core, CLI or API, archive sessions. Each Captain archives
the writer sessions it **created**, after the execution is completed, failed or cancelled, or an
abandoned/replaced writer has ended and its runtime session is confirmed stopped. Idle, waiting,
stale and trust-blocked sessions are not finished. `shift status` includes each Captain's
`ended_writers`, including writers on closed items absent from the open-item list, and its
`cleanup_pending` count.

Session archival adds schema migration 18. Deploy it through a drained upgrade of an existing
store before resuming Captains and writers.

Immediately before the runtime archive call, the creator runs `shift archive-check` with the
writer's `--item`. The check inspects the verified item worktree, fetches its origin branch and
requires every local commit to be on that remote branch. Tracked or staged changes, untracked
non-ignored files, hidden index changes, in-progress Git operations, a missing remote branch and an
unavailable Git check all prevent archival. Gitignored build output does not block archival.
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

1. Start a new chat. Ask it to run `python -m phalanx shift prompt captain --config <shift config>` and follow the printed prompt. Its first wake registers it with `shift start --session-ref <its session ID>`.
2. Have the Captain wake every 5 minutes (D5) with a session automation on its own chat (`save_session_automation`, interval `minutes`, `every_minutes` 5). Use the prompt "Wake: run `python -m phalanx shift prompt captain --config <shift config>` and follow its Every wake steps." Idle notifications from its worker sessions wake it sooner.
3. To launch a worker, the Captain runs `shift launch`, then creates one session with `create_session`:
   - `project_id` set to the returned `session_workspace`: the shift config's folder project for its `worktrees` folder. One project covers every item worktree, so the owner trusts it once. Registering each worktree as its own project would hold every launch on a trust prompt, and autopilot denies tools until it is answered;
   - `name` set to `session_name`;
   - kickoff `prompt` set to `kickoff`, which names the item worktree, with mode `autopilot`.

   It confirms the session with `shift writer --session-id <session ID> --session-url ghapp://sessions/<session ID>`. `list_sessions_and_chats` finds a lost launch by its name, which holds the nonce.
4. Run the keeper (D8) as an app automation that starts a new session every 30 minutes (`save_workflow`, `cron_expression` `*/30 * * * *`, mode `autopilot`) with the prompt "Run `python -m phalanx shift prompt keeper` and follow it. `phx shift` means `python -m phalanx shift --config <shift config>`. Create the Captain session in project `<session_workspace>`." Before any early exit, it checks cleanup of Captain sessions that this keeper session created. It starts no new Captain when a live one exists, the most recent ended less than 10 minutes ago without a successor yet, or it stopped with `Stop condition met:` under the current charter revision. Otherwise it starts a Captain. An automation run that can't schedule itself creates the Captain with `project_id` set to the shift config's `session_workspace`, the name `phx captain keeper` and a 5-minute `schedule`. That same keeper creator must retain a cleanup wake or child-ended notification until its Captain hands off cleanly; a fresh scheduled keeper session has no creator authority over it.
5. The app may let only a session's creator archive it. Captains clean up their own writers and helpers; keeper runs clean up their own Captain children. All use the preservation check and durable result above, not repeated attempts by successors. Missing authority is recorded and escalated once, with `orphan_session` left visible.

A Captain's shift ends when the charter's `[shift]` bounds are reached, at its first compaction or when it degrades. It runs `shift handoff`, which marks it `ending` (at most 30 minutes) and returns a successor name, `phx captain <id prefix>`, and its kickoff. The Captain starts that chat, finishes or abandons its unconfirmed launches, cleans up its ended children, and heartbeats `--state ended` without waiting for the successor. It then stops coordinating, retaining only creator cleanup if necessary. Captain states only move forward: `active`, `ending`, `ended`. A Captain that stops heartbeating for 15 minutes, or stays `ending` past 30 minutes, is stale, and any live Captain or the keeper takes over its unconfirmed launches; this does not authorize archival of unfinished work.

Watch the run on the [operator board](work-items.md#operator-work-board), which shows each item's writer, the live Captains and the flags. `GET /v1/captains` and `GET /v1/flags` return the same data.
