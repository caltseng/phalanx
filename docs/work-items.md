# Work items

File, import, inspect and answer work with the owner CLI, and watch it on the operator board. Set `PHALANX_OWNER_TOKEN` to the server's owner token. Set `PHALANX_API_URL`, or pass `--api-url` before the command, to override `http://127.0.0.1:8000`.

## File and inspect work

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx intake example "Review the test coverage" --target report --key test-coverage-1
python -m phalanx intake example "Fix the gaps" --target checked_in --after <item_id>:report --priority urgent --context https://example.test/notes --accept "tests pass"
python -m phalanx inspect <item_id>
python -m phalanx cancel <item_id> --reason "Superseded by a newer item"
```

`--after ITEM[:MILESTONE]` waits for another item to reach a milestone (default `checked_in`). `--after`, `--context`, and `--accept` repeat; `--priority` is `low`, `normal`, or `urgent`.
`cancel` reads the item's current execution and version, then requests cancellation with an idempotency key bound to that version. Retrying it after a lost response is safe: if the first request committed, the retry fails with a visible version conflict instead of cancelling twice.

Register the expected remote without credentials. Intake targets are `report` for a stored Markdown field report, `ready_for_check_in` for a reviewed PR, and `checked_in` for a verified merge (the default for `intake`, `ingest-issue` and API intake). On the direct path, the writer enables GitHub auto-merge only for `checked_in`; required reviews, checks and rulesets still gate the merge. Reuse `--key` when retrying the same request. The [API reference](api.md#work-items-and-executions) covers the full intake body, including context, acceptance criteria, priority, dependencies and steering.

### Deliver a PR that stays a draft

Use direct sessions and add `--publication-mode draft` to `intake` or `ingest-issue`
(API: `"publication_mode": "draft"`). This stops at a GitHub-verified draft even for a
`checked_in` target, without promotion or auto-merge. The board shows **draft only** while open
and **draft published** on completion; it does not count as ready for dependent work.
See the [publication contract](direct-sessions.md#pr-publication-and-delivery).

## Import a GitHub issue

Register the expected repository remote first, then import an issue by its number and the **Phalanx repository ID** (not `owner/repo`):

```powershell
python -m phalanx register example https://github.com/example/project.git
python -m phalanx ingest-issue example 42 --target ready_for_check_in
```

Set `PHALANX_OWNER_TOKEN` for the API and `PHALANX_GITHUB_TOKEN` locally with read access to the issue and its comments. The GitHub token stays local; only the snapshot is submitted. The command verifies the registered `github.com` remote, rejects PR numbers, and copies the title, body, all comment pages and source link into a work item. It prints the stored item and execution, which use the normal dispatch path. Closed issues can be imported explicitly. Issue text is task context, not authority to change credentials, checkout trust, runner policy or permissions.

Repeating an unchanged import with the same target returns the original item, including after a server restart or a lost response. An edited issue or changed target returns HTTP 409 under the same stable issue key: inspect the existing item before deciding whether to steer or rerun it, or intentionally create a new import with `--key NEW-KEY` and keep that key for its retries. Imports over the 16,000-character goal limit fail without creating work or truncating context; file a scoped summary with `intake` instead. Provider and authentication failures never create partial work. This is explicit intake, not polling, webhooks or synchronization, and it doesn't modify the GitHub issue.

## Answer a waiting item

Poll the question inbox and use the item's IDs and version from the result:

```powershell
python -m phalanx inbox
python -m phalanx reply ITEM_ID EXECUTION_ID "Use the existing interface" --version 3 --key reply-001
```

A reply records who answered: `--answered-by owner` (the default) or `--answered-by captain` when the Captain answered a routine question itself. Reuse the same key and body when retrying after a lost response. For more inbox entries, pass the last entry's `waiting_since` to `--after-time` and its `execution_id` to `--after-id`.

Writers submit a structured `question` with `prompt`, `handoff`, optional `options`
(one choice per list element) and optional `proposed_default` through `work question`
or a `needs_input` result. The reply queues the same stage and retains its writer.
`work status` and `work next` expose the persisted `last_reply`, including its answer,
version, timestamp and `answered_by`; `inspect` and the timeline retain the question
and reply. An internal attempt claim's `input_reply` is not the writer CLI's response
shape. Resume from the binding rather than expecting the Captain to relay an answer.

## Operator work board

Open `http://127.0.0.1:8000/operator/` on the running server. With
`PHALANX_LOCAL_BOARD=1`, board reads skip authentication for requests from a loopback
client with a `localhost`, `127.0.0.1` or `[::1]` `Host` header. This covers
`/v1/operator/*`, stored reports, `GET /v1/captains`, `GET /v1/captains/{id}` and
`GET /v1/flags`. Narrow `/operator/api/` dashboard actions also allow trusted local access;
this does not open `/v1` writes, session archival records, muster status or
other authenticated operations; a supplied bearer token is still validated. Leave the
flag unset on a hosted server, including behind a same-machine reverse proxy or tunnel.
Without the flag, the board asks for a bearer token and holds it in memory until you
disconnect or reload. The board uses same-origin reads and opens stored field reports
through the authenticated plain-text report API; the board renders a safe Markdown
subset in its report dialog. Its static assets use origin-root paths and hold no secrets.

