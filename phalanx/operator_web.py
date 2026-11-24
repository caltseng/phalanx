"""Same-origin operator shell and narrowly authorized dashboard actions."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Path as PathParameter, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import intake
from .auth import require_board_owner, require_same_origin
from .dispatch_api import DispatchSettings, read_settings, settings
from .intake_api import Key, create, run
from .intake_models import Intake

static = Path(__file__).with_name("static")
router = APIRouter()
assets = StaticFiles(directory=static)
BoardIdentifier = Annotated[str, PathParameter(pattern=r"^[0-9a-f]{32}$")]


@router.get("/operator/api/repositories", dependencies=[Depends(require_board_owner)])
def board_repositories(request: Request):
    return run(request, intake.repositories)


@router.post("/operator/api/items", status_code=201,
             dependencies=[Depends(require_board_owner), Depends(require_same_origin)])
def board_create(request: Request, body: Intake, key: Key = None):
    return create(request, body, key)


@router.get("/operator/api/dispatch/settings", dependencies=[Depends(require_board_owner)])
def board_settings(request: Request):
    return read_settings(request)


@router.put("/operator/api/dispatch/settings",
            dependencies=[Depends(require_board_owner), Depends(require_same_origin)])
def board_save_settings(request: Request, body: DispatchSettings):
    return settings(request, body)


@router.get("/operator/", include_in_schema=False)
@router.get("/operator/queue", include_in_schema=False)
@router.get("/operator/history", include_in_schema=False)
def operator_board():
    return FileResponse(
        static / "operator-board.html",
        media_type="text/html",
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; "
                "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/operator/items/{item_id}", include_in_schema=False)
def operator_item(item_id: BoardIdentifier):
    return operator_board()


@router.get("/operator/executions/{execution_id}", include_in_schema=False)
def operator_execution(execution_id: BoardIdentifier):
    return operator_board()
