# Trusted reconciler validation policy

The reconciler observes required GitHub checks and runs trusted local checks serially
in scratch worktrees. Local failures raise flags, not a Phalanx gate (D3). Writers run
repository checks using their prepared [checks environment](direct-path.md#prepared-checks-environments).
GitHub checks, reviews and rulesets gate merging.

Set `validation_policy` in [reconciler configuration](configuration.md#reconciler-config)
to an absolute operator-owned UTF-8 JSON file outside every worker-writable checkout.
Restrict writes to the trusted operator account. Never derive the path or commands from
an item, repository content or writer result. An absolute path alone does not establish trust.

## Schema

```json
{
  "version": 1,
  "repositories": {
    "registered-repo-id": {
      "local_checks": [
        {
          "name": "unit-tests",
          "argv": ["C:\\tools\\python.exe", "-I", "-m", "unittest", "discover", "-s", "tests"],
          "timeout_seconds": 600
        }
      ],
      "draft_checks": [],
      "ready_checks": [
        {"name": "ci", "kind": "check_run", "app_id": 42},
        {"name": "deployment-smoke", "kind": "check_run", "require_execution": true}
      ]
    }
  }
}
```

All three lists are mandatory. `null` means unavailable; `[]` explicitly declares no
requirements. `draft_checks` describes observations while a ready-mode PR is still draft;
it never authorizes promotion. Draft-only publication does not wait for policy or ready CI.
Missing ready policy waits visibly but cannot invalidate an actual verified merge.

`load_validation_policy(config_path, repository_id)` validates every entry before selecting
the exact registered ID. Missing/unreadable files, unknown or duplicate keys, invalid IDs,
missing fields, unsupported versions and malformed values raise `ValidationPolicyError`.
Files are limited to 1 MiB. Never replace an error with an empty policy.
`onboard` proposes ready checks from CI on the base tip, sets `draft_checks: []` and leaves
`local_checks: null` for the operator; it never invents trusted commands.

## Local checks

At most 50 uniquely named checks are allowed. Commands use a nonempty literal argument
list, an absolute executable (`.exe` on Windows), no shell, and no expansion.
`timeout_seconds` is a whole number from 1 to 86400, default 60. A timeout stops the
process tree and records whether output identified failures. The reconciler checks the
PR head in a private scratch worktree and posts results bound to that head.

Every configured local check requires `argv`; bypass and skip settings are unsupported.
Remove checks that do not apply rather than configuring a non-executable entry.

## GitHub checks

The GitHub reader supports github.com only, using its fixed REST and GraphQL endpoints.

Each phase allows at most 100 requirements. A requirement has an exact nonblank `name`
(at most 255 characters, no surrounding whitespace), `kind` (`check_run` or `status`),
optional positive `app_id`, and boolean `use_merge_sha` and
`require_execution` (both default false). Duplicate or overlapping identities are rejected.
App-bound statuses are unsupported because their source cannot be verified.

Head/test-merge revision and execution requirements remain exact. Deployment smoke tests
must publish a GitHub check run or status; preview readers are unsupported. Incomplete
GitHub reads post no new snapshot; earlier flags remain until a complete observation replaces them.

See the [acceptance repository policies](pilots/validation-policies.md) for examples.
