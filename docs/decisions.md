# Captain decisions

These are best-guess calls the Captain made without asking Caleb. Caleb reviews them and may override any one. To override, edit the row and note the change. The Captain appends new decisions as it makes them. Worker questions it answers are also recorded on the item as `answered_by: captain`.

| # | Area | Decision | Why |
| --- | --- | --- | --- |
| 1 | Pilot (pilot-web#1) | titleCase preserves runs of whitespace and capitalizes only non-empty words | Fixes the crash without changing output spacing |
| 2 | Owner questions | The Captain answers all worker questions (`answered_by=captain`) and logs them here | Caleb asked not to be blocked on these |
| 3 | Validation | Pending draft/ready CI waits and rechecks every 30s; it is not counted as a failure | Slow CI shouldn't escalate after five polls |
| 4 | Validation | A transient git/network error during a PR update retries, and escalates only after 5 failures | Self-resolution |
| 5 | Validation | Failed draft checks send the item back to development with the failure reasons as findings | The worker can fix it without the owner |
| 6 | Validation | A ready PR missing draft-only evidence asks the owner: convert to draft or drop the check. No auto-waiver | Waiving a required check is a policy call |
| 7 | Publication | A rebased candidate force-replaces the PR head only if that head is the one Phalanx last verified; otherwise it asks | Never overwrite someone else's push |
| 8 | Review | Independent review "changes required" goes back to development with the findings | Same loop as self-review |
| 9 | Check-in | Ready-only items complete automatically; merge targets wait on the owner until PH-15a part 3 | Automated merge isn't built yet |
| 10 | Check-in | A PR with merge conflicts asks the owner for now; auto-resolution comes in a later row | Kept part 1 small |
| 11 | Architecture | The server needs the `[server]` extra (fastapi/uvicorn); backup/restore are server-host-only commands | Runners stay light and can't touch server storage |
| 12 | Architecture | The dashboard is served from the API server's origin; a separate static origin is deferred | No CORS or extra config needed |
| 13 | Language | Stay on Python for v1; port to TypeScript/Node after PH-V1 and before hosting (PH-25) | Don't delay v1; host once |
| 14 | Process | Feature PRs edit only their own plan row; plan and muster PRs own ordering and statuses | Stops rebase churn |
| 15 | Worker questions | Options must be separate list items; an unambiguous combined string is split rather than rejected (PH-11g) | Lenient over strict |
