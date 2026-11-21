"""Executable JSON bridge fixture; never talks to a real runtime."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5


fixture_path = Path(sys.argv[1])
fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
request = json.load(sys.stdin)
assert request["contract_version"] == 1
operation, arguments = request["operation"], request["arguments"]
if operation == "resolve_project":
    matches = [project for project in fixture["projects"] if project == arguments]
    if len(matches) != 1:
        raise ValueError("No exact fixture project")
    response = matches[0]
elif operation == "launch":
    marker = arguments["marker"]
    thread = str(uuid5(NAMESPACE_URL, marker))
    receipt = {
        "host_id": arguments["host_id"], "marker": marker,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "thread_id": thread,
    }
    if fixture.get("queued"):
        receipt["client_thread_id"] = str(uuid5(NAMESPACE_URL, marker + "-queued"))
    with (fixture_path.parent / f"{marker}.launch.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream)
    response = {field: receipt[field] for field in ("host_id", "marker")}
    field = "client_thread_id" if fixture.get("queued") else "thread_id"
    response[field] = receipt[field]
elif operation == "observe":
    threads = []
    for reference in arguments["references"]:
        marker = reference["marker"]
        path = fixture_path.parent / f"{marker}.launch.json"
        if not path.exists():
            continue
        receipt = json.loads(path.read_text(encoding="utf-8"))
        state = fixture.get("state", "active")
        row = {
            **receipt, "project_id": reference["project_id"], "assignment": reference["assignment"],
            "state": state, "writers_stopped": state == "stopped",
        }
        threads.append(row)
    response = {
        "observed_at": datetime.now(timezone.utc).isoformat(), "threads": threads,
        "discovery_complete": True,
        "cursors": {reference["marker"]: "fixture-next" for reference in arguments["references"]},
    }
else:
    raise ValueError("Unsupported fixture operation")
print(json.dumps(response))
