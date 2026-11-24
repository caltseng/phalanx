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
An observation of the previous green PR head cannot complete the execution before the new
commit is published, even after restart or writer recovery.
After an uncertain response or restart, read status and retry with fresh state. Capacity
transitions are fenced by writer identity and allocation revision; no lease timeout releases work.

## Publication and delivery

PR stages are `development`, `self_review`, `publication`, `delivery`. Writers push and open
their own PRs with the explicit target base, reusing the item's existing open PR. Honor
`publication_mode`: draft work stays draft and never enables auto-merge. Ready work opens a
ready PR and enables GitHub auto-merge only for `checked_in`; `ready_for_check_in` leaves merging
to the owner. Re-read the current target after steering and disable auto-merge if retargeted.
Never reuse a PR merged by an earlier execution.

The reconciler observes GitHub; its observations drive server delivery and flags. Writers fix
checks, conflicts and actionable feedback on the same PR while it waits. Repository checks and
reviews gate merging. Trusted local checks run from reconciler policy and raise flags, not an
extra Phalanx gate. See [publication and delivery](direct-sessions.md#pr-publication-and-delivery).

Reports use `report_drafting`, `report_review`, `report_storage`; results carry the exact
content-addressed draft revision. Server finalization retries storage without redrafting.

## Host state

Keep config, the charter, credentials, worktrees, result artifacts and logs. The reconciler
uses `reconciler.json` and a private scratch directory containing locks and temporary
worktrees, not a local database. Existing unused files are not deleted by an upgrade.
All durable queue and observation records remain behind the API.

Use [Resume](resume.md) after restart and [Recovery](recovery.md) for uncertain launches or
stopped writers. Dispatch pause refuses new writer registrations and new stage attempts, not progress
or results for existing attempts.
