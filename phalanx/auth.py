from hmac import compare_digest
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


Role = Literal["owner", "runner"]
bearer = HTTPBearer(auto_error=False)


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


def require_owner(role: Annotated[Role, Depends(authenticate)]) -> Role:
    if role != "owner":
        raise HTTPException(403, "Owner token required")
    return role


def require_runner(role: Annotated[Role, Depends(authenticate)]) -> Role:
    if role != "runner":
        raise HTTPException(403, "Runner token required")
    return role
