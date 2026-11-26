# Phalanx keeper

Every 30 minutes recover missing Captains, never take over live work. Load
`phx shift prompt captain`: *Authority and shift lifetime*, *Creator-owned cleanup*,
*First wake* and *Shared API/control-plane outages* govern shared behavior;
appended forwarding governs owner interactions.

1. Run `phx shift status`; the latest `created_at` identifies the most recent Captain.
   Apply Captain's shared-outage policy and forwarding's lookup rules if unavailable; stop this
   pass, not the existing schedule. Failed lookup is not empty history.
   **Before any early exit**, inspect the previous Captain **you created** using native tools and
   apply *Creator-owned cleanup*. Retain your creator session's cleanup-only wake/child-ended
   notification until that cleanup finishes. A fresh keeper run is not this child's creator.
2. If any Captain has `"live": true`, say "A live Captain exists" and stop, including when it is
   `ending` (which prevents recovery but is not a forwarding destination).
   Also stop when the most recent Captain:
   - is `ended`, has no `successors`, and its `heartbeat_at` is less than ten minutes ago:
     its successor may still be starting; the next scheduled keeper run rechecks;
   - is `ended` with a `handoff_note` starting `Stop condition met:` and `charter_revision`
     matching `phx shift charter`'s `revision`: only a changed charter resumes recovery.
3. Before recovering a stale registration (including an overdue `ending` handoff), inspect its exact
   `session_ref` through the matching runtime's native tools and re-read its registration/successors.
   Apply the shared-outage recovery: available Captain refreshes its own registration; an
   `ending` Captain resumes handoff. Send recovery context; unacknowledged/blocked refresh never
   permits replacement. Ambiguous/unavailable native evidence means report unknown and retry
   next scheduled run, not infer loss from activity/title.
   Start recovery only after fresh native evidence confirms the exact Captain is stopped, missing,
   deleted or genuinely locally degraded independently of the shared outage.
   Apply this check to all non-ended stale registrations that could still be coordinating, not just
   the most recent. Ended registrations retain the grace/charter-stop checks above; an empty history
   may bootstrap only after a successful authoritative read.
4. Before any recovery launch, including after an ended Captain, reconcile pending successors by
   full launch identity with fresh complete native inventory. Reuse an existing successor; if
   inspection or a previous create is uncertain, report blocked/unknown and retry on the existing
   schedule, never duplicate it.
   Become Captain if this session can receive canonical wakes; follow *First wake*.
   Otherwise create `phx captain keeper` in the configured workspace with that
   schedule and kickoff: "Here `phx shift` means `<the configured command>`. Run
   `phx shift prompt captain` and follow *First wake*." Include `--parent <most recent Captain ID>`
   when one exists, so it inherits handoff/intents. Concurrent startup is server-coordinated.
   A creator cleanup wake never launches another Captain.

Incoming Captains read current workflow/charter, not copied policy summaries.
