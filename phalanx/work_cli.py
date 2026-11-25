"""`phx work`: a registered worker session's commands, authenticated by its writer token.

The Captain writes {"token", "base_url"} to $(git rev-parse --git-path phalanx-writer.json) in the
item's worktree. The file is a credential under the git directory, never committed or pasted.
"""

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote

from .api_client import request as api_request
from .git import git_text

CREDENTIAL = "phalanx-writer.json"


def credential(cwd=None):
    try:
        path = git_text(cwd, "rev-parse", "--git-path", CREDENTIAL, error=ValueError).strip()
    except ValueError as error:
        raise ValueError("phx work must run inside the item's git worktree") from error
    path = Path(cwd or ".") / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"No writer credential at {path}; the Captain writes it at registration") from error
    if not isinstance(data, dict) or not data.get("token") or not data.get("base_url"):
        raise ValueError(f"{path} must hold a token and base_url")
    return data["base_url"], data["token"]


def request(base_url, token, route, *, body=None, method=None, key=None, timeout=30):
    try:
        return api_request(base_url, token, route, body=body, method=method, key=key, timeout=timeout)
    except HTTPError as error:
        with error:
            detail = error.read().decode("utf-8", errors="replace")
        raise ValueError(f"HTTP {error.code}: {detail}") from error


def post_result(base_url, token, attempt, payload, key=None):
    """Post the pinned attempt's result or a version-pinned attempt-free delivery question.

    The attempt is pinned by the caller, so a resend after a lost reply can never land on the
    next stage: once the attempt has ended, the post is refused instead. Attempt-free questions
    require a fresh delivery external wait; an already-open question is not renewed.
    """
    binding = request(base_url, token, "/v1/writer")
    if attempt is None and not (
        binding["state"]["stage"] == "delivery"
        and binding["state"]["status"] == "waiting_for_external"
        and payload.get("outcome") == "needs_input"
    ):
        raise ValueError("An attempt ID is required except for a delivery question during an external wait")
    if binding["writer"]["attempt_id"] != attempt:
        raise ValueError(f"Attempt {attempt} is not open; its result may already be accepted. "
                         "Check `phx work status` before posting again")
    body = {"attempt_id": attempt, "stage": binding["state"]["stage"],
            "expected_version": binding["version"], **payload}
    route = (f"/v1/items/{quote(binding['item_id'], safe='')}"
             f"/executions/{quote(binding['id'], safe='')}/result")
    default_key = f"{attempt}-result" if attempt else f"{binding['id']}-question-{binding['version']}"
    return request(base_url, token, route, body=body, key=key or default_key)


def add_work_command(commands):
    work = commands.add_parser("work", help="a registered worker session's commands (writer token)")
    actions = work.add_subparsers(dest="work_command", required=True)
    actions.add_parser("status", help="show this writer, its execution and open attempt")
    actions.add_parser("next", help="open the next stage's attempt, or show the open one")
    capacity = actions.add_parser("capacity", help="acquire Building before rework, or return to delivery")
    capacity.add_argument("phase", choices=("building", "delivering"))
    progress = actions.add_parser("progress", help="record progress, with an optional note")
    progress.add_argument("--note")
    usage = actions.add_parser("usage", help="submit optional native cumulative usage; never advances work")
    usage.add_argument("--file", type=Path, required=True, help="allowlisted usage snapshot JSON")
    result = actions.add_parser("result", help="post the open attempt's stage result")
    result.add_argument("--attempt", required=True, help="the attempt ID from `phx work next` or status")
    result.add_argument("--file", type=Path, required=True,
                        help="JSON result body; stage and expected_version are filled in")
    result.add_argument("--key", help="idempotency key; defaults to one per attempt")
    question = actions.add_parser("question", help="ask the owner or Captain a question (needs_input)")
    question.add_argument("prompt")
    question.add_argument("--attempt", help="the current attempt ID; omit only during a delivery external wait")
    question.add_argument("--handoff", required=True, help="context a fresh session needs to resume")
    question.add_argument("--option", action="append", default=[], dest="options")
    question.add_argument("--default", dest="proposed_default")
    question.add_argument("--key", help="idempotency key; defaults to one per attempt or delivery version")


def run_work(args):
    base_url, token = credential()
    if args.work_command == "status":
        result = request(base_url, token, "/v1/writer")
    elif args.work_command == "next":
        result = request(base_url, token, "/v1/writer/attempt", method="POST")
    elif args.work_command == "capacity":
        current = request(base_url, token, "/v1/writer")
        body = {"phase": args.phase, "expected_revision": current["writer"]["capacity_revision"]}
        if args.phase == "delivering" and current["writer"]["building"]:
            try:
                body["handoff_head"] = git_text(None, "rev-parse", "HEAD", error=ValueError).strip()
            except ValueError as error:
                raise ValueError("Cannot read the reviewed commit for the publication handoff") from error
        result = request(base_url, token, "/v1/writer/capacity", body=body)
    elif args.work_command == "progress":
        result = request(base_url, token, "/v1/writer/progress", body={"note": args.note})
    elif args.work_command == "usage":
        from .usage_models import Snapshot
        payload = Snapshot.model_validate_json(args.file.read_text(encoding="utf-8"))
        result = request(base_url, token, "/v1/writer/usage", body=payload.model_dump(mode="json"))
    elif args.work_command == "result":
        payload = json.loads(args.file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("The result file must hold a JSON object")
        result = post_result(base_url, token, args.attempt, payload, args.key)
    else:
        question = {"prompt": args.prompt, "options": args.options, "handoff": args.handoff}
        if args.proposed_default:
            question["proposed_default"] = args.proposed_default
        result = post_result(base_url, token, args.attempt,
                             {"outcome": "needs_input", "question": question}, args.key)
    print(json.dumps(result, indent=2))
