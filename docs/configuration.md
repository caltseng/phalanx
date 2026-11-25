# Configuration

The server reads environment settings. Direct clients support explicit config paths or the
[muster locator](../README.md#direct-muster-discovery-and-status). Record paths and credentials
privately; never initialize a second store to repair discovery.

[`deploy init`](deployment.md) creates new-install templates under `~/.phalanx_muster`;
`deploy run` and `deploy cli` use a local `deployment.json` for private environment,
shift/reconciler paths and loopback host/port. Explicit legacy locations stay valid.
This setup never renames or upgrades live directories.

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
Charter `[shift]` accepts positive-integer `max_hours` (default 4). Normal rotation uses
elapsed time since registration only; wake and compaction counts remain telemetry.
Remove retired `max_wakes` and `max_compactions` settings; the client rejects them with
explicit guidance. Queue completion and dependency/external waits never end a shift early.
Five-minute reconciliation, explicit owner stop and genuine safety behavior remain.
Either concurrency key is
rejected with guidance to remove it and use dispatch settings instead. Remove that entry
in the same deployment cutover that introduces this implementation, leaving the live
charter unchanged until then. Upgrading an existing store preserves its saved Total and
initializes Building to min(5, Total), conservatively charging existing workers Building.
Subsequent restarts preserve owner edits. Do not raise the live old single limit to 10;
follow [deployment and separate activation](deployment.md#two-limit-writer-activation).
The reconciler
runs local checks serially and never configures publication or merging.

Writer credentials live only in the item's Git directory. Captains wake through their runtime
scheduler, not a CLI process.

The shift repository mapping may additionally specify `checks_environment`, the exact prepared
interpreter or command prefix included in writer kickoffs. It is not a runner validation-policy
command and need not be Python. The [prepared checks environment guide](direct-path.md#prepared-checks-environments)
documents the tracked npm wrapper, lockfile-staleness check and worktree `node_modules`
junction. Only the operator provisions dependencies; writers never install them.
