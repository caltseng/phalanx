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

### PR #251 reproduction and validation

On 2026-11-25 the [PR #251](https://github.com/caltseng/phalanx/pull/251) GitHub timeline recorded
`auto_merge_enabled` at **14:01:17 UTC**, then `auto_merge_disabled` at **14:05:07 UTC**.
The original commit was `8d95d3c6f9afeaf3df588b88f1a709d6c9f4c1f1`; later commits
`4e298106af2a70d6903fcbc7178f8774e8a8f2a4` (14:06:41 UTC) and
`50c190dd79fbb0ae2554977177bb2156ad47cfc1` (14:11:57 UTC) changed the head.
At inspection, GitHub listed no reviews and the PR description still named the original
reviewed head. That is not evidence of independent final approval of the later head; GitHub
review absence alone also cannot establish whether an out-of-band review happened.
The observed enable/disable order and later rework explain why publication cannot be the
instruction to activate auto-merge, even if an earlier content review passed.

The local reproduction needs no GitHub mutation: call `shift.kickoff` for ready-mode
`checked_in` work. Previously its publication clause said to enable auto-merge immediately,
and its delivery clause repeated target-only activation. `tests/test_shift.py` now checks that
publication can return `pr_url` and retain delivery waiting, both clauses route through the
same pre-activation decision, and that decision distinguishes independent exact-head approval,
required CI/reviews, authority and integration from self-review/stale PASS. Target-steering
cases exercise the execution target rather than the original intake target; draft regressions
retain their ceiling. Run `python -m unittest discover -s tests -p test_shift.py`.
These tests verify the generated instruction boundary, not mathematical enforcement of agent
behavior or an unimplemented engine gate.

### Retained-session activation

After review and check-in, the authorized deployment owner installs the reviewed package through
the existing deployment procedure. No runtime restart is part of this fix. Newly generated
kickoffs then use the corrected generator and packaged worker template. Retained native sessions
do not automatically reload earlier instructions: the Captain relays the exact updated **Merge
decision (publication and delivery)** section and publication/delivery clauses from canonical
`shift prompt worker`, preserving the item's command and context, to still-live retained writers
as needed. Verify receipt and inspect current target/head/gates before any further merge action.

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
