# Versioned deployment tooling

`python -m phalanx deploy` provides installation templates, literal environment loading,
foreground service entrypoints and a CLI wrapper. It does not launch Captains/writers,
schedule tasks, grant trust, migrate directories or restart live services. Native direct
sessions and the existing reconciler remain the only execution path.

## Helper inventory and ownership

The legacy deployment was inspected read-only. No private values or live state are copied
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
  repository mappings, goal and stable reconciler ID. The charter template sets
  `max_writers = 5`. Fill its repository list and corresponding mappings before use.

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

## Environment and local shims

`environment_file` accepts the same literal `KEY=VALUE` syntax as muster discovery, or
an existing `.json` object of supported string settings. Supported keys are the owner,
runner and GitHub tokens, `PHALANX_DB`, `PHALANX_API_URL`, `PHALANX_HOME`,
`PHALANX_SHIFT_CONFIG` and `PHALANX_LOCAL_BOARD`. Unknown/duplicate keys and empty
values fail without printing values. No shell is evaluated. Inherited environment settings
take precedence; audit those on the service account before rollout. `shift_config` supplies
`PHALANX_SHIFT_CONFIG` only when not already set. Preserve any explicit legacy value.

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

Legacy `phalanx-muster` homes, explicit `captain.json` paths and current service/worktree/
session paths remain valid. **Do not run `deploy init` on the live home, rename it, change
the discovery locator, or relocate the database to adopt the new spelling.** The raw API's
legacy `.phalanx/phalanx.sqlite3` fallback remains unchanged for compatibility; supported
new setup writes an explicit absolute path under `.phalanx_muster` instead.

1. Before review, inventory only: record the owning account/supervisor, exact API
   URL/port and database path, private environment source, stable reconciler ID/config/
   scratch path, shift config/charter (`max_writers = 5` for this deployment), workspace,
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
   `PHALANX_HOME` in a legacy API until its owner has reviewed the identity change;
   explicit configs work without it.
3. After the PR is reviewed, the **service owner** schedules rollout. Record authenticated
   `/v1/runner/status`, `/v1/owner/status` and writer snapshots, then take a supported
   `phalanx server backup` to a new private destination with the original database path
   and validate the backup as in [recovery](recovery.md). Keep a pinned previous package
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
   verify charter capacity remains five and the Captain's existing schedules/sessions
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
