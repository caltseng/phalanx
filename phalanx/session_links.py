"""Recorded native session identities; never launch or probe an application."""

import re
from urllib.parse import urlsplit


NATIVE_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
LINK = re.compile(rf"(?:ghapp://sessions/|codex://threads/)({NATIVE_ID})")
PREFIX = {"copilot-app": "ghapp://sessions/", "codex": "codex://threads/"}


def session_id(reference):
    match = LINK.fullmatch(reference or "")
    return match[1] if match else reference


def session_reference(reference, runtime=None):
    """Keep opaque references intact instead of guessing their host."""
    prefix = PREFIX.get(runtime)
    if prefix and re.fullmatch(NATIVE_ID, reference):
        return prefix + reference
    return reference


def session_key(reference, runtime=None):
    if runtime is None:
        runtime = next((name for name, prefix in PREFIX.items()
                        if (reference or "").startswith(prefix)), None)
    return runtime, session_id(reference)


def session_target(reference=None, *, runtime=None, identity=None, availability="unverified",
                   explicit_url=False):
    """Resolve a recorded link without replacing malformed or conflicting references."""
    if availability in ("missing", "deleted"):
        return {"url": None, "reason": f"session {availability}"}
    prefix = PREFIX.get(runtime)
    url = None
    if reference:
        native = LINK.fullmatch(reference)
        if native and (not prefix or reference.startswith(prefix)) and (
                not identity or identity.lower() == native[1].lower()):
            url = reference
        elif re.fullmatch(r"https://[^\s\\]+", reference):
            try:
                parsed = urlsplit(reference)
                if parsed.hostname and not parsed.username and not parsed.password:
                    parsed.port  # Reject malformed ports before publishing a browser link.
                    url = reference
            except ValueError:
                pass
        elif not explicit_url and prefix and re.fullmatch(NATIVE_ID, reference) and (
                not identity or identity.lower() == reference.lower()):
            url = prefix + reference
    elif prefix and re.fullmatch(NATIVE_ID, identity or ""):
        url = prefix + identity
    reason = ("session unavailable: host or identity unknown, or invalid link" if not url else
              "session last reported present" if availability == "present" else
              "recorded session; existence unverified")
    return {"url": url, "reason": reason}
