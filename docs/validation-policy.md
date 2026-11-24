# Trusted reconciler validation policy

The reconciler observes required GitHub checks and runs trusted local checks serially
in scratch worktrees. Local failures raise flags, not a Phalanx gate (D3). Writers run
repository checks using their prepared [checks environment](direct-path.md#shared-non-python-checks-environment-churchtree).
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
        },
        {
          "name": "platform-tests",
          "allow_inapplicable": true,
          "bypass": {
            "outcome": "inapplicable",
            "reason": "This check requires the other platform",
            "authorization": "Operator-approved repository policy section 3"
          }
        }
      ],
      "draft_checks": [],
      "ready_checks": [
        {"name": "ci", "kind": "check_run", "app_id": 42},
        {"name": "deployment-smoke", "kind": "preview", "require_execution": true}
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

`allow_skip` and `allow_inapplicable` default to false and must be booleans. A `bypass`
replaces the command, cannot include `argv` or `timeout_seconds`, and needs the matching
permission, an outcome of `skipped` or `inapplicable`, and nonblank `reason` and
`authorization` (each at most 16000 characters). The reconciler does not execute bypassed
commands. Permission is per check, never authority to bypass repository CI or merging rules.
Remove temporary bypasses when their authority expires.

## GitHub checks

Each phase allows at most 100 requirements. A requirement has an exact nonblank `name`
(at most 255 characters, no surrounding whitespace), `kind` (`check_run`, `status`,
`preview`), optional positive `app_id`, and boolean `use_merge_sha` and
`require_execution` (both default false). Duplicate or overlapping identities are rejected.
App-bound statuses are unsupported because their source cannot be verified.

Head/test-merge revision and execution requirements remain exact. A required preview
without a trusted preview reader is blocked, never silently passed. Incomplete GitHub
reads post no new snapshot; earlier flags remain until a complete observation replaces them.

See the [acceptance repository policies](pilots/validation-policies.md) for examples.
