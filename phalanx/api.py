import asyncio
from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import dependencies, dispatch, finalization
from .auth import require_owner, require_runner
from .config import Credentials, database_path, lease_seconds, local_board, store_identity
from .dispatch_api import router as dispatch_router
from .intake_api import router as intake_router
from .operator_api import router as operator_router
from .operator_web import assets as operator_assets, router as operator_web_router
from .store import connect, migrate, write_transaction


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.credentials = Credentials.from_env()
    app.state.local_board = local_board()
    app.state.database_path = database_path()
    dispatch.LEASE_SECONDS = lease_seconds()
    migrate(app.state.database_path)
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
def store_unavailable(request: Request, error: sqlite3.OperationalError):
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
def runner_status(request: Request):
    # `muster start` and `muster status` compare the store to tell their API from another process on the port.
    return {"role": "runner", "store": store_identity(request.app.state.database_path)}

app.include_router(intake_router)
app.include_router(dispatch_router)
app.include_router(operator_router)
