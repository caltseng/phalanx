"""One generous deadline for tests that wait on threads, processes, loops, or local checks.

Loaded hosts and CI runners can stall work for minutes, and correct code must not fail
because of it. Waits return as soon as their condition holds, so this only bounds a hang.
"""

from time import time


WAIT_SECONDS = 300


class Clock:
    """Wall time plus a manual offset; patch it over a module's `time` to age state without sleeping."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time() + self.offset

    def advance(self, seconds):
        self.offset += seconds
