# PH-DS4 direct-sessions pilot evidence

Evidence snapshot: November 24, 2026, approximately 05:50 UTC. All times below
are UTC. **PH-DS4 is not yet accepted.** The gate is this evidence record plus
one combined post-#196 delivery with a Captain handoff and keeper restart while
its PR is open, as specified by [plan PR #198](https://github.com/caltseng/phalanx/pull/198)
(merged as `5b1356d`). Earlier delivery and continuity runs are evidence, not a
substitute for that combined run.

## Sources and interpretation

GitHub PR links below identify implementation, reported local checks and merge
events. A merged fix proves that code landed, not that every live acceptance
criterion passed. The Captain records distinguish an observed handoff/restart
from an uninterrupted, intervention-free delivery; see
[#186](https://github.com/caltseng/phalanx/pull/186) and
[#190](https://github.com/caltseng/phalanx/pull/190).

Historical item and Captain IDs below are server identifiers (short prefixes
where only a prefix was recorded). The pilot coordination history is session
`97150add-b33c-46ee-b739-ae965836d2e0`, especially turns 2, 18, 28 and 34-48.
The historical Captain records were re-read through `shift status` on November
24 by the coordinating Captain `b1b0966fca924756bd058f853c7ad8c3`.
These are source references, not instructions to edit server state.

## First direct PR and report runs

The PR pilot was item `9f34efa309704673b1b25ab57be97522`, execution
`9b6d8340b3d44b208ab8f87dbbbda4d5`, target `checked_in`. Its setup kept it off
the old runner using `direct-pilot` affinity (coordination history, turn 2).
Captain `a4b92c82` subsequently reported writer `004ff246`, worker session
`38287bab`, and the worker's published
[phalanx-pilot-py#7](https://github.com/caltseng/phalanx-pilot-py/pull/7)
(turn 28). This is evidence of the direct PR path, not of a concurrent handoff
or keeper restart during that PR's short open interval.

| Time on November 23 | Evidence |
| --- | --- |
| 20:42:18 | Pilot-py#7's commit `9956aef4cb973ac97cb92f5dedd707a54039f6d2` adds `median(values)` and focused tests. The PR body reports 14 local tests passing; this record does not rerun them. |
| 20:46:39 | GitHub created [pilot-py#7](https://github.com/caltseng/phalanx-pilot-py/pull/7) on branch `phalanx/6c94c6f0d4efdad7c35b383c11845fe48cb28e233c5524547ae2491d8d24f428`. |
| 20:47:11 | GitHub's PR timeline records merge and close, merge commit `30faec1171c6d0cc6f8efff227b486298de45912`. The Captain reported auto-merge and `delivery / waiting_for_external` immediately afterward (turn 28). |
| 20:47:17 | The same item raised false `pr_closed` although GitHub had merged it (turn 28). [#184](https://github.com/caltseng/phalanx/pull/184) records that the next exact observation cleared it and explains the list-endpoint error. |
| 21:54:53 | [#184](https://github.com/caltseng/phalanx/pull/184) merged as `7fff033`: branch discovery now re-reads the exact PR before posting its merged/mergeable state. |

[Plan #190](https://github.com/caltseng/phalanx/pull/190) and
[#198](https://github.com/caltseng/phalanx/pull/198) count pilot-py#7 as the
first delivered direct pilot. The contemporary wake still showed an external
wait, and this record does not have the later completion event/version.
Consequently, neither a clean flag history nor the final combined acceptance
is claimed for item `9f34efa3`.

The report pilot, item `1b288612b629476c9fcd7fec11c3adfe`, execution
`6ac8f83c6a52468aa9b671fd45fe9ecd`, reached `report_storage / completed`,
version 10, with draft revision
`4ed201b9b5f1772a65242fd6500404eebd67e870d917df62f37075a302be4f37`
(coordination history, turn 18). Its writer `fc28aba3` remained open after
completion, one of the cases addressed by
[#187](https://github.com/caltseng/phalanx/pull/187). This was report delivery,
not proof of the PR gate.

## Captain handoff and keeper pilot

The live continuity run used the #181 deploy `d60b20d`; the coordination
history explicitly held #183's deploy until the pilot finished (turns 34-37).
[#186](https://github.com/caltseng/phalanx/pull/186) and
[#190](https://github.com/caltseng/phalanx/pull/190) record the same Captain
chain and its findings.

| Time on November 23 | Observed event and source |
| --- | --- |
| 21:29:19 | Captain `a4b92c82dd394167b6097c6b1a5665f8` recorded its ending/handoff. Its note named live item `294506a4` ([#184](https://github.com/caltseng/phalanx/pull/184), writer `c25e0460`, session `0bf353fd`) and item `fb3c5b06` (writer `9a5117cc`, session `b0433fcc`). Source: historical Captain record and coordination turn 38. |
| 21:30:06 | Successor Captain `83ab6c5161a3461bac3a87f456b09c79` registered, in session `8f01063e-c250-470c-ba0c-72566fb27507`. Source: historical Captain record; turn 38 records its own five-minute wake schedule. |
| 21:55:41 | Keeper-started Captain `648eeae0613c4e40b222a128f8686b5d` registered. Source: historical Captain record; [#186](https://github.com/caltseng/phalanx/pull/186) records that it woke once and ended because an app automation run could not attach its own session automation. |
| Later in that pilot | Keeper run `1b7d7479` created Captain session `9a6fe897` with its own five-minute automation. [#186](https://github.com/caltseng/phalanx/pull/186) explicitly records live verification, but supplies no exact timestamp; none is inferred here. |
| 22:26 | Deployment of `d4c3e460` (#187) ended the old completed writers, including `fc28aba3`, as `closed as completed`; keeper `6fcab8bd` remained disabled pending #186. Source: coordination turn 46. |
| About 22:36 | #186 (`1462f9f`, merged 22:35:04) was live; keeper `6fcab8bd` was re-enabled with its next run at 23:00. Source: coordination turns 47-48 and [#186](https://github.com/caltseng/phalanx/pull/186). |

This proves that a handoff and keeper-driven replacement were exercised, not
that a clean overlap or a post-fix combined run succeeded.
[#186](https://github.com/caltseng/phalanx/pull/186) records required
interventions: the predecessor supplied the missing concrete CLI command by
hand; successors could skip inherited writers; reports still went to the old
chat; creator-only archiving failed; a keeper could race successor startup;
and an automation-run Captain could not sustain its own wakes. Its fixes
cover those continuity problems, the keeper's stop-condition handling, and
verbatim rather than paraphrased writer instructions. The later
`83ab6c51`/`648eeae0` cleanup notes are not new success proof for the final gate.

## Pilot fixes and Phalanx self-hosting

The following are landed fixes or documented behavior, not independent live
acceptance claims. Merge times are GitHub PR merge events.

| PR | Merge time (UTC) | Evidence and scope |
| --- | --- | --- |
| [#184](https://github.com/caltseng/phalanx/pull/184) | Nov 23 21:54:53 | Exact PR observation after branch lookup fixes pilot-py#7's false `pr_closed`; includes a focused regression. |
| [#186](https://github.com/caltseng/phalanx/pull/186) | Nov 23 22:35:04 | Continuity fixes from the Captain/keeper chain above (decision 83). |
| [#187](https://github.com/caltseng/phalanx/pull/187) | Nov 23 22:24:41 | Report finalization ends the writer; also cleans up writers already left open on terminal executions. Item `fb3c5b06`; report pilot `1b288612` was an affected case. |
| [#189](https://github.com/caltseng/phalanx/pull/189) | Nov 24 01:02:04 | Item `3bfa968cc9964a9dacc00e99a7bb220c` adds draft-only publication and reconciler verification at the reviewed head. Its merge is not a live draft-only acceptance run. |
| [#191](https://github.com/caltseng/phalanx/pull/191) | Nov 24 01:42:05 | Runtime repository-trust blocks become visible owner blockers while retaining the existing writer/session. |
| [#192](https://github.com/caltseng/phalanx/pull/192) | Nov 24 01:41:59 | Restores the default intake fingerprint changed by #189; draft pushes after self-review requeue review; writers can read their own flags. |
| [#193](https://github.com/caltseng/phalanx/pull/193) | Nov 24 01:49:16 | Supplies each repository's configured checks interpreter/prefix and requires an explicit question when that environment is unavailable. |
| [#194](https://github.com/caltseng/phalanx/pull/194) | Nov 24 02:09:18 | Documents best-effort lifecycle title normalization: blocker before draft WIP, without changing delivery gates. |
| [#195](https://github.com/caltseng/phalanx/pull/195) | Nov 24 02:10:13 | Uses the execution's target in kickoff. Live reruns `bd722c4e` and `cc584d7a` had stale `checked_in` kickoffs despite `ready_for_check_in`; the Captain withdrew the intents before launch. |
| [#154](https://github.com/caltseng/phalanx/pull/154) | Nov 24 02:54:27 | Defaults intake to authorized check-in and supports target steering; its own item `cc584d7a` was ready-only and exposed the stale title issue below. |

[Plan #198](https://github.com/caltseng/phalanx/pull/198) explicitly records
that Phalanx's own PRs now ship through direct writers, citing #154 and
#189-#196 (including plan reflection #190). This is self-hosting evidence,
not a claim that every cited PR merged or that all shared one handoff/restart.
The per-PR merge events above distinguish delivered code from open work.

### #196 status and the title failure it addresses

At this snapshot, [#196](https://github.com/caltseng/phalanx/pull/196) is
**OPEN, ready (not draft), not merged**, at head
`9fbd890114c983af06e0c7c83372b5993843604b`. GitHub reports its aggregate
`Tests` check and all 12 platform/version shards successful (aggregate
completed November 24 at 03:14:11). Green checks are not a merge or a
post-deployment pilot result.

Its PR body supplies the failure timeline for item `cc584d7a` / #154 on
November 24: `[Blocked]` at 02:27 for a real conflict; resolution
`2da4ab48` pushed at 02:45:48 and recorded as candidate at 02:46:12; ready at
02:47:42; delivery result at 02:47:49. The latest observation still described
old head `d8454049` (02:46:10). The reconciler observed the new head at
02:48:26, and the execution completed at 02:53, ending the writer before
another title pass. The owner cleared the stale prefix by hand. This is
explicit counter-evidence to claiming intervention-free delivery, not merely
a pending cosmetic check ([#196](https://github.com/caltseng/phalanx/pull/196)).

## Final combined pilot: remaining evidence, NOT PASSED

No verified qualifying run is recorded here. #196 is still open, and the
earlier Captain chain predates it. Later merges alone cannot establish
handoff/restart coverage ([#198](https://github.com/caltseng/phalanx/pull/198),
[#196](https://github.com/caltseng/phalanx/pull/196)).

For **one Phalanx item delivered after #196**, append the following evidence
to this section. Any normal Phalanx item can qualify; no synthetic extra item
is required by [#198](https://github.com/caltseng/phalanx/pull/198).

1. **Identity and deployed fixes:** full item/execution IDs, target,
   publication mode, deployed revision containing #196, writer ID, nonce and
   native session reference; timestamped registration/confirmation events.
   Record identifiers only, never the writer credential.
2. **Writer-owned publication:** accepted candidate/self-review evidence,
   push and PR-created events, URL, head/base and timestamps. Show the worker
   pushed and opened the PR, and the reconciler recorded that same PR.
3. **Handoff while that PR is open:** predecessor/successor Captain IDs,
   ending/start/heartbeat timestamps, the handoff note naming this item and
   its existing writer, and evidence the successor took responsibility without
   a duplicate writer. Do not equate separate Captain records with overlap.
4. **Keeper restart while that same PR is open:** the no-live-Captain
   observation, keeper invocation and new Captain/session IDs, timestamps,
   durable wake schedule and a subsequent successful wake observing the
   existing item/writer.
5. **Verified delivery and clean history:** reconciler observations, current
   required checks/reviews, final status/output and completion event/version;
   merge SHA/method/time if the target is `checked_in`. Record every flag and
   its resolution, show no false flags or manual state edits, and explicitly
   record any manual title or lifecycle intervention rather than claiming a
   clean pass.

These are the missing joint PH-DS4 observations, not results produced by
writing this document. The broader PH-DS5 acceptance remains separate:
lost-launch discovery by nonce, racing Captains/exact-writer matching,
replacement preserving local changes, all three merge methods, and handoff
overlap plus keeper recovery after every Captain is gone. None is newly
certified by this record; see the PH-DS5 row recorded in
[#198](https://github.com/caltseng/phalanx/pull/198) and the
[migration acceptance contract](../direct-sessions.md#migration-order).
