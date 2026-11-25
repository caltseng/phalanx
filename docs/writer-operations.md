# Writer operations

The local installation has an API service, native Captain and writer sessions, and a
GitHub reconciler. API and execution-host responsibilities remain separate even when
they run on one machine; see the [architecture diagrams](architecture.md).
Use [Direct path](direct-path.md) for shift configuration, the charter, native session
launches, scheduled wakes, handoff and creator-owned cleanup.

## Writers

The Captain prepares an isolated item worktree with `phalanx shift launch`, saves the scoped
writer credential in its Git directory, registers a launch nonce and opens one native session.
The writer runs these commands from that worktree:

```text
python -m phalanx work status
python -m phalanx work next
python -m phalanx work capacity building
python -m phalanx work capacity delivering
python -m phalanx work progress --note "Current progress"
python -m phalanx work result --attempt ATTEMPT --file RESULT_JSON
python -m phalanx work question "Decision needed" --attempt ATTEMPT --handoff "Resume context"
```

Keep the same writer through runnable stages. Each stage has its own fenced attempt. On a
question, external wait or retry delay, inspect status and resume when its trigger clears.
Never print the writer credential or give a worker the runner token.

Development, self-review, substantial local tests and coding/validation fixes require
`writer.building=true`. The server releases Building at successful self-review handoff into
publication, but keeps this same writer's Total allocation. Pure publication and observing
external CI/review use only Total. Before rework during publication or delivery, run
`work capacity building` and inspect its returned binding: do not edit or test until
`writer.building=true`. If full, wait and retry about every 30 seconds; retain the session,
Total and progress reporting. Eligible waiting PR fixes take precedence over new development.
After fixes and local validation finish, run `work capacity delivering` before publication
or external waiting. Never infer Building headroom from spare Total or the delivery stage.
Commit the reviewed changes first: the CLI records HEAD as the durable publication handoff.
An observation of the previous green open PR head cannot complete the execution before the
new commit is published, even after restart. A superseding head surfaces `head_drift` and a
visible wait: review and hand off that head, or stop and withdraw the writer to clear the
handoff. A verified GitHub merge remains authoritative and always releases the writer.
After an uncertain response or restart, read status and retry with fresh state. Capacity
transitions are fenced by writer identity and allocation revision; no lease timeout releases work.

## Publication and delivery

PR stages are `development`, `self_review`, `publication`, `delivery`. Writers push and open
their own PRs with the explicit target base, reusing the item's existing open PR. Honor
`publication_mode`: draft work stays draft and never enables auto-merge. Ready work opens a
ready PR and submits its URL even when merge gates are outstanding, retaining the same writer
in delivery with auto-merge off. Merge or enable GitHub auto-merge only for the actual current
`checked_in` target after the gates below pass; `ready_for_check_in` leaves merging to the
owner. Re-read the current target after steering and disable auto-merge if retargeted.
Never reuse a PR merged by an earlier execution.

