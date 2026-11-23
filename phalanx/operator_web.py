"""Same-origin shell for the authenticated operator read client."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


static = Path(__file__).with_name("static")
router = APIRouter()
assets = StaticFiles(directory=static)


@router.get("/operator/", include_in_schema=False)
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
