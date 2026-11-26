# Configuration

The server reads environment settings. Direct clients support explicit config paths or the
[muster locator](#direct-muster-discovery-and-status). Record paths and credentials
privately; never initialize a second store to repair discovery.

[`deploy init`](deployment.md) creates new-install templates under `~/.phalanx_muster`;
`deploy run` and `deploy cli` use a local `deployment.json` for private environment,
shift/reconciler paths and loopback host/port. Explicit existing locations stay valid.
This setup never renames or upgrades live directories.

## Direct muster discovery and status

For an existing direct muster, discovery uses `--home <absolute directory>` first, then
`PHALANX_HOME`, then exactly one locator: `~/.phalanx/muster.json` (`~` is the current
user's home on Windows, macOS and Linux). Create that locator yourself, containing only:

```json
{"home": "/absolute/path/to/existing/muster"}
```

On Windows use a JSON path such as `"C:\\muster"`. Arrays, extra fields, duplicate `home`
fields, relative paths and missing directories fail; Phalanx never scans for another
muster or falls back after an invalid override. Discovery never initializes, migrates,
moves or deletes state. Existing explicit config commands work without a locator.

The selected directory contains `shift.json`, `charter.toml`, `reconciler.json` and
`private.env`. The shift config can omit `charter` or use `charter.toml` (an absolute
path to that same file also works). Other config paths, such as checkout and worktree
mappings, should be absolute.

### Private environment file

`private.env` holds literal `KEY=VALUE` lines for `PHALANX_RUNNER_TOKEN`,
`PHALANX_GITHUB_TOKEN`, optionally `PHALANX_OWNER_TOKEN`, `PHALANX_DB`,
`PHALANX_LOCAL_BOARD`, `PHALANX_API_URL`, `PHALANX_HOME` and `PHALANX_SHIFT_CONFIG`.
Blank lines and whole-line `#` comments are allowed; matching outer single/double
quotes are removed, without escapes, interpolation, inline comments or shell execution.
Duplicate/unknown keys and empty values fail. Protect this file with your OS's private-file
permissions; never commit it. Existing environment values take precedence.
Tokens are loaded only into the environment and never included in status output.

### Status and explicit configuration

```text
python -m phalanx muster status
python -m phalanx muster status --home <absolute directory>
python -m phalanx shift status
python -m phalanx reconcile
```

Without explicit configs, `shift` derives `shift.json` and the charter, and `reconcile`
derives `reconciler.json` and its scratch directory `<muster>/reconciler` (overridable
with `--state`). Both accept `--home`; `shift` accepts it before or after its action.
Explicit `--config` and `PHALANX_SHIFT_CONFIG` retain their existing behavior without
loading `private.env`; an explicit `--home` instead selects the muster layout.
Explicit reconciler configs still need `--state`. `shift prompt` remains usable without
any configuration.

Start the intended API yourself with `PHALANX_HOME` set to this directory, alongside its
**existing** database path and credentials. Authenticated `GET /v1/muster/status` reports
the API's configured home identity, live Captains, registered open writers, each reconciler's
last heartbeat/age/staleness (15 minutes), and dispatch pause. `muster status` reads this API,
prints JSON, and fails nonzero if it is unavailable or its home identity differs. An empty
reconciler list means none has recorded a heartbeat. Writer presence reports registration
state; inspect native sessions separately to verify process liveness.

Use the [deployment entrypoints](deployment.md#new-installation) to start services explicitly
and runtime schedules to wake Captain/keeper sessions. Existing installs can keep explicit
config paths and live state locations when adopting the locator. For one item's correlated
timeline, see the [paginated API example](api.md#read-one-items-correlated-timeline).

## Environment variables

| Variable | Used by | Meaning |
| --- | --- | --- |
| `PHALANX_DB` | Server, `server backup`/`restore` | SQLite file. Defaults to `.phalanx/phalanx.sqlite3`, resolved against the working directory. Use an absolute path. The server creates the file if it is missing, or applies pending schema steps to an existing store, before serving. It refuses a store older than schema version 12 rather than upgrading it. |
| `PHALANX_HOME` | Direct discovery; API identity | Existing absolute muster directory, overridden by `--home` for clients. The API reports its configured home in `/v1/muster/status`; that identity must match the client's selected home. It does not choose `PHALANX_DB` or start any process. |
| `PHALANX_SHIFT_CONFIG` | `shift` | Explicit shift JSON path when `--config` is absent. Explicit config preserves existing layouts and does not load the discovered `private.env`; an explicit `--home` selects the muster layout instead. |
| `PHALANX_OWNER_TOKEN` | Server, owner CLI commands | Owner bearer token. |
| `PHALANX_RUNNER_TOKEN` | Server, Captain/reconciler commands | Trusted service bearer token; never given to writers. |
| `PHALANX_LOCAL_BOARD` | Server | Set to `1` only on a directly accessed, loopback-bound server to allow tokenless board reads and same-origin `/operator/api/` actions. Local Host and actual loopback peer are required; forwarding headers are rejected. All `/v1` writes retain bearer authorization. Leave unset on hosted servers, reverse proxies and tunnels. See [Work items](work-items.md#operator-work-board). |
| `PHALANX_API_URL` | Owner CLI commands | API base URL; defaults to `http://127.0.0.1:8000`. `--api-url` before the command overrides it. |
| `PHALANX_GITHUB_TOKEN` | Reconciler, `onboard`, `ingest-issue` | Local GitHub reads, onboarding CI discovery and issue import. Direct writers publish using their configured Git/GitHub credentials. |

Use one server process per database and local storage. The server defaults to loopback HTTP; arrange HTTPS before exposing it beyond loopback. Rotate tokens by replacing the environment values and restarting the server.

## Reconciler config

The reconciler reads `reconciler.json` from muster discovery or an explicit `--config`:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "reconciler_id": "desktop",
  "validation_policy": "C:/muster/validation.json",
  "repositories": {
    "registered-repo-id": {
      "checkout": "C:/enlistments/example",
      "base_branch": "main"
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `base_url` | API base URL. |
| `reconciler_id` | Stable reconciler identity across restarts; scopes its heartbeat and flags. |
| `repositories` | Registered repository ID to `checkout` and `base_branch`. An optional `post_merge_check` names the repository's [post-merge deployment check](direct-path.md#post-merge-deployment-status). |
| `validation_policy` | Absolute path, outside every checkout, of the [validation policy](validation-policy.md). The reconciler runs local checks and observes required GitHub checks from this policy. Local failures are flags, not a gate. |
| `notify_command` | Executable and arguments, such as `["python", "notify.py"]`, receiving each reconciler owner-notify flag once as JSON on stdin. It runs without Phalanx token variables and has 30 seconds. Failures are printed as actionable errors. |

Keep the reconciler scratch directory outside mapped checkouts and private to its account.
The [shift config and charter](direct-path.md#run-a-captain-on-the-direct-path) define native
session launching. Exactly two persisted dispatch settings control writers:
`max_building_writers` (Building, initially 5) and `max_writers` (Total, initially 10 for a
new store); there is no delivery, per-runner, per-stage or per-charter quota. Change either with
Scribe's **Writer concurrency** control (no token prompt on a trusted local-only board),
or owner-authenticated
`PUT /v1/dispatch/settings` with `{"max_building_writers": 5, "max_writers": 10}`.
The pair updates atomically; omitted settings remain unchanged. Both must be strict integers
1..2147483647, with Building <= Total. Lowering below usage preserves all existing writers
and blocks affected admissions; raising takes effect on the next admission/acquisition retry.
`GET /v1/dispatch/settings` shows both limits, `building_writers`, `delivering_writers` and
`active_writers` (Total usage), all derived from registered allocations.
Charter shift settings and rejected legacy keys are documented with
[Captain shifts and handoff](direct-path.md#captain-shifts-and-handoff). Charter concurrency
keys are rejected in favor of dispatch settings. Existing-store limits and conservative
allocations require [reviewed adoption](deployment.md#existing-runtime-reviewed-adoption-not-a-rename),
not a client-driven increase. The reconciler runs local checks serially and never configures
publication or merging.

Writer credentials live only in the item's Git directory. Captains wake through their runtime
scheduler, not a CLI process.

The shift repository mapping may additionally specify `checks_environment`, the exact prepared
interpreter or command prefix included in writer kickoffs. It is not a runner validation-policy
command and need not be Python. The [prepared checks environment guide](direct-path.md#prepared-checks-environments)
documents the tracked npm wrapper, lockfile-staleness check and worktree `node_modules`
junction. Only the operator provisions dependencies; writers never install them.
