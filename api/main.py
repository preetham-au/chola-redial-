"""FastAPI app for the redial console. See docs/API_CONTRACT.md.

Boots with DRY_RUN=1. While that is set nothing in this process can POST to
Formi: every dialling helper checks `db.dry_run()` as its first statement and
imports `requests` only after that check passes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .db import (DEFAULT_CONFIG, db_path, dry_run, init_db, leads_source,
                 load_env, session)
from .routes_core import router as core_router
from .routes_stage import router as stage_router

# Before anything reads os.environ. Real environment variables still win, so a
# test that exports LEADS_SOURCE=seed is not overridden by the .env on disk.
load_env()


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    init_db().close()
    yield


app = FastAPI(title="chola-redial", version="1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# The contract specifies a single error shape for every failure.
@app.exception_handler(HTTPException)
async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", ())[1:])
    return JSONResponse(status_code=422,
                        content={"error": f"{where or 'body'}: {first.get('msg', 'invalid request')}"})


@app.get("/api/health")
def health() -> dict[str, object]:
    with session() as conn:
        agents = [r["agent_id"] for r in conn.execute(
            "SELECT DISTINCT agent_id FROM campaigns ORDER BY agent_id")]
    return {"ok": True, "dry_run": dry_run(), "db": Path(db_path()).name,
            "leads_source": leads_source(), "agents": agents,
            "test_numbers": list(DEFAULT_CONFIG["test_numbers"])}


app.include_router(core_router)
app.include_router(stage_router)
