# Recovery and troubleshooting

Start with [Resume an existing install](resume.md). Never recreate the database or credentials
to recover. Read the API, GitHub and native session state before repeating an external effect.

## Common symptoms

| Symptom | Meaning and action |
| --- | --- |
| Empty queue after restart | Check the recorded absolute `PHALANX_DB` path. Stop a server pointed at the wrong file. |
| Port already bound | Keep the intended existing API. Run exactly one server per database, even across ports. |
| HTTP 503 with `Retry-After: 1` | Retry the identical idempotent request after the storage outage clears. |
| Queued intake has no writer | Read its shared `pickup` decision in `shift status` or Scribe. In-scope authorized intake needs no per-item charter edit. Launch eligible work; resolve the concrete dependency, hold, affinity, readiness, pause or capacity reason for excluded work. |
| Captain pickup scope/readiness is uninitialized | An updated current Captain heartbeat must publish real charter scope and local readiness. Do not infer permanent exclusion or readiness from an empty rollout snapshot. |
| `launch_unconfirmed` | Native create/confirm has not been verified for ten minutes. The wait is visible immediately; recover each wake, not only after this alert. Search fresh full native inventory for the full nonce, confirm a match, or safely replay the same reservation as below. A timeout does not prove no launch. |
| `writer_stopped`, `writer_error`, `no_progress`, `worker_overdue` | Inspect the exact native session and worktree. Resume it or safely replace it; silence alone never authorizes stopping. |
| Runtime repository trust pending | Notify the owner and retain the same writer/session until the owner approves. |
| `no_live_captain` | The keeper or owner starts a Captain. Existing writers and the reconciler continue. |
| `reconciler_stale`, `observation_stale` | Resume the intended reconciler and inspect its GitHub/API errors. |
| Owner question | Answer through `reply`; see [Work items](work-items.md#answer-a-waiting-item). |

## Writer replacement

Read `shift status`, the writer's nonce/native identity and the actual worktree.
Confirm the old session and all its writers stopped before replacing it with
`shift launch --replaces WRITER`. Keep all commits and uncommitted changes. Registration atomically
revokes the old token and opens an attempt only when the replacement's stage is eligible;
otherwise its binding retains the wait. A stale writer cannot post.
If the worktree is unavailable, retain server handoff context and report the missing artifact.
Never reset, clean or discard work to make replacement easier.

For an unconfirmed intent, only its owning Captain acts until it ends, goes stale or abandons
the intent. First search the full native inventory and verify the exact full nonce in
kickoff/metadata; task-first or renamed display titles are not identity. Confirm only
the exact matching session.
If fresh full inventory proves no matching session exists and no native create is in
flight, the owning Captain reruns `shift launch --captain ID --item ITEM`, verifies
the same writer ID and full nonce, then creates/confirms once. A lost registration
reply replays the staged credential; a lost confirmation reply replays the same native
ID. Do not discard/re-reserve an intent simply because its handoff was interrupted.
Incomplete inventory, unavailable kickoff metadata, an uncertain in-flight create or
missing replay credentials means retain the reservation and report the exact blocker.
Reserve/create/confirm one item before reserving another; no second launcher, capacity
bypass or service restart is needed.
Withdraw an intent only after proving launch never happened, or its session stopped.
See [registration and fencing](direct-sessions.md#writer-registration-and-fencing).

## Uncertain result submission

Read `work status` before retrying. `work result --attempt ...` pins the attempt:
it fills stage/version from the current binding and refuses to submit to a different
attempt after a lost response. If the stage advanced, continue that stage rather than
reposting the old result as new work. If the writer gets HTTP 401, stop; its binding may
have ended on completion, withdrawal or replacement. The Captain/owner checks the API
timeline and final output with its own credential. Do not print the credential file,
renew a retired lease, recreate a local journal or launch a second writer to repair
an ambiguous response.

## Back up and restore SQLite

These are server-host-only admin commands, not runner operations. Use the existing server's
environment and absolute `PHALANX_DB`. Backup uses SQLite's snapshot API while the server is up.
The destination directory must exist and support hard links. Commands validate copies and
refuse to overwrite existing files.

```powershell
python -m phalanx server backup C:\muster\backups\phalanx-01.sqlite3
```

Restore is an operator recovery window, not a writer action. Account for live sessions
and external effects with their owners before rolling state back. Stop the server and
its restart supervisor, choose a fresh database path in an existing directory, restore
there and verify `/health` and authenticated identity after restart. A backup may predate
writer registrations, replies or GitHub effects; reconcile them before resuming dispatch.
Keep the old database until verification and record the new path privately.

```powershell
$env:PHALANX_DB = "C:\muster\db\restored.sqlite3"
python -m phalanx server restore C:\muster\backups\phalanx-01.sqlite3
```

Restart with the recorded service entrypoint/supervisor and restored path. Do not start
a raw Uvicorn process alongside a deployment-managed server. See
[deployment and recovery boundaries](architecture.md#deployment-and-recovery-boundaries).
