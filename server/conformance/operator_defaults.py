"""Exact named v4/v9 read defaults; explicit modes and malformed states stay distinct."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


def cases():
    routes = {
        "operator-current-item": "/v1/operator/items/" + h.SEED,
        "operator-list": "/v1/operator/executions",
        "operator-group": "/v1/operator/groups/synthetic-group",
        "operator-timeline": "/v1/operator/executions/" + h.SEED_EXECUTION + "/timeline",
    }
    return [{"id": name + "/" + mode, "path": path, "mode": mode}
            for name, path in routes.items() for mode in ("omitted", "ready", "draft", "null", "missing-target", "invalid-wait")]


def characterize(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    summary = {}
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            summary[shape] = {}
            for label in ("P0", "D0", "Go"):
                directory = root / f"mode-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                with closing(sqlite3.connect(database)) as db:
                    base, = db.execute("SELECT state_json FROM executions WHERE id=?", (h.SEED_EXECUTION,)).fetchone()
                    base = json.loads(base)
                    db.execute("UPDATE work_items SET request_json=json_set(request_json,'$.label','synthetic-group') WHERE id=?", (h.SEED,))
                    db.commit()
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database), "--operator-clock"]
                observations = []
                with h.server(command, directory) as url:
                    for case in cases():
                        state = dict(base)
                        mode = case["mode"]
                        if mode == "omitted":
                            state.pop("publication_mode")
                        elif mode == "missing-target":
                            state.pop("target")
                        elif mode == "invalid-wait":
                            state["wait"] = "invalid"
                        else:
                            state["publication_mode"] = None if mode == "null" else mode
                        with closing(sqlite3.connect(database)) as db:
                            db.execute("UPDATE executions SET state_json=? WHERE id=?", (json.dumps(state), h.SEED_EXECUTION))
                            db.execute("UPDATE operator_change SET token='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'")
                            db.commit()
                        before = h.snapshot(database)
                        response = h.wire(url, "GET", case["path"], [("Authorization", "Bearer " + h.OWNER)], "")
                        after = h.snapshot(database)
                        if before != after:
                            raise AssertionError("Default-mode conformance changed durable state: " + case["id"])
                        expected = 500 if mode in ("missing-target", "invalid-wait") or mode == "omitted" and label == "P0" else 200
                        if response["status"] != expected:
                            raise AssertionError(f"{shape}/{label}/{case['id']}: expected{expected}, got{response['status']}")
                        observations.append({"case": case["id"], "response": response, "durable": after})
                        summary[shape].setdefault(case["id"], {})[label] = response["status"]
                        if response["status"] == 200:
                            item = response["json"]["items"][0] if case["path"].endswith("executions") or "/groups/" in case["path"] else response["json"]["item"]
                            expected_mode = None if mode == "null" else "ready" if mode == "omitted" else mode
                            if item["publication_mode"] != expected_mode or item["target"] != "report":
                                raise AssertionError("Explicit/null/default publication mode or report target changed")
                (output / f"defaults-{shape}-{label}.json").write_text(json.dumps(observations, ensure_ascii=True), encoding="utf-8")
                if label == "P0":
                    p0 = observations
                elif label == "D0":
                    d0 = observations
                    for first, second in zip(p0, d0, strict=True):
                        if not first["case"].endswith("/omitted") and h.comparable(first["response"]) != h.comparable(second["response"]):
                            raise AssertionError("Unclassified P0/D0 wire difference: " + first["case"])
                else:
                    for first, second in zip(d0, observations, strict=True):
                        if h.comparable(first["response"]) != h.comparable(second["response"]) or first["durable"] != second["durable"]:
                            raise AssertionError("Go did not match complete D0 default/mode/error response: " + first["case"])
    result = {"pins": h.PINS, "results": summary,
              "classification": "Executionv4 current-item and separately acceptedv9 list/group/timeline omitted-field read cases preserve D0/spec ready. Explicit modes/null and malformed refusal remain unchanged; no global500 normalization or durable rewrite.",
              "Go_behavior": "Matches complete D0 wire/durable results on all24 named/default/explicit/null/malformed cases per schema shape."}
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
