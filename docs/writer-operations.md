# Writer operations

The [worker prompt](../phalanx/prompts/worker.md) is the authoritative writer/delivery
policy. Print it with `python -m phalanx shift prompt worker`; generated kickoffs substitute
the item's identity, configured commands, checks environment and publication instructions.
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
The [PR #251 reproduction](https://github.com/caltseng/phalanx/pull/255) is historical
evidence for the ordering, not another approval contract.

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

### Applying classification to current timing work

The owner classified #252 (envelope), #253 (API) and #258 (CLI) as ordinary optimization;
their numeric misses remain historical evidence, not achieved targets or current dispositions.
The [decision-94 closeout](decisions.md#ordinary-performance-case-closeout-decision-94)
records verified merges. Preserve these source measurements:

| Work | Historical measurement | Follow-up actor/action |
| --- | --- | --- |
| [#252](https://github.com/caltseng/phalanx/pull/252), `4852183e` | 10.549 s vs under-10 s; [run 45646662719](https://github.com/caltseng/phalanx/actions/runs/45646662719); one probe and 313 expected unit IDs | Captain coordinates remaining shift-CLI diagnosis. |
| [#253](https://github.com/caltseng/phalanx/pull/253), `7e231933` | Windows 3.14/shard 3: 128 s vs 125.4 s, 38.76% vs 40%; five other reductions passed; [run 45652410596](https://github.com/caltseng/phalanx/actions/runs/45652410596) | API writer retains 321-ID comparison; Captain tracks 2.6 s residual. |
| [#258](https://github.com/caltseng/phalanx/pull/258), CLI-only evidence | Windows shift/writer 3.482/3.234 s (3.12), 2.919/3.423 s (3.14), vs under-2 s; [run 45649550659](https://github.com/caltseng/phalanx/actions/runs/45649550659) | CLI writer reports frozen 326-ID combined diagnostic; Captain tracks residual latency. |

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
Older APIs retain queued-only behavior: see [deployment boundary](deployment.md#coordination-policy-adoption-boundary).

### Retained-session activation

Install the reviewed package through the deployment procedure. Retained chats do not
automatically reload prompts: relay changed canonical sections verbatim with the writer's
configured `$work` substituted, and verify receipt. `shift prompt worker` prints a template,
not a new kickoff; do not send unexpanded `$publication` or rerun `shift launch` to refresh it.
Do not revive/contact ended writers. Captain adoption follows
[shift reinforcement](../phalanx/prompts/captain.md#every-wake).

## Host state

Host-local files are configuration, charter, credentials, worktrees, result artifacts and logs.
The reconciler uses `reconciler.json` and scratch locks/temporary worktrees, not a local database.
Upgrades do not delete unused files. Durable queue/observation state remains behind the API.
See [Resume](resume.md) and [Recovery](recovery.md).
