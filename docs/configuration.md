# Configuration

The server reads environment settings. Direct clients support explicit config paths or the
[muster locator](../README.md#direct-muster-discovery-and-status). Record paths and credentials
privately; never initialize a second store to repair discovery.

## Environment variables

| Variable | Used by | Meaning |
| --- | --- | --- |
| `PHALANX_DB` | Server, `server backup`/`restore` | SQLite file. Defaults to `.phalanx/phalanx.sqlite3`, resolved against the working directory. Use an absolute path. The server creates the file if it is missing, or applies pending schema steps to an existing store, before serving. It refuses a store older than schema version 12 rather than upgrading it. |
| `PHALANX_HOME` | Direct discovery; API identity | Existing absolute muster directory, overridden by `--home` for clients. The API reports its configured home in `/v1/muster/status`; that identity must match the client's selected home. It does not choose `PHALANX_DB` or start any process. |
| `PHALANX_SHIFT_CONFIG` | `shift` | Explicit shift JSON path when `--config` is absent. Explicit config preserves existing layouts and does not load the discovered `private.env`; an explicit `--home` selects the muster layout instead. |
| `PHALANX_OWNER_TOKEN` | Server, owner CLI commands | Owner bearer token. |
| `PHALANX_RUNNER_TOKEN` | Server, Captain/reconciler commands | Trusted service bearer token; never given to writers. |
| `PHALANX_LOCAL_BOARD` | Server | Set to `1` to let loopback requests read the operator board without a token. Leave it unset on any hosted server, including behind a same-machine reverse proxy or tunnel. See [Work items](work-items.md#operator-work-board). |
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
      "base_branch": "main",
      "host_id": "local",
      "project_id": "project-id-from-the-runtime"
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
session launching and `max_writers`; there is no per-runner admission limit. The reconciler
runs local checks serially and never configures publication or merging.

Writer credentials live only in the item's Git directory. Captains wake through their runtime
scheduler, not a CLI process.

The shift repository mapping may additionally specify `checks_environment`, the exact prepared
interpreter or command prefix included in writer kickoffs. It is not a runner validation-policy
command and need not be Python. The [churchtree example](direct-path.md#shared-non-python-checks-environment-churchtree)
documents the live external npm/Playwright wrapper, lockfile-staleness check and worktree
`node_modules` junction. Only the operator provisions dependencies; writers never install them.