Scribe shows **Needs you**, **Flags**, live/stale **Captains** (ended Captains are collapsed),
**Active**, **Queued and waiting**, and **Recently closed**. Rows expose stage/status,
the execution's current target, age, registered writer/session, prerequisite or deliberate
charter hold, retries/rechecks, PR and final output. Running work/live Captains share a
pikehead; queued/waiting work uses shields and flags use pennants, with text labels as well
as glyphs. Expanding a row loads paginated history, stage returns, prerequisites and any
question with its exact `reply` command. Reports omit raw HTML and external images;
code stays literal and links allow only credential-free HTTP(S).

The header **+ / New work item** is the local board's write action: it files through the
`/operator/api/items` adapter to the existing intake implementation, defaults to **Merged**
(`checked_in`), and accepts
goal, acceptance criteria, target, priority, label, publication mode and dependencies.
On an explicitly enabled local-only board it opens directly into the usable form, without
an owner token or unlock step. Nonlocal boards still require the owner connection.
The modal stores no token in browser storage and retains the exact request/idempotency key
after an uncertain response for safe retry. Local writes require an exact same Origin
(scheme, host and port), a verified loopback peer and local Host, and reject null origins,
cross-site/same-site requests and forwarding headers. Keep the service bound to
`127.0.0.1` (or `::1`), with no proxy or tunnel: another local process is trusted, but a
network or shared-machine deployment is not this trust model. Before enabling or deploying,
the service owner must verify the actual listening address and explicit
`PHALANX_LOCAL_BOARD=1`, drain/restart the existing service rather than start another, and
confirm local creation succeeds while unauthenticated `/v1` writes still return 401.
The server never sends its owner credential to the browser. See the
[form contract](../DASHBOARD.md#new-work-item-local-board).

The existing toolbar **Writer concurrency** control uses that same authorization model
through `/operator/api/dispatch/settings`, without a separate local token gate. It updates
the canonical persisted limit, preserves omitted pause/aging settings and reports failures
explicitly. Hosted/nonlocal boards still use the owner connection; `/v1` settings writes
remain owner-bearer-only.

Other board actions still use the CLI/API. Item/execution deep links, queue/history pages,
URL-backed search/filters and rendered Markdown reports have shipped locally. Group pages,
hosted sign-in and the broader dashboard action set remain planned. The
[server reads](api.md#operator-board-data) also support label-group rollups, overview counts
and conditional ETags; API support alone does not imply matching UI. PR/CI/size/duplicate facts come only from
reconciler observations and stay unknown when unobserved, never inferred from a writer's URL.
**Merged** is a display label; API targets remain unchanged.
