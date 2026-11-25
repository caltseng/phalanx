# Observed writer usage

Scribe execution details show **Observed usage**, defaulting to writer usage for the current
execution, with a separately labeled all-executions item subtotal. This is optional telemetry,
not an invoice, billing monitor or admission control. Missing values are unavailable, never
zero. Captain/shared reviewer overhead is unallocated, not apportioned to items. Self-review
in the writer's session is already included.

## Native collection and authorization

The existing authorized runtime agent transports a numeric snapshot; the Python client does
not attach to desktop internals. Use `phx work usage --file SNAPSHOT.json` with the current
writer credential, including during attempt-free delivery. An owner/runner-authenticated live
Captain in the item's repository scope can use:

```text
phx shift usage --captain CAPTAIN --item ITEM --execution EXECUTION --writer WRITER --file SNAPSHOT.json
```

The API routes are `POST /v1/writer/usage` (snapshot body) and
`POST /v1/items/{item}/executions/{execution}/writers/{writer}/usage`
(`{"captain_id":"CAPTAIN","snapshot":{...}}`). Runtime and native session must exactly match the
confirmed writer, not its display name, app workspace alias or attempt ID. The trusted runner
token and Captain repository scope are the existing authorization boundary; the current server
has no separate multi-account/workspace grant model. Native tools must also authorize access
to that exact session in the caller's account/backend. `account` is an opaque source-account
partition label, not a credential and not proof of provider access.

In the Copilot App, resolve the actual native identity with the supported session metadata
tool, then compare it with `work status`'s confirmed `writer.session_id`. For a recent,
newly created item-exclusive session, use the App's **read-only `session_store_sql` tool** with
`source: "local"` and substitute only the confirmed authorized identity:

```sql
SELECT COUNT(*) AS events,
       SUM(e.input_tokens) AS input_tokens, SUM(e.output_tokens) AS output_tokens,
       SUM(e.cache_read_tokens) AS cache_read_tokens,
       SUM(e.cache_write_tokens) AS cache_write_tokens
FROM assistant_usage_events e
WHERE e.session_id IN (
  SELECT s.id FROM sessions s
  WHERE s.id = '<confirmed-native-session-id>'
    AND substr(s.created_at, 1, 10) >= date('now', '-7 days')
)
```

This selects the **whole lifetime of a recently created session**, not seven days of usage.
No rows/null aggregates mean unavailable. Map only the four numeric sums to `tokens`; no raw
messages, arbitrary result fields, `cost`, prompts or credential files are transported.
The query tool is supplied by the App, not callable by name from Python. The runtime agent
writes the allowlisted JSON below outside the checkout and submits it at an ordinary progress
or Captain observation. Use an increasing observation sequence, not event count as billing
identity. Cumulative snapshots replace rather than add event sums. Local/cloud mirrors, SDK
metrics and per-model partitions must not be added to this authoritative aggregate.

The supported own-session query on 2026-11-25 returned 8 usage records with input 571954,
output 3831, cache-read 508365 and cache-write 63565 tokens for this implementation's
confirmed item-exclusive Copilot App writer. This verifies numeric metadata acquisition, not
credit availability, model partitions, record completeness, SDK parity or a final total.
Such a capture can be transported through this contract; **no live API ingestion or rollout
is claimed**. These counters may overlap according to provider definitions; do not add
cache/reasoning/input/output to manufacture a provider total.

Codex App's public app-server token protocol is not proof of a read-only connection to the
actually bound Desktop thread. No such App-bound export was available to verify here.
Automatic Codex App capture therefore remains **unavailable/unverified**, explicitly shown
in details. A future adapter needs a supported authorized existing App connection/export,
confirmed thread identity, pinned schema and counter/window semantics. Do not start a new
app-server, resume a thread, inspect private rollout files or use an unrelated CLI session.
An authorized manual App UI observation can use the same contract with its actual source
and estimated basis; this does not establish automatic integration.

## Snapshot contract

