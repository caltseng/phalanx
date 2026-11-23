"""One generous deadline for tests that wait on threads, processes, loops, or local checks.

Loaded hosts and CI runners can stall work for minutes, and correct code must not fail
because of it. Waits return as soon as their condition holds, so this only bounds a hang.
"""

WAIT_SECONDS = 300
