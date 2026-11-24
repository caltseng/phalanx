# Install Phalanx

This guide sets up a **new** install: it creates tokens, a database, client configuration and repository trust. To restart or resume an install that already exists, follow [Resume an existing install](resume.md) instead. Running these steps again against an existing install would replace its credentials or database.

## Choose and record your muster directory

The server reads `PHALANX_DB` and its tokens from the environment. Direct `shift` and
`reconcile` support explicit `--config` paths (the reconciler also needs `--state`), or the
optional [per-user muster locator](../README.md#direct-muster-discovery-and-status):
`--home`, then `PHALANX_HOME`, then `~/.phalanx/muster.json`. It only finds existing
configuration; it never creates or migrates state. Pick one muster directory now and record
these in a private local runbook outside the repository:

- the muster directory and the Phalanx checkout and virtual environment you run from;
- the absolute `PHALANX_DB` path;
- the API base URL and port;
- where the owner, runner and GitHub tokens are stored (a password manager or other private store, never the repository);
- the reconciler config path, scratch directory and stable reconciler ID;
- the validation policy path, if any;
- the shift config, charter, Captain wake schedule and keeper schedule.

New installations default to `~/.phalanx_muster`. The [tracked deployment setup](deployment.md)
generates consistent absolute paths, a five-writer charter template and configurable service
entrypoints without starting anything. Keep the database, client configs, reconciler scratch
directory and validation policy there, outside every mapped checkout. Explicit existing
locations, including legacy `phalanx-muster` homes, are honored and must not be renamed
under running workers. [Resume an existing install](resume.md) uses the recorded runbook.

## Install the package

Use CPython 3.14 for deployment and pilot runs. Phalanx supports CPython 3.12 and later. The CLI, native sessions and reconciler run on Windows, macOS and Linux; CI tests all three. From the repository root in PowerShell on Windows:

```powershell
py -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.lock -e .
```

Or in a POSIX shell on macOS or Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock -e .
```

The `python -m phalanx` commands in these docs are the same in either shell once the virtual environment is active. Examples use Windows paths such as `C:\phalanx-runner` for the runner state directory and `C:/enlistments/example` for a checkout. On macOS or Linux use paths such as `~/phalanx-runner` on the command line and absolute paths such as `/Users/you/src/example` in JSON config. Set environment variables with `$env:NAME = "value"` in PowerShell or `export NAME="value"` in a POSIX shell. On macOS, configure checkout and state paths with their exact on-disk letter case.

If the machine cannot reach PyPI, install from a local wheel directory with `python -m pip install --no-index --find-links <wheel-dir> -r requirements.lock -e .`. The lock file pins runtime dependencies for repeatable local installs, while the package metadata keeps compatible ranges for downstream installs. Add the `server` extra (`pip install -e ".[server]"`) on the server host when not using the lock file.

## Create credentials and start the server

Generate the owner and runner tokens **once** and store them privately where your runbook says. In PowerShell:

```powershell
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_DB = Join-Path $HOME ".phalanx_muster\db\phalanx.sqlite3"
$env:PHALANX_LOCAL_BOARD = "1"
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

In a POSIX shell:

```sh
export PHALANX_OWNER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PHALANX_RUNNER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PHALANX_DB="$HOME/.phalanx_muster/db/phalanx.sqlite3"
export PHALANX_LOCAL_BOARD=1
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

`PHALANX_DB` defaults to `.phalanx/phalanx.sqlite3` resolved against the server's working directory, so set an absolute path. The server creates the file and its directory if they are missing, or applies pending schema steps to an existing store, before serving requests. It refuses a Phalanx store older than schema version 12 rather than upgrading it. Use one server process per database and local storage. Arrange HTTPS before exposing the API beyond loopback. See [Configuration](configuration.md) for every server setting, including the local-only `PHALANX_LOCAL_BOARD` flag. Direct writers have no timed lease expiry.

## Check health and credentials

From a second shell, call `Invoke-RestMethod http://127.0.0.1:8000/health` (or `curl http://127.0.0.1:8000/health`). This public probe checks database access. Send `Authorization: Bearer <token>` to `GET /v1/owner/status` or `GET /v1/runner/status` to verify each credential. Each probe requires its matching token. The server returns 401 for missing or invalid credentials and 403 for the other role.

Give each client its own token and the API base URL. Rotate tokens by replacing the environment values and restarting the server, then update every client and your private store.

## Register repositories and file work

With `PHALANX_OWNER_TOKEN` set in the client shell:

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
```

See [Work items](work-items.md) for intake targets, GitHub issue import, answering questions and the operator board.

## Set up the execution host

The host runs native Captain/writer sessions and the reconciler. Prepare:

1. **A stable reconciler ID** and private scratch directory outside mapped checkouts.
2. **A reconciler config**, such as `C:/muster/reconciler.json`, containing `base_url`, `reconciler_id` and repository checkout/base mappings. Keep native launching and runtime project settings in the Captain's shift config. See [Configuration](configuration.md#reconciler-config).
3. **`PHALANX_RUNNER_TOKEN`** in Captain/reconciler command environments and **`PHALANX_GITHUB_TOKEN`** for GitHub observation. Native writers use their own configured Git/GitHub credentials.
4. **A validation policy** defining required ready checks so the reconciler can verify ready delivery, plus optional trusted local checks that raise flags rather than gate direct work. Write it as described in [the validation policy guide](validation-policy.md) at an absolute path outside every checkout. Configure each writer repository's prepared `checks_environment` separately in the shift config.
5. **Standing repository trust** in the worker runtime, below.

Fetch the desired origin branch into each mapped checkout before the first muster.

### Onboard a repository in one step

Once the config has `base_url` (and `validation_policy`), run:

```powershell
python -m phalanx onboard registered-repo-id --config captain.json --state C:\muster\onboarding `
  --remote https://github.com/owner/example --checkout C:/enlistments/example --project-id PROJECT
```

It registers the remote with the server (with `PHALANX_OWNER_TOKEN`; otherwise it verifies the registration), clones the checkout when it is absent or empty and verifies its origin and base branch (default: origin's HEAD), writes the complete mapping into the supplied Captain config, and adds a validation policy entry whose `ready_checks` list the CI that reported on the base branch tip (with `PHALANX_GITHUB_TOKEN`). `--host-id` defaults to the host the other mappings share. Copy the verified checkout/base mapping into the separate reconciler config before observing that repository. The new entry leaves `local_checks` unset for you to fill in or set to `[]`. The command never overwrites an existing registration, validation entry, or mapping field you did not pass. It prints each step as `done`, `ok`, `missing` (with the action) or `manual`, and exits 1 until nothing is missing. Rerun it to verify. Owner trust stays manual; configure the shared worktrees workspace in the shift config.

### Grant repository trust once

Grant the Captain's runtime standing access to each mapped checkout before the first muster. A runtime that gates a repository on owner trust prevents a worker from running until trust is granted. For Copilot CLI path trust, choose "trust for future sessions" once per checkout, or add the checkout's path to the `trustedFolders` array in `~/.copilot/config.json` ([Copilot CLI configuration](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/configure-copilot-cli)). The GitHub Copilot app also has a per-project repository trust card: the owner must approve it in the project's session even when the path is already trusted in runtime configuration. For direct sessions, approve the configured shared worktrees project (`session_workspace`); do not register a separate project for each worktree.

Only the owner grants trust; a Captain or worker never edits it. The Captain inspects runtime-reported repository trust immediately after launching or finding a writer session and on every wake. A pending trust approval is a visible owner blocker with the runtime's reason and an owner notification, not a stopped or failed writer. The Captain retains the same writer/session without relaunching, continues unrelated work, and resumes it after the runtime confirms owner approval.

## Start the first muster

With the server up, start the [reconciler and Captain](direct-path.md). Configure the shift
charter, shared worktrees workspace and per-repository checks environment. Start a native
Captain chat with `python -m phalanx shift prompt captain`, attach its five-minute wake and
the keeper's thirty-minute schedule. The Captain launches registered native writers.

Record in your runbook how you started each piece. From now on, use [Resume an existing install](resume.md) after a restart or reboot.
