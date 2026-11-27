"""Cross-platform OS exclusion only; not a substitute for the pinned wire oracle."""

import argparse
import json
from pathlib import Path
import platform
import sqlite3

import harness as h


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        h.lock_checks(args.binary.resolve(), root, sources)
    (args.output / "locks.json").write_text(json.dumps({
        "scope": "actual OS lock exclusion in both directions and release, P0/D0/Go",
        "os": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "pins": h.PINS, "wire_oracle_runtime_equivalence": False, "passed": True,
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