Example shape only; replace placeholders and timestamps with verified source metadata:

```json
{
  "runtime": "copilot-app",
  "provider": "github-copilot",
  "account": "opaque-authorized-account",
  "origin": "app-local-history",
  "session_id": "confirmed-native-session",
  "source": "app_session_metadata",
  "source_version": "actual-app-build",
  "epoch": "session-created",
  "model": null,
  "sequence": 1,
  "window_start": "2026-11-25T14:00:00Z",
  "cutoff": "2026-11-25T14:01:00Z",
  "observed_at": "2026-11-25T14:01:00Z",
  "scope": "item_exclusive",
  "status": "pending_final",
  "reason": "Last native turn and source lag not yet observed",
  "tokens": {"input": 12, "output": 0, "cache_read": null, "cache_write": null},
  "credits": null,
  "money": null
}
```

Unknown keys are rejected. Counters are optional nonnegative safe integers; optional `total`
and `reasoning` preserve source-native definitions and are never derived. Subtotals exceeding
the JavaScript safe-integer range are returned as exact decimal strings. One cumulative
source/epoch/model aggregate per writer keeps storage bounded. Increasing sequence and
nondecreasing cutoff/counters prevent replay, rewind, missing fields and out-of-order
snapshots from inflating/erasing consumption. Identical replay is accepted without adding.
Source-account/origin/epoch/model/window/scope changes conflict instead of summing incompatible
series. `work status` includes only this writer's latest `usage.sequence`, retained `snapshot`
and `acquisition_error` so an interrupted observer can resume without a client journal.
For a reset/fork/missing baseline, submit an unavailable acquisition with a sanitized
reason; retained measurements stay visible as partial. Model partitions and baseline-delta
accounting are deliberately not implemented. Use model null for a whole-session aggregate.

Only newly created item-exclusive sessions with a window no earlier than writer allocation
enter subtotals. Shared/resumed/copied histories without a verified attribution baseline use
`session_lifetime` and remain visible but excluded. A native ID bound more than once anywhere
in this server is conservatively excluded from every subtotal, irrespective of origin labels,
so replacement/rerun reuse cannot count a mirrored lifetime twice. Distinct replacement
sessions contribute once; failed/stopped writers remain represented. Attempt references are
not metering identities.

Statuses are `observed` (declared measured scope), `partial`, `unavailable`, `pending_final`.
Coverage includes field-level session counts. An active writer, or a cutoff before ownership
ended, cannot display final completeness even if the source calls its snapshot observed.
An unavailable acquisition contains no invented quantities and preserves the last good
snapshot and its original window, alongside the latest failure/recovery reason.

Optional `credits`/`money` each require decimal-string `amount`, explicit provider `unit`
(currency for money), `basis` (`provider_reported` or `estimated`) and `source`, all for the
snapshot's exact window/account/session. Preserve provider UI estimates. Quantities remain
per-source rows: no cross-provider credit, currency or estimate totals. Copilot history
`cost` is a billing-multiplier aggregate, **not verified credits or USD**; context-window fill
and account-wide quotas are not cumulative writer usage.

A writer credential still ends at delivery. Later in-scope Captain observations may update
only telemetry and must retain a cutoff within ended ownership. A full-lifetime refresh
including later unrelated turns is not attributable; do not relabel its cutoff. Without a
bounded final refresh, leave partial/pending-final coverage. No reopening, capacity retention,
delivery delay, queue pausing, enforcement, purchases, schedules or daemon are involved.

## Source versus deployment

This source adds `writer_usage` to the canonical direct schema and change-token triggers to
the canonical dashboard schema, following the repository's no-new-migrations policy.
It does **not** alter an existing live schema, restart services, install tools, change
running Captains/writers or activate collection. Fresh test/new-install stores include the
relation. An existing installation needs a separately coordinated, data-preserving schema
and source rollout by its service owner before these endpoints/detail projection are used.
Do not delete/reset live data or treat a source merge as deployment evidence.
