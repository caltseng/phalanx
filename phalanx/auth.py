from hmac import compare_digest
import ipaddress
import re
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


Role = Literal["owner", "runner"]
bearer = HTTPBearer(auto_error=False)
LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?", re.IGNORECASE)


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
    raise HTTPException(401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"})


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
