# Go HTTP server

The packaged Go server is Phalanx's only supported HTTP runtime. Python clients,
the reconciler and offline server admin remain supported; Python HTTP has been
[removed](../docs/python-server-deprecation.md). Repository changes do not
upgrade a live installation. The service owner coordinates deployment and
rollback using verified packages and current data.

## Operate

Use [Install Phalanx](../docs/install.md) for a new installation,
[Resume an existing install](../docs/resume.md) for retained state, and
[deployment tooling](../docs/deployment.md) for supervised service ownership.
[Release and rehearsal](../docs/go-server-release.md) covers package verification,
backup, cutover and rollback. Never reset an existing database or generate
replacement credentials to repair startup.

The production entrypoint is `cmd/phalanx-server`. A verified package contains
the executable and its sibling `assets` directory. With the stored environment
configured, a foreground PowerShell launch is:

```powershell
& "C:\packages\reviewed\phalanx-server.exe" --host 127.0.0.1 --port 8000 --assets "C:\packages\reviewed\assets"
Invoke-RestMethod http://127.0.0.1:8000/health
```

Run the health probe in a second shell while the foreground process is running.
Replace both package paths with the verified extracted package. On macOS/Linux,
use native paths and omit `.exe`. `--host`, `--port` and the absolute `--assets`
path are required. The host must be loopback; remote access uses the existing
HTTPS proxy. `--version` prints credential-free build identity without starting
the server.

For supervised operation, `python -m phalanx deploy run ... server` selects Go
and requires an absolute `server.go_binary` or `--go-binary`. Explicit saved or
CLI Python selections fail with migration guidance; there is no fallback.

| Environment | Requirement |
| --- | --- |
| `PHALANX_DB` | Absolute path to an existing regular SQLite database with the supported application identity, schema26 and DELETE journaling. |
| `PHALANX_OWNER_TOKEN`, `PHALANX_RUNNER_TOKEN` | Distinct stored credentials, each at least 32 URL-safe characters; never commit or print them. |
| `PHALANX_HOME` | Existing muster location for client identity. Expanded absolute paths are filesystem-resolved; unset or relative values report no home. |
| `PHALANX_LOCAL_BOARD` | Unset or `0` by default. `1` enables the directly accessed loopback-only board; never enable behind a proxy or tunnel. |
| `FORWARDED_ALLOW_IPS` | Trusted proxy peers, default `127.0.0.1`; an explicit empty value trusts none. Forwarding never grants anonymous board access or bearer authority. |

See [configuration](../docs/configuration.md) and
[local-board security](../docs/work-items.md#operator-work-board) for details.
The board is at `/operator/`; authenticated routes are documented in
[the API reference](../docs/api.md).

Go refuses missing or unsupported stores and performs no initialization or
migration. Only a **new** installation uses
`python -m phalanx server initialize` with a new absolute `PHALANX_DB`; this
offline admin command refuses existing files and starts no HTTP process.

The database-adjacent OS lock covers the entire server lifetime. Shutdown stops
admission, drains dispatched HTTP operations and durable background work, closes
database connections, then releases ownership. Caller disconnect does not cancel
accepted work. After forced termination or an unknown commit, reconcile receipts
and current state before retrying; do not restore an old data snapshot.

## Contribute

Follow [repository instructions](../AGENTS.md) and
[Develop Phalanx](../docs/development.md) for targeted checks and shared fixtures.
Use the repository's prepared checks environment, not another checkout or a
replacement toolchain. Go and dependencies are pinned in [go.mod](go.mod) and
[go.sum](go.sum); `GOTOOLCHAIN=local` prevents implicit compiler replacement.
From `server`, append the appropriate commands to that environment:

```text
go mod verify
go build -trimpath -buildvcs=false ./...
go vet ./...
go test ./...
```

Native Go tests use disposable Go-owned schemas and need no Python runtime or
generated source manifest. Python HTTP/client integration tests use the real Go
production executable through `PHALANX_TEST_SERVER`, with packaged sibling
assets. CI covers Windows, Linux and macOS; Unix race checks and runtime-safety
jobs feed the required `Tests` aggregate. A Windows environment without CGO and a
C compiler cannot provide a local race-check result.

| Code | Responsibility |
| --- | --- |
| `internal/httpapi` | HTTP transport, authentication, validation projection and Scribe assets. |
| `internal/contract` | Presence/default validation and canonical encoding. |
| `internal/application` | Atomic domain operations, receipts, injected clock and durable jobs. |
| `internal/sqlite` | Typed storage, admission, connection pools and transaction outcomes. |
| `internal/instancelock` | Cross-platform exclusive server ownership. |
| `internal/testdb` | Disposable test schemas and fixture connections, never production initialization. |
| `package.py` | Deterministic server-only release packaging. |

Keep provider calls and native-session work outside database transactions.
Preserve authorization, receipt bytes, unknown-commit reporting and ownership
through drain. See [architecture](../docs/architecture.md) for the wider system
and [historical compatibility evidence](COMPATIBILITY.md) for retained transition
records; neither historical evidence nor source integration proves live adoption.
