# Phalanx

Phalanx is an agentic orchestrator for work across repositories. Work items live in an API-backed queue. A local runner lets multiple agents claim work in parallel and deliver either a Markdown report or a pull request.

1. File a work item for a registered repository with a goal, enough context to execute it, and the expected output.
2. Start a **muster**, a batch of eligible work launched from the Captain chat. Agents work in parallel, use isolated checkouts for changes, and verify their results.
3. Follow each item through its lifecycle. After development, Phalanx opens a draft PR. Self-review, pre-validation, and draft-only checks gate promotion to ready for review. CI, previews, independent review, feedback, and conflict resolution gate check-in. The runner checks in the PR when authorized. Report work ends with a verified **field report** in Markdown.

**Phalanx advancing** means a work item has met the conditions to move to its next stage.

The first milestone runs the HTTP API server and durable store locally, with concurrent work in visible worker sessions launched from a Captain chat. Phalanx is runtime-neutral: any agent runtime that can run visible worker sessions, such as Codex, GitHub Copilot CLI or the GitHub Copilot app, and Claude Code, can serve as Captain through the [bridge contract](#bridge-contract). The runner has a configurable concurrency limit and can pause dispatch. The same server is intended to move to cloud hosting while agents continue to run locally. A future ChatGPT phone workflow can file work through the hosted API and return a work-item link.

See the [design spec](SPEC.md) for the proposed workflow, the [roadmap](ROADMAP.md) for the order of the big steps, and the [implementation plan](IMPLEMENTATION_PLAN.md) for milestones and acceptance checks. Open [state-machine.html](state-machine.html) locally in a browser to explore the lifecycle and execution status graphs.

Every closed work item has a final output. If it ends without the requested deliverable, Phalanx stores a field report explaining what happened and what remains.

## Run the local API

Use CPython 3.14 for deployment and pilot runs. Phalanx supports CPython 3.12 and later. The client side (CLI, runner, Captain loop and relay) runs on Windows, macOS and Linux; CI tests all three. From the repository root in PowerShell on Windows:

```powershell
py -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.lock -e .
$env:PHALANX_OWNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_RUNNER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PHALANX_LOCAL_BOARD = "1"
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

Or in a POSIX shell on macOS or Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock -e .
export PHALANX_OWNER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PHALANX_RUNNER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PHALANX_LOCAL_BOARD=1
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

The `python -m phalanx` commands below are the same in either shell once the virtual environment is active. Examples use Windows paths such as `C:\phalanx-runner` for the runner state directory and `C:/enlistments/example` for a checkout; on macOS or Linux use paths such as `~/phalanx-runner` on the command line and absolute paths such as `/Users/you/src/example` in JSON config. Set environment variables with `$env:NAME = "value"` in PowerShell or `export NAME="value"` in a POSIX shell. On macOS, configure checkout and state paths with their exact on-disk letter case.

If the machine cannot reach PyPI, install from a local wheel directory with `python -m pip install --no-index --find-links <wheel-dir> -r requirements.lock -e .`. The lock file pins runtime dependencies for repeatable local installs. The package metadata keeps compatible version ranges for downstream installs. Without the lock file, `pip install -e .` installs only what the CLI, runner and Captain loop need; add the `server` extra (`pip install -e ".[server]"`) on the server host. Tests use the standard library and the same runtime dependencies.

Set `PHALANX_DB` to select a SQLite file. It defaults to `.phalanx/phalanx.sqlite3` relative to the server's working directory. Use an absolute path when running commands from different directories. The server applies pending migrations before serving requests and defaults to local loopback HTTP with the command above. Use one server process and local storage. Arrange HTTPS before exposing the API beyond loopback.

Set `PHALANX_LEASE_SECONDS` to change how long a claim or renewal keeps an attempt leased. It defaults to 300 and accepts whole seconds from 30 to 3600; the server refuses to start otherwise. Keep each runner's renewal interval well under it.

From a second shell, call `Invoke-RestMethod http://127.0.0.1:8000/health` (or `curl http://127.0.0.1:8000/health`). This public probe checks database access. Send `Authorization: Bearer <token>` to `GET /v1/owner/status` or `GET /v1/runner/status` to verify credentials. Each probe requires its matching token. The server returns 401 for missing or invalid credentials and 403 for the other role. Store the tokens privately and provide each client with its own token and API base URL. Rotate tokens by replacing the environment values and restarting the server.

### Run attended assignments

Use a server with durable intake and dispatch enabled. Register the repository
with the owner API, fetch the desired origin branch into its local checkout,
and file a development or report item. Give the machine one stable
runner ID and share one private local state directory and config across all
Captain commands for that runner. The directory contains active lease secrets.
Put it outside the mapped checkouts and restrict access to the runner account.
Keep the runner token in the shell that runs these commands.

Save a local JSON config such as:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "runner_id": "desktop",
  "capacity": 2,
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

Local capacity defaults to 1. Use numbered slots from 1 through the configured
capacity. The server still applies dispatch pause, stage and item eligibility,
and its per-runner capacity. A local capacity reduction applies to new
reservations. Existing reservations in higher-numbered slots can still be
inspected, renewed, and submitted, and count toward local capacity. When
upgrading the bridge, finish any in-flight CLI commands and use the new
version for all subsequent commands. The existing journal remains usable
and its original reservation is slot 1.

Set `PHALANX_GITHUB_TOKEN` in the runner environment to publish initial drafts
for registered GitHub repositories. After development succeeds, run
`python -m phalanx attended --config runner.json --state C:/phalanx-runner prepare --draft`
to poll for draft publication. This command uses the runner's Git credentials
for the push and its GitHub token for draft creation. It returns
`draft_submitted` when the API accepts the verified PR. Keep the same state
directory to retry an interrupted publication, and use `status` to inspect a
blocked reservation. Draft assignments run directly on the runner.

PR validation also runs on the runner. Add `"validation_policy": "C:/phalanx-runner/validation.json"`
(an absolute path outside every checkout) to the runner config and write that file as described
in [the validation policy guide](docs/runner-validation-policy.md). After self-review,
`prepare` with stage `pre_pr_validation` runs the configured local checks in the clean item
worktree, `pr_promotion` verifies draft checks and marks the primary PR ready, and
`pr_validation` records ready CI for the current head. Pending CI waits; the Captain loop
requests due rechecks each poll. Pending draft and ready checks are rechecked every
`"check_recheck_seconds"` (optional runner config, 5 to 86400, default 30). After
`"check_wait_limit_seconds"` (60 to 2592000, default 7200) pending on one head and base,
the owner is asked; a reply starts a fresh window. At most `"local_check_capacity"`
(1 to 64, default 1) validations run local checks at once on the runner host; the rest wait
with the reason "Waiting for local check capacity" without using their check timeout. A local
check that times out with no identified failure retries validation twice with backoff before
returning to development. A missing or
broken policy waits for an owner reply that
names the problem, and startup preflight reports a malformed file before any claim.

For a `checked_in` item, `pr_reconciliation` records the merge intent once every gate is
current, and `check_in` merges
it with a merge commit once GitHub's required reviews and checks allow it; Phalanx adds no
gate of its own. Set `"auto_merge": false` in the runner config to deliver the ready PR
instead and leave the merge to the owner (default `true`).

From an authorized Captain muster:

1. Verify the project and host with the runtime's project discovery. Run
   `python -m phalanx attended --config runner.json --state C:/phalanx-runner --slot 1 prepare`.
   On `launch_intent`, launch **one** visible worker session from the returned
   `session` (`project_id`, `title`, `prompt`), as the `launch` operation in the
   [runtime mapping](#runtime-mapping) describes. Save the marker for receipt and result
   commands. Prepare each additional assignment in a free slot, such as
   `--slot 2 prepare`. The worker targets its assigned Git worktree explicitly.
2. With the same command prefix and slot number, run
   `receipt MARKER HOST --thread-id THREAD` with the worker session's UUID, or
   `receipt MARKER HOST --client-thread-id CLIENT` for queued setup. Resolve a
   queued or lost receipt in the runtime and verify the exact launch marker and
   assignment before recording the native ID. Use `status` to inspect a
   reservation or `status --all` to list occupied slots. If the claim response
   was lost, repeat `prepare` in the original slot to replay the claim. Keep an
   ambiguous launch reserved and reconcile it using the original marker.
   Other free slots can continue preparing eligible assignments.
3. Run `renew` for every known active claim in its slot independently of
   observation, comfortably before that lease expires. The CLI supports manual
   renewal. Use short observation waits.
4. Read the worker session and verify the exact marker, assignment, and that the
   worker and any writers it started have stopped. Then run
   `submit MARKER HOST THREAD --confirmed-stopped`. The runner verifies the
   result file and current worktree before posting. If the submission response
   is lost, repeat the same command to replay the recorded submission.

Development success requires a new local candidate commit and a clean
worktree. Report drafting sends the Markdown to the server as a draft.
Worker stages record local checkpoints for questions and failures. Questions
wait for an owner reply. A technical failure from a confirmed-stopped worker
enters the shared 5, 10, 20, and 40 second retry backoff. Exhausting the budget
asks for owner intervention.
Run `prepare` again to launch an independent review with the exact stored draft
revision. Review findings return to drafting with the previous draft in the new
packet. Run `prepare --report-storage` in a free slot to submit a reviewed
revision for server finalization. A `report_storage_submitted` result needs no
native chat. If the response is lost, repeat the command in the same slot. The
server retries final storage independently and exposes the finished Markdown
through the report endpoint.

Keep the state directory when restarting. Expired leases and unverified results
require reconciliation.
The continuous loop below supplies independent heartbeat scheduling and
native observation through the Captain relay, accepted live in the PH-11b muster.

### Continuous attended Captain loop

Use the same stable runner ID, private state directory, and numbered slots as
the attended commands. Finish other preparation commands before starting one
loop for that directory. A process lock excludes a second loop; it does not
prevent manual status, receipt, or recovery commands.

```powershell
python -m phalanx captain --config runner.json --state C:\phalanx-runner
```

The runner first runs a startup preflight (also run by `attended prepare`). It
reports every missing package, Git older than 2.36, missing
`PHALANX_RUNNER_TOKEN` and unreachable or rejected API together, and stops
before any claim. Optional settings such as `PHALANX_GITHUB_TOKEN` are not
required; without it, initial draft claims are excluded as before.

**One-time setup per repository.** Alongside the tokens, grant the Captain's
runtime standing access to each mapped checkout before the first muster. A
runtime that gates a repository on owner trust denies every worker tool until
trust is granted, so an untrusted checkout would stop a worker mid-stage. The
GitHub Copilot CLI reads trust from the `trustedFolders` array in
`~/.copilot/config.json` ([Copilot CLI configuration](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/configure-copilot-cli)),
or from choosing "trust for future sessions" once per checkout. The GitHub
Copilot app also keeps its own per-project repository trust: a `trustedFolders`
entry alone does not clear it, and a new session in an untrusted project shows a
trust card and runs nothing until the owner accepts it. For the app, open one
session in each mapped project and accept its trust card before the first
muster, in addition to the `trustedFolders` entry.
Only the owner grants trust; a Captain or worker never edits it. The Captain
checks trust while serving `resolve_project` and answers an untrusted checkout
with an error naming it and the setup action. The loop records every
unverified mapping, with its project ID, checkout path and error, together in
one `mappings` loop status
(`attended status --all`) and launches no worker for them, while unrelated
mappings keep claiming and launching. Once trust is granted, the next check
verifies the mapping and admission resumes. If a launched worker still waits
on trust, the Captain reports its session with a `blocker`. The slot keeps its
reservation and lease, reports one `worker_blocked` loop status, and the
operator board shows the blocker as the item's wait reason. It never treats
the worker as stopped; the worker continues once the owner accepts the trust prompt.

By default the loop reaches the worker runtime through the **Captain chat relay**,
because the Captain chat holds the runtime's session tools. The loop queues
each bridge request in `<state>\captain-relay.sqlite3` and waits; the Captain
chat runs it with its runtime's tools and records the reply. See
[Captain chat relay](#captain-chat-relay). To use a trusted subprocess bridge
instead, put `--adapter-command BRIDGE [ARGS...]` last; all remaining arguments
belong to the bridge. The executable fixture in `tests/fixtures/captain_bridge.py`
tests that boundary without contacting a runtime, and one shared contract test
runs both the fixture and the relay.

The loop enumerates durable reservations and the server's expired attempts
before admitting work. An unavailable recovery listing or unmatched attempt
pauses admission with a visible recovery reason. Existing reservations,
including slots above a reduced capacity, still run and renew. New claims
route implemented worker stages (`development`, `self_review`, `adversarial_review`,
`pr_revision`, `report_drafting`, `report_review`) separately from runner-only `pr_creation`,
`report_storage` and, when `validation_policy` is configured, `pre_pr_validation`,
`pr_promotion`, `pr_validation`, `pr_reconciliation` and `check_in`. An optional config
`stages` array restricts this set. Draft, promotion, published-validation, revision and
check-in claims are excluded without `PHALANX_GITHUB_TOKEN`,
and validation claims without `validation_policy`. The server's atomic eligible-stage claim still
applies pause, capacity, priority, repository, dependency and affinity gates.
Worker claims require verified runtime project mappings; runner-only claims retain all
Git-verified mappings even when their runtime is unavailable. The loop alternates
which group it polls first and preserves each pending claim's exact original
filters and secret. One asynchronous project discovery pass per poll verifies
each distinct host/project/checkout mapping once and shares the results across
slot admission and launch checks. Failed discovery is retried on the next poll.

Each slot has one work lane. Lease renewals have separate lanes (60 seconds
by default), as do bounded native observation batches (eight slots, two-second
wait by default). Git, provider requests, a slow native batch, or an ambiguous
slot cannot occupy the heartbeat or another slot's work lane. Configure
`--batch-size 1` to isolate individual native waits. `--poll-seconds`,
`--heartbeat-seconds`, `--batch-size`, and `--wait-seconds` tune the bounded
scheduler. If a renewed lease has less than two heartbeat intervals left, the
slot reports `renewal_interval_too_long`. Per-attempt cursors and admission rotation stay in memory as
optimizations; they reset on restart and never prove stop or authorize replacement.

Every prepared launch is relayed at most once. The loop journals relay intent
before invoking the bridge and immediately saves a native or queued receipt.
After timeout, failure or restart, a recorded relay intent is **never** repeated.
A prepared packet with no relay intent, receipt, submission or recovery ownership
can resume discovery and launch using its original claim and packet. Its clean,
accounted worktree and live claim are rechecked before relaying.
Discovery must correlate its original marker, assignment, host, project, launch
time and any queued ID to exactly one native thread. A stopped turn also requires
an explicit attestation that downstream writers stopped. The existing result
collector validates the local result and checkpoint before submitting it.
Once a queued ID has been correlated to a native thread, repeated observations
of that same native ID preserve the queued identity and remain idempotent.
Saved submissions replay their exact original receipt even when the local lease
deadline has passed; the server decides whether they were already accepted.
Expired attempts with a known active writer, a verified result or a saved
submission are adopted rather than released. The runner journals a recovery
claim with a fresh lease secret, and the server re-leases the same open attempt
and fences the old secret. The writer, checkpoint and result stay bound to one
attempt, so nothing is redrafted. Stopped-attempt recovery still releases only
verified safe reservations.

A stopping worker assignment with no relay intent, receipt, submission or recovery
ownership (whether or not its packet was committed)
can use the leased `stop-confirmed` route without waiting for expiry. The loop
checks its absent or clean, accounted worktree and records the exact stop request
before sending it. Retries recheck that proof and replay the original request.
Only a verified finalization acknowledgment releases the slot. Dirty checkpoints,
partial workspace refs, attempt evidence, ambiguous launches and runner-only
publication/storage effects still require explicit reconciliation.
Runner-only `report_storage` with no saved submission and `pr_creation` with no
draft publication intent or sealed observation use the same no-writer stop path.
Draft cancellation checks the runner's publication journals under the item
publication lock; a saved intent remains reserved even when the slot has none.
An existing clean candidate must match the claimed candidate, not today's origin.
For a launched stopping worker, the same stop path requires its exact stopped
native turn and verifies the worktree and any result through the recovery
verifier; it never submits a worker result as a cancellation acknowledgment.
A clean unlaunched checkpoint remains accounted when origin advances to a
descendant. Unmatched expired attempts consume capacity rather than stopping
all admission. Recovery waits for an observation before starting its uncertainty
backoff, and malformed native rows block only their associated markers.
Rejected submissions remain observable across restart and stop confirmation
archives their evidence once. An expired prepared packet with no relay effects
uses the verified no-launch checkpoint for recovery without waiting for a
native observation that cannot exist.

A worker that is still active three hours after launch with no result keeps
its reservation and lease; the loop never stops it. The slot reports
`worker_overdue` once and the operator board shows it as the item's wait
reason until the worker produces a result. A writer observed stopped without a
result file stops renewing its lease, so stopped-attempt recovery reconciles
the expired attempt instead of the item staying reserved forever.

The CLI prints JSON activity and actionable errors; reservation status includes
the last loop and recovery reason. Loop status has one durable store and is
written and emitted only when its status or reason changes. Ctrl+C stops scheduling
and drains in-flight operations without discarding reservations. Restart with the same configuration.
The journal refuses a different runner ID or API URL.

#### Bridge contract

Both transports carry the same request: `contract_version: 2`, `operation`, and
`arguments`, answered by one JSON object. A bridge or Captain must reject any
other version with an error rather than guess; version 1 used the old operation names. The contract names no runtime; the
[runtime mapping](#runtime-mapping) says how each runtime serves it.

A subprocess bridge (`--adapter-command`) is invoked once per operation, with
the request on stdin and the reply on stdout. Commands must not write
diagnostic text to stdout; nonzero exits, malformed replies, replies exceeding
1 MiB and a 15-second command timeout are visible failures. Stdin/stdout use
temporary files, not pipes whose inherited handles could keep a timed-out helper
or loop shutdown waiting for EOF. Timeout terminates only the direct helper, not
native workers or its process tree; an uncertain launch stays reserved. Bridges
must detach native workers' stdio from these short-lived request files. Shutdown
still drains in-flight runner/Git operations.
The bridge receives no queue token or
lease in the protocol. Phalanx runner, owner and GitHub token environment
variables are removed from its subprocess environment; keep the bridge trusted.

| Operation | Arguments and required response |
| --- | --- |
| `resolve_project` | Receives `host_id`, `project_id`, `checkout`. Discover the exact project on that host and return those verified fields; do not echo unverified input. The checkout must resolve to the configured root. |
| `launch` | Receives `host_id`, original `marker`, and the prepared `session`: `project_id`, `title` and `prompt`. Start **one** visible worker session in that project on that exact host whose first prompt is exactly `prompt`, which contains the marker. Use `title` as the session name where the runtime allows it; otherwise use a name that fits, such as the attempt UUID from the marker. Return the same host and marker and exactly one UUID: the session's `thread_id`, or a queued `client_thread_id` when the runtime returns a provisional ID first. You can also return a `worker_url`, an HTTPS link to that session with no credentials in it, which the operator board shows. The runner ignores an invalid link and reports a warning; the launch still succeeds. |
| `observe` | Receives at most the configured batch of `references` (slot, host, project, original launch time, marker, assignment and receipt), per-marker `cursors`, and `timeout_seconds`. Return `observed_at` with timezone, boolean `discovery_complete`, `threads` (one row per worker session), and optional per-marker `cursors` (string or null to reset). |

Session rows use the recovery observation fields documented below. A `stopped`
row additionally requires `writers_stopped: true`. A session that cannot
progress until the owner acts, such as one waiting on a repository trust
prompt, is not stopped: report it `active` or `unknown` with a nonempty
`blocker` naming the action. A `stopped` row with a `blocker` is malformed. Observation timestamps must
be fresh (at most 60 seconds old). Discovery is complete only after the relevant
history pages have been reconciled, including queued or lost receipts; a cursor
must not hide the first-turn identity or be treated as complete history.
`thread_id` is the field name for the runtime's worker session UUID in every runtime.

#### Captain chat relay

Start the loop without `--adapter-command`, then open a Captain chat in any
runtime that can launch and read visible worker sessions in the mapped
projects. The chat needs a shell with write access to the runner state
directory. Repeat this cycle while the loop runs:

1. `python -m phalanx relay --state C:\phalanx-runner next --wait 30` hands out
   the oldest request exactly once, or `{"status": "idle"}`. Output is
   ASCII-escaped JSON, so a non-ASCII prompt survives any pipe code page;
   decode it with a JSON parser before passing it on.
2. Serve the request with the runtime's tools, following the contract table
   above and the [runtime mapping](#runtime-mapping):
   - `resolve_project`: discover the project; reply with the verified host,
     project and root path. If the runtime gates the repository on owner
     trust and it is not trusted, reply with `--error` naming the checkout and
     the one-time setup action instead, and tell Caleb once.
   - `launch`: start the worker session **once**; reply with the host, marker
     and session UUID as `thread_id` (or a provisional ID as `client_thread_id`).
   - `observe`: read each receipt's session, or find the marker in session
     first prompts or history; reply with fresh rows. Set `writers_stopped` only after verifying
     the session's turn stopped. When the runtime's tools cannot read a
     session's first prompt, list the worker sessions and their state with
     those tools, then let the observe helper read the local transcripts and
     write the reply:
     `python -m phalanx relay --state C:\phalanx-runner observe ID --runtime RUNTIME --host HOST --session SESSION_ID STATE PROJECT_ID --complete --out reply.json`.
     Repeat `--session` for each listed session; `STATE` is `active`, `stopped`
     (writers verified stopped) or `unknown`. Pass `--complete` only when every
     worker session on the host was listed. Add `--blocked SESSION_ID REASON`
     for a listed session that waits for the owner, such as on a trust prompt,
     and list it `active`, never `stopped`. The helper keeps only sessions
     whose first prompt carries a queued marker and its exact assignment.
3. Record the reply right away (observations go stale after 60 seconds):
   `python -m phalanx relay --state C:\phalanx-runner respond ID --file reply.json`,
   or `respond ID --error "what failed"` when the runtime action fails.
4. Between requests, run `python -m phalanx inbox` with the owner token and
   triage each worker question. When the answer follows from the repository,
   the issue, its conventions or the question's proposed default, answer it
   yourself with `python -m phalanx reply ... --answered-by captain`.
   Escalate to Caleb in the Captain chat only when human judgment materially
   changes the outcome or authority is missing. `attended status --all` shows
   slot reasons. Tell Caleb once about a `worker_overdue` or `worker_blocked`
   slot, after inspecting the worker to say whether it is still making
   progress or what he must do.

Identical requests waiting to be taken share one entry. Verified project resolutions
are cached for ten minutes and failed ones for one minute, so an untrusted
repository costs the Captain one answer a minute. The loop waits up to five minutes for a request to
be taken and five more for its reply. A launch the Captain never took expires
and is retracted, so the same packet can launch again. A taken request is never
handed out again, even after a Captain restart. An unanswered one is settled as
lost: a later observation or resolution queues afresh, while an unanswered launch
stays reserved until native discovery reconciles it by marker. A restarted loop
expires requests its predecessor left pending for longer than the timeout. A
younger one may still be served. Its waiter is gone, so the answer goes unused,
and a launch is reconciled by marker as above. Once a slot no longer
observes, the loop retracts its untaken `observe` requests, so the Captain never
answers for a finished worker. Never rerun a request
whose outcome you did not record; answer it from discovery instead.

#### Standing relay serve

The relay drains only while some agent turn is running the cycle above. A
runtime's session tools can be called only from inside an agent turn, so the
loop cannot serve or wake the relay itself. Make a standing serve part of muster
setup:

- Serve from a dedicated relay chat that repeats `relay next` without ending
  its turn. Keep owner conversation in another chat so it never waits on the
  relay.
- Attach the runtime's own scheduler to that chat so a wake restarts serving
  whenever its turn ended (see the runtime mapping below). Each wake runs the
  cycle until it is told to stop. Overlapping servers are safe: `relay next`
  hands each request to exactly one taker, so a wake that lands while another
  server is active takes only requests nobody else took.

When no server has polled or answered for `relay_stall_seconds` (180 seconds)
while a request waits, the loop reports `relay_stalled` with the time of the
first untaken request. It adds the reason to the lease renewal of every item
whose worker needs the relay, so the operator board shows it as each item's
wait reason. It then notifies the owner. The first poll or answer after that
clears the stall. The loop then reports `relay_serving`, clears the board
reasons on the next renewal, and notifies the owner again. It resumes by itself
with no restart: queued observations are taken where they wait, and an expired
launch or resolution is queued afresh on the next poll.

Set `"notify_command"` in the runner config to an executable and its arguments,
such as `["python", "notify.py"]`, to receive each `relay_stalled` and
`relay_serving` event as one JSON object on stdin. Each event has `event`,
`reason`, `items`, `runner_id` and `at`. The command runs without the Phalanx
token variables and has 30 seconds. A failure shows as `notify_blocked` in loop
status. The stall status itself is always written and emitted.

Recovery bounds: a stall shows within `relay_stall_seconds`, plus one loop poll
and one heartbeat (about four minutes with defaults). Once a wake is due,
draining resumes after the wake interval, plus the runtime's turn-start delay,
plus up to one loop poll. The turn-start delay covers waiting behind a running
turn of that chat, and the app or terminal must be open to fire the wake. An
expired request also costs at most one more poll to requeue.

#### Runtime mapping

Each runtime serves the same three operations with its own tools. Set the
config's `host_id` and `project_id` to the values the runtime reports. The
Codex desktop and GitHub Copilot app mappings have field evidence in
[the worker recovery report](docs/pilots/worker-recovery.md); verify the others
in their first muster and record any correction here.

| Runtime | `resolve_project` | `launch` | `observe` |
| --- | --- | --- | --- |
| Codex desktop app | `list_projects`; match the project ID, host and root path. | `create_thread` with `title`, `prompt` and target `{"type": "project", "projectId": project_id, "environment": {"type": "local"}}`. Return `threadId` as `thread_id` or `clientThreadId` as `client_thread_id`. | `read_thread` for receipts; `list_threads` history search for the marker. Use `codex app-server` only after verifying its threads appear in the app sidebar with the same identity as the app's tools. |
| GitHub Copilot app | `list_repositories`; match the project ID to its `id` and the checkout to its `main_repo_path`. Use `local` as the host. Confirm the checkout is trusted (listed in `trustedFolders` in `~/.copilot/config.json`), and, when any existing session in the project shows `repository_trust` blocking progress in `get_session`, treat the project as untrusted too. Otherwise reply with an error that names the project ID and checkout path (two projects can share a name) and tells the owner to open a session in that project and accept the app's trust card. | `create_session` with `project_id`, `workspace_type: "branch"` and no `base_branch` (the packet already names Phalanx's worktree), `name` set to the attempt UUID from the marker (`name` allows 40 characters, too few for `title`), and a kickoff with `prompt` in autopilot mode. Return the session ID as `thread_id`. | `get_session` for receipts; `list_sessions_and_chats` lists candidates. Both return only metadata, and no app tool reads a session's turns, so pass each worker session with its state to the observe helper with `--runtime copilot`; it reads the first `user.message` in `~/.copilot/session-state/<id>/events.jsonl` on the same machine. A session `get_session` reports busy is `active`; an idle session whose last turn ended is `stopped`. A session whose `get_session` `repository_trust` blocks progress is waiting for the owner, not stopped, even when idle: pass it `active` with `--blocked SESSION_ID "repository trust pending for CHECKOUT; accept the trust card in the app"`. If its transcript has no first prompt yet, write that row by hand from the launch receipt and request reference. |
| GitHub Copilot CLI | Verify the checkout exists and is the configured root; the project ID is the configured value. | Start `copilot` in a new visible terminal in the checkout with `prompt` as its first prompt. Return its session UUID as `thread_id`. | Pass each session with its state to the observe helper with `--runtime copilot`, which reads `~/.copilot/session-state/<id>/events.jsonl`. |
| Claude Code | Verify the checkout exists and is the configured root; the project ID is the configured value. | Start `claude` in a new visible terminal in the checkout with `prompt` as its first prompt. Return its session UUID as `thread_id`. | Read the session transcript under `~/.claude/projects/`, or find the marker in it. |

Standing relay serve per runtime:

- **GitHub Copilot app:** in the relay chat, attach a session automation with
  `save_session_automation` (`interval: "minutes"`, `every_minutes: 5`). Its
  prompt is to serve the relay until told to stop. It wakes the same chat with its existing
  context. A wake that arrives during a running turn waits for that turn. Worst
  case, draining resumes about 5 minutes plus turn-start delay after the relay
  stopped, while the app is open.
- **Codex desktop app, GitHub Copilot CLI, Claude Code:** unverified. Use the
  runtime's own scheduled or recurring prompt if it has one, or an OS scheduler
  that resumes the relay session with a serve prompt. Record the mechanism and
  its measured bound here after the first muster.

**Acceptance gap:** The relay and fixture pass one shared contract test, and
the PH-11b muster on the GitHub Copilot app accepted it natively: two real
visible workers, a worker question answered through the Captain chat, and
Captain restarts around a receipt, recorded in
[the worker recovery report](docs/pilots/worker-recovery.md). Relay stall
reporting and overlapping servers are covered by tests with a simulated
scheduled wake. The runtime scheduler wake itself still needs a live muster.
Wider PR stages and due external rechecks remain integration work; this is not
full PH-11 or v1 acceptance.

### Load trusted validation policy

The callable [trusted validation policy loader](docs/runner-validation-policy.md)
loads per-repository local checks, authorized bypasses, and draft/ready
requirements from a separate operator-owned runner file. It is not yet wired
into the attended CLI; preview adapters and draft-only recovery remain explicit
integration requirements.

### Reconcile an attended reservation

From a fresh Captain chat, inspect the runner's original configuration and state directory. Run one recovery step with the runner token.

```powershell
python -m phalanx attended-recover --config runner.json --state C:/phalanx-runner --slot 1 --observation observation.json
```

The Captain supplies a fresh JSON snapshot from the runtime's native session history and session reads. It uses the same format `relay observe` writes, so a saved observe reply can be passed as is. Set `observed_at` to the time of the snapshot as ISO 8601 with an offset. Include `discovery_complete` and a `threads` array with one row per worker session. Set `discovery_complete` only after inspecting all relevant history pages, including archived history where relevant. A session already bound to a native receipt can be read directly.

Each thread row includes the following fields.

| Field | Source |
| --- | --- |
| `host_id`, `project_id`, `thread_id`, `started_at` | Native host, project, thread UUID and offset-aware original launch time. |
| `marker`, `assignment` | The exact first-turn attempt marker and the original packet's `result_identity` object from the first prompt. |
| `state` | Use `active`, `stopped`, or `unknown`. Attest `stopped` only after confirming the native turn ended and accounting for any worktree writer it started. A missing thread or ambiguous state is `unknown`. |
| `writers_stopped` | Required as `true` on a `stopped` row, after accounting for every worktree writer the session started. |
| `blocker` | Optional. Why the session cannot progress until the owner acts, such as a pending repository trust prompt. Never on a `stopped` row. |
| `client_thread_id` | The queued UUID when the native launch can be positively correlated with its saved queued receipt. Otherwise omit it. |

A saved stop request can be replayed without another observation file. A `retry` response with `retry_at` gives the earliest next probe as Unix seconds. After an `owner_wait`, address the reason before using `--recheck`. An unmatched server attempt requires the original journal or explicit recovery of its writer. The recovery command verifies the local Git checkpoint and checks any result against the original packet. An expired attempt with an active writer or a verified result is adopted under a recovery claim. Any stage other than development leaves the checkout unchanged, so its saved result is verified against the unchanged worktree. The claim request and its fresh secret are journaled before sending, so a lost reply replays the same key. Afterwards the Captain renews and submits under the new lease. If the server refuses adoption, the reservation backs off and then waits for the owner; nothing is released. For a verified result with a live lease, use the exact receipt with the existing `attended submit` command. A reservation with no relay, receipt or submission is released only after `--recheck` proves no launch happened: no result file and a clean worktree at the accounted revision, or no worktree or branch at all. Use the original launch path to replay an unconfirmed claim, which reuses the saved claim request.

### Import a GitHub issue

Register the expected repository remote first, then import an issue by its number
and the **Phalanx repository ID** (not `owner/repo`):

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx ingest-issue example 42 --target ready_for_check_in
```

Set `PHALANX_OWNER_TOKEN` for the API and `PHALANX_GITHUB_TOKEN` in the local
environment with read access to the issue and comments. The GitHub token stays
local; only the snapshot is submitted to the API. The command verifies the
registered `github.com` remote, rejects PR numbers, and copies the title, body,
all comment pages and source link into a work item. It prints the stored item
and execution, which appear on the work board and use the existing dispatch path.
Closed issues can also be imported explicitly. Issue text remains task context,
not authority to change credentials, checkout trust, runner policy or permissions.

Repeating an unchanged import with the same target returns the original item,
including after a server restart or a lost API response. An edited issue or
changed target returns HTTP 409 under the same stable issue key: inspect the
existing item before deciding whether to steer/rerun it or intentionally create
a new import with `--key NEW-KEY`. Keep that key for its retries. Imports exceeding
the 16,000-character goal limit fail without creating work or truncating context;
file a scoped summary with `intake` instead. Provider/authentication failures are
reported and never create partial work. This is explicit intake, not automatic
polling, webhooks, or synchronization; it does not modify the GitHub issue.

### Operator work board

Open `http://127.0.0.1:8000/operator/` on the running server. With `PHALANX_LOCAL_BOARD=1`, as in the local setup above, the board opens without a token: its read-only routes (`/v1/operator/*` and the stored report route) skip authentication for requests from a loopback client with a `localhost`, `127.0.0.1` or `[::1]` `Host` header. Every other route still requires its token. Leave the flag unset on a hosted server, including one behind a reverse proxy or tunnel on the same machine, because those connect from loopback too. Without the flag, the board asks for the owner token and holds it in memory until you disconnect or reload the page. The board uses the same-origin read API and opens stored field reports as plain text. Its static assets use origin-root paths and hold no secrets, so a hosted server serves the board at `/operator/` on its own origin.

The board shows each item's prerequisite blockers, any scheduled retry or external recheck, its worker reference and chat link, its current PR and its final output. It is read-only. When an item is waiting for an answer, its detail shows the question and the `reply` command to answer it with. See [Answer a waiting item](#answer-a-waiting-item).

### Back up and restore SQLite

These are server-host-only admin commands: they open the server's database file directly rather than going through the API, so run them on the server host, never from a runner. Run them in the same environment as the server, or set `PHALANX_DB` to the same absolute path. The backup command uses SQLite's snapshot API and can run while the server is up. The destination directory must already exist and support hard links, such as NTFS, APFS or ext4. Each command validates its copy before creating the destination and refuses to overwrite an existing file.

```powershell
New-Item -ItemType Directory -Force .phalanx/backups | Out-Null  # POSIX: mkdir -p .phalanx/backups
python -m phalanx server backup .phalanx/backups/phalanx-01.sqlite3
```

For recovery, stop the server and point `PHALANX_DB` at a fresh path in an existing directory. Restore the backup there, restart the API using the same path, and verify `GET /health`. Keep the old database until you've verified the recovered data.

```powershell
$env:PHALANX_DB = "$PWD/.phalanx/phalanx-restored.sqlite3"  # POSIX: export PHALANX_DB="$PWD/.phalanx/phalanx-restored.sqlite3"
python -m phalanx server restore .phalanx/backups/phalanx-01.sqlite3
python -m uvicorn phalanx.api:app --host 127.0.0.1 --port 8000
```

### Develop the API

#### Dispatch and leases

Owner routes set dispatch policy. Runner routes require the runner bearer token.
In the local deployment, the runner token identifies a trusted runner role.
Use one stable runner ID per local runner across restarts. The ID scopes
capacity and recovery within that trusted role.

| Method and path | Request and behavior |
| --- | --- |
| `PUT /v1/dispatch/settings` | Owner sets `paused` and optionally `aging_seconds` (default 300). A pause stops new assignments while existing leases, results, and recovery continue. |
| `PUT /v1/dispatch/runners/{runner_id}/capacity` | Owner sets `capacity` (1–256, default 4 per runner). Reductions take effect for the next claim and leave all reservations in place. |
| `POST /v1/dispatch/claim` | Runner sends `runner_id`, `repository_ids` (1–100 local checkout mappings), optional `slots` to lower the capacity for this muster, and optional `stages` to claim only supported stages. Send a fresh cryptographically random secret, for example Python's `secrets.token_urlsafe(32)`, in `Phalanx-Lease`. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/renew` | Runner sends the original `Phalanx-Lease` before expiry. The response gives `lease_expires_at` in Unix seconds. Renew independently of worker observation, by default every 60 seconds. The optional body carries `worker` (`reference` and HTTPS `url`) and `blocker`, the runner's current blocker text; a renewal without `blocker` clears it. |
| `GET /v1/dispatch/runners/{runner_id}/recovery` | Runner lists expired reservations for its ID with the item, execution, attempt, stage, current execution version, and reconciliation action. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/recover-stopped` | Runner supplies `item_id`, `expected_version`, and a `reconciliation` note describing how it verified the worker stopped and accounted for downstream effects. Accepts `Idempotency-Key`. A live, replaced or changed attempt is rejected. |
| `POST /v1/dispatch/runners/{runner_id}/attempts/{attempt_id}/recover-claim` | Adopts an expired open attempt under the new `Phalanx-Lease` secret, with the same body as `recover-stopped`. The attempt, version and stage stay the same, and the old secret stops working. Accepts `Idempotency-Key`. A live lease (renew it instead), an ended attempt or a changed execution is rejected. The adoption appears in both timelines as a `recover_claim` event at the same version. |

A claim returns HTTP 200 with one of `claimed`, `paused`, `at_capacity`, or
`no_eligible_work`. A successful assignment includes the work request,
execution and version, attempt ID, and lease expiry (`PHALANX_LEASE_SECONDS`
after the claim, 300 by default).
Pass its attempt ID, execution version, and stage to the existing result route
with the same lease header. Keep the bearer token and lease secret with the runner.
After a lost claim response, replay the same body and lease secret to retrieve
the original assignment. Deduplicate by attempt ID and verify that the lease
and execution are still current before starting work on a replayed assignment.
Empty polls leave no receipts. The server stores only the lease-secret digest.

An intake item can set `runner_affinity` to require a specific runner ID.
For matching repositories and affinity, urgency rises one band per aging
interval. Within the effective band, the server chooses the oldest eligible
timestamp and breaks ties by item ID. Execution views report
`ready_since_ms` and `retry_at_ms` as Unix milliseconds, and `retry_count`
as the retries scheduled in the current stage. A reconciled retry
starts accruing age when its delay elapses. A newly queued stage gets its
own clock. The claim index excludes blocked stages. A stage begins to age
once its last prerequisite clears and any retry delay elapses.

#### Prerequisites and queued steering

An owner can include `dependencies` in `POST /v1/items`. Each entry names
an `upstream_id` and a `milestone` of `report`, `ready_for_check_in`, or
`checked_in` (the default). Prerequisites may belong to other repositories.
Item inspection includes configured links and the execution's
`unmet_dependencies`, with an explanation for each wait.

An owner can send `PATCH /v1/items/{item_id}/executions/{execution_id}` with
`expected_version` and changes to `goal`, `context_links`,
`acceptance_criteria`, `priority`, `runner_affinity`, or `dependencies`.
Omitted fields keep their values. An empty list clears a list field, and
`null` clears runner affinity. The patch accepts `Idempotency-Key`.
Stale versions and executions outside the queued state return HTTP 409.

A successful claim includes a `dependencies` snapshot with each upstream
execution ID, output reference, and exact revision. For a reviewed PR it
includes the reviewed head and base. For a check-in it also identifies the
verified merge commit. Use these revisions in the work packet. The snapshot
is recorded on the attempt's claim event and returned on claim replay even
if an upstream execution is rerun.

Milestone changes refresh up to 32 dependents in the upstream transaction.
The API processes larger fan-outs and restart audits in indexed batches
across short transactions. Queued dependents in later batches may become
eligible after the upstream transaction commits. The claim transaction
always checks the current durable evidence before assigning work.

An expired lease prevents stale results and renewals. Its attempt still holds
the slot and item reservation. Leave uncertain or live workers reserved.
After confirming that a worker stopped and that repeating the stage is safe,
submit stopped recovery. It records the reconciliation and uses delays of
5, 10, 20, and 40 seconds for the first four retries. A fifth stopped attempt
waits for owner intervention. If cancellation is already pending, stopped
recovery closes it with a stored cancellation report. PH-12 extends recovery
to launch intent, live session reattachment, and collection of finished
results under a recovery claim.

#### Transactions and tests

Use the `require_owner` and `require_runner` dependencies in `phalanx.auth` to declare route permissions. Synchronous handlers read one consistent snapshot with `phalanx.store.read_transaction(path)` and the path in `request.app.state.database_path`. It takes its read lock between serialized writes: a read that arrives during a write starts before the next writer, and one that arrives while a writer waits starts right after that writer. So neither reads nor writes wait more than one turn of the other, however deep the write queue gets in DELETE journal mode. The gate adds no timeout of its own, because one write turn may legitimately outlast the busy timeout across `BEGIN` and `COMMIT`. Don't call it inside a write transaction. Use `phalanx.store.write_transaction(path)` for writes, including all reads that inform a write. It serializes write transactions in the server process, starts with `BEGIN IMMEDIATE`, and commits or rolls back on exit. Keep the transaction short and do all external I/O outside it. Connections use foreign-key enforcement, `synchronous=EXTRA`, and a two-second busy timeout. When the store stays locked or busy past that timeout, any route returns 503 with `Retry-After: 1` and the detail `SQLite store unavailable (<SQLite reason>); retry the same request`. Lease checks and idempotency keys make retrying the identical request safe. Other SQLite operational errors, such as a missing table or a read-only file, stay 500 because retrying won't fix them. Add schema changes as a new tuple of SQL statements at the end of `MIGRATIONS` in `phalanx.store`. The migrator runs the SQL and updates the schema version in one transaction.

Run the localhost HTTP and persistence tests with `python -m unittest discover -s tests -v`. The [CI workflow](.github/workflows/ci.yml) runs the same suite with the locked dependencies on every pull request and push to `main`. It covers Windows with CPython 3.12 and 3.14 and Ubuntu and macOS with CPython 3.14. Each of those runs is split into three parallel jobs with `python tests/shard.py INDEX 3`, which deals the discovered tests round-robin so each test runs exactly once, and the single `Tests` check passes only when every shard passes. Test modules share fixtures by importing sibling modules such as `test_bootstrap`, so run one module with `python -m unittest discover -s tests -p test_intake.py` rather than `python -m unittest tests.test_intake`. Git fixtures set their own commit identity and address bare repositories with `--git-dir`, so they need no global Git configuration and work with `safe.bareRepository=explicit`.

### Answer a waiting item

With the owner token configured, poll the local question inbox and use the item's IDs and version from the result:

```powershell
python -m phalanx inbox
python -m phalanx reply ITEM_ID EXECUTION_ID "Use the existing interface" --version 3 --key reply-001
```

A reply records who answered: `--answered-by owner` (the default) or `--answered-by captain` when the Captain answered a routine question itself. Reuse the same key and body if retrying the reply after a lost response. For more inbox entries, pass the last entry's `waiting_since` to `--after-time` and its `execution_id` to `--after-id`.

Runners submit a structured `question` with `prompt`, `handoff`, optional `options` (a list with one choice per element), and optional `proposed_default` in a stage result that needs input. The next claim after a reply includes `input_reply` with the answer, `answered_by` and question. The authenticated execution timeline retains them, and `inspect` shows the current execution's `last_reply`.

Runners can post `/external-wait` under an execution with the live attempt lease, expected version, and an idempotency key. Include the attempt and stage, a reason, a gate ID, the expected head and base, and an optional provider retry delay in seconds. Poll `GET /v1/runner/due-rechecks?limit=50` for due waits and post `/recheck` under each execution with its listed version and a stable idempotency key. Both operations use the runner token. A recheck queues the same stage for verification. Provider observation and recurring polling are runner integration work.

### File and inspect work

Set `PHALANX_OWNER_TOKEN` to the same owner token used by the server. Set `PHALANX_API_URL` or pass `--api-url` before the command to override `http://127.0.0.1:8000`.

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
python -m phalanx inspect <item_id>
```

Register the expected remote without credentials. Use `ready_for_check_in` for a reviewed PR or `checked_in` for a verified merge. These are intake targets. The [runner PR observation API](docs/runner-pr-observations.md) records evidence for publication, promotion, and PR validation.

The authenticated API exposes these routes under `/v1`.

| Route | Access | Purpose |
| --- | --- | --- |
| `POST /repositories` | Owner | Register an ID and expected remote. Repeating the same registration returns it. |
| `GET /repositories` | Owner or runner | Read registered repositories. |
| `POST /items` | Owner | File a new item with an initial queued execution. |
| `GET /items/{item_id}` | Owner or runner | Read the request and current execution, including its version, stage, status, wait and output. |
| `GET /items/{item_id}/executions` | Owner or runner | Read past and current executions. |
| `GET /items/{item_id}/executions/{execution_id}/timeline` | Owner or runner | Read durable transition history and evidence. |
| `GET /items/{item_id}/executions/{execution_id}/drafts/{revision}` | Owner or runner | Retrieve the exact draft for review. |
| `GET /items/{item_id}/executions/{execution_id}/report` | Owner or runner | Retrieve the final stored Markdown. |
| `POST /items/{item_id}/executions/{execution_id}/cancel` | Owner | Request cancellation with `expected_version` and `reason`. |
| `POST /items/{item_id}/executions/{execution_id}/rerun` | Owner | Create a linked execution from the current closed execution with `expected_version` and an explicit `target`. |
| `POST /items/{item_id}/executions/{execution_id}/result` | Runner | Submit `outcome`, stage-specific evidence and the current attempt preconditions. |
| `POST /items/{item_id}/executions/{execution_id}/stop-confirmed` | Runner | Confirm a cancellation under a live lease after accounting for the worker and its effects. |

Intake accepts `repository_id`, `goal` and `target`, plus optional `context_links`, `acceptance_criteria`, `target_branch` and `priority` (`low`, `normal` or `urgent`). Use an `Idempotency-Key` header when retrying a mutation. The server returns the original response for a replay of the same validated request in its operation scope, or HTTP 409 for a different request under the same key. Registration is keyed by the repository ID and unique remote. Version conflicts also return 409. Versions start at 1 for each execution. A rerun preserves the prior execution and keeps the work item's output type.

Results and stop confirmations require a matching `attempt_id`, `stage` and `expected_version`, plus the current secret in the `Phalanx-Lease` header. A successful report-drafting result sends `report_markdown`. The server persists it and derives a SHA-256 revision. Report review and storage send `report_revision`. The server copies the reviewed draft into final storage in the same transaction that closes the execution. A `needs_input` result supplies `question.prompt` and `question.handoff`, with optional `options` and `proposed_default`. Active cancellation preserves the attempt while awaiting reconciliation. An expired lease or uncertain provider effect requires recovery before confirmation.

#### Queue storage handoff

Migration 2 stores execution snapshots as JSON and exposes `stage` and `status` as queryable columns. It enforces one open execution per item and one open attempt per execution. PH-06 can select a queued execution and call `intake.claim_attempt` inside the same `store.write_transaction` after checking admission. Supply a stable `runner_id`, the SHA-256 hex digest of a freshly generated URL-safe lease secret, and `lease_expires_at` as Unix seconds. Return the plaintext secret only to the claiming runner. The claim returns the new attempt ID and execution snapshot. It appends history, increments the state version and changes the execution to running. A committed result ends the attempt. Renewal and recovery should use the persisted attempt ID, runner ID, lease digest and expiry. A cancellation request keeps the attempt open until stop confirmation or recovery.

PH-06 owns candidate indexing, time eligible, aging, capacity, admission and lease renewal. PH-08 owns reply and external recheck routes. PH-09 owns general failure finalization and retries. Provider verification and recovery of uncertain writes belong to the GitHub and runner integrations. The result API rejects caller-supplied PR observations. The operator API provides bounded history reads for the board.

### Read the operator board data

Both owner and runner bearer tokens can call these routes.

| Route | Response |
| --- | --- |
| `GET /v1/operator/executions?kind=open` | `items` and `next_cursor`. Input waits appear first. Each group is ordered by execution creation time, oldest first. |
| `GET /v1/operator/executions?kind=closed` | `items` and `next_cursor`. Closures appear newest first, including outputs from earlier executions of rerun items. |
| `GET /v1/operator/executions/{execution_id}/timeline` | The execution summary in `item`, the current attempt ID in `attempt`, newest-first transition `events`, and `next_cursor`. A `reply` event carries `answered_by`, and a `recover_claim` event records an adoption. |

Each route defaults to 25 entries and accepts `limit` from 1 through 100. Pass the
returned `next_cursor` as `cursor` on the same route and list kind until it is
null. Pages read live state. An open execution entering or leaving the input-wait
group can cross the cursor between requests. Refresh from the first page to
see the current order. New timeline events and closures normally precede the
current cursor. The timestamp and version fields describe the persisted state.

`last_advanced_at` records entry into the current stage. `last_progress_at` records
the latest execution transition. Wait details include the stored reason, resume
trigger, and age of the current recorded wait. A scheduled retry is available
as `retry_at_ms` and a due external gate recheck as `recheck_at_ms`, both in Unix
milliseconds. `primary_pr_url` represents the recorded primary PR. `worker_reference`
is the latest attempt's opaque worker ID, which the runner republishes on each lease
renewal. `worker_url` is the bridge's optional HTTPS chat link. When the runner is
blocked on an item it would otherwise show as plainly running, such as an
unreachable runtime or an ambiguous worker, it sends the reason as `blocker` with
each renewal and the item's `wait_reason` shows it until a renewal omits it. `version` is the
execution version that a reply must name. The timeline lists blocking `prerequisites`
and, while the item is waiting for input, the current `question`. The draft state has
no current read mapping. Report output URLs use the authenticated report route.
