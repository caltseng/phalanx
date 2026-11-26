"""Pinned datetime coercion/error characterization; HTTP families also test it on loopback."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import subprocess
import sys

import harness as h


def requests():
    values = [None, True, False, {}, [], "", "0", "+0", "-1.25", ".5", "-.5",
              "1.", " 0", "0 ", "1e3", "1_000", "NaN", "Infinity",
              "2020-01-01", "2020-01-01T12:34:56", "2020-01-01t12:34:56z",
              "2020-01-01 12:34:56+0130", "2020-01-01T12:34Z",
              "2020-01-01T12:34:56.123456789Z", "2020-01-01T12:34:56+01:23:45",
              "2020-01-01T12:34:56+24:00", "2020-13-01T00:00:00Z",
              "2020-02-30T00:00:00Z", "2020-01-01T25:00:00Z",
              "2020-01-01T00:61:00Z", "2020-01-01T00:00:60Z",
              "2020-01-01X00:00:00Z", "2020-01-01T00:00:00Zx",
              "2020-01-01T12:34:56,1Z", "2020-01-01T12:34:56.Z",
              "2020-01-01T12:34:56+01", "2020-01-01T12:34:56+01:60",
              "0000-01-01T00:00:00Z", "9999-12-31T23:59:59.999999Z",
              "2020-1-01T00:00:00Z", "2020/01/01", "abcdefghijkl",
              "2020-01", "2020-00-01", "2020-01-00", "2020-02-29"]
    for whole in (-62167219200001, -62167219200000, -62135596800001,
                  -62135596800000, -20000000001, -20000000000, -1, 0, 1,
                  20000000000, 20000000001, 253402300799999, 253402300800000):
        values += [whole, str(whole), float(whole)]
    for number in (0.0000005, 0.0000015, 0.9999999, 1.25, 20000000000.1):
        values += [number, -number, str(number), str(-number)]
    for year in (1, 1900, 2000, 2024, 9999):
        for month in (1, 2, 12):
            for day in (1, 28, 29, 30, 31):
                values.append(f"{year:04}-{month:02}-{day:02}T00:00:00Z")
    for separator in ("T", "t", " ", "_", "\t"):
        for zone in ("Z", "z", "+00:00", "-00:00", "+23:59", "-23:59", "+0130", ""):
            for fraction in ("", ".1", ",01", ".123456", ".1234567"):
                values.append("2020-01-01" + separator + "12:34:56" + fraction + zone)
    for offset in range(-23, 24):
        zone = timezone(timedelta(hours=offset, minutes=30))
        values.append(datetime(2020, 1, 2, 3, 4, 5, 123456, tzinfo=zone).isoformat())
    for position in range(10):
        values.append("2020-01-01"[:position] + "x" + "2020-01-01"[position + 1:] + "T00:00:00Z")
    randomizer = random.Random(2601)
    for _ in range(500):
        number = randomizer.randint(-62167219200000, 253402300800000) + randomizer.random()
        values += [number, str(number)]
    for value in (1e20, -1e20, 1e100, -1e100, 1e-100, -1e-100):
        values += [value, str(value)]
    return values


def expected(values):
    script = """
import json, sys
from pydantic import AwareDatetime, TypeAdapter, ValidationError
values = json.load(sys.stdin)
adapter = TypeAdapter(AwareDatetime)
result = []
for value in values:
    try:
        response = {"value": adapter.dump_python(adapter.validate_python(value), mode="json")}
    except ValidationError as error:
        issues = error.errors(include_url=False)
        for issue in issues:
            issue["loc"] = ["body", "observed_at", *issue["loc"]]
        response = {"errors": issues}
    result.append(response)
print(json.dumps(result))
"""
    process = subprocess.run([sys.executable, "-P", "-c", script], input=json.dumps(values),
                             text=True, capture_output=True, check=True, timeout=60, cwd=h.ROOT)
    return json.loads(process.stdout)


def differential(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    values = requests()
    oracle = expected(values)
    process = subprocess.run([str(binary)], input="".join(json.dumps(value) + "\n" for value in values),
                             text=True, capture_output=True, check=True, timeout=60, cwd=h.ROOT)
    actual = [json.loads(line) for line in process.stdout.splitlines()]
    output.mkdir(parents=True, exist_ok=True)
    (output / "datetime-proof.json").write_text(json.dumps(
        {"runtime": h.RUNTIME, "requests": values, "pydantic": oracle, "Go": actual}, indent=2),
        encoding="utf-8")
    mismatches = [(value, first, second) for value, first, second in zip(values, oracle, actual, strict=True)
                  if first != second]
    if mismatches:
        raise AssertionError(f"{len(mismatches)} datetime mismatches\n" + "\n".join(
            f"{value!r}\n{first!r}\n{second!r}" for value, first, second in mismatches[:20]))
    print(f"{len(values)} exact pinned AwareDatetime coercion/error vectors PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
