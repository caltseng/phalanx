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
`Captain <own ID prefix> [phx]`, and an off-shift chat `Off-shift <own ID prefix> [phx]`.
A pending successor's `phx captain <predecessor prefix>` is only its recovery launch nonce;
preserve that identity in its kickoff/registration even if its display name changes.
If rename is unavailable or restricted, state the current/off-shift role explicitly in replies;
never claim a rename happened. Do not change runtime nesting or detach chats to clarify names.
Succession history, writer bindings, creator-owned archive authority, cleanup wakes and retirement
rules are unchanged.
