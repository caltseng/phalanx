# Acceptance repository validation policy

The runner validation policy for the PH-14a live runs on
[phalanx-pilot-py](https://github.com/caltseng/phalanx-pilot-py) and
[phalanx-pilot-web](https://github.com/caltseng/phalanx-pilot-web). The format
is described in [runner-validation-policy.md](../runner-validation-policy.md).

Save it outside every checkout, for example `C:\phalanx-runner\validation.json`,
and set `"validation_policy"` in the runner config to that path. Before use:

- Replace each key with the repository ID registered on the server.
- Replace each `argv[0]` with an absolute interpreter on the runner host. On
  Windows it must be a real `.exe`, not the `WindowsApps` store alias. On macOS
  or Linux use, for example, `/usr/bin/python3` and `/usr/local/bin/node`.

```json
{
  "version": 1,
  "repositories": {
    "phalanx-pilot-py": {
      "local_checks": [
        {"name": "unit tests",
         "argv": ["C:\\Python312\\python.exe", "-m", "unittest", "discover", "-s", "tests", "-v"],
         "timeout_seconds": 600}
      ],
      "draft_checks": [],
      "ready_checks": [
        {"name": "test (ubuntu-latest)", "kind": "check_run", "app_id": 15368},
        {"name": "test (windows-latest)", "kind": "check_run", "app_id": 15368}
      ]
    },
    "phalanx-pilot-web": {
      "local_checks": [
        {"name": "unit tests",
         "argv": ["C:\\Program Files\\nodejs\\node.exe", "--test"],
         "timeout_seconds": 600}
      ],
      "draft_checks": [],
      "ready_checks": [
        {"name": "test (ubuntu-latest)", "kind": "check_run", "app_id": 15368},
        {"name": "test (windows-latest)", "kind": "check_run", "app_id": 15368}
      ]
    }
  }
}
```

## Sources

Each entry comes from the repository's `AGENTS.md`, `.github/workflows/ci.yml`
and its `main` ruleset, checked against the check runs on `main`
(pilot-py `38480d5`, pilot-web `096591a`) with
`gh api repos/caltseng/REPO/commits/SHA/check-runs`.

- **Local checks.** pilot-py runs `python -m unittest discover -s tests -v`
  (Python 3.12+, standard library only). pilot-web documents `npm ci` and
  `npm test`, which runs `node --test` (Node.js 20+). The package has no
  dependencies, so `node --test` runs the same tests, and it avoids `npm.cmd`,
  which the policy cannot run on Windows.
- **Draft checks.** None. CI runs on `pull_request` for drafts too, but the
  ruleset requires it only for merge, so promotion doesn't wait for it.
- **Ready checks.** The ruleset on `main` requires `test (ubuntu-latest)` and
  `test (windows-latest)`. Both are GitHub Actions check runs (app 15368).
- **Previews and bypasses.** Neither repository has a preview gate or a
  documented local-check bypass.
