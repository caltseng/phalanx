# Resume an existing install

Use this guide after a restart, a reboot or a crashed process, when the database, tokens, runner config and runner state already exist. It never creates credentials or a database. For a new install, follow [Install Phalanx](install.md).

## Before you start

Open your private local muster runbook (see [Install Phalanx](install.md#choose-and-record-your-muster-directory)). The optional [direct muster locator](../README.md#direct-muster-discovery-and-status) finds an existing direct configuration and supports read-only `muster status`; it does not start services or replace the restart procedure. Without that locator/layout, use the explicit paths from your runbook. You need:

- the muster directory, Phalanx checkout and virtual environment;
- the absolute `PHALANX_DB` path;
- the API base URL and port;
- the owner, runner and GitHub tokens from your private store;
- the runner config path and state directory.

Keep all of these exactly as they were:

- **Don't generate new tokens.** Load the stored ones. New tokens lock out every client still holding the old ones.
- **Don't point `PHALANX_DB` anywhere else.** The server creates an empty database at any path that has none, so a wrong or relative path silently starts an empty queue. It defaults to `.phalanx/phalanx.sqlite3` under the server's working directory.
- **Don't create a new runner state directory or change `runner_id` or `base_url`.** The state directory holds the reservation journal, lease secrets and relay queue. The journal refuses a different runner ID or API URL.

## Restart in order

Start the server, then the relay chat, then the Captain loop. The loop's startup preflight stops before any claim if the API is unreachable, and the relay chat simply idles until the loop queues a request.

### 1. Server

First check whether it is already running: `Invoke-RestMethod http://127.0.0.1:8000/health` (or `curl`). If it answers, don't start another.

Otherwise activate the virtual environment, load the stored tokens and the recorded `PHALANX_DB` into the environment, and start the server on its recorded port, as in [Install Phalanx](install.md#create-credentials-and-start-the-server) but without generating tokens:

```powershell
$env:PHALANX_DB = "<absolute path from your runbook>"
# Load PHALANX_OWNER_TOKEN and PHALANX_RUNNER_TOKEN from your private store.
$env:PHALANX_LOCAL_BOARD = "1"   # only for a loopback-only local server
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

Only one process can bind the host and port, so a second server on the same port fails to start. That does not stop a second server on another port or another host from opening the same database: run exactly one server per database.

Verify with `GET /health`, then `GET /v1/owner/status` and `GET /v1/runner/status` with each token.

### 2. Relay chat

Run exactly one standing [Captain relay](captain-relay.md#standing-relay-serve) server for the runner state directory.

If an old relay chat might still be serving, stop it or confirm that it has stopped before starting a new one. The protocol is at-most-once: each request is handed to exactly one taker, a taken request is never reissued, and a launch whose reply was lost is reconciled by its marker rather than rerun. The current implementation has a reproduced failure when relay servers overlap (SQLite lock contention that can lose a request; tracked as item 108f6009), so don't rely on overlapping servers. See [Relay ownership is uncertain](recovery.md#relay-ownership-is-uncertain).

If the runtime's scheduled wake was attached to the relay chat, confirm it is still attached and re-attach it if not.

### 3. Captain loop

Run the loop with the same config and state directory:

```powershell
python -m phalanx captain --config runner.json --state C:\phalanx-runner
```

The loop holds `captain-loop.lock` in the state directory. A second loop on the same directory waits up to 30 seconds for the lock, then exits with `Cannot acquire ... lock`. That means a loop is already running: keep it, or stop it with Ctrl+C and wait for it to drain before starting a new one. The lock does not block manual `attended status`, `receipt` or recovery commands.

On start, the loop enumerates its durable reservations and the server's expired attempts before admitting new work. It resumes each reservation from its journal; it never relaunches a recorded launch.

## Check status

- `GET /health` and the two token probes above.
- `python -m phalanx attended --config runner.json --state C:\phalanx-runner status --all` lists occupied slots, loop status and each slot's reason, including `mappings`, `relay_stalled` and `worker_overdue`.
- The operator board at `http://127.0.0.1:8000/operator/` shows each item's stage and wait reason.
- `python -m phalanx inbox` lists work waiting for an answer.

A `relay_stalled` status that doesn't clear within a few minutes means no relay server is polling. See [Recovery and troubleshooting](recovery.md).

## After a reboot: unverified behavior

These are not verified and not guaranteed:

- **Scheduled wakes.** Whether a runtime's scheduled wake on the relay chat survives a reboot is unverified. Check it after every reboot and re-attach it if it's gone.
- **Interrupted workers.** How worker sessions interrupted by a reboot recover is unverified end to end. The designed path is: the loop observes each launched worker; a worker that stopped without a result stops renewing its lease, and stopped-attempt recovery retries the stage after its lease expires; a worker still active keeps its reservation. Inspect `attended status --all` and the board, and use [Reconcile a reservation](recovery.md#reconcile-a-reservation) for anything left ambiguous.

Record anything you learn about your runtime's reboot behavior in your runbook.
