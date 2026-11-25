# Versioned deployment tooling

`python -m phalanx deploy` provides installation templates, literal environment loading,
foreground service entrypoints and a CLI wrapper. It does not launch Captains/writers,
schedule tasks, grant trust, migrate directories or restart live services. Native direct
sessions and the existing reconciler remain the only execution path.
The [deployment diagram](architecture.md#deployment-and-recovery-boundaries) shows the
API service, native runtime and reconciler boundaries. Tooling merged in
[#220](https://github.com/caltseng/phalanx/pull/220); that is not evidence that an existing
installation has adopted it. Keep installation state in the operator's private runbook.

## Standing authority for compatible upgrades

Routine compatible reviewed/checks-green upgrades are executable by the authorized service operator
under standing owner deployment authority; do not ask for routine permission again or depend on a
particular Captain, keeper or native session. This is scoped authority, not permission for an item
writer to use a Captain token or stop services it does not own. If standing authority or exact
service ownership cannot be verified, name that concrete blocker and responsible operator.
Destructive operations, possible data loss, schema downgrades, new services and credential-scope
changes remain explicit owner decisions. Repository trust/privacy and required review/CI remain hard.

Use the existing supervisor and deployment tooling, not a new installer, watchdog or polling loop:

1. Pin the reviewed/checks-green merge revision and previous installed revision; inventory the
   exact API database/URL/config and supervisor identity privately. Record authenticated status,
   active Captain/writer bindings, attempts, nonces, capacity settings/counts and reconciler identity.
   Coordinate the concrete outage with the current Captain. Pause admissions only when the schema
   or protocol requires it; do not stop healthy native sessions or serialize unrelated development.
2. Take a fresh supported `server backup` to a new private destination and validate its integrity
   and expected records. Rehearse the **complete** old-installed-to-new-pinned schema transition on
   a disposable copy, including all intervening additive schema steps and built-in migrations, not
   just the latest column. Start/probe the pinned candidate against that copy in isolation without
   live credentials, GitHub writes or native launches; verify preserved rows, versions, allocations
   and integrity. Record the rehearsed revisions and steps. Missing migration knowledge, an invalid
   backup or an unexplained rehearsal difference blocks this upgrade; do not improvise live SQL.
3. Keep the pinned previous package/config and a proven compatible rollback path. Stop/restart only
   the **exact API service** through its owning supervisor (and identified child when required).
   Preserve the original URL, database, credentials and board settings; never use process-name kills,
   select a service merely by port, run a second API, initialize/reset live state, or replace writers.
   Leave the existing reconciler, keeper/Captain schedules and credentials untouched for API-only
   upgrades. A separately required reconciler upgrade uses its own exact owner and sole-instance
   stop/start procedure, never an incidental restart.
4. Apply only the rehearsed transition, install the complete pinned revision and restart through the
   same supervisor. Check health and authenticated owner/runner identity, preserved bindings/attempts/
   nonces/capacity, one intended API and the existing reconciler's fresh heartbeat. Exercise affected
   read/control routes without fabricating live work. Verify normal writer API continuity/retries.
   Restore admissions to the recorded owner setting if this rollout paused them.
5. On failure, stop the candidate through that same exact owner and restore the pinned previous
   package/config only if it can safely use the resulting schema. Never restore an older database
   over active writers or hide a schema downgrade inside rollback. If compatibility is not proven,
   keep the affected API safely stopped, preserve evidence and ask for an owner recovery decision.
   An upgrade without a recoverable rollback plan must not start.

Record source merge, installed package/API revision, schema/rehearsal/backup verification, continuity
results and retained-session receipts separately. **Source merge is not a deployed result.** A
successful rollout emits a material release event to the current Captain for bounded reconciliation
and eligible-capacity refill; it is not permission to increase limits.

## Helper inventory and ownership

The existing deployment was inspected read-only. No private values or live state are copied
into the repository. Adopt these replacements **after review**, with the service owner:

| External helper | Tracked replacement / disposition |
| --- | --- |
| `server.ps1` | `deploy run server`: configured loopback address/port, explicit database and private environment; stdout/stderr go to the host supervisor. |
| `reconcile.ps1` | `deploy run reconciler`: delegates to the existing `phalanx reconcile` loop and its scratch-directory lock. The service manager, not a second script loop, restarts an exited process. |
| `phx.ps1`, `phx.cmd` | `deploy cli -- <arguments>`: loads the configured environment and invokes the installed CLI, preserving arguments, current directory and exit status. |
| `ensure-services.ps1` | Replace process-name, port and recent-log guesses with **one** operator-owned supervisor for each service. See below; do not run the old watchdog alongside the new supervisor. |
| `projects.py` | Retire the app's private SQLite registry query. Use the runtime's supported repository/workspace discovery tools and configure the returned workspace ID locally. No app storage schema belongs in Phalanx core. |
| `trusted.py` | Retire the ad-hoc commented-JSON trust probe. Use runtime-reported repository trust; only the owner grants it. CLI path trust is not app project trust. |
| prepared npm environment's `checks.ps1` | `scripts/checks.ps1 -EnvironmentRoot <absolute prepared environment> <command> ...`; retains normalized lock verification and worktree junction behavior without a repository-specific implementation. |

Database snapshots, validation-policy backups, temporary API responses, logs, credentials,
existing worktrees and other historical runtime data are **not deployment tooling**. Leave
them alone. Do not copy the runtime tree into git.

## New installation

Install the package and locked dependencies as in [Install Phalanx](install.md), then run:

```powershell
python -m phalanx deploy init
# Or select a new absolute directory and another port:
python -m phalanx deploy init --home D:\.phalanx_muster --port 8765
```

The default is `~/.phalanx_muster`, on every OS. Explicit `--home`, then `PHALANX_HOME`,
override that default. An existing directory is always refused, even if empty; initialization
never updates an existing install or follows the discovery locator. `--port` generates
matching server/client defaults; `--api-url` separately overrides client URLs (for a
reverse proxy or hosted API). API URLs must use HTTPS or loopback HTTP without credentials.
Server launch deliberately binds only loopback; configure a TLS proxy for hosted access.

Generated files contain no credentials, database or live state:

- `deployment.json`: absolute private environment, shift config, reconciler config and
  scratch paths, plus server host/port.
- `private.env.example`: paths under the chosen home and commented credential keys.
  Copy it privately to `private.env`, restrict it to the service account (POSIX mode 600;
  on Windows a private account ACL), and supply the once-generated owner/runner tokens
  and GitHub token. The new directory requests mode 700 on POSIX; Windows permissions
  must be checked by the owner before saving secrets.
- `shift.json`, `charter.toml`, `reconciler.json`: edit the runtime, native workspace,
  repository mappings, goal and stable reconciler ID. The charter template sets only
  `max_hours = 24` under `[shift]`, generated from the same
  defaults the Captain client uses. Fill its repository list and
  corresponding mappings before use. Concurrency comes from persisted
  `dispatch_settings.max_building_writers` (Building, initially 5) and `max_writers`
  (Total, initially 10 for new installs), editable together by the owner in Scribe or
  through the dispatch settings API, not from the charter.

## Two-limit writer activation

Deployment is not activation. Do not raise the old single live limit to 10 first.
The authorized service owner coordinates these steps with the live Captain
after owner review and merge:

1. Hold new launches and preserve existing workers, sessions, worktrees, attempts and settings.
   Rehearse the appended migration on a database copy and verify integrity, unchanged Total,
   Building=min(5, Total), writer identities and all live allocations conservatively Building.
2. Deploy the API, dashboard, CLI and updated Captain/writer prompts together using the existing
   drained service procedure. Migration never raises an existing store's Total or resets owner
   edits on subsequent starts. Do not release Building simply because an old worker is in delivery.
3. Send the changed protocol verbatim to every live Captain and writer (or safely drain them
   while retaining work). Confirm that code changes, self-review and substantial tests require
   a granted `work capacity building`, including delivery-stage fixes. Never release an allocation
   while that writer is still building. The writer explicitly returns to `capacity delivering`
   once local work has finished.
4. Separately authorize activation after the protocol is enforceable. Save the atomic pair
   `{"max_building_writers": 5, "max_writers": 10}` through Scribe or owner-authenticated
   `PUT /v1/dispatch/settings`. Preserve any newer owner decision rather than overwriting it.
   Verify both saved limits and Building/Delivering/Total counts, resume launches and observe
   full-Building/full-Total waits plus PR-fix reacquisition before declaring activation complete.

If protocol rollout cannot be confirmed, retain the previous Total and conservative allocations;
report the gap instead of silently enabling the larger pool. No writer is killed or abandoned
by deployment, lowering settings or waiting for capacity.

See [direct-path configuration](direct-path.md) for mappings, prepared checks environments
and runtime schedules. Do not put credentials in the JSON configs. Discovery is optional:
pass `--home` to deployment commands, set `PHALANX_HOME`, or create the existing
`~/.phalanx/muster.json` locator pointing at the new home. The locator's directory/name
does not change.

```powershell
# Only for a NEW installation, after configuring private.env:
python -m phalanx deploy run --home D:\.phalanx_muster server --initialize-database
# In a second shell, after health/authentication and repository setup:
python -m phalanx deploy run --home D:\.phalanx_muster reconciler --once
python -m phalanx deploy run --home D:\.phalanx_muster reconciler
python -m phalanx deploy cli --home D:\.phalanx_muster -- shift status
```

Remove `--initialize-database` from all persistent server commands. Normal startup refuses
a missing database instead of silently initializing an empty replacement. The initialization
flag refuses an existing file. Uvicorn performs the existing server schema checks; deployment
tooling never opens or changes queue storage itself. Each supported server holds a
database-adjacent OS lock for its whole lifetime and uses one Uvicorn worker. The reconciler
retains its own existing scratch-directory lock.

Both services remain foreground processes. Configure **one** host service manager
(Windows service supervisor, systemd or launchd) to run each command with the absolute
installed Python executable, a fixed working directory outside repositories, the service
account's environment, restart-on-failure/backoff and private stdout/stderr log retention.
Stop the supervisor and its exact owned process tree before replacing a command. Do not
stack a polling task, PowerShell restart loop and service-manager restart policy. The
database lock coordinates these entrypoints, not arbitrary preexisting raw Uvicorn
commands; inventory and retire those with the service owner first.

### Windows API scheduling under host load

Configure the existing API task with Normal process priority (Task Scheduler priority 5),
not the default task priority 7 (BelowNormal). Verify the actual listener **and** any
virtual-environment launcher with `Get-Process -Id <verified-PID>`, checking `PriorityClass`
and `BasePriority`; inspect the exact owning task's `Settings.Priority`. A database-free
authenticated status request can stall when a BelowNormal API competes with sustained
Normal-priority host work. A fast database snapshot probe or occasional health response
does not rule this out.

The server entrypoint promotes only its own inherited Idle/BelowNormal Windows process
to Normal before starting Uvicorn, logging the correction. It preserves Normal and
explicitly higher classes, never selects High/Realtime, and fails visibly if Windows
rejects the priority operation. Other platforms, CLI commands, writers and the reconciler
are unchanged. Keep the supervisor setting correct as well so launchers inherit Normal;
the API does not edit scheduled tasks or other processes.

For an existing incident, verify exact task/process ownership before an operator changes
only that API task's priority and its live listener/launcher to Normal. This needs no
restart, schema conversion or queue mutation. Preserve the task's other settings and
record before/after authenticated control and genuine writer reads/writes under comparable
host load. Sustained recovery still requires bounded store admission and same-writer
continuity; neither scheduling priority nor `/health` alone proves those guarantees.

### Windows deployment-test teardown

The isolated deployment test must stop the service interpreter before waiting for its
launcher. On Windows, a virtual-environment `python.exe` can launch another interpreter:
terminating and waiting for the launcher's PID does not wait for that child's teardown.
The deployment test reproduced `WinError 32` deleting its own `.service.lock` on repetition
11. A diagnostic opening synchronization handles for only the launcher's direct children
observed `WAIT_TIMEOUT` (258) for the service child immediately after the launcher wait
returned. A later exact-installation process lookup found no persistent service process;
an orphan, antivirus interference or a broken lock implementation was not established.

The test now owns a stdin pipe into the interpreter running the real deployment CLI.
Closing that pipe raises SIGINT inside that interpreter, letting Uvicorn shut down and
the deployment context release its lock before the launcher exits. It asserts completed
application shutdown, successful process exit and immediate service-lock reacquisition
after each stop, while retaining real bootstrap, restart and duplicate-owner checks.
The pipe is test-only: production service behavior and lock ownership are unchanged.
Cleanup errors and shutdown timeouts remain failures; no process-name termination,
shared temporary-root deletion or live-service intervention is part of this procedure.

## Environment and local shims

`environment_file` accepts the same literal `KEY=VALUE` syntax as muster discovery, or
an existing `.json` object of supported string settings. Supported keys are the owner,
runner and GitHub tokens, `PHALANX_DB`, `PHALANX_API_URL`, `PHALANX_HOME`,
`PHALANX_SHIFT_CONFIG` and `PHALANX_LOCAL_BOARD`. Unknown/duplicate keys and empty
values fail without printing values. No shell is evaluated. Inherited environment settings
take precedence; audit those on the service account before rollout. `shift_config` supplies
`PHALANX_SHIFT_CONFIG` only when not already set. Preserve any explicit existing value.

Git/GitHub credentials and noninteractive credential-helper setup remain service-account
configuration, not hard-coded shell injection. The reconciler uses `PHALANX_GITHUB_TOKEN`
for provider reads and intentionally sanitizes inherited `GIT_*` overrides for its git
operations; configure the account's normal Git credential helper when needed.

No wrapper is required. If a local alias or scheduled task needs one, its only behavior
should be delegation, for example:

```powershell
# Machine-local phx.ps1; paths supplied by the operator:
& $env:PHALANX_PYTHON -P -m phalanx deploy cli --config $env:PHALANX_DEPLOYMENT -- @args
exit $LASTEXITCODE
```

Service tasks call `deploy run --config <absolute deployment.json> server` or `reconciler`
directly. Keep only settings, credentials and such minimal entrypoint shims outside git.
The installed package (or operator-pinned checkout for `scripts/checks.ps1`) is the
single source of reusable behavior.

For a prepared Windows npm environment, keep only the dependency installation, browsers,
`lock.sha256` and optionally a shim outside git. The shim delegates to the pinned tracked
`scripts/checks.ps1`, passing `-EnvironmentRoot $PSScriptRoot` and `@args`. The tracked
script verifies the worktree lockfile's UTF-8 SHA-256 after CRLF-to-LF normalization,
refuses stale/missing dependencies and mismatched existing `node_modules`, and creates
a junction only when absent. It sets `PLAYWRIGHT_BROWSERS_PATH`, preserves the worktree
working directory and returns the command's exit code. It never installs dependencies.
Keep the junction untracked. On other platforms use an operator-prepared checks prefix
appropriate to that repository; the Windows junction helper is not a portable installer.

## Existing runtime: reviewed adoption, not a rename

Existing `phalanx-muster` homes, explicit `captain.json` paths and current service/worktree/
session paths remain valid. **Do not run `deploy init` on the live home, rename it, change
the discovery locator, or relocate the database to adopt the new spelling.** The raw API's
default `.phalanx/phalanx.sqlite3` path remains unchanged; supported
new setup writes an explicit absolute path under `.phalanx_muster` instead.

1. Before review, inventory only: record the owning account/supervisor, exact API
   URL/port and database path, private environment source, stable reconciler ID/config/
   scratch path, shift config/charter and its current writer budget, workspace,
   worktrees, active writer registrations/nonces and current native sessions. Keep private
   values and identities in the operator's runbook, not this repo or a PR.
2. Prepare a **new local** `deployment.json` by filling this shape with the existing paths;
   do not copy private values or rewrite the existing configs:

   ```json
   {
     "environment_file": "<absolute existing private env.json or private.env>",
     "shift_config": "<absolute existing captain.json or shift.json>",
     "server": {"host": "127.0.0.1", "port": 8000},
     "reconciler_config": "<absolute existing reconciler.json>",
     "reconciler_state": "<absolute existing reconciler scratch directory>"
   }
   ```

   Run `python -m phalanx deploy check --config <that file>`. This is a read-only
   path/credential-presence check, **not** a connectivity, schema or GitHub validation.
   It never starts services, opens SQLite or alters configs. Do not add/change
   `PHALANX_HOME` in an existing API until its owner has reviewed the identity change;
   explicit configs work without it.
3. After review and required checks pass, the authorized service operator executes rollout under
   [standing authority](#standing-authority-for-compatible-upgrades). Record authenticated
   `/v1/runner/status`, `/v1/owner/status` and writer snapshots, then take a supported
   `phalanx server backup` to a new private destination with the original database path
   and validate the backup as in [recovery](recovery.md). Rehearse the complete installed-to-candidate
   transition on a copy. Keep a pinned previous package
   and launcher config for rollback. Never log/export tokens.
4. Disable the old watchdog/restart owner first. Stop only the exact identified API
   and reconciler processes under that owner's control; leave all native writers,
   Captain/keeper schedules and writer credential files untouched. Do not register
   replacement sessions or reset attempts. Install the reviewed package in the
   operator-managed environment; do not update a writer's environment underneath it.
5. Start exactly one API with the original URL/port, database, tokens and local-board
   setting, **without** `--initialize-database`. Verify public health and both authenticated
   role probes. Start the reconciler with `--once` only after the previous instance has
   stopped; verify no errors and a fresh heartbeat for the same reconciler ID. Then
   start its sole persistent supervisor with the same scratch path. Do not run `--once`
   concurrently with the persistent reconciler.
6. Compare API/database identity, registrations and active writer nonces to the snapshot;
   verify configured writer capacity is unchanged and the Captain's existing schedules/sessions
   still point at the old paths. Use `muster status` only if that install has already
   configured matching API home identity; otherwise use explicit shift config and
   authenticated API status. Ensure there is one service owner and one reconciler
   heartbeat, then replace any retained wrappers with delegation-only shims.

If a probe fails, stop the new service with its owner before restoring the previous
launcher/package against the **same** paths and credentials. Do not restore an older
database over active writers; a schema downgrade needs a separate owner-approved
recovery window. Preserve logs, artifacts and historical data. A later offline relocation,
if desired, is separate work requiring all service and session owners; no automatic
directory migration is supplied.

## Elapsed-time shift activation

This change supersedes the earlier compaction-threshold and four-hour rollout.
The 24-hour default affects the per-command client and packaged prompts; reliable 90-minute
reinforcement additionally needs the API and one Captain acknowledgement column.
Keep the original database, private environment, credentials,
active writers/attempts, writer cap and sole server/reconciler unchanged. Updating a client
does not update instructions already loaded in retained native conversations.

After review/check-in, the authorized deployment owner:

0. Backs up with supported server-host `server backup` and validates the backup.
   Rehearses the following additive schema change on that backup, then schedules the
   controlled API-only rollout with its existing service owner. Stop just the exact API
   process through its supervisor, apply the column once using server-host administration,
   and restart the same API with the same configuration, database and credentials, without
   `--initialize-database`. Leave the reconciler and every native writer/Captain running;
   API calls during the short outage may need their normal retry. Never reset registrations,
   attempts, counters, limits or a dirty checkout. Advance the complete clean pinned deployment
   checkout to the reviewed merge commit, not a selection of runtime files. There is no new
   migration or old-schema fallback. Verify health, authenticated status and existing
   writer bindings after startup. A missing column/API is an activation blocker, not a
   reason to report reinforcement as active.

   ```sql
   ALTER TABLE captains ADD COLUMN reinforced_interval INTEGER NOT NULL DEFAULT 0
       CHECK (reinforced_interval >= 0);
   ```

   Existing Captains start with no acknowledged intervals, so an already-aged Captain gets
   one current reinforcement on its next `shift wake`, not a replay of every missed interval.
1. Records the deployed revision, validated charter revision/effective bounds, current
   Captain/native session binding, writer registrations and dispatch settings through
   existing authenticated controls. Confirms the client command's installed source and
   clean, pinned deployment checkout before advancing it to the reviewed merge commit.
   Preserve its existing branch/detached deployment style.
   Never install dependencies into a writer environment or reset a dirty checkout.
2. With charter-owner authority, removes `max_wakes` and `max_compactions` from `[shift]`,
   sets `max_hours = 24`, and replaces queue-completion stop conditions with explicit
   owner stop. Add a latest-policy note superseding earlier count-based handoff notes.
   Preserve unrelated authority, lanes and content. Uses
   `deploy cli --config <existing deployment.json> -- shift charter` to validate the
   charter against the new client; record effective bounds and revision. Old clients
   still supply retired count limits from their defaults even after keys are removed:
   a charter edit alone does not activate the new client behavior.
3. Relays the changed *Your charter*, *Every wake* heartbeat and *Ending a shift* sections verbatim from
   `shift prompt captain` to every retained coordinating Captain, and the changed live-Captain
   step from `shift prompt keeper` to retained keepers. Have each recipient acknowledge
   that normal rotation is now elapsed-time only and supersedes earlier queue/count-based
   stop instructions. Keep the existing registration and five-minute reconciliation schedule,
   not a 24-hour wake schedule. Replace the old full-prompt wake paragraph with:
   "Run the existing configured CLI's `shift wake --captain <current id>`. Follow the
   briefing and fresh charter. When reinforcement is due, read its full workflow, then
   run `reinforcement.acknowledgement_arguments` through that same CLI. Continue ordinary
   reconciliation every five minutes; preserve existing writers, owner stop/safety rules,
   current-execution target authority and repository protections."
   Preserve unrelated automation content. Future prompts/successor
   kickoffs load the tracked version. Do not revive ended Captains or launch replacements
   to update their instructions; before any later coordination they must load the current prompt.
4. The current Captain runs `shift wake` and acknowledges any workflow it actually read.
   Report trustworthy cumulative successful compaction telemetry separately on a non-wake
   heartbeat, or omit it when unavailable. Verify `shift.max_hours == 24`, absence of
   retired count-limit fields, age-only `handoff_due` and preserved known/unknown totals.
   An elapsed-age or genuine degradation handoff may
   still be due. Recheck writer identity/cap and service/reconciler continuity. Record
   the exact revision, charter, recipients and heartbeat time as the rollout boundary;
   until retained instructions are acknowledged and the existing automation uses `shift wake`,
   activation is only partial. Verify an immediate repeat does not repeat acknowledged
   reinforcement. Exercise future intervals only in fixtures, never by rewriting live clocks
   or registration timestamps.

Rollback changes only the client revision and charter under the same owner and requires
owner approval before restoring the superseded early-ending policy.
Relay changed instructions explicitly. Never roll back the database or fabricate
telemetry to exercise live rotation; boundary tests use isolated fixtures.

### Default intake pickup activation

This change extends the direct schema, without appending a store migration. Only the service
owner applies it to an existing installation, rehearsing on a backup before deployment.
Do not reset or replace the store. Preserve credentials, Captain/writer rows, attempts,
dispatch settings and history. Before starting the updated API against an existing store,
the owner applies this additive schema change once through controlled server administration:

```sql
ALTER TABLE captains ADD COLUMN pickup_json TEXT NOT NULL
    DEFAULT '{"repositories":{},"runner_id":null}';
```

The default backfills existing Captain rows with **unknown/no pickup authority**, not guessed
scope from a hash or old prose. No work-item backfill or per-item charter edits are needed.
After deploying the reviewed API and client, relay the updated Captain prompt verbatim to
retained coordinating Captains, and have each run its normal `shift heartbeat --captain ID --wake`
with the current config/charter. This populates scope/readiness without re-registering the Captain
or replacing its writers. Include a stable config `runner_id` for any pinned intake and publish
known native repository trust blocks with `--repository-blocked`. Verify `pickup_candidates`,
specific wait reasons and unchanged writer/capacity state through status and Scribe.
Keep deployment, schema application, external charter edits and retained-prompt activation
with their authorized operators; an item's scoped writer credential grants none of this authority.
Routine compatible deployment does not require a fresh owner decision when the standing-authority
procedure above applies.

## Coordination-policy adoption boundary

The tracked Captain/wake/keeper/worker prompts, review skill, charter template and operating guides
define the policy. There is no engine-enforced independent-review predicate, native event bus or new
scheduler in this change. Runtime material-event notifications use existing session messaging and
the Captain's bounded pass; scheduled wakes remain the fallback when the runtime cannot notify.

Check-in changes source only. The authorized operator must install the reviewed package/API before
new generated prompts and attempt-free delivery criteria steering use it. Existing charter notes,
schedules, kickoffs and retained conversations are not rewritten by a merge. Remove stale exclusive
review-routing prose from active private charter/automation templates under their existing owner,
preserving actual GitHub required reviewers/protections and unrelated authority. Relay the canonical
changed sections to retained Captains, keepers and writers using supported messaging, and record
recipient/revision/receipt; normal 90-minute reinforcement also refreshes Captain instructions.
Do not revive ended sessions or launch replacement writers for policy adoption.

### Outage-continuity instruction activation

Source delivery does not update retained chats or an installed template. Relay the canonical
Captain outage section and keeper native-verification rules to the current verified Captain
and existing keeper, and add the guard to their existing automation prompts without changing
cadence, bindings or authority. Require a readback/receipt; an already-running keeper may have
captured the old prompt and needs the same supported message. Do not start a Captain for adoption.

When an unrelated schema upgrade prevents package deployment, an authorized operator may apply
an independently reviewed **outage-only prompt patch** to the exact installed baseline: back up
the affected prompt files, verify the baseline and absence of conflicting edits, apply only the
reviewed hunks and verify the installed CLI's `shift prompt captain|keeper|forwarding` output
and packaged `wake` text through the same prompt loader (`wake` is not a CLI prompt choice).
Record baseline revision, patch digest, file hashes and recipient receipts separately
from source merge. Keep API Python code, database/schema/config/credentials, services, writers,
capacity and schedules unchanged. Old server stale/no-live recovery text remains undeployed
until the reviewed API can be adopted; the explicit prompt guard overrides that launch advice.
Restoring backup prompts must not silently restore the rejected outage-handoff policy: retain
the direct owner guard in existing automations/conversations while repairing activation.

For the owner's ordinary-target classification of Phalanx [#252](https://github.com/caltseng/phalanx/pull/252),
[#253](https://github.com/caltseng/phalanx/pull/253) and
[#258](https://github.com/caltseng/phalanx/pull/258), follow
[the versioned timing-work procedure](writer-operations.md#bounded-performance-work). Read actual
current state first: this document is neither a live mutation nor evidence that a timing target was
met. Record each accepted API receipt and retained-writer acknowledgement in the existing timeline/
operator runbook, retaining baseline/round measurements and explicit remaining misses. Until those
receipts, API installation and instruction adoption are verified, report activation as partial.

### API-outage continuity adoption

Source check-in alone does not activate the outage-continuity correction. This correction
changes packaged worker/Captain instructions, review guidance and CLI transport diagnostics,
not schema, lifecycle or API permissions; it needs no service restart by itself. Only the
authorized installation owner installs the reviewed revision through the existing deployment
procedure. Preserve unrelated installed hotfixes and prompt edits. Fresh generated kickoffs
use the installed template; retained writers/Captains need the canonical **Control-plane
outages** section and corresponding capacity/merge clauses explicitly relayed through supported
messaging. Record exact source/installed revisions, recipients and receipts separately.
Do not regenerate a retained writer with `shift launch`, revive ended credentials, or
represent a sent message as acknowledged adoption. Deferred Phalanx evidence remains pending
until its own authenticated receipts and actual GitHub reconciliation are verified.
