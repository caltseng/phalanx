# Versioned deployment tooling

`python -m phalanx deploy` provides credential-free installation templates, literal
environment loading, foreground service entrypoints and a CLI wrapper. It does not launch
native sessions, schedule tasks, grant trust, migrate directories or restart live services.
See the [deployment diagram](architecture.md#deployment-and-recovery-boundaries) for ownership
boundaries. Keep installed revisions, private paths and rollout receipts in the operator's
private runbook, not this repository.

[Go cutover](go-server-release.md) is opt-in; Python remains default.

## Standing authority for compatible upgrades

Authorized service operators may execute routine compatible reviewed/checks-green upgrades
under standing owner deployment authority. Do not require repeated routine permission or a
particular Captain/session. This does not let item writers use Captain credentials or stop
services they do not own. If authority or exact service ownership is unknown, name the
blocker and responsible operator. Destructive operations, possible data loss, schema
downgrades, new services and credential-scope changes require explicit owner decisions.
Trust/privacy and required review/CI remain hard.

Use the existing supervisor and deployment tooling:

1. Pin the reviewed/checks-green candidate and previous installed revision. Privately inventory
   the exact API database/URL/config and supervisor, authenticated status, Captain/writer
   bindings, attempts, nonces, capacity and reconciler identity. Coordinate any outage with
   the current Captain. Pause admissions only when schema/protocol requires it; preserve
   healthy native sessions and unrelated development.
2. Take a fresh supported `server backup` to a new private destination and validate integrity
   and expected records. Rehearse the **complete** installed-to-pinned schema transition on a
   disposable copy, including every intervening step. Startup is not a general schema
   upgrader: use [existing-store conversion](development.md#existing-store-conversion) when
   applicable. Probe the candidate API against the copy without live credentials, GitHub
   writes or native launches; verify records, versions, allocations and integrity. Record
   revisions and steps. Missing migration knowledge, an invalid backup or unexplained
   differences block rollout; never improvise live SQL.
3. Retain the pinned previous package/config and prove a compatible rollback path. Stop only
   the **exact API service**, through its owning supervisor and identified child if required.
   Preserve URL, database, credentials and board settings. Never select by process name/port,
   start a second API, initialize/reset live state or replace writers. Leave the reconciler
   and native schedules untouched for API-only upgrades. A separately required reconciler
   upgrade uses its own exact owner and sole-instance stop/start.
4. Apply only the rehearsed transition, install the complete pinned revision and restart through
   the same supervisor. Check health and authenticated owner/runner identity, preserved
   bindings/attempts/nonces/capacity, one intended API and the existing reconciler's fresh
   heartbeat. Exercise affected routes and genuine writer continuity/retries without
   fabricating live work. Restore admissions to the recorded owner setting if paused.
5. On failure, stop the candidate through the same owner. Restore the previous package/config
   only if compatible with the resulting schema. Never restore an older database over active
   writers or hide a schema downgrade in rollback. If compatibility is unproven, keep the
   affected API safely stopped, preserve evidence and request an owner recovery decision.
   Do not begin an upgrade without recoverable rollback.

Record source merge, installed package/API revision, schema/rehearsal/backup verification,
continuity and retained-instruction receipts separately. **Source merge is not a deployed
result.** Notify the current Captain after a successful rollout for bounded reconciliation
and eligible-capacity refill; this grants no capacity increase.

Dispatch overdue is a loaded 60-minute API constant; verify adoption through the reviewed
upgrade, never monkeypatch. Backlog/pickup warnings and holds remain 30 minutes.

For intake's retired default-null field, follow the [receipt contract](../SPEC.md#intake-receipt-fingerprints)
and disposable bidirectional rollback rehearsals; unchanged older packages cannot replay
intermediate null-omitting receipts.

## New installation

Install the package and locked dependencies as in [Install Phalanx](install.md), then run:

```powershell
python -m phalanx deploy init
# Or select a new absolute directory and another port:
python -m phalanx deploy init --home D:\.phalanx_muster --port 8765
```

Default home is `~/.phalanx_muster` on every OS; explicit `--home`, then `PHALANX_HOME`,
override it. Initialization refuses an existing directory, even empty, and never follows
the discovery locator or updates an installation. `--port` generates matching server/client
defaults; `--api-url` overrides client URLs for a reverse proxy/hosted API. URLs require
HTTPS or loopback HTTP without credentials. Server launch binds loopback; hosted access
needs a TLS proxy.

Generated files contain no credentials, database or live state:

| File | Configure before use |
| --- | --- |
| `deployment.json` | Absolute private environment, shift config, reconciler config/scratch paths and server host/port. |
| `private.env.example` | Copy privately to `private.env`; supply owner/runner/GitHub tokens. Restrict to the service account (POSIX mode 600 or a private Windows ACL). The new directory requests POSIX mode 700; Windows permissions need owner verification. |
| `shift.json`, `charter.toml`, `reconciler.json` | Runtime/workspace, repository mappings, goal and stable reconciler ID. See [Captain configuration and charter](direct-path.md#run-a-captain-on-the-direct-path). |

Concurrency uses persisted owner dispatch settings, not the charter. New installations
default to Building 5 and Total 10; configure the pair in Scribe or the dispatch settings API.
Never put credentials in JSON configs. Discovery is optional: use `--home`, `PHALANX_HOME`
or the existing `~/.phalanx/muster.json` locator. See [direct-path configuration](direct-path.md).

```powershell
# Only for a NEW installation, after configuring private.env:
python -m phalanx deploy run --home D:\.phalanx_muster server --initialize-database
# In a second shell, after health/authentication and repository setup:
python -m phalanx deploy run --home D:\.phalanx_muster reconciler --once
python -m phalanx deploy run --home D:\.phalanx_muster reconciler
python -m phalanx deploy cli --home D:\.phalanx_muster -- shift status
```

Remove `--initialize-database` from persistent commands. Startup refuses missing
databases; initialization refuses existing files. Only the server opens storage
and validates schema. Python (one Uvicorn worker) or Go (one foreground server)
holds the database-adjacent OS lock throughout its lifetime. Reconciler scratch
locks are separate.

Both services remain foreground processes. Configure **one** host supervisor per service
(Windows service/task supervisor, systemd or launchd), using the absolute installed Python,
a fixed working directory outside repositories, the service account's environment,
restart-on-failure/backoff and private log retention. Stop that supervisor and its exact
owned process tree before replacing commands. Do not stack watchdogs, script restart loops
and supervisor policies. Raw Uvicorn does not acquire the deployment lock: inventory and
retire preexisting launchers with their owner.

### Windows critical-service scheduling under host load

Configure the API and reconciler tasks with Normal process priority (Task Scheduler priority 5), not the
default priority 7 (BelowNormal). Verify the actual listener **and** virtual-environment
launcher with `Get-Process -Id <verified-PID>` (`PriorityClass`, `BasePriority`) and inspect
the exact owning task's `Settings.Priority`.

The API and reconciler entrypoints promote only their own inherited Idle/BelowNormal Windows process to
Normal before serving or reconciling and log the correction. They preserve Normal/higher classes, never
choose High/Realtime and fail visibly if Windows rejects the operation. Other platforms,
CLI commands and writers are unchanged. Keep the supervisors correct too;
the services do not edit tasks or other processes.

[Windows API supervision](windows-api-supervisor.md).

Git commands in checkout verification, preparation and reconciliation have a 60-second
completion limit. On timeout or interruption the caller stops only its owned Git process
tree; Windows tree cleanup and direct-child reap each have a five-second limit. Start,
timeout and cleanup errors surface to the caller, including unchecked ref probes.
Only the documented absent-ref exit counts as absence. A failed reconciler item keeps its
last-known observation (which can become stale), logs the error and does not stop other
items from being observed. Priority, `/health` and Git timeout bounds alone do not prove
host-load latency, bounded admission or same-writer continuity; verify affected authenticated
operations under comparable load.

## Environment and local shims

`environment_file` accepts literal `KEY=VALUE` syntax or an existing `.json` object of
supported string settings: owner/runner/GitHub tokens, `PHALANX_DB`, `PHALANX_API_URL`,
`PHALANX_HOME`, `PHALANX_SHIFT_CONFIG`, `PHALANX_LOCAL_BOARD`. Unknown/duplicate keys
and empty values fail without printing values. No shell is evaluated. Inherited settings
take precedence; audit them on the service account. `shift_config` supplies
`PHALANX_SHIFT_CONFIG` only when unset.

Git/GitHub credentials and noninteractive credential helpers belong to the service account,
not shell injection. The reconciler uses `PHALANX_GITHUB_TOKEN` for provider reads and
sanitizes inherited `GIT_*` overrides; configure the normal Git credential helper separately.

No wrapper is required. Any retained local shim should only delegate:

```powershell
# Machine-local phx.ps1; paths supplied by the operator:
& $env:PHALANX_PYTHON -P -m phalanx deploy cli --config $env:PHALANX_DEPLOYMENT -- @args
exit $LASTEXITCODE
```

Service tasks call `deploy run --config <absolute deployment.json> server` or `reconciler`
directly. Keep settings, credentials and minimal shims outside Git; reusable behavior belongs
in the pinned package. Use supported runtime discovery/trust tools, not private app SQLite
queries or ad-hoc trust probes. Only the owner grants repository trust.

For prepared Windows npm environments, retain dependencies, browsers and `lock.sha256`
externally. Any shim delegates to pinned `scripts/checks.ps1 -EnvironmentRoot $PSScriptRoot`
with `@args`. See [prepared checks environments](direct-path.md#prepared-checks-environments)
for lock normalization, junction behavior and operator-only provisioning. Other platforms
use their repository's prepared checks prefix, not this Windows helper.

## Existing runtime: reviewed adoption, not a rename

Existing homes, explicit `captain.json` and service/worktree/session paths remain valid.
**Never run `deploy init` on a live home, rename it, change its locator or relocate its
database to adopt a spelling.** The raw API default `.phalanx/phalanx.sqlite3` is unchanged;
new setup writes explicit absolute paths.

Prepare a new local config referencing the existing paths, without copying private values:

```json
{
  "environment_file": "<absolute existing private env.json or private.env>",
  "shift_config": "<absolute existing captain.json or shift.json>",
  "server": {"host": "127.0.0.1", "port": 8000},
  "reconciler_config": "<absolute existing reconciler.json>",
  "reconciler_state": "<absolute existing reconciler scratch directory>"
}
```

`python -m phalanx deploy check --config <that file>` checks paths/credential presence
read-only, not connectivity, schema or GitHub. It starts no services and changes no config
or SQLite. Do not add/change an existing API's `PHALANX_HOME` without owner review of its
identity; explicit configs work without it.

Use the [upgrade procedure](#standing-authority-for-compatible-upgrades). Retire an old
watchdog before its replacement starts. If changing reconciler launchers, stop the exact
previous instance before `--once`, verify its stable identity/fresh heartbeat, then start
one persistent supervisor with the same scratch path. Never run both concurrently.
Use explicit authenticated status when muster-home identity is not configured. Do not
update a writer's environment underneath it, delete runtime data or import retired client
SQLite state.

Adoption preserves the saved dispatch limits and actual writer allocations. Capacity-setting
changes are separate owner actions through [dispatch settings](../SPEC.md#settings-and-dispatch-gates),
not a deployment default or an inference from stage labels.

## Coordination-policy adoption boundary

Source check-in does not install code, convert a schema or refresh retained conversations.
Use the upgrade procedure above before relying on new routes or generated instructions.
Instruction/CLI-only changes do not inherently require a service restart; inspect the actual
diff and preserve unrelated installed changes.

Existing sessions need explicit instruction delivery through supported messaging. Relay
reviewed canonical sections verbatim, expand placeholders for the recipient's configured
commands, and record recipient, revision and acknowledged receipt. A printed worker template
is not a fresh kickoff: never reserve a second writer to refresh instructions or contact an
ended writer. Captain refresh follows [the operating guide](direct-path.md#captain-shifts-and-handoff).
Private charters and schedules remain under their existing owner; preserve unrelated authority,
output ceilings and repository protections.

Keep package/schema verification, external configuration and retained-instruction receipts
separate. Adoption remains partial until the applicable receipts are verified; a sent message
is not readback, and deferred control-plane evidence still requires authenticated reconciliation.
