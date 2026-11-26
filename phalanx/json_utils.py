"""Shared JSON output, decoding and atomic writes."""

import json
import os


def print_json(data, *, indent=2, flush=False):
    print(json.dumps(data, indent=indent), flush=flush)


def write_json(path, data, *, indent=None, newline=False):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=indent) + ("\n" if newline else ""), encoding="utf-8")
    os.replace(temporary, path)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result
