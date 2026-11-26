"""JSON decoding and atomic writes for host configuration and credentials."""

import json
import os


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
