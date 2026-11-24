# Install Phalanx

This guide sets up a **new** install: it creates tokens, a database, a runner state directory and repository trust. To restart or resume an install that already exists, follow [Resume an existing install](resume.md) instead. Running these steps again against an existing install would replace its credentials or database.

## Choose and record your muster directory

Phalanx has no install locator. There is no `PHALANX_HOME` or per-user config file. The server reads `PHALANX_DB` and its tokens from the environment, and every runner command takes explicit `--config` and `--state` paths. Pick one muster directory now and record these in a private local runbook outside the repository:

- the muster directory and the Phalanx checkout and virtual environment you run from;
- the absolute `PHALANX_DB` path;
- the API base URL and port;
- where the owner, runner and GitHub tokens are stored (a password manager or other private store, never the repository);
- the runner config path, runner state directory and runner ID;
- the validation policy path, if any;
- how you serve the Captain relay and run the Captain loop.

A typical layout keeps the database, runner config, runner state and validation policy in sibling subdirectories of the muster directory, outside every mapped checkout. [Resume an existing install](resume.md) is written in terms of this runbook.

## Install the package

Use CPython 3.14 for deployment and pilot runs. Phalanx supports CPython 3.12 and later. The client side (CLI, runner, Captain loop and relay) runs on Windows, macOS and Linux; CI tests all three. From the repository root in PowerShell on Windows:

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

If the machine cannot reach PyPI, install from a local wheel directory with `python -m pip install --no-index --find-links <wheel-dir> -r requirements.lock -e .`. The lock file pins runtime dependencies for repeatable local installs, while the package metadata keeps compatible ranges for downstream installs. Without the lock file, `pip install -e .` installs only what the CLI, runner and Captain loop need; add the `server` extra (`pip install -e ".[server]"`) on the server host.

## Create credentials and start the server

Generate the owner and runner tokens **once** and store them privately where your runbook says. In PowerShell:

```powershell
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_DB = "C:/muster/db/phalanx.sqlite3"
$env:PHALANX_LOCAL_BOARD = "1"
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

In a POSIX shell:

```sh
export PHALANX_OWNER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PHALANX_RUNNER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PHALANX_DB="$HOME/muster/db/phalanx.sqlite3"
export PHALANX_LOCAL_BOARD=1
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

`PHALANX_DB` defaults to `.phalanx/phalanx.sqlite3` resolved against the server's working directory, so set an absolute path. The server creates the file and its directory if they are missing, or applies pending schema steps to an existing store, before serving requests. It refuses a Phalanx store older than schema version 12 rather than upgrading it. Use one server process per database and local storage. Arrange HTTPS before exposing the API beyond loopback. See [Configuration](configuration.md) for every server setting, including `PHALANX_LEASE_SECONDS` and the local-only `PHALANX_LOCAL_BOARD` flag.

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

## Set up a runner

A runner executes work on one machine. Give it:

1. **A stable runner ID** and one private local state directory, outside every mapped checkout and readable only by the runner account. The directory holds active lease secrets and the relay queue. Use the same ID, directory and config for every Captain command and across restarts.
2. **A runner config**, such as `C:/muster/runner.json`, mapping each registered repository to its local checkout and runtime project. See [Configuration](configuration.md#runner-config).
3. **`PHALANX_RUNNER_TOKEN`** in the shell that runs runner commands, and optionally **`PHALANX_GITHUB_TOKEN`** to publish and check in PRs. Without the GitHub token, draft, promotion, published-validation, revision and check-in claims are excluded.
4. **A validation policy** for PR validation, if you want PRs promoted and checked in. Write it as described in [the validation policy guide](runner-validation-policy.md) at an absolute path outside every checkout.
5. **Standing repository trust** in the worker runtime, below.

Fetch the desired origin branch into each mapped checkout before the first muster.

### Onboard a repository in one step

Once the config has `base_url` (and `validation_policy`), run:

```powershell
python -m phalanx onboard registered-repo-id --config runner.json --state C:\phalanx-runner `
  --remote https://github.com/owner/example --checkout C:/enlistments/example --project-id PROJECT
```

It registers the remote with the server (with `PHALANX_OWNER_TOKEN`; otherwise it verifies the registration), clones the checkout when it is absent or empty and verifies its origin and base branch (default: origin's HEAD), writes the complete mapping into the config, and adds a validation policy entry whose `ready_checks` list the CI that reported on the base branch tip (with `PHALANX_GITHUB_TOKEN`). `--host-id` defaults to the host the other mappings share. The new entry leaves `local_checks` unset for you to fill in or set to `[]`. The command never overwrites an existing registration, validation entry, or mapping field you did not pass. It prints each step as `done`, `ok`, `missing` (with the action) or `manual`, and exits 1 until nothing is missing. Rerun it to verify. Owner trust in the worker runtime (below) stays manual, and the Captain loop still verifies the runtime project before launching workers.

### Grant repository trust once

Grant the Captain's runtime standing access to each mapped checkout before the first muster. A runtime that gates a repository on owner trust (the GitHub Copilot app asks once per repository) denies every worker tool until trust is granted, so an untrusted checkout would stop a worker mid-stage. For the GitHub Copilot app and CLI, choose "trust for future sessions" once per checkout, or add the checkout's path to the `trustedFolders` array in `~/.copilot/config.json` ([Copilot CLI configuration](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/configure-copilot-cli)).

Only the owner grants trust; a Captain or worker never edits it. The Captain checks trust while serving `resolve_project`. An untrusted mapping is reported in the loop's `mappings` status and gets no workers, while other mappings keep running. Once trust is granted, the next check verifies the mapping and admission resumes.

## Start the first muster

With the server up, start one standing [Captain relay](captain-relay.md#captain-chat-relay) chat, then the [Captain loop](runner-operations.md#continuous-captain-loop):

```powershell
python -m phalanx captain --config runner.json --state C:\phalanx-runner
```

Record in your runbook how you started each piece. From now on, use [Resume an existing install](resume.md) after a restart or reboot.
