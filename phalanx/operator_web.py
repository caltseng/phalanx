"""Same-origin operator shell, with owner-authenticated intake."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Path as PathParameter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

static = Path(__file__).with_name("static")
router = APIRouter()
assets = StaticFiles(directory=static)
BoardIdentifier = Annotated[str, PathParameter(pattern=r"^[0-9a-f]{32}$")]


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
