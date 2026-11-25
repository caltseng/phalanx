"""Run one deterministic shard of the test suite: python tests/shard.py INDEX TOTAL.

Tests are discovered as `python -m unittest discover -s tests` does, then
assigned longest-first to the lightest shard using measured platform timings.
Every test runs in exactly one of the TOTAL shards, including unmeasured tests.
"""

import argparse
import json
from pathlib import Path
import sys
import unittest


def _flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _flatten(test)
        else:
            yield test


def select(tests, index: int, total: int, timings=None) -> list:
    if not 1 <= index <= total:
        raise ValueError("INDEX must be between 1 and TOTAL")
    timings = timings if timings is not None else {}

    def estimate(test):
        test_id = test.id()
        return timings.get(test_id, timings.get(test_id.split(".")[0], 1.0))

    shards = [[] for _ in range(total)]
    loads = [0.0] * total
    for test in sorted(tests, key=lambda test: (-estimate(test), test.id())):
        lightest = min(range(total), key=lambda shard: (loads[shard], len(shards[shard]), shard))
        shards[lightest].append(test)
        loads[lightest] += estimate(test)
    return sorted(shards[index - 1], key=lambda test: test.id())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=int, help="1-based shard number")
    parser.add_argument("total", type=int, help="number of shards")
    args = parser.parse_args(argv)
    if not 1 <= args.index <= args.total:
        parser.error("INDEX must be between 1 and TOTAL")
    # Import failures become failing tests here, so they still fail some shard.
    tests = list(_flatten(unittest.defaultTestLoader.discover(str(Path(__file__).parent))))
    timings = json.loads(Path(__file__).with_name("shard_timings.json").read_text())["profiles"][sys.platform]
    selected = select(tests, args.index, args.total, timings)
    print(f"Shard {args.index}/{args.total}: {len(selected)} of {len(tests)} tests", flush=True)
    if not selected:
        print("No tests selected; use fewer shards", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2, durations=0).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
