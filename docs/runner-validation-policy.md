# Trusted runner-local validation policy

`phalanx.validation_policy.load_validation_policy(config_path, repository_id)`
is the callable policy interface for stage wiring. Supply an explicit absolute
path to a separate operator-owned UTF-8 JSON file and the exact registered
repository ID from the claim. The loader never searches a checkout, reads
repository instructions automatically, or accepts commands or bypasses from
an item or worker result.

Keep this file outside **every worker-writable checkout and worktree**, with
write permissions restricted to the trusted operator/runner account. The caller
must enforce that trust boundary; an absolute path alone is not proof of trust.
The operator translates repository instructions, expected CI, and provider
requirements into this configuration. Do not take the path from a work packet,
checkout setting, or worker-controlled environment. Do not authorize edits to
the policy file as part of a worker's task.

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
            "reason": "This check requires the other target platform",
            "authorization": "Operator-approved repository policy section 3"
          }
        }
      ],
      "draft_checks": [
        {
          "name": "draft-build",
          "kind": "check_run",
          "app_id": 42,
          "use_merge_sha": true,
          "require_execution": true
        }
      ],
      "ready_checks": [
        {"name": "ci", "kind": "check_run", "app_id": 42},
        {"name": "deployment-smoke", "kind": "preview", "require_execution": true}
      ]
    }
  }
}
```

This is separate from the attended runner's checkout/capacity config. The
loader does not modify that config or add a CLI option.

All three phase lists are mandatory for each configured repository. `null`
means that phase's policy is **unavailable**; `[]` explicitly confirms there
are no applicable requirements. Missing files, unconfigured repository IDs,
missing fields, unknown keys, duplicate JSON keys, malformed values, unsupported
versions, invalid registered IDs, and oversized files raise
`ValidationPolicyError`, not an empty policy. Validate and repair the file before
retrying; stage wiring must expose the error as a blocker, never replace it with
`[]`. Every repository entry is validated, including entries not selected by
the current call. Files are bounded to 1 MiB.

Local checks are capped at 50 per repository with unique exact names. Commands
need a nonempty argument list whose executable is absolute (and `.exe` on
Windows). They run without a shell through `CandidateLocal` in the verified,
clean item worktree at the claimed candidate. Gitignored build output does not
make the worktree dirty. Arguments are literal: no path
discovery, interpolation, or environment expansion. `timeout_seconds` defaults
to 60 and must be an integer from 1 through 86400; booleans are not integers.
A check that times out is stopped with the processes it started and recorded as
failed.

`allow_skip` and `allow_inapplicable` default to false and must be JSON booleans.
A `bypass` replaces the command and cannot include `argv` or `timeout_seconds`.
It requires `outcome: "skipped"` with `allow_skip: true`, or
`outcome: "inapplicable"` with `allow_inapplicable: true`, plus nonblank `reason`
and `authorization` (each at most 16000 characters). Permission is scoped to
that named check and disposition, not the whole phase. A request for urgency
does not grant permission: the operator must record the authorized disposition
in this trusted file. Remove temporary bypasses when their authority expires.

Published checks are capped at 100 **per phase**. Each has an exact `name`
(1 through 255 characters without surrounding whitespace), `kind`
(`check_run`, `status`, or `preview`), optional positive integer `app_id`, and
boolean `use_merge_sha` and `require_execution`, both defaulting to false.
Duplicate identities `(kind, name, app_id)` are rejected even if revision flags
differ. An unbound app identity cannot overlap a specific app for the same
kind/name. Distinct explicit apps and a status sharing a check-run name are
valid separate requirements. App-bound statuses are rejected because the
current GitHub status reader cannot verify their source app.

## Existing consumers and evidence

```python
from pathlib import Path
from phalanx.validation_policy import load_validation_policy

policy = load_validation_policy(
    Path(r"C:\phalanx-runner\validation.json"), claim["request"]["repository_id"]
)
body = candidate_local.pre_validate(policy.local_checks)
# Journal this exact body before the existing result submission.
```

`policy.local_checks` contains the existing `candidate_local.LocalCheck` and
`validation.PreCheck` types. `CandidateLocal.pre_validate` binds results to
the claimed candidate only after verifying the clean worktree HEAD, records
the affected check's name, disposition, reason and authorization, and returns
`prevalidated_candidate` on success (or `candidate` on a blocked/failed result).
The existing result route stores those fields in the candidate-bound timeline.
Earlier failed attempts remain in history; a later bypass is not a rewritten
pass. The loader itself neither runs checks nor fabricates revision evidence.

`policy.draft_checks` contains `validation.RequiredCheck` tuples for the draft
collector/promotion caller. `policy.ready` is the existing
`published_validation_stage.PublishedValidationPolicy`. All source-app,
head/test-merge revision and execution requirements survive conversion. A local
bypass does **not** remove draft checks, ready CI, previews, or check-in gates.
Carry the complete configured requirements into the existing exact-PR,
head/base-bound observations, not just the checks currently returned by GitHub.

## Remaining integration

This loader is not PH-14a pipeline acceptance. Captain/CLI configuration,
claim dispatch, local result journaling, draft collection/promotion, ready
collection and durable recheck orchestration still need stage wiring.

Preview requirements load as ordinary `RequiredCheck` entries, but the loader
never creates a provider reader: `policy.ready.read_preview` is `None`.
The existing collector blocks a required preview without a trusted
`github_checks.PreviewReader`. Subsequent wiring must supply a repository-specific
adapter that verifies the provider/environment, served SHA, and smoke-test
evidence for the exact observed revision and phase. Provider URLs, reader names,
commands, environment selection and other preview adapter settings are not
supported by this file and are rejected as unknown keys. Do not silently drop
the requirement.

Draft-only fallback configuration is also unsupported and rejected. When an
already-ready PR lacks required draft evidence, the existing validation gate
demands a recovery decision. Wiring must expose that blocker or implement an
explicit authorized recovery under the authoritative state-machine rules,
not turn the draft requirement into an empty list or waive published gates.
