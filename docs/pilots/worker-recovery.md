# PH-01 worker recovery field report

This report records field evidence for each runtime that serves the runtime-neutral bridge contract (see the README's runtime mapping). The Codex desktop app was piloted first, for discovery and live observation. The PH-11b native muster then ran on the GitHub Copilot app as Captain; see [GitHub Copilot app muster](#github-copilot-app-muster-on-november-21-2026).

A local runner process handles durable attempt state, workspace preparation, lease renewal, evidence verification, and publication. The Captain chat serves launch and observation through the Captain chat relay.

## Live evidence on November 20, 2026

| Capability | Evidence and limits |
| --- | --- |
| App launch and project | The Captain created the pilot worker, “PH-01 Codex recovery pilot.” The worker received app delegation and its first turn recorded `create_thread`. `list_projects` identified Phalanx as a local Git project. `list_threads` returned the active worker with a project ID, thread UUID, `hostId: local`, and the primary checkout as `cwd`. The sidebar and original creation response were not inspected. |
| Workspace | The worker's app cwd was `C:/enlistments/phalanx`. It created `C:/enlistments/phalanx-ph01` with `git worktree add` from `origin/main` at `9800e4e6c7d870f5a95772a3353e95c5f0cad077` and targeted commands there explicitly. `git worktree list --porcelain` confirmed the separate branch and path. App-managed worktree launch and existing-worktree attachment were not exercised. |
| Known identity | `read_thread` with the worker's host and UUID returned its cwd, active status and in-progress turn. A zero-timeout `wait_threads` call against the separate Captain returned active status, turn data and a cursor. A call targeting the calling thread was rejected. These checks exercised fresh attachment for observation without restarting a Captain. |
| Unknown identity | A local Codex history query exposed the worker by its unique title, project and cwd without using the worker UUID as a request parameter. A read of a synthetic absent UUID returned a no-readable-match error. Discovery supplies candidates. An empty page or unreadable thread does not establish that a launch failed. No launch response was deliberately dropped. |
| Outcome and reply | This field report is the worker's concrete deliverable. A runner result, a live question/reply, and continuation of the same chat were outside this pilot. The pilot launched no extra test chats. |

## GitHub Copilot app muster on November 21, 2026

**Runtime.** The Captain was a GitHub Copilot app session on Windows. It served the [Captain chat relay](../../README.md#captain-chat-relay) and the app's runtime mapping row by hand. The server and the `captain` loop ran from `main` at `ae224b4` (#62) as detached local processes, with capacity 2. Both pilot checkouts were registered with `add_repository` (pilot-py `c690dc60-199b-4d4e-be4f-764d2a007d3d`, pilot-web `252f7d83-3066-4a2c-aa64-7cc7754c8ce7`, host `local`). Workers were `create_session` sessions with a branch workspace, the attempt UUID as name, and an autopilot kickoff. They appear in the app sidebar under their pilot projects.

**Items.** A PR item for [phalanx-pilot-py#3](https://github.com/caltseng/phalanx-pilot-py/issues/3) (slugify) and a report item for [phalanx-pilot-web#1](https://github.com/caltseng/phalanx-pilot-web/issues/1) (titleCase). The report goal told the worker to ask one question before drafting.

All times are UTC.

| Acceptance check | Evidence |
| --- | --- |
| Preflight stops before any claim | With `PHALANX_RUNNER_TOKEN` unset, `captain` printed "Runner preflight failed: Set PHALANX_RUNNER_TOKEN in the runner environment" and exited 1 at 13:25:32. The database still had zero attempts. |
| Two concurrent visible workers | At 13:37:28 `get_sessions_status` showed worker `062a34cb` (pilot-web, attempt `1ecb399d`) and worker `7ca37c63` (pilot-py, attempt `ac01c7b7`) busy in autopilot at once. Both slots were `attached` with recorded receipts. |
| Question round trip | Worker `a0d98b4b` (attempt `86600ba1`) returned `needs_input` at 13:29:50. The question reached `phalanx inbox`. Caleb answered "preserve" in the Captain chat (`ask_user`), and the Captain sent it with `phalanx reply` at 13:35:54. The next launch (`1ecb399d`, state version 5) carried `answer: "preserve"` and the earlier handoff, and its report records the chosen behavior. |
| Stored report | Draft `1ecb399d` succeeded at 13:38. Independent review `4a34ef57` (worker `f6db1b3a`) succeeded at 13:47. The runner stored the report at 13:49:31 (`report_storage_submitted`), and the item reached `completed`. The Markdown is readable at `/v1/items/8f640e3c8ea4487c820657d9accc52d9/executions/b618368c6e8e41d28a09f0426aac5700/report`. |
| Published draft | Development `ac01c7b7` succeeded with candidate `d8d5e4dc`. The runner published draft [phalanx-pilot-py#4](https://github.com/caltseng/phalanx-pilot-py/pull/4) at 13:45:38 with that head and the Phalanx footer. |
| Restart before the receipt | The Captain took launch `8190a328` at 13:26:27, created worker `a0d98b4b`, and never answered. A fresh child Captain session ("Captain A") then took over the relay. At 13:31:27 the loop settled the launch as lost and held the slot ("reconciled by native discovery"). Captain A answered the next observation by marker, with `stopped` and `writers_stopped`. The slot submitted the worker's existing result at 13:31:34. No second session was started for `86600ba1`. |
| Restart after the receipt | Captain A recorded receipt `7ca37c63` for `ac01c7b7` at 13:32:02 and stopped. The parent Captain recorded receipt `062a34cb` for `1ecb399d` at 13:37:05 and stopped serving. A second fresh child Captain ("Captain B") took over. It observed slot 1 by receipt as `active` at 13:38:15 and slot 2 by marker as `stopped` at 13:38:37. Slot 2 submitted at 13:38:41. Captain B launched nothing. |

After the run, each attempt marker had exactly one session in `list_sessions_and_chats`: `a0d98b4b`, `7ca37c63`, `062a34cb` and `f6db1b3a`. Each slot held one reservation throughout.

**How the restarts were done.** One app session cannot restart itself and keep serving. Each restart was a fresh child Captain session that started with no relay history. It got only the README procedure, the state directory and the relay helpers, and served `relay next` until a stop condition. Ending a Captain's turn mid-request is the same failure as a crashed chat: the request stays taken and is never handed out again.

**Where it stopped.** The report item completed. The PR item stopped at `self_review` (queued) after the draft was published, because no dispatcher stage on `main` serves `self_review` yet (PH-14a part 2).

### Findings

**What worked live**
- Every launch was relayed at most once, and every lost or late reply was reconciled by marker without a replacement writer.
- The preflight blocked a missing token before any claim.
- The owner reply resumed the report with its handoff, and the runner-only stages published the draft and stored the report without a worker.
- Per-mapping `resolve_project` errors blocked only the affected slots.

**What broke**
- **Repository trust.** The app asks the owner, once per repository, to trust it. Until then, every worker tool call is denied ("denied-no-approval-rule", unattended fallback). The worker then ends without a result, which the runner would count as a technical failure. Both pilot workers hit this on their first launch. Caleb trusted both repositories, and the blocked worker resumed in place. Agents cannot grant trust.
- **No transcript tool.** No app tool reads a child session's first user turn. `get_session` and `list_sessions_and_chats` return metadata only. The Captain read `~/.copilot/session-state/<id>/events.jsonl` (the `user.message` content and `session.start` cwd) with a small helper. This works only on the same machine.
- **Manual observe replies.** Building a schema-valid `observe` reply by hand within the 60-second freshness window was impractical. The Captain used a local helper for it.

**What surprised us**
- The stored first user turn drops the prompt's trailing newline. Exact-prompt checks must compare after trimming trailing whitespace.
- A single Captain serving launches one at a time took 25 to 42 seconds per launch. Observations queued meanwhile arrived late. One was discarded as aged ("Native observation aged while this slot was busy"), and one arrived with `receipt: null` because it was queued before the receipt was recorded. Both healed on the next poll.
- Observations queued just before a slot submitted stayed pending, and the Captain answered them after the slot was free.
- The draft PR title was the goal truncated at 256 characters, mid-sentence.
- Running `python -m phalanx` from a working directory containing another Phalanx checkout imported that checkout instead of the installed one. The muster scripts use `python -P`.
- The worker question went to Caleb, but its answer followed from the item's stated default. Caleb wants routine questions answered by the Captain.

Each gap has a fix row ahead of PH-12a in the [implementation plan](../../IMPLEMENTATION_PLAN.md#remaining-v1-integration-work).

## Live rerun 1 on November 21, 2026

**Setup.** The same Captain, checkouts and capacity as the muster above. The server and loop ran from `main` at `98fd6a9` (#63, #69) and were redeployed to `895e602` (#70) mid-run. Two fresh items: a PR item for [phalanx-pilot-web#2](https://github.com/caltseng/phalanx-pilot-web/issues/2) (clamp) and a report item for [phalanx-pilot-py#1](https://github.com/caltseng/phalanx-pilot-py/issues/1) (truncate) whose goal asks one question first. All times are UTC.

| Check | Evidence |
| --- | --- |
| Runner restart during a live worker | The loop and server were stopped at about 14:28 with worker `c885db53` (pilot-py, attempt `675d0560`) running and launch `1984c51c` (pilot-web, attempt `70661a69`) queued but not taken. After the restart, slot 1 kept its receipt and slot 2 was `launch_uncertain`. The new loop served the same pending launch once. `list_sessions_and_chats` showed one session per marker. |
| Captain-answered question | Attempt `675d0560` returned `needs_input` at 14:30:28. The Captain answered with the proposed default using `phalanx reply --answered-by captain` at 14:40:45. `inspect` shows `answered_by: captain`, and the next launch (`ad7b861c`, state version 5) carried the answer and `answered_by` in its handoff. Caleb was not asked. |
| Adoption (PH-12a) | Worker `9ded7d4f` wrote a `succeeded` result for `ad7b861c`. The Captain stopped the loop at 14:47:57 before it submitted, and waited past the 90-second lease. After the restart at 14:50:29 the loop reported "Awaiting native observation before recovery". It took a `stopped` observation and then called `recover-claim` (HTTP 200) for the same attempt. It submitted the result at 14:51:28 under the new lease. Nothing was relaunched or redrafted. |
| Stored report | Review `0e8d362b` (worker `cdfa1b5a`) succeeded. The report was stored at 14:54:51, and the item completed. |
| Published draft | Development `70661a69` (worker `6453a4f8`) succeeded with candidate `b8c06d9`. The runner published draft [phalanx-pilot-web#4](https://github.com/caltseng/phalanx-pilot-web/pull/4) at 14:45:47 with that head and the Phalanx footer. |
| Board (PH-18a) | The board's API showed `worker_reference` for both live items. The owner question with its options reached `inbox`, and the board script renders it. `recheck_at_ms` stayed null because no external gate was pending. The token was not typed into the browser, so this check used the API and the board script. |

**Where it stopped.** The report item completed. The PR item stopped at `self_review` (queued) after the draft, as before, because nothing on `main` dispatches `self_review` yet (PH-14a part 2).

### Findings

**What worked live**
- The runner restart, the Captain-answered reply and the adoption all ran without a duplicate session, attempt or draft.
- The report path, including a mid-run redeploy, finished without Caleb.

**What broke**
- **Relay output encoding.** `phalanx relay next` prints JSON with raw non-ASCII. On Windows, Python writes a piped stdout in the locale code page, so a Captain reading it through a shell got U+FFFD where the stored report had U+2026. The relay database and the stored report were correct. The Captain launched from the database copy. A Captain pasting the corrupted prompt would start a worker whose first turn fails the exact-prompt check.
- **No proposed default.** The `needs_input` result contract has no `proposed_default` field. The worker put its default inside `options`, so the board's "Proposed default" line never shows.
- **Silent history.** The timeline has no event for the adoption (version 5 `claim` is followed directly by version 6 `result`), and the `reply` event does not carry `answered_by`. Only `inspect` shows who answered.

**What surprised us**
- A finished Copilot app session ends its transcript with `session.shutdown`, not `assistant.turn_end`. The muster's busy check had to treat both as idle.
- The muster's own relay script once hung after taking an observation. The loop showed it as "took observe but never answered" and later "never took observe". A restarted script recovered with no lost slot.
- A queued `self_review` that nothing dispatches shows `queued` on the board with no wait reason. PH-14a part 2 removes this case.

The gaps have a fix row (PH-11f) in the [implementation plan](../../IMPLEMENTATION_PLAN.md#remaining-v1-integration-work).

## Live rerun 2 on November 21, 2026

**Setup.** The same Captain, checkouts and capacity as above. The server and loop ran from `main` at `c7ba138` (#71, #74, #75, #79). `runner.json` gained a `validation_policy` file outside every checkout. It runs each repository's unit tests as a local check and requires both pilot CI check runs as ready checks (`test (ubuntu-latest)` and `test (windows-latest)`, GitHub Actions app ID 15368, confirmed with `gh api`). Items: a fresh PR item `23891c11` for [phalanx-pilot-py#1](https://github.com/caltseng/phalanx-pilot-py/issues/1), and a report item `46618208` for [phalanx-pilot-web#1](https://github.com/caltseng/phalanx-pilot-web/issues/1) whose goal asks one question. Old items `66a07152` ([phalanx-pilot-web#4](https://github.com/caltseng/phalanx-pilot-web/pull/4)) and `ef9fe2cf` ([phalanx-pilot-py#4](https://github.com/caltseng/phalanx-pilot-py/pull/4)) were queued at `self_review`. All times are UTC.

| Gate | Evidence |
| --- | --- |
| (a) Untrusted checkout (PH-11d) | Phase 1 mapped pilot-py to a fresh, untrusted clone. The Captain answered `resolve_project` with the setup action. The loop's `mappings` status named that mapping, both pilot-py slots stayed blocked with the action, and nothing launched for pilot-py. pilot-web launched `aa176aa3` (web#4 `self_review`) and `30092045` (report) in the same passes. Phase 2 swapped in the trusted mapping and restarted the loop, and pilot-py dispatched normally. |
| (b) Observe without manual steps (PH-11e) | The Captain's relay script served every `observe` through `phalanx relay observe --runtime copilot`, which read each transcript and matched marker and assignment. No observation was hand-written. One Captain-side bug: the script's busy check counted any recent `assistant.turn_end` as stopped, but an autopilot session starts its next turn right away. The loop got "No final result file" and blocked the slot. It kept the reservation and recovered on the next correct observation. The fixed check prefers the last turn event plus 30 seconds of quiet. |
| (c) Encoding, default and history (PH-11f) | `relay next` printed ASCII-escaped JSON. Every first turn matched its prompt exactly, including U+201C, U+2013 and U+2026. The report question carried `proposed_default` ("A) Preserve whitespace…"). The adoption event is **not shown**. Adoption failed (see below), so there was no `recover-claim` to record. |
| (d) Owner answer (PH-18a) | The question reached the owner inbox at 16:12:04 (item `46618208`, execution `b0b253cf`, version 3). The board showed the wait reason, question, proposed default and worker reference `30092045`, with resume trigger "owner reply". Caleb's answer was **pending** when this section was written. |
| Validation path (PH-14a part 2) | web#4 (`66a07152`) ran `self_review` → `pre_pr_validation` → `pr_promotion` → `pr_validation` → `adversarial_review` (versions 6–13, 16:10–16:12). The PR is ready, head `b8c06d9`, with both checks green. py#4 and the fresh item took the same path. |
| Fresh PR item | `23891c11`: development (worker `216815e3`) published draft [phalanx-pilot-py#5](https://github.com/caltseng/phalanx-pilot-py/pull/5) at 16:25:55, head `5dab552`. Its title is cut at 72 characters, and the footer is intact. After recovery, `self_review` (worker `44a7074b`, state version 8) ran through `adversarial_review` in 11 seconds (16:33:19–30). CI was already green, so no external recheck was scheduled. |
| Adoption (PH-12a) | `self_review` worker `06e757ba` (attempt `acc910f8`) wrote a `succeeded` result at 16:27:01. The loop was stopped before submitting, and the restart came after the 90-second lease had expired. The loop observed `stopped`, then retried "Local checkpoint or output reconciliation is incomplete" four times and moved to an owner wait. The operator archived the result and ran `attended-recover --recheck`, which requeued `self_review` (a `retry` event at version 7). |

**Where it stopped.** All three PR items reached `adversarial_review` (queued) as ready PRs with green checks. Nothing on `main` dispatches it yet (PH-15a part 1). The report item waits for Caleb's answer.

### Findings

**What worked live**
- The trust check blocked only the untrusted repository and named the fix before any launch.
- The validation path promoted three PRs from draft to ready on real check runs, with no Captain involvement beyond launches and observations.
- Prompts, defaults and the owner question crossed the relay intact.

**What broke**
- **Review-stage results cannot be adopted.** `_verify_local` in `attended_recovery_service.py` verifies a saved result only for development and the report stages. A `self_review` result on an unchanged checkout falls to "unresolved", so recovery fails and the work redoes.
- **`attended-recover --observation` rejects `relay observe` output.** It raised `TypeError: unexpected keyword 'writers_stopped'`. The operator had to edit the file.
- **Options arrived as one string.** The worker put both choices into a single `options` element.

**What surprised us**
- `README.md` said a restarted loop expires all requests its predecessor left pending. It only expires those older than the timeout. A stale pilot-py `resolve` from the previous loop was served after the swap. The answer went unused, so nothing broke. This PR corrects the README.
- Status noise: a two-second `slot_blocked` ("Native discovery has not established one exact writer") right after a receipt, and an `unmatched_attempts` entry while the operator's `attended-recover` ran.

The gaps have a fix row (PH-11g) in the [implementation plan](../../IMPLEMENTATION_PLAN.md#remaining-v1-integration-work).

## Attended path for PH-11

1. Start the runner independently of the Captain. On an authorized muster, claim a stage, reserve a slot, and prepare the item's worktree. Persist launch intent before calling the app. Include the item/stage/attempt IDs, one unique marker, host, project, worktree, expected revision and state version, and start time. Give one Captain launch ownership of the attempt. The runner retains queue credentials and leases.
2. In the Captain chat, call `list_projects` to resolve the local project and host. Call `create_thread` with `target: { type: "project", projectId, environment: { type: "local" } }`. Put the exact attempt marker in both title and first prompt, and include the stage packet. The app requires an explicit human request to create tasks, so the muster request needs to cover worker chat launches. Creation is asynchronous. A ready receipt provides `threadId` and `hostId`. A queued receipt can instead provide `clientThreadId`, which cannot be passed to tools that require `threadId`. The launch schema exposes neither a caller-supplied idempotency key nor a field for an existing worktree path.
3. Route workspace writes through PH-10's verified Git worktree. For project-local launch, include the absolute path, repo, branch and expected revision in the packet. The worker verifies its Git root and branch before writing and uses explicit paths or cwd for edits and commands. This relies on worker/tool discipline because the app cwd is the project checkout. If the worker cannot consistently target the worktree, surface a workspace blocker before writes. App-managed `worktree` mode requires an explicit request and a Git project. It accepts a branch or working-tree starting state. `attach_worktree(path)` targets the parent chat under its installed contract. Reusing an arbitrary worktree as a new chat's app cwd needs a separate live test. [Codex worktree documentation](https://learn.chatgpt.com/docs/environments/git-worktrees)
4. The Captain uses the Phalanx CLI to relay app receipts and observations to the local runner. Record the native host and thread UUID before using results. Store an app-provided open reference when available. Use `wait_threads` with bounded waits, rotating batches of up to eight targets. Treat cursors as an optimization and request a fresh snapshot after a restart or cursor failure. Use `read_thread` for the launch marker, workspace context and terminal turn. Verify the stage outcome independently of chat status.
5. A proposed result transport is an attempt-specific JSON file at a runner-provided absolute path. Have the worker write a sibling temporary file, rename it into place when complete, and point to the outcome in its final chat message. The runner checks identity, contract, workspace and revision, confirms the prior writer has stopped, and submits using the current lease and version. GitHub writes stay with the runner. For `needs_input`, store the question and checkpoint before releasing the slot. The owner replies through the API or CLI. Resume under a new claim with the reply and handoff. Same-chat continuation is optional and requires explicit authorization to message the destination under the installed app contract, plus verification that the old turn has ended and the new claim owns the workspace.

The Captain is the app-tool caller. The runner uses Git and Phalanx's local interfaces and renews leases independently of chat polling. The installed shell reported `codex-cli 0.87.0`; its help exposes `resume [SESSION_ID]` and experimental App Server tooling. App Server documents thread and turn operations for its own clients. CLI-to-desktop sidebar placement, session identity equivalence, and takeover of a live desktop session were unverified. Keep CLI execution as a distinct adapter unless that integration is established. [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)

## Recovery for PH-12

These cases describe proposed behavior under the [execution rules](../../STATE_MACHINE.md). Restore reservations before admitting more work after a runner restart.

| Interruption | Resolution |
| --- | --- |
| Observation lost after recording native identity | Start a fresh observer with the recorded host and UUID. Keep the slot and worktree while the worker is active. For a finished turn, verify its result and checkpoint. If the lease expired, adopt evidence only under a new server-authorized recovery claim. If the worker stopped without a valid result, reconcile edits and outputs and retry from the verified checkpoint. |
| Launch response lost or setup pending | Keep the slot and worktree reserved. Page local history with bounded retries, including archived history when relevant. Filter by host, project, launch time and marker. Read candidate first turns to verify the exact marker and assignment. Bind a single verified match. Multiple matches, inaccessible history and missing matches leave the launch uncertain. Check local and external outputs before considering a replacement. |
| Rediscovery inconclusive | Show the attempt marker, workspace, latest check and why the slot is held. The smallest operator action is to identify the existing chat for association with the attempt, or confirm in the app that launch never happened or every matching writer has stopped. Record the evidence before releasing the reservation. Continue unrelated eligible work within remaining capacity. |
| Worker outlives its lease | The expiry does not stop the chat or fence filesystem writes. Keep the reservation while the worker may still write. Reject stale results and reconcile before adopting any verified evidence. |

## Remaining acceptance checks

The GitHub Copilot app muster closed these checks: two workers in distinct projects with sidebar placement, a schema-valid outcome, a question through the owner reply and next claim, and Captain interruptions before and after durable identity recording. Queued creation does not apply to the Copilot app, which returns the session ID synchronously. Still open: restart the runner during a live worker and stall the API. Verify that the runner recovers a known worker, discovers an accepted launch with no saved ID, and retains one slot and worktree for an ambiguous launch while unrelated work continues. Confirm that no replacement can write before reconciliation. Test app-managed worktrees and existing-worktree reuse if the adapter needs either operation.
