# PH-V1 release acceptance muster

The muster ran on November 22, 2026 against Phalanx `main` at `f5de3ca`. **All five gates passed in one muster.** It used only normal entry points: no fixtures, no test doubles and no manual state edits. Times below are UTC.

## Setup

| Part | Detail |
| --- | --- |
| API server | `phalanx server` from `main` `f5de3ca`, started with the muster's `server.ps1` at `http://127.0.0.1:8000`, on SQLite in the muster's `db` directory. Caleb watched `/operator/` during the run. |
| Runner and loop | `phalanx captain --config runner.json --state runner`, with capacity 2, both acceptance repositories, `validation_policy` from outside every checkout, and `auto_merge` left at its default (on). The muster scripts from rerun 2 still matched the CLI and were not changed. |
| Captain chat | A GitHub Copilot app chat served the Captain chat relay (`phalanx relay next` / `respond`). It launched each worker with the app's session tool, using the attempt UUID as the name, and recorded the receipt. Every launch's first turn matched the saved prompt byte for byte. |
| Workers | Visible native GitHub Copilot app sessions, one per stage, in `caltseng/phalanx-pilot-py` and `caltseng/phalanx-pilot-web`. |
| Intake | `POST /v1/items` with the owner token and an idempotency key per item. |

### Clearing earlier items

