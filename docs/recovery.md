# Recovery and troubleshooting

Start with [Resume an existing install](resume.md) after any restart. This page covers what to do when something is stuck or uncertain. Never recreate the database, tokens or runner state directory to recover.

## Common symptoms

| Symptom | Meaning and action |
| --- | --- |
| The loop exits during preflight | It lists every missing package, Git older than 2.36, a missing `PHALANX_RUNNER_TOKEN`, and an unreachable or rejecting API together. Fix them and restart. Start the server before the loop. |
| `Cannot acquire ... lock at ...captain-loop.lock` | Another Captain loop already runs on this state directory. Keep it, or stop it with Ctrl+C before starting another. |
| The journal refuses the runner ID or API URL | The config doesn't match the state directory. Restore the original `runner_id` and `base_url`; don't start a fresh state directory. |
| The server starts with an empty queue | `PHALANX_DB` points at the wrong file; the server creates an empty database at any path with none. Stop it and restart with the recorded absolute path. |
| A second server fails to bind the port | A server already runs on that port. Use it. |
| HTTP 503 with `Retry-After: 1` | The SQLite store was locked or busy past its timeout. Retry the identical request; lease checks and idempotency keys make that safe. |
| `mappings` loop status lists a checkout | Its runtime project is unverified, often because the checkout isn't trusted. Grant [repository trust](install.md#grant-repository-trust-once); admission resumes on the next check. |
| `relay_stalled` | No relay server has polled for 180 seconds while a request waits. Start or wake the one standing relay chat. See below. |
| `worker_overdue` | A worker has been active for three hours with no result. The loop never stops it. Inspect the worker and tell the owner whether it is still progressing. |
| `renewal_interval_too_long` | A renewed lease has less than two heartbeat intervals left. Lower `--heartbeat-seconds` or raise `PHALANX_LEASE_SECONDS`. |
| An item waits on the owner | Answer it with `reply`; see [Work items](work-items.md#answer-a-waiting-item). |

## Relay stalled

The loop reports `relay_stalled` when no relay server has polled or answered for 180 seconds while a request waits, puts the reason on each affected item, and notifies the owner. Resume serving from the one standing relay chat, or let its scheduled wake fire. The first poll clears the stall with no loop restart. See [Stall reporting](captain-relay.md#stall-reporting).

## Relay ownership is uncertain

When you can't tell whether an old relay chat is still serving, for example after a reboot or a chat that may still be in a turn:

1. Stop the old relay server, or confirm that it has stopped (its turn ended and no scheduled wake will resume it). Don't start a second server alongside it; overlapping servers have a reproduced failure that can lose a request (item 108f6009).
2. Start exactly one relay server.
3. Don't rerun anything the old server may have taken. A taken request is never reissued. An unanswered observation or resolution is settled as lost and queued afresh. An unanswered launch stays reserved until native discovery reconciles it by its marker; it is never relaunched.

## Reconcile a reservation

From a fresh Captain chat, with the runner's original config and state directory and the runner token, run one recovery step:

```powershell
python -m phalanx attended-recover --config runner.json --state C:/phalanx-runner --slot 1 --observation observation.json
```

The Captain supplies a fresh JSON snapshot from the runtime's native session history and reads. It uses the same format `relay observe` writes, so a saved observe reply can be passed as is. Set `observed_at` to the snapshot time as ISO 8601 with an offset. Include `discovery_complete` and a `threads` array with one row per worker session. Set `discovery_complete` only after inspecting all relevant history pages, including archived history where relevant. A session already bound to a native receipt can be read directly.

### Observation fields

| Field | Source |
| --- | --- |
| `host_id`, `project_id`, `thread_id`, `started_at` | Native host, project, thread UUID and offset-aware original launch time. |
| `marker`, `assignment` | The exact first-turn attempt marker and the original packet's `result_identity` object from the first prompt. |
| `state` | `active`, `stopped` or `unknown`. Attest `stopped` only after confirming the native turn ended and accounting for any worktree writer it started. A missing thread or ambiguous state is `unknown`. |
| `writers_stopped` | Required as `true` on a `stopped` row, after accounting for every worktree writer the session started. |
| `client_thread_id` | The queued UUID when the native launch can be positively correlated with its saved queued receipt. Otherwise omit it. |

### What recovery does

- A saved stop request replays without another observation file.
- A `retry` response with `retry_at` gives the earliest next probe as Unix seconds.
- After an `owner_wait`, address the reason before using `--recheck`.
- An unmatched server attempt requires the original journal or explicit recovery of its writer.
- The command verifies the local Git checkpoint and checks any result against the original packet. Any stage other than development leaves the checkout unchanged, so its saved result is verified against the unchanged worktree.
- An expired attempt with an active writer or a verified result is adopted under a recovery claim. The claim request and its fresh secret are journaled before sending, so a lost reply replays the same key. Afterwards the Captain renews and submits under the new lease. If the server refuses adoption, the reservation backs off and then waits for the owner; nothing is released.
- For a verified result with a live lease, use the exact receipt with `attended submit`.
- A reservation with no relay, receipt or submission is released only after `--recheck` proves no launch happened: no result file and a clean worktree at the accounted revision, or no worktree or branch at all.
- To replay an unconfirmed claim, use the original launch path, which reuses the saved claim request.

## Back up and restore SQLite

These are server-host-only admin commands: they open the server's database file directly, not through the API. Run them on the server host, never from a runner, in the same environment as the server or with `PHALANX_DB` set to the same absolute path. Backup uses SQLite's snapshot API and can run while the server is up. The destination directory must already exist and support hard links, such as NTFS, APFS or ext4. Each command validates its copy before creating the destination and refuses to overwrite an existing file.

```powershell
New-Item -ItemType Directory -Force .phalanx/backups | Out-Null  # POSIX: mkdir -p .phalanx/backups
python -m phalanx server backup .phalanx/backups/phalanx-01.sqlite3
```

To recover, stop the server and point `PHALANX_DB` at a fresh path in an existing directory. Restore the backup there, restart the API with the same path, and verify `GET /health`. Keep the old database until you've verified the recovered data, and record the new path in your runbook.

```powershell
$env:PHALANX_DB = "$PWD/.phalanx/phalanx-restored.sqlite3"  # POSIX: export PHALANX_DB="$PWD/.phalanx/phalanx-restored.sqlite3"
python -m phalanx server restore .phalanx/backups/phalanx-01.sqlite3
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```
