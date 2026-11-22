# Captain decisions

These are best-guess calls the Captain made without asking Caleb. Caleb reviews them and may override any one. To override, edit the row and note the change. The Captain appends new decisions as it makes them. Worker questions it answers are also recorded on the item as `answered_by: captain`.

| # | Area | Decision | Why |
| --- | --- | --- | --- |
| 1 | Pilot (pilot-web#1) | titleCase preserves runs of whitespace and capitalizes only non-empty words | Fixes the crash without changing output spacing |
| 2 | Owner questions | The Captain answers all worker questions (`answered_by=captain`) and logs them here | Caleb asked not to be blocked on these |
| 3 | Validation | Pending draft/ready CI waits and rechecks every 30s; it is not counted as a failure | Slow CI shouldn't escalate after five polls |
| 4 | Validation | A transient git/network error during a PR update retries, and escalates only after 5 failures | Self-resolution |
| 5 | Validation | Failed draft checks (the repo's configured `draft_checks`, CI that must pass on the draft before promotion) send the item back to development with the failure reasons as findings. The findings are also posted on the draft PR as a comment and listed under "Known gaps" in its description (decision 17) | The worker can fix it without the owner, and reviewers see the gap on the draft |
| 6 | Validation | A ready PR missing draft-only evidence is converted back to draft by the runner, which comments on the PR, collects the draft checks again and re-promotes. It asks the owner only when GitHub refuses the conversion (for example, permissions). No auto-waiver | Caleb wants a bias towards draft PRs; waiving a required check is still a policy call |
| 7 | Publication | A rebased candidate force-replaces the PR head only if that head is the one Phalanx last verified; otherwise it asks | Never overwrite someone else's push. Confirmed by Caleb. |
| 8 | Review | Independent review "changes required" goes back to development with the findings | Same loop as self-review |
| 9 | Check-in | Ready-only items complete automatically; merge targets wait on the owner until PH-15a part 3 | Automated merge isn't built yet |
| 10 | Check-in | Merge conflicts are resolved automatically by a development attempt that rebases and revalidates; the owner is asked only when retries can't make progress. | Set by Caleb |
| 11 | Architecture | The server needs the `[server]` extra (fastapi/uvicorn); backup/restore are server-host-only commands | Runners stay light and can't touch server storage |
| 12 | Architecture | The dashboard is served from the API server's origin; a separate static origin is deferred | No CORS or extra config needed |
| 13 | Language | Stay on Python for v1; port to TypeScript/Node after PH-V1 and before hosting (PH-25) | Don't delay v1; host once |
| 14 | Process | Feature PRs edit only their own plan row; plan and muster PRs own ordering and statuses | Stops rebase churn |
| 15 | Worker questions | Options must be separate list items; an unambiguous combined string is split rather than rejected (PH-11g) | Lenient over strict |
| 16 | Worker questions | An options string that can't be split unambiguously is kept as one option instead of rejected; a single option is allowed (PH-11g follow-up) | A malformed option list shouldn't stall the item |
| 17 | Publication | When a stage returns the item to development with findings or records a permitted pre-validation bypass, the runner posts one PR comment (keyed on revision and findings, so retries don't duplicate) and keeps a "Known gaps" section in the PR description, removed once a later revision clears it. Writes go through a runner-local outbox, retry up to 5 times and never block the item | Caleb wants deficiencies noted on the draft PR rather than hidden in the queue |
| 18 | Publication | The draft PR opens as soon as development delivers a verified candidate. Only push safety (candidate verified on the base, claim current, no outside push to the item branch), duplicate-PR lookup and a deleted target branch can stop it. A moved target branch no longer sends the item back to development; later stages validate against the current base, and deficiencies go into Known gaps | Caleb: "The barrier to create draft PRs should be fairly low" |
| 19 | Process | Feature PRs don't edit IMPLEMENTATION_PLAN.md; the Captain batches status updates after merges. Approved PRs use GitHub auto-merge pinned to the reviewed head | Plan conflicts forced rebases and CI reruns; auto-merge removes a round trip |