Three stale items from earlier reruns were cancelled with a reason through `POST /v1/items/{id}/executions/{exec}/cancel`. The CLI has no cancel command; see [Findings](#findings).

| Item | Was | Result |
| --- | --- | --- |
| `ef9fe2cf` | pilot-py#3 | Cancelled at v15 |
| `66a07152` | pilot-web clamp, at adversarial_review | Cancelled at v15 |
| `23891c11` | pilot-py truncate | Cancelled at v17 |

Each one stored a cancellation report as its final output. Their stale PRs ([pilot-py#4](https://github.com/caltseng/phalanx-pilot-py/pull/4), [pilot-py#5](https://github.com/caltseng/phalanx-pilot-py/pull/5) and [pilot-web#4](https://github.com/caltseng/phalanx-pilot-web/pull/4)) were closed with a comment.

## Items

| Key | Item | Execution | Repository | Target | Purpose |
| --- | --- | --- | --- | --- | --- |
| A | `2abe1a14e1e94f88a7b6a0c64d2403ae` | `ddb7a88e` | pilot-web | `report` | Input-handling report that must ask the owner one question (gates 1, 3, 4) |
| B | `4c061d6fb5dd4defa575b5f0f782cd1e` | `7b33057f` | pilot-py | `checked_in` | Fix [pilot-py#3](https://github.com/caltseng/phalanx-pilot-py/issues/3), slugify separator runs (gates 2, 4) |
| C | `ef9cfc4f84384a3680f154bd5a65f350` | `5fb61f25` | pilot-py | `ready_for_check_in` | README docs for [pilot-py#2](https://github.com/caltseng/phalanx-pilot-py/issues/2). It depends on cancelled item `23891c11` at milestone `checked_in` (gate 5). |

## Timeline

| Time | Event |
| --- | --- |
| 12:31:36 | A, B and C filed. C was immediately "Waiting for 1 prerequisite". |
| 12:31:46 | Loop started. The relay resolved both projects. |
| 12:32:14 | A1 `report_drafting` (attempt `493adaa2`, session `d746e7a4`) and B1 `development` (attempt `76f411e9`, session `32a71dd0`) were claimed and launched. Both workers ran concurrently, one in each repository. |
| 12:33:45 | **Restart (gate 4).** The loop and API server were stopped mid-stage while both workers ran, and stayed down for more than 120 s, past the 90 s lease. Both workers wrote their result files during the outage: A1's was `needs_input`, B1's was `succeeded`. |
| 12:36:10 | Server restarted and healthy. |
| 12:36:15 | Loop restarted. Both slots showed "Awaiting native observation before recovery". |
| 12:36:55 | A fresh Captain chat ("PH-V1 Captain B (restart)") served the observation requests. Both workers were observed stopped. Both attempts got `recover_claim`: "Adopted the attempt: Exact stopped native thread … Verified attempt result". The launch was never replayed. |
| 12:37:01 | A went to `waiting_for_input` (v3), and the question reached the owner inbox. |
| 12:37:02–12:37:18 | B: `pr_creation` opened draft [pilot-py#6](https://github.com/caltseng/phalanx-pilot-py/pull/6) at head `7620e04`. |
| 12:37:45 | **Reply (gate 3).** `phalanx reply --answered-by captain` was sent to A (v3→v4), and A's resumed `report_drafting` was claimed (attempt `83b29f2f`, session `d567b154`). |
| 12:37:18–12:39:28 | B `self_review` (attempt `18db9deb`, session `0f092aa2`) succeeded. |
| 12:39:30–12:39:43 | B: `pre_pr_validation`, then `pr_promotion` (GitHub `ready_for_review` at 12:39:38), then `pr_validation`. Both required checks on `7620e04` were green: `test (ubuntu-latest)` and `test (windows-latest)`, app 15368. |
| 12:39:44–12:42:16 | B: `pr_reconciliation`, then `adversarial_review` (attempt `84c18ed5`, session `5c753a29`), which succeeded with no findings. |
| 12:40:58–12:43:06 | A: `report_review` (attempt `c0e67991`, session `c4505774`) succeeded, then `report_storage` ran and the report was stored at v11. |
| 12:42:18–12:42:26 | B: `pr_reconciliation`, then `check_in`. GitHub recorded the merge at 12:42:24. Phalanx recorded `merge_verified` at v21 with outcome `checked_in`, and pilot-py#3 closed. |
| 12:48 | **Runner offline (gate 1).** All `phalanx captain` processes were stopped, then the report was read through the API and `phalanx inspect`. |
| 12:50:27 | Loop restarted. |
| 12:51 | C was cancelled with a reason (v3) after the gate 5 evidence was recorded, so the queue is left clean. |

## Gates

| Gate | Result | Evidence |
| --- | --- | --- |
| (1) Report reaches verified storage and stays readable with the runner offline | **Pass** | A closed as `report_stored` at v11 after independent `report_review` succeeded. With every `phalanx captain` process stopped, `GET /v1/items/2abe1a14…/executions/ddb7a88e…/report` returned HTTP 200, `text/plain`, 5,101 bytes. `phalanx inspect` showed `final_output.draft_revision` `0b6f54ac…` equal to the reviewed `report_draft`. |
| (2) PR goes draft to ready, passes validation and an independent review, and is checked in with the merge verified exactly once | **Pass** | [pilot-py#6](https://github.com/caltseng/phalanx-pilot-py/pull/6) was created as a draft by `pr_creation` and made ready by `pr_promotion` (GitHub `ready_for_review` at 12:39:38). The pre-PR and PR validation gates passed, with both required checks green on head `7620e04`. A separate native session ran `adversarial_review` and succeeded after reviewing the full diff from `38480d5`. GitHub shows one merge: commit `acde94b` at 12:42:24, parents `38480d5` and `7620e04`, the verified head. The timeline has exactly one `merge_verified` event (v21), and the item closed `checked_in`. The PR body ends with the Phalanx footer. No other PR was opened for the item. |
| (3) A worker question, answered through the inbox, resumes the worker with the answer and preserved context | **Pass** | A1 asked whether recommendations should throw errors or return safe fallbacks. The options were a proper list, with a proposed default. The Captain answered through `phalanx reply --answered-by captain`: throw `TypeError`/`RangeError`, but empty and whitespace-variant strings are valid. That is [decision 44](../decisions.md). The timeline records event `reply` at v4 with `answered_by: captain`. The resumed packet's handoff carried the question, the answer, `answered_by: captain`, A1's prior handoff and `wait_version` 3. The stored report states the owner decision and frames every recommendation by it. |
| (4) Restarting the Captain and runner mid-stage recovers in-flight work without a replacement writer or redrafting | **Pass** | The API server, runner loop and Captain chat were all restarted while A1 and B1 were running. After lease expiry, each attempt was adopted under a fresh lease (`recover_claim` with "Adopted the attempt: Exact stopped native thread … Verified attempt result"). The worker's own result was submitted. No relaunch, no second development or drafting session, no redraft and no duplicate PR. The new Captain chat took the next launch request, and the original Captain launched it from that same request: one writer per attempt. |
| (5) One item blocked on a failed prerequisite or pending external gate does not stall others | **Pass** | C stayed `queued` with `wait_reason` "Waiting for 1 prerequisite" and `resume_trigger` "Prerequisite milestone". `inspect` reason: "Prerequisite disposition is cancelled. Rerun or replace the prerequisite." Meanwhile A and B claimed, ran concurrently and completed. |

## Deviations

- **Gate 5 used a cancelled prerequisite rather than a failed one.** The prerequisite was the stale item `23891c11`, cancelled during setup. Dependencies treat any non-success disposition the same way and give the same visible reason and recovery path.
- **A1's question was raised during the restart outage.** Its `needs_input` result was written while the server was down. The result was adopted on recovery, and the question reached the inbox at 12:37:01. Gates 3 and 4 therefore overlap on A1, which exercises both the question path and adoption.
- **The restart Captain took one launch request without launching it.** "PH-V1 Captain B (restart)" served observation and project resolution after the restart, then took the B `self_review` launch request `2819878c` and stopped. The original Captain then launched that request (attempt `18db9deb`) and recorded its receipt. The loop showed the reservation as `launch_uncertain` until then, and no second session was created.
- **The worker question was answered by the Captain**, as allowed by the gate. The decision is recorded for Caleb's review as decision 44.
- **C was cancelled at the end** with a reason through the API, after its blocked state was recorded, so later musters start from a clean queue.

## Findings

No gate failed, so no fix row blocks v1. The muster turned up two small proposals that are not gating:

| Proposal | Evidence |
| --- | --- |
| **Clear the loop's slot-0 `unmatched_attempts` status once no attempts are unmatched.** `captain_loop.py` writes it when `self.unmatched` changes but never clears it. | `phalanx attended status --all` still showed "Unmatched attempts reserve capacity; restore their journals: acc910f8…" from 2026-11-21 throughout this muster. It reserved no capacity: two workers ran concurrently at capacity 2. But the stale line reads as an active blocker to an operator. |
| **Add `phalanx cancel ITEM --reason`** over the existing cancel route. | Clearing stale items needed a raw `POST …/cancel` with `expected_version` and an idempotency key. Every other routine operator action in the muster had a CLI command. |
