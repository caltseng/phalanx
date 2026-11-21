from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request

from .auth import require_owner, require_runner
from .config import Credentials, database_path
from .store import connect, migrate


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.credentials = Credentials.from_env()
    app.state.database_path = database_path()
    migrate(app.state.database_path)
    yield


app = FastAPI(title="Phalanx", lifespan=lifespan)


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
