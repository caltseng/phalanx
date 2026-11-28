# Opt-in Windows foreground API ownership and stop receipts

`deploy supervise` is an explicit, foreground replacement for the API action
**within the same existing logical task**, not a second service, watchdog or task.
It never registers, edits, enables, disables or starts a task. It does not adopt
an already-running process tree. The existing uninstrumented `cmd -> Python ->
Go` installation is **unsupported by this stop path** until its owner establishes
a separate trusted bootstrap boundary. A PID/CIM snapshot or retroactive Job
assignment cannot prove descendants hidden by an already-exited intermediate
parent. Root exit alone is not full-tree exclusion.

Current packages run only Go HTTP. Explicit or saved Python selections fail with
migration guidance before task/native queries, containment or ownership-record writes.
An explicit Go override does not rewrite a saved engine. An omitted override remains
omitted in the child command, preserving the documented deployment precedence.
`deploy stop` still operates on its verified retained ownership, not engine selection.

After the reviewed bootstrap/preparation gates below, the owner can explicitly
configure the same task's foreground API action, retaining its security, principal,
settings, triggers, working directory and existing deployment configuration:

```powershell
python -P -m phalanx deploy supervise --config D:\muster\deployment.json `
  --task '\ExistingApiTask' --record D:\muster\private\api-ownership.json `
  --engine go --go-binary D:\packages\reviewed\phalanx-server.exe
```

Use the installed, pinned interpreter/package, not an incidental checkout import.
Engine/package selection delegates unchanged to `deploy run`: CLI overrides saved
configuration, then the supported Go default; Python requires an explicit temporary
rollback selection. The action must be a single absolute executable
action in the supervisor's proven native ancestor chain, with exactly one running
instance. Asynchronous shells which exit before that association is captured are
not a supported launch contract. The task engine is an association check only;
it is never a termination target. Missing/ambiguous task instances, changed
definitions/security, unavailable active identities and an occupied listener/shared
lock refuse launch or stop.

The supervisor creates one **unnamed** Windows Job, with kill-on-close and no
breakaway flags. It creates its API root suspended, assigns it before resuming
any thread and retains the original native root/ancestor/engine handles. Ordinary
`CreateProcess` descendants remain contained even when the root exits first.
The supported Phalanx API launch does not delegate creation through WMI, remote
brokers or other out-of-Job process creators. Such launchers require a separate
reviewed ownership contract; do not assume Windows Job inheritance covers them.
No API/drain/admission/busy/resource timeout, server route or capacity setting
changes.

An authorized operator explicitly stops that exact recorded task/owner:

```powershell
python -P -m phalanx deploy stop --config D:\muster\deployment.json `
  --task '\ExistingApiTask' --record D:\muster\private\api-ownership.json `
  --receipt D:\muster\private\stop-20261127-01.json
```

The record and receipt require operator-private directories/ACLs; do not publish
production copies. The record is a handle-coordinate handoff from the currently
running supervisor, not a transferable stop authorization. The controller verifies
the native supervisor creation identity and task definition/security/instance,
duplicates the supervisor's **original kernel Job and process handles**, then checks
every captured active identity. It never opens a Job by name or terminates a PID.
Native creation FILETIME is retained exactly; CIM's microsecond representation may
differ by at most 10 ticks, never an arbitrary time window. Original-handle exit
proves only that original process exited and never authorizes acting on a reused PID.

Ownership records and stop receipts become visible only as complete JSON: the writer
flushes and closes an exclusively created temporary file in the same private directory,
then atomically links it to the new final path without replacing any existing record.
Write, publication and temporary cleanup failures propagate; the final path stays absent
or complete. An interrupted writer may leave its private temporary file, which is not an
ownership record or stop receipt. Cleanup never removes a final record or another writer's file.

Stop uses the exact running task instance's `Stop()` plus its original owned Job.
It is a **hard stop, not a clean-drain guarantee**. The receipt always identifies
interrupted caller outcomes as `transport-unknown`; reconcile requests/jobs on
current data using their original keys and status. An unreceived response is
neither an invented acceptance nor proof of failure.

The JSON receipt includes `stopped`, task definition/security/action hashes and
instance, per-node native FILETIME and expected/observed field diagnostics, original-
handle exit, Job emptiness, task/listener exclusion, shared-lock acquisition/release,
unknown-outcome recovery and any failure. Executable/argument/provider-error fields
are hashed or unavailable, never raw command lines, task XML or credentials.
`stopped: true` requires every proof **and successful handle cleanup**. Listener
and lock checks are exclusion evidence, not process-selection authority. A receipt
proves that observation, not that a future scheduled invocation can never start.

There is **no approved default API/task stop deadline**. Git's five-second cleanup
limit is unrelated and is not borrowed. Native completion and supervisor calls
can remain pending; there is no avoidance sleep, deadline extension or automatic
force escalation. If completion cannot be established under the operator's
existing policy, preserve the original record/evidence, do not start a replacement,
and ask a focused requirement question identifying the minimum required policy
or budget change. A failed/interrupted controller may have already stopped part
or all of the tree; its failure receipt is not a stopped claim. A stale record
cannot recover lost native ownership or authorize a new task instance.

## Bootstrap and recovery boundary

Before first adoption, the authorized service owner must provide a separately
reviewed, positively verified quiescence boundary for the old API generation,
together with NEW current-data/backup/compatible-rollback gates and outage
notification. This tool cannot manufacture that boundary for the current
uninstrumented tree. Absence of one task root, listener or lock alone is insufficient,
and a future-only wrapper is not a production recovery cure. If that proof requires
an initial quiescence action outside existing authority, stop and obtain the exact
scope/precondition decision; do not prescribe a shared-host reboot, kill unrelated
processes or invent permission. Preserve task security/settings and all prior
negative operational evidence; the historical mismatched node/field/cause remains
**UNKNOWN**.

After a fully proven stop, archive the prior ownership record and stop receipt
privately before the next start of the **same unchanged task**; supervision refuses
to overwrite a record. Restart on the evolved current database and retained
credentials/configuration. Replay accepted and transport-unknown original keys,
verify unchanged durable identities/settings/Building/Total and fresh reconciler
continuity. Never restore an old database over active work or run a second API.
The reconciler, clients, native writers and schedules remain untouched. Follow
the [deployment gates](deployment.md#standing-authority-for-compatible-upgrades).

## Synthetic evidence boundary

`tests/test_windows_supervisor.py` exercises native ownership/negative/cleanup
cases. The existing isolated Windows CI job additionally runs
`tests/windows_supervisor_rehearsal.py`, with an explicit isolation environment
marker. It registers only unique disposable fixture tasks, exercises real
cmd-root-before-deploy/Go exit, stop/exclusion and accepted/unknown replay after
restart on evolved synthetic data, and preserves an unrelated task. Missing
Windows/Task Scheduler isolation is an explicit environment gap, not a skipped
PASS. Synthetic PASS does not authorize or explain production recovery. Qualified
independent approval of the exact final source head and required CI remain
mandatory before normal guarded merge; production reuse remains operator-only.