The reconciler observes GitHub; its observations drive server delivery and flags. Writers fix
checks, conflicts and actionable feedback on the same PR while it waits. Repository checks and
reviews gate merging. Trusted local checks run from reconciler policy and raise flags, not an
extra Phalanx gate. See [publication and delivery](direct-sessions.md#pr-publication-and-delivery).

### Merge decision before auto-merge

Independent review is required by the target repository's instructions, the charter, or
Captain/owner steering on the item. Any qualified reviewer independent of the author may provide
final exact-head review; no particular native session or person is a universal dependency.
Preserve genuine GitHub required reviewers, CODEOWNERS and protections. Old routing preferences
do not make a reviewer exclusive. Reuse recorded approval when it covers the current exact head
and change scope and has no unresolved actionable findings; do not demand a redundant review.
If none requires independent review, required CI, GitHub reviews and repository rules remain
the gates, not an invented independent-review wait. If required approval is pending, record
the missing approval/reviewer with `work progress` and route to another qualified independent
reviewer through the Captain if needed. Use `work question` when required review cannot be
obtained or authority/access is genuinely missing, not just because a preferred session is busy.

Before either direct merge or auto-merge activation, read fresh `work status` and GitHub facts.
Require the current execution's `checked_in` target, ready publication, no pending stop,
repository-specific merge-owner authority, applicable independent final approval of the
**current exact PR head**, green required CI on that head, required GitHub reviews, resolved
actionable feedback, repository rules/protections and dependency/actual-main integration.
A dependency PR being published is not proof its prerequisite reached the required target.
Self-review is not independent approval; previous-head or content-only PASS is not final
exact-head approval. Missing required GitHub reviews, or no configured GitHub review requirement,
cannot stand in for applicable independent review.

While any gate is unknown or outstanding, disable any existing auto-merge and retain the same
writer and Total allocation in delivery. Disable auto-merge before pushing rework, acquire
Building before editing/self-review/substantial tests, then commit and hand off with
`work capacity delivering` before pushing. Obtain fresh final-head approval and CI evidence.
Immediately before the merge action recheck target, mode, head and all gates, using an
exact-head guard where supported. Once satisfied, deliver the actual merge without another
routine permission request; explicit owner-ready/report/draft ceilings and repository
merge-owner restrictions still govern. No admin bypass is authorized.

This corrects generated writer instructions, **not an engine-enforced independent approval
predicate**. The reconciler still observes GitHub; its ready milestone or a GitHub configuration
without required reviews does not certify independent final-head review. There is no new review
agent, watchdog, scheduler, state field, API/CLI contract or lifecycle system.

The [PR #251 reproduction and validation narrative](https://github.com/caltseng/phalanx/pull/255)
records the incident behind this ordering. The focused `test_shift.py` regressions verify the
generated instructions and current-target routing, not mathematical enforcement of agent behavior.

## Bounded performance work

Ordinary performance targets have a **two-round optimization budget**, not an indefinite right to
retain Building or Total for tuning. Record the baseline, each round's change and measured result
under the same workload/environment, the remaining numeric miss, and a concrete follow-up with an
accountable actor and next action. Reuse existing measurements rather than resetting the budget
after a stage transition or reviewer change. Never claim a missed number was met.
Explicit safety/SLO/resource-contract gates stay hard: name the exact gate and measurement/evidence,
and fix it or raise the specific owner decision; two rounds cannot waive it. Independent exact-head
review, required CI, trust, dependency integration and ready/report/draft ceilings also remain hard.

After two ordinary tuning rounds, deliver the verified scope through the authorized current target
and track the remaining miss as follow-up. If recorded criteria still require that number, first
record the owner-approved classification/change through supported versioned controls; a progress
note, this policy or elapsed time alone cannot silently weaken acceptance. Release Building after
reviewed work as usual; release Total only through verified completion or safe stop, never by
abandoning a live writer.

### Applying classification to current timing work

The owner classified the timing work on Phalanx #252 (envelope), #253 (API) and #258 (CLI) as ordinary
optimization targets, not implicit safety/SLO/resource contracts. Keep their original numeric targets
and all measured misses visible; any separately explicit safety/SLO/resource-contract requirement
remains a hard gate. This classification does not assert that any number was achieved.

Measurements already published when preparing this policy remain historical evidence, not proof
for a later integrated head. Preserve the PR bodies, CI runs and diagnostic manifests:

| Work | Recorded miss | Concrete follow-up after the budget |
| --- | --- | --- |
| [#252](https://github.com/caltseng/phalanx/pull/252), head `4852183e` | Slowest unit 10.549 s against strict under-10 s; [CI 45646662719](https://github.com/caltseng/phalanx/actions/runs/45646662719). One probe and all 313 expected unit IDs were verified. | Captain coordinates the remaining shift-CLI case with the existing CLI writer; retain the exact duration rather than asserting the threshold passed. |
| [#253](https://github.com/caltseng/phalanx/pull/253), head `7e231933` | Five of six Windows reductions passed 40%; Python 3.14/shard 3 was 128 s versus a 125.4 s maximum (38.76%); [CI 45652410596](https://github.com/caltseng/phalanx/actions/runs/45652410596). | API writer preserves the 321-ID matched comparison; Captain tracks the 2.6 s remaining miss and phase-level diagnosis as follow-up, not a blocking forced tuning round. |
| [#258](https://github.com/caltseng/phalanx/pull/258), earlier CLI-only evidence | Windows shift/writer durations 3.482/3.234 s (3.12), 2.919/3.423 s (3.14) against under-2 s; [CI 45649550659](https://github.com/caltseng/phalanx/actions/runs/45649550659). | CLI writer finishes the already-running frozen 326-ID combined diagnostic and reports actual numbers; Captain tracks residual CLI latency separately. Pending combined evidence is not a passed target or authority to merge other items. |

The Captain/service operator, not another item's writer credential, reads fresh authenticated item
state and uses these existing owner controls:

- For queued work, `PATCH /v1/items/{item}/executions/{execution}` with `expected_version`,
  `acceptance_criteria` and `steered_by` records before/after values.
- For an attempt-free `delivery`/`waiting_for_external` execution, the same owner-authenticated
  route permits **only** `acceptance_criteria` edits (plus attribution/version). This narrow
  extension preserves lifecycle state, primary PR, candidate, writer, Total/Building, target and
  publication mode. It is not a general active-scope/goal/dependency editor.
- For an existing question, use the normal versioned `reply` and record classification plus the
  concrete follow-up in its answer. An owner reply is not proof of checks or review.

Retain every unrelated criterion verbatim. Annotate the ordinary target with baseline/round evidence,
the measured remaining miss, the two-round stopping point and concrete follow-up; explicitly retain
named hard gates. Use `Idempotency-Key` for retries. On HTTP 409, reread state/timeline before retrying,
not a direct SQLite edit, fake result, cancellation or rerun. `work question --attempt` cannot create
a question during a delivery wait with no attempt; do not invent an attempt or CLI option.
If the execution already completed, preserve it and attach the classification/follow-up through
existing audit/action records rather than pretending to edit the closed scope.

For example, the authorized operator can use PowerShell with the existing private environment.
Set `$item` to the verified item ID. Prepare `$approvedCriteria` as the **complete** approved criteria
list from the exact `$view` read below, preserving every unrelated criterion and measured miss.
Do not print the headers or credential environment:

```powershell
$api = $env:PHALANX_API_URL.TrimEnd('/')
$headers = @{ Authorization = "Bearer $env:PHALANX_OWNER_TOKEN" }
$view = Invoke-RestMethod "$api/v1/items/$item" -Headers $headers
# Prepare $approvedCriteria from this exact $view.acceptance_criteria before continuing.
$execution = $view.execution.id
$route = "$api/v1/items/$item/executions/$execution"
$body = @{ expected_version = $view.execution.version
           acceptance_criteria = $approvedCriteria
           steered_by = "captain" } | ConvertTo-Json -Depth 8
$headers["Idempotency-Key"] = "ordinary-timing-$execution-$($view.execution.version)"
$receipt = Invoke-RestMethod $route -Method Patch -Headers $headers `
    -ContentType "application/json" -Body $body
$timeline = Invoke-RestMethod "$route/timeline" -Headers $headers
```

Verify the inspected execution is still the intended attempt-free delivery wait before the PATCH.
An uncertain response is not a new edit: retry the exact body/key or read the timeline first.
Never refresh only the version while reusing criteria prepared from an older view.
The existing queued-only rule applies on older APIs; HTTP 409 is not permission to modify storage.

Verify the receipt's version and timeline before/after criteria and unchanged writer/allocations.
Relay accepted classification and next action to that same retained writer through supported
messaging. Fresh independent review must cover the effective scope at the current exact head;
reuse existing exact-head approval only when it already covers that scope without unresolved
findings. Re-read GitHub required checks/protections before merge. The tracked route requires API
deployment before use; see [the exact adoption boundary](deployment.md#coordination-policy-adoption-boundary).

### Retained-session activation

After review and check-in, the authorized deployment owner installs the reviewed package through
the existing deployment procedure. No runtime restart is part of this fix. Newly generated
kickoffs then use the corrected generator and packaged worker template. Retained native sessions
do not automatically reload earlier instructions: the Captain relays the exact updated **Merge
decision (publication and delivery)** section and delivery clause from canonical `shift prompt worker`
to still-live retained writers as needed, using their existing command for `$work`.
Explicitly state that this section supersedes the earlier generated publication-time instruction
to enable auto-merge. The command prints a template, not a new item kickoff: do not relay the
unexpanded `$publication` placeholder or call `shift launch` to regenerate an existing writer.
Verify receipt and inspect current target/head/gates before any further merge action.

Existing in-flight writers already received explicit corrective gate instructions for this
incident; preserve that containment, rather than reverting them to older kickoff wording.
Do not contact, revive or replace ended writers. Keep the same live writer/Total through rework
and respect Building acquisition/handoff. This activation does not change the new24h Captain
cadence: 24-hour shifts, five-minute reconciliation and 90-minute reinforcement remain unchanged.

If cancellation or forced failure sets `state.stopping`, stop new work and reconcile any
in-flight GitHub effects. Delivery has no open attempt, but its writer is still live:
the Captain stops that exact native session and uses `shift withdraw --stopped` to confirm
the stop. A verified primary-PR merge into the target observed before confirmation delivers
the PR instead of recording a cancellation or failure.

Reports use `report_drafting`, `report_review`, `report_storage`; results carry the exact
content-addressed draft revision. Server finalization retries storage without redrafting.

## Host state

Keep config, the charter, credentials, worktrees, result artifacts and logs. The reconciler
uses `reconciler.json` and a private scratch directory containing locks and temporary
worktrees, not a local database. Existing unused files are not deleted by an upgrade.
All durable queue and observation records remain behind the API.

Use [Resume](resume.md) after restart and [Recovery](recovery.md) for uncertain launches or
stopped writers. Dispatch pause refuses new writer registrations and Building acquisitions,
not stage continuation, progress or results for already allocated writers.
