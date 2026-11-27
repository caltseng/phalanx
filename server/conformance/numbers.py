"""Exact Python numeric canonical bytes/digests, including fractional receipt material."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import random
import struct
import subprocess
import sys


def cases():
    values = ["-0", "-0.0", "0e0", "1.0", "1e0", "1e-4", "1e-5", "1e15", "1e16",
              "1.0000000000000000001", "1.234567890123456789", "1e400", "-1e400",
              "5e-324", "2.2250738585072014e-308", "1.7976931348623157e308",
              "99999999999999999999999999999999999999999999999999"]
    for exponent in range(-324, 309):
        values.append("1e" + str(exponent))
        values.append("-1e" + str(exponent))
    random_source = random.Random(0x50484C58)
    for _ in range(1024):
        value, = struct.unpack(">d", random_source.getrandbits(64).to_bytes(8, "big"))
        if value == value and abs(value) != float("inf"):
            values.append(repr(value))
    for value in values:
        yield value
        yield '{"writer_id":"synthetic","body":{"units":[' + value + ',null,0.0]}}'


def oracle(source):
    sys.path.insert(0, str(source))
    from phalanx.intake import digest, encode
    for line in sys.stdin:
        try:
            text = encode(json.loads(line))
            result = {"canonical": text, "digest": digest(text)}
        except ValueError as error:
            result = {"error": str(error)}
        print(json.dumps(result))


def differential(binary, output):
    import harness as h
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    inputs = list(cases())
    data = "\n".join(inputs) + "\n"
    (output / "numeric-requests.jsonl").write_text(data, encoding="utf-8")
    (output / "numeric-manifest.json").write_text(json.dumps({
        "count": len(inputs), "request_sha256": sha256(data.encode()).hexdigest(),
        "pins": h.PINS, "runtime": h.RUNTIME}), encoding="utf-8")
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in ("P0", "D0")}
        expected = None
        for label in ("P0", "D0", "Go"):
            command = [str(binary)] if label == "Go" else [
                sys.executable, "-P", str(Path(__file__).resolve()), "--oracle", str(sources[label])]
            process = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     encoding="utf-8", timeout=120, check=True, cwd=h.ROOT)
            (output / f"numeric-{label}.jsonl").write_text(process.stdout, encoding="utf-8")
            actual = [json.loads(line) for line in process.stdout.splitlines()]
            if len(actual) != len(inputs):
                raise AssertionError(f"{label}: numeric results missing")
            if expected is None:
                expected = actual
            else:
                for raw, first, second in zip(inputs, expected, actual, strict=True):
                    if first != second:
                        raise AssertionError(f"{label}: numeric bytes/digest mismatch {raw}\n{first!r}\n{second!r}")
    print(f"{len(inputs)} exact numeric canonical bytes/digests x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.oracle:
        oracle(args.oracle)
    elif args.binary and args.output:
        differential(args.binary.resolve(), args.output.resolve())
    else:
        parser.error("--oracle or --binary and --output required")


if __name__ == "__main__":
    main()
