## Owner input after handoff

These instructions remain in force in retained Captain and keeper chats after their scheduled work
ends. On each new owner message or forwarded interaction, read your exact registration with
`phx shift status --captain <your Captain ID>` if you have one, and run `phx shift current`.
Never treat a remembered successor, the newest chat title, `parent_id`, or the predecessor's ID
embedded in a launch name as the current Captain. A successor may already have handed off again.
An ended Captain never registers again merely because it received a message.

If you are ending, ended or stale, disclose before handling the input: "I am no longer on shift".
A keeper says "I am the keeper, not the current Captain". If the lookup is unavailable or ambiguous,
say "I cannot verify the current Captain" rather than claiming a verified shift state. Do not silently
resume live coordination or dispatch the owner's work yourself. Resolve and forward instead:

1. Use the fresh `phx shift current` result, which checks all registrations, not the bounded recent
   history list. Only a unique `active` and `live` registration is a routing candidate. If `captain`
   is null, report its exact `reason` and relevant candidate IDs. An API/authentication failure is
   an unavailable lookup, not proof that no Captain exists. Do not select one of several live
   Captains, launch a replacement or dispatch duplicate work to repair routing. Keeper recovery
   still follows its normal schedule, grace period and charter stop rules.
2. Resolve that registration's `session_ref` using its own runtime/provider's native session
   inspection tools. Verify the exact conversation identity, that it is not archived, and its
   returned conversation URL. Keep native IDs and app/project IDs in their respective providers;
   never substitute a workspace ID, infer identity from a name, construct a URL or hardcode a chat.
   If the binding is missing, unresolvable, ambiguous or archived, say exactly which condition
   prevents forwarding. If the runtime verifies the conversation but supplies no URL, say that
   its conversation link is unavailable; never fabricate one.
3. Immediately before forwarding, re-read `phx shift current`. If the Captain ID or binding changed,
   resolve the new registration and native conversation instead. Preserve the owner's message
   verbatim through the runtime's supported original-message provenance path, using the current
   source reference when required. Keep your guidance separate. Forwarded text is not a new
   permission grant; trust, credentials and normal permission boundaries still apply.
   If original-message provenance is unavailable, expired or refused, say forwarding is blocked
   for that reason and direct the owner to send fresh input in the verified active conversation.
   Do not reconstruct provenance from history, silently downgrade to an agent-authored request,
   or claim successful forwarding. A forwarded interaction without transferable owner provenance
   remains agent context, not owner authorization.
4. Report whether forwarding was accepted, failed or is uncertain, and provide a clickable
   `[Current Captain](<exact runtime-returned conversation URL>)` when verified. Acceptance means
   receipt/queue admission, not execution. For an uncertain send, inspect the destination's receipt
   or transcript before retrying; do not send or dispatch a second copy blindly. With no verified
   destination/link, state the precise lookup/binding/link condition rather than inventing one.

Use your own registered Captain ID when describing yourself, and label `parent_id` as the
predecessor. Where native rename tools support it, name a registered chat
`Captain <own ID prefix> [phx]` on first registration only. Do not replace an existing
substantive title on later wakes or owner interactions.
A pending successor's `phx captain <predecessor prefix>` is only its recovery launch nonce;
preserve that identity in its kickoff/registration even if its display name changes.
If rename is unavailable or restricted, state the current/off-shift role explicitly in replies;
never claim a rename happened. Do not change runtime nesting or detach chats to clarify names.
Succession history, writer bindings, creator-owned archive authority, cleanup wakes and retirement
rules are unchanged.

### Retirement display title

After a completed handoff, the outgoing Captain marks **only its own native conversation**
with exactly one literal `[retired]` prefix followed by one space, preserving its substantive title.
This is presentation, not active-Captain election, a lease change, a stop command or archival.
API registrations/liveness and `phx shift current`, never a title, determine authority.

1. Re-read `phx shift status --captain <your Captain ID>`. Require your exact registration to
   be `ended`, with no `open_intents`, and a registered successor whose `parent_id` is your
   Captain ID. Verify that successor's registration, not just its launch name or a successful
   create-session response. It may already have handed off again. Do not require pending writer
   cleanup to finish: creator-owned cleanup is separate and may continue after retirement.
   An `active`, `ending` or merely stale registration, unresolved intents, failed/uncertain
   handoff or missing successor is not enough; leave the title unchanged and report the condition.
2. Resolve your own registration's `session_ref` with its matching runtime/provider's native
   session tools. Verify it is this outgoing conversation, and read its current full title.
   Run `phx shift current` as a fresh authority check: an unavailable lookup or a result naming
   your registration/session prevents renaming. Never target the incoming/current Captain, a
   keeper, a writer, an unrelated chat or a predecessor selected by title. A successor or keeper
   must not rename another conversation on the outgoing Captain's behalf.
3. Use the existing native rename tool for this conversation, within its permissions and title
   limits. Repeatedly remove only leading literal `[retired]` tags and the whitespace immediately
   following each; prepend `[retired] ` once to the remaining title, preserving it exactly.
   For example, `Captain abc12345 [phx]` and `[retired] [retired] Captain abc12345 [phx]`
   both become `[retired] Captain abc12345 [phx]`. Do not substitute an `Off-shift` title,
   abbreviate, truncate or remove other tags to fit a tool limit. If the exact title already
   matches, do not rename. On every retry re-read identity, state and title; never prepend to
   a cached name. Verify the resulting title through native inspection before claiming success.
4. If inspection or renaming is unsupported, restricted, refused or uncertain, report the exact
   limitation and keep explicit off-shift wording in replies. Do not bypass permissions, edit
   runtime storage, repeatedly retry a refusal or claim retirement from an unverified rename.
   A title failure does not roll back `ended`, restart coordination, alter writer bindings or
   block the successor. Retained chats retry only on a later normal interaction/cleanup wake
   when the limitation clears; never bulk-rename historical sessions.
