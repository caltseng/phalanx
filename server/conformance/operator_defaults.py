"""Retain raw evidence for omitted-mode routes outside the sole v4 classification."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


def characterize(binary, output):
    h.verify_manifest()
    output.mkdir(parents=True, exist_ok=True)
    summary = {}
    routes = {
        "operator-current-item/absent-publication-mode": "/v1/operator/items/" + h.SEED,
        "operator-list/absent-publication-mode": "/v1/operator/executions",
        "operator-group/absent-publication-mode": "/v1/operator/groups/synthetic-group",
        "operator-timeline/absent-publication-mode": "/v1/operator/executions/" + h.SEED_EXECUTION + "/timeline",
    }
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            summary[shape] = {}
            for label in ("P0", "D0", "Go"):
                directory = root / f"mode-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                with closing(sqlite3.connect(database)) as db:
                    db.execute("UPDATE executions SET state_json=json_remove(state_json,'$.publication_mode') WHERE id=?", (h.SEED_EXECUTION,))
                    db.execute("UPDATE work_items SET request_json=json_set(request_json,'$.label','synthetic-group') WHERE id=?", (h.SEED,))
                    db.commit()
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database), "--operator-clock"]
                observations = []
                with h.server(command, directory) as url:
                    for name, route in routes.items():
                        before = h.snapshot(database)
                        response = h.wire(url, "GET", route, [("Authorization", "Bearer " + h.OWNER)], "")
                        after = h.snapshot(database)
                        if before != after:
                            raise AssertionError("Default-mode characterization changed durable state: " + name)
                        observations.append({"case": name, "response": response, "durable": after})
                        summary[shape].setdefault(name, {})[label] = response["status"]
                (output / f"defaults-{shape}-{label}.json").write_text(json.dumps(observations, ensure_ascii=True), encoding="utf-8")
    result = {"pins": h.PINS, "results": summary,
              "classification": "Only current-item is approved by executionv4. Additional list/group/timeline differences require explicit classification; no general500-to200 normalization or durable rewrites.",
              "Go_behavior": "Conservative500 for missing mode outside the named current-item case until authority is resolved."}
    (output / "characterization.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    characterize(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
