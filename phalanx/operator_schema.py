"""Shared execution predicates for bounded operator reads."""

CLOSED = "status IN ('completed', 'failed', 'cancelled')"
OPEN = f"NOT ({CLOSED})"
INPUT_RANK = "CASE status WHEN 'waiting_for_input' THEN 0 ELSE 1 END"
