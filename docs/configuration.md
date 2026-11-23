# Configuration

Phalanx has no install locator or per-user config file. The server reads its settings from the environment, and every runner command takes explicit `--config` and `--state` paths. Record your values in your private muster runbook (see [Install Phalanx](install.md#choose-and-record-your-muster-directory)).

## Environment variables

| Variable | Used by | Meaning |
| --- | --- | --- |
| `PHALANX_DB` | Server, `server backup`/`restore` | SQLite file. Defaults to `.phalanx/phalanx.sqlite3`, resolved against the working directory. Use an absolute path. The server creates the file if it is missing, or applies pending schema steps to an existing store, before serving. It refuses a store older than schema version 12 rather than upgrading it. |
| `PHALANX_OWNER_TOKEN` | Server, owner CLI commands | Owner bearer token. |
| `PHALANX_RUNNER_TOKEN` | Server, runner commands | Runner bearer token. |
| `PHALANX_LEASE_SECONDS` | Server | How long a claim or renewal keeps an attempt leased. Default 300; whole seconds from 30 to 3600, or the server refuses to start. Keep each runner's renewal interval well under it. |
| `PHALANX_LOCAL_BOARD` | Server | Set to `1` to let loopback requests read the operator board without a token. Leave it unset on any hosted server, including behind a same-machine reverse proxy or tunnel. See [Work items](work-items.md#operator-work-board). |
| `PHALANX_API_URL` | Owner CLI commands | API base URL; defaults to `http://127.0.0.1:8000`. `--api-url` before the command overrides it. |
| `PHALANX_GITHUB_TOKEN` | Runner, `ingest-issue` | GitHub token for draft publication, promotion, validation, revision, check-in and issue import. It stays local to the runner or client. |

Use one server process per database and local storage. The server defaults to loopback HTTP; arrange HTTPS before exposing it beyond loopback. Rotate tokens by replacing the environment values and restarting the server.

## Runner config

The runner reads a JSON file passed as `--config`:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "runner_id": "desktop",
  "capacity": 2,
  "validation_policy": "C:/phalanx-runner/validation.json",
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
| `base_url` | API base URL. The runner journal refuses a different value later. |
| `runner_id` | Stable runner ID across restarts. It scopes server capacity and recovery. The journal refuses a different value later. |
| `capacity` | Local agent worker slots, default 1. The runner adds two runner-only slots, `capacity`+1 and `capacity`+2. The server also applies its per-runner capacity. |
| `repositories` | Registered repository ID to `checkout`, `base_branch`, and the runtime's `host_id` and `project_id` (see the [runtime mapping](captain-relay.md#runtime-mapping)). |
| `stages` | Optional list restricting which stages the loop claims. |
| `validation_policy` | Absolute path, outside every checkout, of the [validation policy](runner-validation-policy.md). Without it, validation stages are not claimed. |
| `check_recheck_seconds` | How often pending draft and ready checks are rechecked. 5 to 86400, default 30. |
| `check_wait_limit_seconds` | How long checks may stay pending on one head and base before the owner is asked. 60 to 2592000, default 7200. |
| `local_check_capacity` | Validations that run local checks at once on the runner host. 1 to 64, default 1. |
| `auto_merge` | `true` (default) merges at check-in; `false` delivers the ready PR and leaves the merge to the owner. |
| `notify_command` | Executable and arguments, such as `["python", "notify.py"]`, that receives each `relay_stalled`, `relay_serving` and slot `owner_wait` event as one JSON object on stdin. Each event has `event`, `reason`, `items`, `runner_id` and `at`; `owner_wait` adds `slot`, `attempt_id` and `next_action`, and is sent once per new reason. The [reconciler](direct-path.md#run-the-github-reconciler) also sends each owner-notify flag once. It runs without the Phalanx token variables and has 30 seconds. A failure shows as `notify_blocked` in loop status. |

Keep the runner state directory private, outside every mapped checkout, and readable only by the runner account. It holds active lease secrets, the reservation journal and the relay queue.

Captain loop flags `--poll-seconds`, `--heartbeat-seconds`, `--batch-size` and `--wait-seconds` tune its scheduler; see [Runner operations](runner-operations.md#scheduling).
