"""Migration failure for the removed ``uvicorn phalanx.api:app`` command."""

raise RuntimeError(
    "Python HTTP server has been removed; use the verified packaged Go HTTP server. "
    "See docs/python-server-deprecation.md. Python CLI, reconciler and offline admin remain supported; "
    "use a retained known-good package for coordinated rollback, never an old data snapshot."
)
