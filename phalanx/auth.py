from hashlib import sha256
from hmac import compare_digest
import ipaddress
import re
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .store import read_transaction


Role = Literal["owner", "runner", "writer"]
bearer = HTTPBearer(auto_error=False)
LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?", re.IGNORECASE)
OPEN_WRITER = """
    SELECT writer.* FROM writers AS writer JOIN executions AS execution ON execution.id = writer.execution_id
    WHERE writer.token_digest = ? AND writer.ended_at IS NULL
      AND execution.status NOT IN ('completed', 'failed', 'cancelled')
"""


def authenticate(
    request: Request,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Role:
    if authorization:
        token = authorization.credentials.encode("utf-8")
        credentials = request.app.state.credentials
        if compare_digest(token, credentials.owner.encode("ascii")):
            return "owner"
        if compare_digest(token, credentials.runner.encode("ascii")):
            return "runner"
        with read_transaction(request.app.state.database_path) as db:
            writer = db.execute(OPEN_WRITER, (sha256(token).hexdigest(),)).fetchone()
        if writer is not None:
            scope_writer(request, writer)
            request.state.writer = dict(writer)
            return "writer"
    raise HTTPException(401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"})


def scope_writer(request, writer):
    """A writer token reaches only its own item's routes and the /v1/writer routes."""
    params = request.path_params
    if request.url.path == "/v1/writer" or request.url.path.startswith("/v1/writer/"):
        return
    if params.get("item_id") == writer["item_id"] and params.get("execution_id") in (
            None, writer["execution_id"]):
        return
    raise HTTPException(403, "This writer token is scoped to another item")


def is_local(request: Request) -> bool:
    # The Host check blocks DNS rebinding; the client check blocks remote callers spoofing Host.
    try:
        loopback = ipaddress.ip_address(request.client.host).is_loopback
    except (AttributeError, ValueError):
        loopback = False
    return loopback and bool(LOCAL_HOST.fullmatch(request.headers.get("host", "")))


def authenticate_board_read(
    request: Request,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Role | Literal["local"]:
    """Read-only board routes skip the token when the server opted into a local board."""
    if authorization is None and request.app.state.local_board and is_local(request):
        return "local"
    return authenticate(request, authorization)


def require_owner(role: Annotated[Role, Depends(authenticate)]) -> Role:
    if role != "owner":
        raise HTTPException(403, "Owner token required")
    return role


def require_runner(role: Annotated[Role, Depends(authenticate)]) -> Role:
    if role != "runner":
        raise HTTPException(403, "Runner token required")
    return role


def require_coordinator(role: Annotated[Role, Depends(authenticate)]) -> Role:
    """Captains coordinate with the owner or runner token, never a writer token."""
    if role not in ("owner", "runner"):
        raise HTTPException(403, "Owner or runner token required")
    return role


def require_worker(role: Annotated[Role, Depends(authenticate)]) -> Role:
    if role not in ("runner", "writer"):
        raise HTTPException(403, "Runner or writer token required")
    return role


def require_writer(role: Annotated[Role, Depends(authenticate)]) -> Role:
    if role != "writer":
        raise HTTPException(403, "Writer token required")
    return role