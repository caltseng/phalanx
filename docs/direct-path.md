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

For an existing installation, use the [service-owner adoption procedure](deployment.md#existing-runtime-reviewed-adoption-not-a-rename),
preserving reconciler identity, scratch path, credentials and active writers.

### Post-merge deployment status

For a repository that deploys from its base branch, optionally set `"post_merge_check": "deploy"` in its reconciler mapping. Use the exact check-run name of the post-merge deployment job (for example your GitHub Pages deploy job), not a PR build check. The reconciler then also polls recent executions checked into the mapping's `base_branch` and reads that check on each PR's **merge commit**, never its PR head or the latest base tip.

- During the 24-hour window after check-in, the board shows a derived `deployment_pending`, `deployment_failed` or `deployed` flag. Missing or running checks are pending; only a successful completed run means deployed. Other conclusions, including skipped or cancelled, mean failed. The newest run wins when a job is rerun before success.
- Polling stops on success or at the end of the window, and closed-page enumeration stops at the window boundary instead of walking all history.
- After the window, successful flags disappear, failed flags remain, and a still-pending deployment becomes one owner-notifying `deployment_missing` flag. Closed executions never raise `observation_stale`.
- Each heartbeat reports the mapped repositories' signal configuration. Removing `post_merge_check` retires their previous deployment signals, including expired failures, without GitHub reads or changing execution state.

Unconfigured repositories keep the existing behavior. This is GitHub observation only: check-in still completes on merge, dependencies unblock, and no deployment stage or agent-runtime integration is added. Deployment reads start after check-in is recorded, so an unavailable deployment-check API can't hold up delivery. The design is in [direct-session reconciliation](direct-sessions.md#github-reconciliation).

## Periodic GitHub issue scouting

Scouting starts **disabled**. The service owner must first adopt the reviewed API and
Captain client together. Existing deployed schema-24 stores require the complete,
record-preserving [offline 24 -> 26 conversion](development.md#existing-store-conversion);
the same explicit tool accepts an already-converted schema-25 snapshot. Startup does
not migrate, reset or replace live data. Rehearse on a disposable snapshot, including
authenticated writer continuation, board reads, integrity, exact retained rows, capacities
and credentials. Only the designated live-service owner performs the quiesced cutover.

On the Captain's host, keep `PHALANX_GITHUB_TOKEN` in the private environment, with read
access to the selected repositories. The token must represent the explicitly configured
author, verified via GitHub `/user` on every page; do not infer identity from a machine's
login, repository owner or issue body. No token is stored in the API settings or diagnostics.
Inspect `GET /v1/issue-scout`, then owner-authenticated
`PUT /v1/issue-scout/settings` with the returned `settings.revision`:

```json
{
  "expected_revision": 0,
  "enabled": true,
  "cadence_minutes": 60,
  "author_login": "your-explicit-github-login",
  "author_id": 12345,
  "repositories": ["your-registered-repository-id"]
}
```

Replace both identity fields with the actual authenticated login/numeric ID and explicitly
select registered github.com repositories. Cadences are 15, 30, 60, 180, 360, 720 or 1440
minutes after a full pass. Five-minute Captain wakes remain the only timing mechanism:
each existing wake advances at most one 30-entry created-order page, including closed
issues for reconciliation and excluding PRs. Large backlogs therefore span multiple
wakes; due does not promise a full pass within one wake. A live active Captain must cover
the full configured repository scope. No separate scout session or replacement Captain
is started, and scouting consumes no writer capacity.

Claims are server-fenced for ten minutes. Page intake/cursor updates commit atomically;
lost responses, restart and Captain handoff resume the same durable pass/cursor.
Provider/credential failures retain progress and retry no sooner than five minutes later.
An uncertain API response leaves its claim intact until expiry, rather than asserting
an empty scan or repeating intake. Settings changes fence the old pass and start the next
configured pass without removing source/decision history. Disabling settings stops new
scouting effects, not already admitted work.
The wake reports a separate redacted `observation_error: api` for an API outage; this
does not invalidate its already-recorded heartbeat or require Captain replacement.

Configured-author open issues enter ordinary intake with the normal merged target,
source URL and an explicit untrusted-context criterion. Pauses, both capacity limits,
repository readiness, dependencies and later steering apply through existing dispatch.
Existing issue keys and historical goal/context URL links, including completed, failed
and cancelled work, are reconciled on first sighting. Later observations reuse the retained
source binding and check stable manual-intake keys only while unlinked, without rescanning
work history. Add historical goal/context links before the first sighting; use manual
`ingest-issue` with its default issue key for later imports. Edits, closure or reopening never
change that work or automatically create a rerun. Historical work with **no issue URL
or source identity** cannot be reliably inferred: add the source link to authorized
queued work before activation or reconcile it explicitly with the owner.

Other-author issues appear in Scribe **Needs you** with their source link, author and
triage reason. Approve/decline persists an owner-attributed, fingerprint-bound decision.
Approval is **not** queued intake: a later pass re-fetches the issue; changed content
requires approval again, and closed issues are skipped. Declines remain declined across
edits/reopening. The owner API equivalent is
`POST /v1/issue-scout/issues/<repository-id>/<number>/decision` with
`{"expected_fingerprint":"<current fingerprint>","decision":"approved"}` (or `"declined"`).
The dashboard's narrowly authorized local action keeps same-origin checks.

Scribe and `shift status` expose configuration, due/running state, durable cursor, last
completion, classified failure/retry, and imported/needs-owner/approved/skipped counts.
Only metadata and digests enter scouting diagnostics; raw issue bodies are stored only
in admitted work. Neither issue content nor a coordinator message grants permission to
change identity, scope, configuration or to approve another author's issue.

## Run a Captain on the direct path

A Captain is an agent chat in any runtime that can start visible sessions. It launches one native worker session per item, registers it as the item's writer and coordinates it. It never does item work. The worker posts its own stage results with `phx work`, and the reconciler observes GitHub.

The Captain's commands are `python -m phalanx shift ...`. They need `PHALANX_RUNNER_TOKEN` and a shift config, passed with `--config` (before or after the action) or set in `PHALANX_SHIFT_CONFIG`. Alternatively, the [per-user muster locator](configuration.md#direct-muster-discovery-and-status) derives `shift.json`, `charter.toml`, `reconciler.json` and `private.env` from an existing muster directory (`--home`, then `PHALANX_HOME`, then `~/.phalanx/muster.json`). With that layout, `shift status`, `reconcile` and `muster status` need no explicit config paths. Discovery/status never initialize or migrate state.

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

For `copilot-app` and `codex`, `shift start` records a UUID native session reference
as `ghapp://sessions/<UUID>` or `codex://threads/<UUID>` respectively, so Scribe can
link to the correct app without guessing. Other runtimes and opaque references
are preserved. Canonical references still match the native ID for ownership and cleanup.
The server never opens an app or starts a conversation for a link.

After an explicit native session lookup, a heartbeat may report
`--session-availability ID present|missing|deleted` alongside its matching `--session ID NAME`.
The API stores this optional evidence in the existing session inventory. Scribe labels
present as last reported, disables known missing/deleted links, and labels other recorded
links unverified. Do not infer missing/deleted from an incomplete session listing or from
writer termination. Keep an explicit unavailable entry in subsequent inventories until
native inspection supersedes it. The CLI includes the configured runtime on inventory
entries so observations never cross host apps that happen to use the same native ID.

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
allowed_actions = ["launch, confirm, replace and withdraw writer sessions", "answer routine questions", "coordinate delivery through each current target, including checked_in merges under repository rules"]
escalate = ["scope changes", "destructive actions", "missing credentials or authority"]
stop_when = ["the owner ends the pilot"]
notes = "Own progress and outcomes on each scheduled wake and material work event: assess evidence, proactively unblock within existing authority, and verify the effect."

[shift]
max_hours = 24
```

Interpret charter authority under the [Captain workflow](../phalanx/prompts/captain.md#authority-and-shift-lifetime)
and execution delivery under the [worker policy](../phalanx/prompts/worker.md#merge-decision-publication-and-delivery).

`shift charter` validates the file and returns its SHA-256 revision; each heartbeat
re-reads it. Concurrency keys are rejected in charters: configure persisted Building/Total
limits through Scribe or the owner dispatch settings API. See [dispatch settings](../SPEC.md#settings-and-dispatch-gates)
and [writer capacity operations](writer-operations.md). Capacity exhaustion is a normal wait,
not authority to raise limits, stop writers or infer Building from spare Total.

### Captain shifts and handoff

The [Captain prompt](../phalanx/prompts/captain.md) owns lifetime, wakes, outage recovery,
progress assessment, cleanup and succession. The [keeper prompt](../phalanx/prompts/keeper.md)
owns recovery startup. Print them with `shift prompt captain` / `shift prompt keeper`.
Pilot reports and decisions are historical evidence, not alternative operating policy.

Server contracts: Captain states advance `active -> ending -> ended`; `ending_at` is set
once. Fifteen minutes without heartbeat, or thirty minutes in `ending`, is stale.
Reinforcement stores the greatest acknowledged 90-minute interval; non-wake heartbeats do
not consume it. The [execution warning](../SPEC.md#execution-status-and-recovery) is a
separate read-time projection. See [deployment adoption](deployment.md#coordination-policy-adoption-boundary)
for installed-source and instruction-receipt evidence.

### Commands

| Command | Does |
| --- | --- |
| `shift prompt captain\|keeper\|worker\|forwarding` | Prints the Captain/keeper prompt (including durable off-shift forwarding), worker template, or just the forwarding section for retained chats. Needs no config |
| `shift wake --captain ID` | Records one wake, prints a compact briefing and the fresh charter, and includes the canonical workflow when a 90-minute interval is unacknowledged |
| `shift ack-reinforcement --captain ID --interval N` | Acknowledges only an interval actually read. Duplicate or older acknowledgements are harmless; future intervals are refused |
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
Normal owner-authenticated intake through either `POST /v1/items` or Scribe's **+** enters
Captain pickup by default when its repository is in the charter's `repositories`.
An item need not be named in charter notes, lanes or a handoff. No per-item charter edit
is needed. An out-of-scope repository remains excluded.

Captain start/heartbeat publishes a small `pickup` snapshot: repository scope derived from
the current charter, verified checkout readiness and concrete runtime blockers.
The API does not read local charter files or checkout paths.
`shift status --captain ID` exposes `pickup_candidates`, the discovery list to launch,
ordered by urgent/normal/low intake priority and FIFO within each priority,
and each unbound queued item's `pickup` decision. `excluded_items` exposes out-of-scope work
with its reason, never as a launch candidate. Scribe and operator API reads use the same
server decision and `wait_reason`. Named writer registration remains a trusted low-level
operation; normal Captain dispatch uses `shift launch`, which checks the discovery decision.

Lane order and mutual exclusion belong to the operator's charter and the Captain's
selection, not a separate server lane setting. For each runnable item deliberately deferred
by lane order, overlap or the writer cap, the Captain records
`shift action --captain ID --item ITEM --action held --reason "<concrete charter reason>"`.
Refresh each wake: a hold expires after 30 minutes, a version change, or its Captain
ending/becoming stale. It explains the board wait and suppresses dispatch-overdue warnings;
it does not hide informational backlog when the canonical writer limit is full or dispatch is paused.
A hold never reserves capacity. Captain discovery and `shift launch` respect active holds;
record the predecessor item ID and actual lane/owner constraint, not merely "not listed".
Existing persisted dependencies, dispatch pause, retry, Building/Total capacity and PR-rework
preference remain authoritative waits. Eligible intake with no writer and no exclusion
uses the 60-minute `captain_idle_with_work` (**Dispatch overdue**) flag; capacity
and pause retain the 30-minute `queue_backlogged` budget. Scope, uninitialized
authority and repository/trust blockers retain `pickup_blocked` on that 30-minute budget,
with the shared decision's reason/recovery and owner notification. This is not a
`captain_idle_with_work` handoff signal.

Checkout mappings are checked at start/heartbeat; missing mappings and origin mismatches
become visible repository blockers instead of preventing observation of the entire charter.
When native inspection finds a repository-wide trust block, publish it with
`shift heartbeat --captain ID --repository-blocked REPOSITORY "owner approval needed: ..."`:
ordinary heartbeats retain it. Only after native verification clear it with
`--repository-ready REPOSITORY`. Never grant trust on the owner's behalf. A writer already
awaiting trust keeps its session and allocation.

### Creator-owned session cleanup

The runtime's session tools, not Phalanx core, CLI or API, archive sessions. Each Captain archives
the writer sessions it **created**, after the execution is completed, failed or cancelled, or an
abandoned/replaced writer has ended and its runtime session is confirmed stopped. Idle, waiting,
stale and trust-blocked sessions are not finished. `shift status` includes each Captain's
`ended_writers`, including writers on closed items absent from the open-item list, and its
`cleanup_pending` count.

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
2. Have the Captain wake every 5 minutes (D5) with a session automation on its own chat (`save_session_automation`, interval `minutes`, `every_minutes` 5). Use the prompt "Wake: run `python -m phalanx shift wake --captain <your registered Captain ID> --config <shift config>`. Follow the briefing and fresh charter. When reinforcement is due, read the full canonical workflow, then run its acknowledgement arguments through the same configured CLI." Idle notifications from its worker sessions wake it sooner.
3. To launch a worker, the Captain runs `shift launch`, then creates one session with `create_session`:
   - `project_id` set to the returned `session_workspace`: the shift config's folder project for its `worktrees` folder. One project covers every item worktree, so the owner trusts it once. Registering each worktree as its own project would hold every launch on a trust prompt, and autopilot denies tools until it is answered;
   - `name` set to `session_name`;
   - kickoff `prompt` set to `kickoff`, which names the item worktree, with mode `autopilot`.

   It confirms the session with `shift writer --session-id <session ID>` and, when the
   runtime returns a link, `--session-url <that exact link>`. Do not construct a session
   URL from its ID.
   To recover a lost launch, list the full inventory, including renamed sessions, then
   inspect candidate kickoffs/metadata for the exact `session_identity.writer_nonce`. Names and
   goals may collide and titles may change; neither is identity. If inspection is unavailable or
   inconclusive, retain the intent and report the blocker instead of launching twice.
   Finish this reserve/create/confirm handoff before reserving the next item. On an
   interruption, recover on the next wake rather than waiting for the ten-minute alert:
   confirm a full-nonce match from fresh full inventory. If none exists and no create is
   in flight, replay `shift launch`, verify the same writer ID/full nonce, then create
   and confirm once. Do not withdraw/re-reserve just to recover the interruption.
   Missing replay credentials or incomplete inventory are explicit recovery blockers,
   never permission to create another writer.
4. Run the keeper (D8) as an app automation that starts a new session every 30 minutes (`save_workflow`, `cron_expression` `*/30 * * * *`, mode `autopilot`) with the prompt "Run `python -m phalanx shift prompt keeper` and follow it. `phx shift` means `python -m phalanx shift --config <shift config>`. Create the Captain session in project `<session_workspace>`." Before any early exit, it checks cleanup of Captain sessions that this keeper session created. It starts no new Captain when a live one exists, the most recent ended less than 10 minutes ago without a successor yet, or it stopped with `Stop condition met:` under the current charter revision. Otherwise it starts a Captain. An automation run that can't schedule itself creates the Captain with `project_id` set to the shift config's `session_workspace`, the name `phx captain keeper` and a 5-minute `schedule`. That same keeper creator must retain a cleanup wake or child-ended notification until its Captain hands off cleanly; a fresh scheduled keeper session has no creator authority over it.
5. The app may let only a session's creator archive it. Captains clean up their own writers and helpers; keeper runs clean up their own Captain children. All use the preservation check and durable result above, not repeated attempts by successors. Missing authority is recorded and escalated once, with `orphan_session` left visible.

Follow [Captain shifts and handoff](#captain-shifts-and-handoff) for runtime-neutral
lifetime, reinforcement, recovery and handoff rules.

### Retained chats and owner-message forwarding

The [forwarding prompt](../phalanx/prompts/forwarding.md) owns routing, provenance,
display naming and retirement. Captain startup owns [dashboard handling](../phalanx/prompts/captain.md#first-wake).
The API derives `dashboard_url` from the configured base plus `/operator/`, including host,
port and path. `shift current` resolves across all Captain history, not just recent rows.

**Deployment boundary:** forwarding is tracked in `phalanx/prompts/forwarding.md`, composed into
`shift prompt captain` and `shift prompt keeper`, and included in generated handoffs.
Deploy both API and client from the reviewed revision through the normal service-owner rollout;
an old server will not serve `shift current`. No private muster instruction patch is required.
Title retirement uses existing client prompts and native tools; it adds no API, schema or runtime
election change and needs no service restart. Existing chats do not retroactively receive new prompts:
explicitly relay the changed section
verbatim from `shift prompt forwarding` to retained Captain/keeper chats via supported messaging.
Do not claim that merge/deployment updated those chats or that the relay was delivered without
runtime evidence. This item does not itself grant authority to edit unrelated retained chats.

Watch the run on the [operator board](work-items.md#operator-work-board), which shows each item's writer, the live Captains and the flags. `GET /v1/captains` and `GET /v1/flags` return the same data.
