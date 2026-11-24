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

Runners submit a structured `question` with `prompt`, `handoff`, optional `options` (one choice per list element) and optional `proposed_default` in a stage result that needs input. The next claim after a reply includes `input_reply` with the answer, `answered_by` and the question. The execution timeline keeps them, and `inspect` shows the current execution's `last_reply`.

## Operator work board

Open `http://127.0.0.1:8000/operator/` on the running server. With `PHALANX_LOCAL_BOARD=1`, the board opens without a token: its read-only routes (`/v1/operator/*` and the stored report route) skip authentication for requests from a loopback client with a `localhost`, `127.0.0.1` or `[::1]` `Host` header. Every other route still requires its token. Leave the flag unset on a hosted server, including one behind a reverse proxy or tunnel on the same machine, because those connect from loopback too. Without the flag, the board asks for the owner token and holds it in memory until you disconnect or reload. The board uses the same-origin read API and opens stored field reports as plain text. Its static assets use origin-root paths and hold no secrets, so a hosted server serves it at `/operator/` on its own origin.

The board is read-only. It shows each item's prerequisite blockers, any scheduled retry or external recheck, its worker reference and chat link, its current PR and its final output. When an item is waiting for an answer, its detail shows the question and the `reply` command to answer it. Its data comes from the [operator board API](api.md#operator-board-data).
