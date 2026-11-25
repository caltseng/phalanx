import asyncio
from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import dependencies, finalization
from .auth import require_owner, require_runner
from .config import Credentials, database_path, local_board
from .captains import router as captains_router
from .dispatch_api import router as dispatch_router
from .flags import router as flags_router
from .intake_api import router as intake_router
from .muster_api import configured_home, router as muster_router
from .operator_api import router as operator_router
from .operator_web import assets as operator_assets, router as operator_web_router
from .store import connect, initialize, write_transaction
from .writers import router as writers_router
from .usage import router as usage_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.credentials = Credentials.from_env()
    app.state.local_board = local_board()
    app.state.database_path = database_path()
    app.state.muster_home = configured_home()
    initialize(app.state.database_path)
    with write_transaction(app.state.database_path) as db:
        dependencies.start_reconciliation(db)
    tasks = (
        asyncio.create_task(finalization.run(app.state.database_path)),
        asyncio.create_task(dependencies.run_refresh(app.state.database_path)),
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Phalanx", lifespan=lifespan)
app.include_router(operator_web_router)
app.mount("/operator/assets", operator_assets, name="operator-assets")


@app.exception_handler(sqlite3.OperationalError)
async def store_unavailable(request: Request, error: sqlite3.OperationalError):
    # Only contention is transient; other operational errors stay 500 so clients don't retry forever.
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        busy = code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    else:
        message = str(error).lower()
        busy = "locked" in message or "busy" in message
    if not busy:
        raise error
    return JSONResponse(
        {"detail": f"SQLite store unavailable ({error}); retry the same request"},
        status_code=503,
        headers={"Retry-After": "1"},
    )


@app.get("/health")
def health(request: Request):
    try:
        with connect(request.app.state.database_path, mode="ro") as db:
            db.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        raise HTTPException(503, "SQLite store unavailable") from None
    return {"status": "ok"}


@app.get("/v1/owner/status", dependencies=[Depends(require_owner)])
def owner_status():
    return {"role": "owner"}


@app.get("/v1/runner/status", dependencies=[Depends(require_runner)])
def runner_status():
    return {"role": "runner"}

app.include_router(intake_router)
app.include_router(dispatch_router)
app.include_router(operator_router)
app.include_router(captains_router)
app.include_router(flags_router)
app.include_router(writers_router)
app.include_router(usage_router)
app.include_router(muster_router)
