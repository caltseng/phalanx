# PH-DS4 direct-sessions pilot evidence

Updated November 24, 2026, after #209's observed check-in at 09:07:45 UTC.
All times below are UTC. **PH-DS4's combined handoff/keeper gate is accepted** by
the coordinating session on the evidence in
[Post-#196 handoff and keeper gate](#post-196-handoff-and-keeper-gate).
The earlier sections retain their approximately 05:50 snapshot and its then-open
findings. The gate came from [plan PR #198](https://github.com/caltseng/phalanx/pull/198)
(merged as `5b1356d`); earlier deliveries alone did not pass it.

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
The same pilot coordinator subsequently supplied server/GitHub evidence for
the writer lifecycle and action IDs below, from session `97150add` on November
24. Events without full IDs retain the prefixes supplied in that report.

## First direct PR and report runs

The PR pilot was item `9f34efa309704673b1b25ab57be97522`, execution
`9b6d8340b3d44b208ab8f87dbbbda4d5`, target `checked_in`. Its setup kept it off
the old runner using `direct-pilot` affinity (coordination history, turn 2).
Captain `a4b92c82` registered writer `004ff2469ba4409dad58dcf779f4dcf0` at
20:39:58 and confirmed native session
`38287bab-455a-43b4-943d-a553b5d47178` at 20:40:51 (server writer record,
re-read by the pilot coordinator). Its published
[phalanx-pilot-py#7](https://github.com/caltseng/phalanx-pilot-py/pull/7)
was reported in turn 28. This is evidence of the direct PR path, not of a concurrent handoff
or keeper restart during that PR's short open interval.

| Time on November 23 | Evidence |
| --- | --- |
| 20:42:18 | Pilot-py#7's commit `9956aef4cb973ac97cb92f5dedd707a54039f6d2` adds `median(values)` and focused tests. The PR body reports 14 local tests passing; this record does not rerun them. |
| 20:46:39 | GitHub created [pilot-py#7](https://github.com/caltseng/phalanx-pilot-py/pull/7) on branch `phalanx/6c94c6f0d4efdad7c35b383c11845fe48cb28e233c5524547ae2491d8d24f428`. |
| 20:47:11 | GitHub's PR timeline records merge and close, merge commit `30faec1171c6d0cc6f8efff227b486298de45912`. The Captain reported auto-merge and `delivery / waiting_for_external` immediately afterward (turn 28). |
| 20:47:17 | The same item raised false `pr_closed` although GitHub had merged it (turn 28). [#184](https://github.com/caltseng/phalanx/pull/184) records that the next exact observation cleared it and explains the list-endpoint error. |
| 20:50:18 | Writer `004ff246` ended; the pilot coordinator's server inspection confirms execution `9b6d8340` completed. No exact completion event/version was supplied. |
| 21:54:53 | [#184](https://github.com/caltseng/phalanx/pull/184) merged as `7fff033`: branch discovery now re-reads the exact PR before posting its merged/mergeable state. |

[Plan #190](https://github.com/caltseng/phalanx/pull/190) and
[#198](https://github.com/caltseng/phalanx/pull/198) count pilot-py#7 as the
first delivered direct pilot, consistent with the later server inspection.
The contemporary wake still showed an external wait before completion.
Neither a clean flag history nor the final combined acceptance is claimed
for item `9f34efa3`.

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
| 21:32:00 / 21:32:06 | Successor takeover actions `f57d52d8e6bd4fa3bac38e65918ce058` (item `294506a4`, execution `d59890e5`) and `0b9bb4e9fbb94b7ea5a2990eeadcba28` (item `fb3c5b06`, execution `cffc54b5`) record taking over the existing writers and redirecting reports. #184 was open from 21:15:43 to 21:54:53. |
| 21:38:48 | Action `90909ad0` on item `294506a4` records rebased #184 head `aa2d453`, evidence of work continuing after takeover. |
| 21:55:41 | Keeper-started Captain `648eeae0613c4e40b222a128f8686b5d` registered. Source: historical Captain record; [#186](https://github.com/caltseng/phalanx/pull/186) records that it woke once and ended because an app automation run could not attach its own session automation. |
| 21:57:19 / 21:57:26 | Keeper-started Captain action `8b121f1e` on execution `cffc54b5` acted while writer `9a5117cc` was live and #187 was open (21:46:23-22:24:41). Escalation `d5865bcb` records failure to schedule its own wakes. |
| 21:57:44 | Captain `648eeae0` heartbeated and cleared `no_live_captain`, following the simulated crash of `83ab6c51` and the resulting `captain_stale`/`no_live_captain` flags. Keeper run `48f36f56`, session `433981da-d954-4ac0-961b-3e8f4093c4f5`, performed the restart. |
| 22:19:06 / 22:23:08 | After run `03f4f58c` was refused chat creation, keeper run `1b7d7479` created project session `9a6fe897-6fb3-4f25-becc-b62ca35c478a` with its own five-minute automation. Its Captain `d3c8b56dfa1e41f9b3c8cfed28db5970` registered at 22:19:06 and ended at 22:23:08 with `Stop condition met`, having inspected all seven charter items. Source: coordinator's server records and [#186](https://github.com/caltseng/phalanx/pull/186). |
| 22:26 | Deployment of `d4c3e460` (#187) ended the old completed writers, including `fc28aba3`, as `closed as completed`; keeper `6fcab8bd` remained disabled pending #186. Source: coordination turn 46. |
| About 22:36 | #186 (`1462f9f`, merged 22:35:04) was live; keeper `6fcab8bd` was re-enabled with its next run at 23:00. Source: coordination turns 47-48 and [#186](https://github.com/caltseng/phalanx/pull/186). |

Keeper automation `6fcab8bd-21a4-46f8-9f1d-0c1ecec7d47d` also ran a
live-Captain control (run session `a6ffb22f`) and exited without registering,
according to the pilot coordinator's report. The resulting chain was
`a4b92c82` -> `83ab6c51` -> `648eeae0` -> `d3c8b56d`.
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

### #196 at the earlier snapshot and the title failure it addresses

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

## Gate checklist at the earlier snapshot (then NOT PASSED)

This checklist is preserved as the earlier snapshot's requirements, not the current gate result.

No verified qualifying run is recorded here. #196 is still open, and the
earlier Captain chain predates it. Later merges alone cannot establish
handoff/restart coverage ([#198](https://github.com/caltseng/phalanx/pull/198),
[#196](https://github.com/caltseng/phalanx/pull/196)).
The pilot coordinator also reports later handoffs
`aa3f71b7` -> `11f5960c` -> `f4e99892` -> `b1b0966f` (last about November
24 05:02, with #200 and churchtree writers live), but no keeper restart in
that chain. Those handoffs do not fill the missing combined evidence.

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

## Post-#196 handoff and keeper gate

**Result: accepted combined PH-DS4 evidence, with no rerun required.** The
owner-operator coordinating session `97150add-b33c-46ee-b739-ae965836d2e0`
accepted the natural first-compaction handoff while PH-DS5's writer was active,
the deliberate keeper restart while #207 was open, and subsequent verified
delivery. This is a combined record, **not** a claim that one PR was open across
both the first handoff and the restart. #209 was published later. The broader
PH-DS5 acceptance cases are not all newly certified by this gate.

### Independently verified

The documentation writer re-read `GET /v1/captains?limit=100`, both items'
execution timelines and closed operator summaries, plus `gh pr view` for
#196, #207, #208 and #209, on November 24. Captain records retain the latest
heartbeat and handoff note, not a heartbeat-event history.

| Time | Retained API/GitHub evidence |
| --- | --- |
| 05:58:33 | [#196](https://github.com/caltseng/phalanx/pull/196) merged as `0c5a093dda42f7b16083313ea9ebb7ee2f134923`. The earlier open-PR snapshot above is historical. |
| 06:17:13 | The PH-DS5 item's Captain action records writer `940ff45d0f484fe793e52023a4b69a67`, native session `7f3334a8-4604-41ce-9ce0-bcd4ec90cf37`, confirmed busy with no reported trust block. Item `1279ccb6d8fa43b6bdd77621dd79914a`, execution `9af23669543a4c868a3c6c712e3f7c54`, was running development. |
| 06:43:08 / 06:44:35 | Captain `d1ae000a2776479f8c0ba7df6e3bc8b3` (chat `e7b4686f-c042-432d-b4e6-34ed5f0a188e`) entered **ending** at 06:43:08.76 with a first-compaction handoff naming that live writer. Its final heartbeat is 06:44:35.62. Successor `da4397c563a444b1abda857f2f5f6c36` (chat `d3d08b37-24bc-4342-9897-1ed107982e15`, parent `d1ae000a`) registered at 06:44:35.25. At 06:45:30 its item action records routing reports to the successor, not replacing the writer. |
| 06:56:48-06:57:38 | Row-11 item `2d9b239961f2420aaa49e69dac60fb0e`, execution `c7cb9d817ba54886914510d5fa860596`, writer `d963985f1c9e4521b5b853ee71aff275` (named in `d1ae000a`'s handoff), accepted candidate `69fdbacd790f54a5af06ba87c8ba5d38e77b7594` (v3), self-review (v5) and writer-owned publication of [#207](https://github.com/caltseng/phalanx/pull/207) (v7). GitHub created the ready PR at 06:57:23; it stayed open across the keeper restart. |
| 07:05:13 | #207's execution completed ready-only at v8, not yet checked in. |
| 07:31:48 / 07:32:28-30 | Captain `db02d2b3837e447a952b2932bdf48905` registered `baa6b9cf-e77a-49a9-90c8-8d53fb1c152d` as its session reference, then entered ending at 07:32:28 and recorded ended at 07:32:30. Its handoff note explicitly says this was the app project-session ID, not native chat ID, and names the correction. **Known prompt-adherence gap:** the packaged Captain prompt already required the native ID. This is not a missing prompt requirement or successful registration claim. |
| 07:32:32 / 07:33:11 | Corrected Captain `e28eea7ee07b4fbba623c883d94088d6`, native chat `43a0595d-389b-4f74-9b34-671672f5f6b1`, registered with parent `da4397c5`. Its 07:33:11.77 timeline action routes the existing PH-DS5 writer's reports to it, explicitly with no replacement. |
| 07:38:23 | `da4397c5`'s final stored heartbeat/end write has note `Keeper drill crash (06:58:50Z); keeper successor e28eea7ee07b4fbba623c883d94088d6 took over`. The crash-time heartbeat is supplied through this note, not a retained heartbeat snapshot. |
| 07:38:53 / 08:08:15 | #207 merged as `cd46be7d40ed2b1780c7ebe75bbf0c44892b046b`; urgent fix [#208](https://github.com/caltseng/phalanx/pull/208) later merged as `c213c5ce0db9530b3d53c153ed7fd97fef12aeb3`. #208's body identifies the first deploy's reconciler `KeyError: 'revision'`: compact operator `output` lacked the revision. The fix adds `output_revision` and isolates per-execution observation failures. |
| 08:09:14 / 08:09:16 | Urgent item `6cdfafb115284c00918837f71f4db152`, execution `188b1f7f6c164a39acbd2f2a1452d7e4`, completed delivery v10 as checked in. #207 then received a server-owned `pr_observed` v9 at 08:09:16.49: status remains completed, target remains ready-only, output now records the verified merge and board outcome is `checked_in`. Its retained timeline shows observation-based enrichment, not a manual lifecycle edit. |
| 09:00:38-09:00:59 | The inherited PH-DS5 writer published ready [#209](https://github.com/caltseng/phalanx/pull/209) at head `e2fdc170ea062ce534182a7159a3add881800da9` and posted its URL (v17) under `e28eea7e`'s shift. Development and self-review results name that exact head. |
| 09:02:17-09:03:42 | `e28eea7e` handed off after first compaction, final heartbeat 09:03:26.95. Successor `2cecc858044e4f389f0fe72d9052c0fa`, parent `e28eea7e`, registered at 09:03:42.96. Thus publication was under the keeper successor; final readiness/merge were observed under its successor, not while `e28eea7e` was still active. |
| 09:05:39 / 09:06:53 / 09:07:45 | #209 completed ready-only at v18 after required checks were satisfied, with empty feedback and the exact head above. GitHub then records the owner merge as `fe859348905ebf4efba0403df6719e686a993fe7` at 09:06:53. The reconciler enriched delivery at v19, 09:07:45.82, with `checked_in` outcome. No writer replacement or manual state edit is recorded in this delivery sequence. |

#209's own report and decision 87 disclose its separate envelope timing exception:
invariants/fault checks passed, timing targets did not. The PH-DS4 gate does not waive
those targets or relabel them passed.

### Supplied by coordinating sessions

The following details are accepted coordinating evidence, **not independently
verified API/gh facts by this documentation writer**. Owner-operator session
`97150add-b33c-46ee-b739-ae965836d2e0` supplied the drill/run/action details and
authorized this two-tier record; implementation coordinator `ff9ebb04` supplied
the host deployment/rollback account.

- About 06:59, `da4397c5` cleared its wake automation under charter revision
  `8e7d23aa`, simulating a crash while leaving writers untouched. Last heartbeat
  before the drill was 06:58:50, so the 15-minute stale threshold was 07:13:50.
- Keeper run `18c3b061`, session `dd7bbec1-f96b-4ad4-8ce6-e28ee07b740c`,
  ran 07:30:49-07:31:40 and started a successor. The app retained only its latest
  automation run, not this run's historical API record.
- The owner-operator read the server's `captain_actions` table: takeover action
  `6fefef04` is the 07:33:11.77 successor-routing event on execution `9af23669`
  v6. Holds `753fdd86` (execution `c6b9d7fe` v2), `e68788bc` (`dd458ce5` v2)
  and `2e2895c9` (`a44374df` v1) were recorded at 07:33:14, 07:33:16 and
  07:33:18. The supported API timelines expose the corresponding actions and
  times but omit their action UUIDs; the documentation writer did not query
  the server database.
- The first #207 deploy crash-looped the reconciler; the host rolled back to
  `efe2fb0e` pending urgent item `6cdfafb1` / #208. The exception and fix are
  corroborated by #208; the rollback revision is the implementation coordinator's
  deployment evidence.
- The coordinating sessions accepted the resulting #207 observation and PH-DS5
  delivery as satisfying the combined gate without manual state edits or false
  execution flags. Retained delivery observations corroborate the heads/checks/
  outputs; they are not a complete historical flag audit.

`captain_stale` and `no_live_captain` are derived **at read time** and were not
snapshotted during this drill. The stale threshold above is calculated from
the supplied heartbeat, not proof of a persisted flag event. Existing unrelated
orphan-session cleanup flags are not evidence of delivery failure or proof that
every global flag was absent.
