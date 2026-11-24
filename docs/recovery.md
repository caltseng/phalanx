# Recovery and troubleshooting

Start with [Resume an existing install](resume.md). Never recreate the database or credentials
to recover. Read the API, GitHub and native session state before repeating an external effect.

## Common symptoms

| Symptom | Meaning and action |
| --- | --- |
| Empty queue after restart | Check the recorded absolute `PHALANX_DB` path. Stop a server pointed at the wrong file. |
| Port already bound | Keep the intended existing API. Run exactly one server per database, even across ports. |
| HTTP 503 with `Retry-After: 1` | Retry the identical idempotent request after the storage outage clears. |
| Awaiting a direct Captain | A queued item has no writer. A Captain must register and launch it; unpinned intake needs no routing override. |
| `launch_unconfirmed` | Find the launch nonce in the runtime's full session list before confirming or withdrawing it. A timeout does not prove no launch. |
| `writer_stopped`, `writer_error`, `no_progress`, `worker_overdue` | Inspect the exact native session and worktree. Resume it or safely replace it; silence alone never authorizes stopping. |
| Runtime repository trust pending | Notify the owner and retain the same writer/session until the owner approves. |
| `no_live_captain` | The keeper or owner starts a Captain. Existing writers and the reconciler continue. |
| `reconciler_stale`, `observation_stale` | Resume the intended reconciler and inspect its GitHub/API errors. |
| Owner question | Answer through `reply`; see [Work items](work-items.md#answer-a-waiting-item). |

## Writer replacement

Read `shift status`, the writer's nonce/native identity and the actual worktree.
Confirm the old session and all its writers stopped before replacing it with
`shift launch --replaces WRITER`. Keep all commits and uncommitted changes. Registration atomically
revokes the old token and opens the replacement's attempt. A stale writer cannot post.
If the worktree is unavailable, retain server handoff context and report the missing artifact.
Never reset, clean or discard work to make replacement easier.

For an unconfirmed intent, only its owning Captain acts until it ends, goes stale or abandons
the intent. First search native sessions by nonce; confirm the exact matching session.
Withdraw an intent only after proving launch never happened, or its session stopped.
See [registration and fencing](direct-sessions.md#writer-registration-and-fencing).

## Back up and restore SQLite

These are server-host-only admin commands, not runner operations. Use the existing server's
environment and absolute `PHALANX_DB`. Backup uses SQLite's snapshot API while the server is up.
The destination directory must exist and support hard links. Commands validate copies and
refuse to overwrite existing files.

```powershell
python -m phalanx server backup C:\muster\backups\phalanx-01.sqlite3
```

To restore, stop the server, choose a fresh database path in an existing directory, restore
there and verify `/health` after restart. Keep the old database until verification and record
the new path privately.

```powershell
$env:PHALANX_DB = "C:\muster\db\restored.sqlite3"
python -m phalanx server restore C:\muster\backups\phalanx-01.sqlite3
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```
