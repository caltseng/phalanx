"""Run one deterministic shard of the test suite: python tests/shard.py INDEX TOTAL.

Tests are discovered as `python -m unittest discover -s tests` does, sorted by
ID and dealt round-robin, so every test runs in exactly one of the TOTAL shards.
"""

import argparse
from pathlib import Path
import sys
import unittest


def _flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _flatten(test)
        else:
            yield test


def select(tests, index: int, total: int) -> list:
    ordered = sorted(tests, key=lambda test: test.id())
    return ordered[index - 1::total]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=int, help="1-based shard number")
    parser.add_argument("total", type=int, help="number of shards")
    args = parser.parse_args(argv)
    if not 1 <= args.index <= args.total:
        parser.error("INDEX must be between 1 and TOTAL")
    # Import failures become failing tests here, so they still fail some shard.
    tests = list(_flatten(unittest.defaultTestLoader.discover(str(Path(__file__).parent))))
    selected = select(tests, args.index, args.total)
    print(f"Shard {args.index}/{args.total}: {len(selected)} of {len(tests)} tests", flush=True)
    if not selected:
        print("No tests selected; use fewer shards", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2, durations=15).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
