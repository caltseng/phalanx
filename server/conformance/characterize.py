"""Record unresolved pinned Python wire differences; never normalize them away."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


def characterize(output):
    h.verify_manifest()
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for label in ("P0", "D0"):
            directory = root / ("characterize-" + label)
            database = h.fresh(directory, sources)
            with closing(sqlite3.connect(database)) as db:
                db.execute("UPDATE executions SET state_json=json_remove(state_json,'$.publication_mode') WHERE id=?",
                           (h.SEED_EXECUTION,))
                db.commit()
            command = [sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
            with h.server(command, directory) as url:
                before = h.snapshot(database)
                response = h.wire(url, "GET", "/v1/operator/items/" + h.SEED,
                                  [("Authorization", "Bearer " + h.OWNER)], "")
                after = h.snapshot(database)
                if before != after:
                    raise AssertionError("Characterization read changed disposable state")
                results[label] = {"response": response, "durable": after}
            (output / f"missing-publication-mode-{label}.json").write_text(
                json.dumps(results[label], ensure_ascii=True), encoding="utf-8")
    summary = {"case": "operator-current-item/absent-publication-mode", "pins": h.PINS,
               "P0_status": results["P0"]["response"]["status"], "D0_status": results["D0"]["response"]["status"],
               "equal_wire": h.comparable(results["P0"]["response"]) == h.comparable(results["D0"]["response"]),
               "source": {"P0": "operator_api.summary reads state['publication_mode']",
                          "D0": "operator_api.summary reads state.get('publication_mode','ready')"},
               "effect": "Both requests read separate disposable schema26 files; no write effects",
               "resolution": "Unresolved semantic oracle discrepancy; do not treat as JSON representation normalization"}
    (output / "characterization.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    characterize(args.output.resolve())


if __name__ == "__main__":
    main()
