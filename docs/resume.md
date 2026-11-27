# Resume an existing install

Use the recorded muster directory, absolute `PHALANX_DB` path, API URL, existing credentials,
shift config, charter and repository mappings. Never regenerate tokens or initialize another
database to recover. The optional [muster locator](configuration.md#direct-muster-discovery-and-status)
only finds existing configuration.

For an install already using [tracked deployment tooling](deployment.md), resume with
`python -m phalanx deploy run --config <recorded deployment.json> server` and the matching
`reconciler` command under the recorded single service owner. Never add
`--initialize-database` during recovery. Do not move an existing `phalanx-muster` directory
to `.phalanx_muster`; the new spelling is only the new-install default.
Go is now the supported repository default. Before a package upgrade, preserve a
recorded explicit `server.engine`/`--engine` choice and verified package path;
an old implicit Python command needs an explicit `--engine python` for temporary
rollback, not a silently changed task. See [engine precedence](configuration.md#http-engine-and-package-selection).
Missing Go packages fail rather than starting Python. Never run `server initialize`
to repair an existing installation.

## Restart in order

1. Check the recorded API's `/health`. If it answers, keep it. Otherwise load the existing
   owner/runner tokens and database path. If the recorded path is missing, stop and
   investigate; raw Uvicorn would create an empty store. Use the existing supervisor
   and entrypoint. The raw alternative below is only for an installation already
   managed that way, after verifying the file exists. It is a deprecated historical
   Python-only recovery command, not the supported new-install/default launch:

   ```powershell
   $env:PHALANX_DB = "<recorded absolute path>"
   # Load existing PHALANX_OWNER_TOKEN and PHALANX_RUNNER_TOKEN privately.
   python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
   ```

2. Verify authenticated `/v1/owner/status` and `/v1/runner/status`. Resume the
   [reconciler](direct-path.md#run-the-github-reconciler) with its original config and scratch directory.
3. Inspect `python -m phalanx muster status` when discovery and matching API home
   identity are configured; otherwise keep explicit config and authenticated API
   status. Read `python -m phalanx shift --config <recorded shift config> status`.
   Keep any live Captain and its scheduled wake. If none is live, run the
   [keeper procedure](direct-path.md#in-the-github-copilot-app).

## Reconcile native sessions

A fresh Captain reads server writer registrations and the runtime's session list.
Find an unconfirmed launch by its exact nonce before launching anything else. Confirm a
matching native session; do not infer failure from a missing launch reply. Resume a stopped
writer or replace it only after confirming its session stopped and preserving its local work.
Writer credentials fence attempts without timed expiry. Workers resume with `phalanx work status`
and `phalanx work next`; they retry uncertain result posts idempotently.

Check the operator board and `phalanx inbox` for questions, trust blocks, stale Captains,
stale observations and stopped writers. Only the owner approves runtime repository trust.
Check that Captain wakes and the keeper schedule survived the reboot; runtime scheduling
persistence is not assumed. See [recovery](recovery.md) for ambiguous cases.
