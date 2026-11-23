"""`phx work`: a registered worker session's commands, authenticated by its writer token.

The Captain writes {"token", "base_url"} to $(git rev-parse --git-path phalanx-writer.json) in the
item's worktree. The file is a credential under the git directory, never committed or pasted.
"""

import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

CREDENTIAL = "phalanx-writer.json"


def credential(cwd=None):
    try:
        path = subprocess.run(["git", "rev-parse", "--git-path", CREDENTIAL], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("phx work must run inside the item's git worktree") from error
    path = Path(cwd or ".") / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"No writer credential at {path}; the Captain writes it at registration") from error
    if not isinstance(data, dict) or not data.get("token") or not data.get("base_url"):
        raise ValueError(f"{path} must hold a token and base_url")
    return data["base_url"], data["token"]


def request(base_url, token, route, *, body=None, method=None, key=None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    call = Request(base_url.rstrip("/") + route, data=data, headers=headers, method=method)
    try:
        with urlopen(call, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        with error:
            detail = error.read().decode("utf-8", errors="replace")
        raise ValueError(f"HTTP {error.code}: {detail}") from error


def post_result(base_url, token, payload, key=None):
    """Post a stage result for the writer's open attempt, filling the fence fields it omits."""
    binding = request(base_url, token, "/v1/writer")
    attempt = binding["writer"]["attempt_id"]
    if not attempt:
        raise ValueError("No open attempt; run `phx work next` to start the next stage")
    body = {"attempt_id": attempt, "stage": binding["state"]["stage"],
            "expected_version": binding["version"], **payload}
    route = (f"/v1/items/{quote(binding['item_id'], safe='')}"
             f"/executions/{quote(binding['id'], safe='')}/result")
    # Keyed by attempt so a retry of the same result replays; check `phx work status` after a lost reply.
    return request(base_url, token, route, body=body, key=key or f"{body['attempt_id']}-result")


def add_work_command(commands):
    work = commands.add_parser("work", help="a registered worker session's commands (writer token)")
    actions = work.add_subparsers(dest="work_command", required=True)
    actions.add_parser("status", help="show this writer, its execution and open attempt")
    actions.add_parser("next", help="open the next stage's attempt, or show the open one")
    progress = actions.add_parser("progress", help="record progress, with an optional note")
    progress.add_argument("--note")
    result = actions.add_parser("result", help="post the open attempt's stage result")
    result.add_argument("--file", type=Path, required=True,
                        help="JSON result body; attempt_id, stage and expected_version are filled in")
    result.add_argument("--key", help="idempotency key; defaults to one per attempt")
    question = actions.add_parser("question", help="ask the owner or Captain a question (needs_input)")
    question.add_argument("prompt")
    question.add_argument("--handoff", required=True, help="context a fresh session needs to resume")
    question.add_argument("--option", action="append", default=[], dest="options")
    question.add_argument("--default", dest="proposed_default")
    question.add_argument("--key", help="idempotency key; defaults to one per attempt")


def run_work(args):
    base_url, token = credential()
    if args.work_command == "status":
        result = request(base_url, token, "/v1/writer")
    elif args.work_command == "next":
        result = request(base_url, token, "/v1/writer/attempt", method="POST")
    elif args.work_command == "progress":
        result = request(base_url, token, "/v1/writer/progress", body={"note": args.note})
    elif args.work_command == "result":
        payload = json.loads(args.file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("The result file must hold a JSON object")
        result = post_result(base_url, token, payload, args.key)
    else:
        question = {"prompt": args.prompt, "options": args.options, "handoff": args.handoff}
        if args.proposed_default:
            question["proposed_default"] = args.proposed_default
        result = post_result(base_url, token, {"outcome": "needs_input", "question": question}, args.key)
    print(json.dumps(result, indent=2))
