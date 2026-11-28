# Writer operations

The [worker prompt](../phalanx/prompts/worker.md) is the authoritative writer/delivery
policy. Print it with `python -m phalanx shift prompt worker`; generated kickoffs substitute
the item's identity, configured commands, checks environment and publication instructions.
Kickoffs include the item's accepted context; retained writers recover it through the
existing scoped status command under the [task-context policy](../phalanx/prompts/worker.md#task-context).
Use [Direct path](direct-path.md) for installation and Captain commands, and
[architecture](architecture.md) for API/execution-host boundaries.

## Writers

The Captain prepares an isolated worktree with `shift launch`, registers the nonce and
opens the native session. The scoped CLI reads that worktree's Git-directory credential.
The [work loop](../phalanx/prompts/worker.md#work-loop) owns continuation, capacity,
publication handoff and stopping behavior. Command forms:

```text
python -m phalanx work status
python -m phalanx work next
python -m phalanx work capacity building
python -m phalanx work capacity delivering
python -m phalanx work progress --note "Current progress"
python -m phalanx work result --attempt ATTEMPT --file RESULT_JSON
python -m phalanx work question "Decision needed" --attempt ATTEMPT --handoff "Resume context"
```

Capacity transitions are fenced by writer identity/allocation revision, not lease expiry.
The CLI records a publication handoff revision; a superseding head surfaces `head_drift`.
See [the state machine](../STATE_MACHINE.md) for server predicates.

## Control-plane outages

Apply the canonical [outage protocol](../phalanx/prompts/worker.md#control-plane-outages).
For Captain-local recovery use [its shared-outage section](../phalanx/prompts/captain.md#shared-apicontrol-plane-outages).

## Publication and delivery

The [stage result contract](../phalanx/prompts/worker.md#stage-results) specifies writer
submissions. [Direct sessions](direct-sessions.md#pr-publication-and-delivery) describes
the GitHub observation interface; trusted reconciler checks use
[validation policy](validation-policy.md).

### Merge decision before auto-merge

Follow [Merge decision](../phalanx/prompts/worker.md#merge-decision-publication-and-delivery).

## Bounded performance work

The [optimization and decisions policy](../phalanx/prompts/worker.md#bounded-optimization-and-decisions)
owns the budget, evidence, hard-gate boundary and authority rules. The following describes
the existing API controls, not an alternative policy.

### Visible owner decisions during delivery

`work question` submits `needs_input` through the result route. During a running stage,
it pins the attempt. During an attempt-free delivery external wait it pins execution version,
stage and writer without creating an attempt. Stale versions, active attempts, changed stages,
stops and finalization are refused. Repeating an open question is also refused.
The owner inbox/Scribe exposes the question; an authenticated versioned reply queues the
same stage and populates `last_reply`.

### Versioned acceptance-criteria steering

An authorized operator reads fresh authenticated item state before editing criteria:

- Queued: `PATCH /v1/items/{item}/executions/{execution}` accepts `expected_version`,
  complete `acceptance_criteria` and `steered_by`.
- Attempt-free `delivery`/`waiting_for_external`: that route permits only criteria edits
  plus version/attribution. Lifecycle, candidate, primary PR, writer/allocations, target
  and publication mode remain unchanged.
- Existing question: use normal versioned `reply` with classification/follow-up.
- Completed: preserve closed scope; attach follow-up through existing audit/action records.

Preserve unrelated criteria verbatim. Use the complete list from the exact read,
not an old partial edit. Retry an uncertain PATCH with its exact body/`Idempotency-Key`,
or inspect timeline first; on 409 reread state. Never refresh only version against stale
criteria, edit storage or fake a result/rerun. Verify receipt version, timeline before/after
and unchanged writer/allocations; relay accepted scope to the retained writer.
Verify installed route support before steering; [deployment](deployment.md#coordination-policy-adoption-boundary)
owns package and retained-instruction adoption. Historical timing cases belong in the
[decision log](decisions.md#ordinary-performance-case-closeout-decision-94).

## Host state

Host-local files are configuration, charter, credentials, worktrees, result artifacts and logs.
The reconciler uses `reconciler.json` and scratch locks/temporary worktrees, not a local database.
Upgrades do not delete unused files. Durable queue/observation state remains behind the API.
See [Resume](resume.md) and [Recovery](recovery.md).
