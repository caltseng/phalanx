# Captain relay and bridge

The Captain loop reaches the worker runtime through one runtime-neutral bridge contract. By default a Captain chat serves it through the relay; a trusted subprocess bridge can serve it instead. Each runtime maps the contract onto its own tools.

## Bridge contract

Both transports carry the same request: `contract_version: 2`, `operation` and `arguments`, answered by one JSON object. A bridge or Captain must reject any other version with an error rather than guess; version 1 used the old operation names. The contract names no runtime; the [runtime mapping](#runtime-mapping) says how each runtime serves it.

A subprocess bridge (`captain --adapter-command ...`) is invoked once per operation, with the request on stdin and the reply on stdout. Commands must not write diagnostic text to stdout. Nonzero exits, malformed replies, replies over 1 MiB and a 15-second command timeout are visible failures. Stdin and stdout use temporary files, not pipes whose inherited handles could keep a timed-out helper or loop shutdown waiting for EOF. Timeout terminates only the direct helper, not native workers or its process tree; an uncertain launch stays reserved. Bridges must detach native workers' stdio from these short-lived request files. Shutdown still drains in-flight runner and Git operations. The bridge receives no queue token or lease, and the Phalanx runner, owner and GitHub token variables are removed from its environment; keep the bridge trusted. The executable fixture `tests/fixtures/captain_bridge.py` tests this boundary without a runtime, and one shared contract test runs both the fixture and the relay.

| Operation | Arguments and required response |
| --- | --- |
| `resolve_project` | Receives `host_id`, `project_id`, `checkout`. Discover the exact project on that host and return those verified fields; do not echo unverified input. The checkout must resolve to the configured root. |
| `launch` | Receives `host_id`, original `marker`, and the prepared `session`: `project_id`, `title` and `prompt`. Start **one** visible worker session in that project on that exact host whose first prompt is exactly `prompt`, which contains the marker. Use `title` as the session name where the runtime allows it; otherwise use a name that fits, such as the attempt UUID from the marker. Return the same host and marker and exactly one UUID: the session's `thread_id`, or a queued `client_thread_id` when the runtime returns a provisional ID first. You can also return a `worker_url`, an HTTPS link to that session with no credentials in it, which the operator board shows. The runner ignores an invalid link and reports a warning; the launch still succeeds. |
| `observe` | Receives at most the configured batch of `references` (slot, host, project, original launch time, marker, assignment and receipt), per-marker `cursors`, and `timeout_seconds`. Return `observed_at` with timezone, boolean `discovery_complete`, `threads` (one row per worker session), and optional per-marker `cursors` (string or null to reset). |

Session rows use the [observation fields](recovery.md#observation-fields). A `stopped` row also requires `writers_stopped: true`. Observation timestamps must be at most 60 seconds old. Discovery is complete only after the relevant history pages have been reconciled, including queued or lost receipts; a cursor must not hide the first-turn identity or be treated as complete history. `thread_id` names the runtime's worker session UUID in every runtime.

## Captain chat relay

Start the loop without `--adapter-command`, then open a Captain chat in any runtime that can launch and read visible worker sessions in the mapped projects. The chat needs a shell with write access to the runner state directory. The loop queues each request in `<state>/captain-relay.sqlite3` and waits; the chat runs it with its runtime's tools and records the reply. Repeat this cycle while the loop runs:

1. `python -m phalanx relay --state C:\phalanx-runner next --wait 30` hands out the oldest request, or `{"status": "idle"}`. Output is ASCII-escaped JSON, so a non-ASCII prompt survives any pipe code page; decode it with a JSON parser before passing it on.
2. Serve the request with the runtime's tools, following the [contract](#bridge-contract) and [runtime mapping](#runtime-mapping):
   - `resolve_project`: discover the project; reply with the verified host, project and root path. If the runtime gates the repository on owner trust and it isn't trusted, reply with `--error` naming the checkout and the one-time [trust setup](install.md#grant-repository-trust-once), and tell the owner once.
   - `launch`: start the worker session **once**; reply with the host, marker and session UUID as `thread_id` (or a provisional ID as `client_thread_id`).
   - `observe`: read each receipt's session, or find the marker in session first prompts or history; reply with fresh rows. Set `writers_stopped` only after verifying the session's turn stopped. When the runtime's tools cannot read a session's first prompt, list the worker sessions and their state with those tools, then let the observe helper read the local transcripts and write the reply:
     `python -m phalanx relay --state C:\phalanx-runner observe ID --runtime RUNTIME --host HOST --session SESSION_ID STATE PROJECT_ID --complete --out reply.json`.
     Repeat `--session` for each listed session; `STATE` is `active`, `stopped` (writers verified stopped) or `unknown`. Pass `--complete` only when every worker session on the host was listed. The helper keeps only sessions whose first prompt carries a queued marker and its exact assignment.
3. Record the reply right away (observations go stale after 60 seconds): `python -m phalanx relay --state C:\phalanx-runner respond ID --file reply.json`, or `respond ID --error "what failed"` when the runtime action fails.
4. Between requests, run `python -m phalanx inbox` with the owner token and triage each worker question. When the answer follows from the repository, the issue, its conventions or the question's proposed default, answer it with `python -m phalanx reply ... --answered-by captain`. Escalate to the owner only when human judgment materially changes the outcome or authority is missing. `attended status --all` shows slot reasons. Tell the owner once about a `worker_overdue` slot, after inspecting the worker to say whether it is still making progress.

### Request lifecycle

Identical requests waiting to be taken share one entry. Verified project resolutions are cached for ten minutes and failed ones for one minute, so an untrusted repository costs the Captain one answer a minute. The loop waits up to five minutes for a request to be taken and five more for its reply.

- A launch nobody took expires and is retracted, so the same packet can launch again.
- A taken request is never handed out again, even after a Captain restart. An unanswered one is settled as lost: a later observation or resolution queues afresh, while an unanswered launch stays reserved until native discovery reconciles it by marker.
- A restarted loop expires requests its predecessor left pending longer than the timeout. A younger one may still be served; its waiter is gone, so the answer goes unused, and a launch is reconciled by marker.
- Once a slot no longer observes, the loop retracts its untaken `observe` requests, so the Captain never answers for a finished worker.

Never rerun a request whose outcome you did not record; answer it from discovery instead.

## Standing relay serve

The relay drains only while some agent turn is running the cycle above. A runtime's session tools can be called only from inside an agent turn, so the loop cannot serve or wake the relay itself. Make a standing serve part of muster setup:

- Serve from **exactly one** dedicated relay chat that repeats `relay next` without ending its turn. Keep owner conversation in another chat so it never waits on the relay.
- Attach the runtime's own scheduler to that chat so a wake restarts serving whenever its turn ended (see [per runtime](#standing-relay-serve-per-runtime)). Each wake runs the cycle until told to stop.

### One server at a time

The intended protocol is at-most-once: `relay next` hands each request to exactly one taker, so in principle a wake that lands while another server is active takes only requests nobody else took. The current implementation does not meet that reliably when servers overlap: item 108f6009 reproduced SQLite lock contention between overlapping relay servers that can lose a request. Until it is fixed, run one standing relay server per state directory, and make each scheduled wake resume that same chat rather than start a second server. If ownership is uncertain, follow [Relay ownership is uncertain](recovery.md#relay-ownership-is-uncertain).

### Stall reporting

When no server has polled or answered for `relay_stall_seconds` (180 seconds) while a request waits, the loop reports `relay_stalled` with the time of the first untaken request. It adds the reason to the lease renewal of every item whose worker needs the relay, so the operator board shows it as each item's wait reason, and notifies the owner. The first poll or answer after that clears the stall: the loop reports `relay_serving`, clears the board reasons on the next renewal, and notifies the owner again. It resumes with no restart: queued observations are taken where they wait, and an expired launch or resolution is queued afresh on the next poll.

Set `notify_command` in the [runner config](configuration.md#runner-config) to receive each `relay_stalled` and `relay_serving` event.

Recovery bounds: a stall shows within `relay_stall_seconds`, plus one loop poll and one heartbeat (about four minutes with defaults). Once a wake is due, draining resumes after the wake interval, plus the runtime's turn-start delay, plus up to one loop poll. The turn-start delay covers waiting behind a running turn of that chat, and the app or terminal must be open to fire the wake. An expired request costs at most one more poll to requeue.

## Runtime mapping

Each runtime serves the same three operations with its own tools. Set the config's `host_id` and `project_id` to the values the runtime reports. The Codex desktop and GitHub Copilot app mappings have field evidence in [the worker recovery report](pilots/worker-recovery.md); verify the others in their first muster and record any correction here.

| Runtime | `resolve_project` | `launch` | `observe` |
| --- | --- | --- | --- |
| Codex desktop app | `list_projects`; match the project ID, host and root path. | `create_thread` with `title`, `prompt` and target `{"type": "project", "projectId": project_id, "environment": {"type": "local"}}`. Return `threadId` as `thread_id` or `clientThreadId` as `client_thread_id`. | `read_thread` for receipts; `list_threads` history search for the marker. Use `codex app-server` only after verifying its threads appear in the app sidebar with the same identity as the app's tools. |
| GitHub Copilot app | `list_repositories`; match the project ID to its `id` and the checkout to its `main_repo_path`. Use `local` as the host. Confirm the checkout is trusted (listed in `trustedFolders` in `~/.copilot/config.json`); otherwise reply with an error that names the project ID and checkout path (two projects can share a name) and tells the owner to open a session in that project to get the app's trust prompt. | `create_session` with `project_id`, `workspace_type: "branch"` and no `base_branch` (the packet already names Phalanx's worktree), `name` set to the attempt UUID from the marker (`name` allows 40 characters, too few for `title`), and a kickoff with `prompt` in autopilot mode. Return the session ID as `thread_id`. | `get_session` for receipts; `list_sessions_and_chats` lists candidates. Both return only metadata, and no app tool reads a session's turns, so pass each worker session with its state to the observe helper with `--runtime copilot`; it reads the first `user.message` in `~/.copilot/session-state/<id>/events.jsonl` on the same machine. A session `get_session` reports busy is `active`; an idle session whose last turn ended is `stopped`. |
| GitHub Copilot CLI | Verify the checkout exists and is the configured root; the project ID is the configured value. | Start `copilot` in a new visible terminal in the checkout with `prompt` as its first prompt. Return its session UUID as `thread_id`. | Pass each session with its state to the observe helper with `--runtime copilot`, which reads `~/.copilot/session-state/<id>/events.jsonl`. |
| Claude Code | Verify the checkout exists and is the configured root; the project ID is the configured value. | Start `claude` in a new visible terminal in the checkout with `prompt` as its first prompt. Return its session UUID as `thread_id`. | Read the session transcript under `~/.claude/projects/`, or find the marker in it. |

### Standing relay serve per runtime

- **GitHub Copilot app:** in the relay chat, attach a session automation with `save_session_automation` (`interval: "minutes"`, `every_minutes: 5`). Its prompt is to serve the relay until told to stop. It wakes the same chat with its existing context, and a wake that arrives during a running turn waits for that turn. Worst case, draining resumes about 5 minutes plus turn-start delay after the relay stopped, while the app is open. Whether the automation survives a reboot is unverified.
- **Codex desktop app, GitHub Copilot CLI, Claude Code:** unverified. Use the runtime's own scheduled or recurring prompt if it has one, or an OS scheduler that resumes the relay session with a serve prompt. Record the mechanism and its measured bound here after the first muster.

### Acceptance gap

The relay and fixture pass one shared contract test, and the PH-11b muster on the GitHub Copilot app accepted it natively: two real visible workers, a worker question answered through the Captain chat, and Captain restarts around a receipt, recorded in [the worker recovery report](pilots/worker-recovery.md). Relay stall reporting and overlapping servers are covered by tests with a simulated scheduled wake, though item 108f6009 has since reproduced a lost request with overlapping servers (see [One server at a time](#one-server-at-a-time)). The runtime scheduler wake itself still needs a live muster. Wider PR stages and due external rechecks remain integration work; this is not full PH-11 or v1 acceptance.
