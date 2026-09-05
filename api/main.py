"""FastAPI app for the redial console. See docs/API_CONTRACT.md.

Boots with DRY_RUN=1. While that is set nothing in this process can POST to
Formi: every dialling helper checks `db.dry_run()` as its first statement and
imports `requests` only after that check passes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import os

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .db import (DEFAULT_CONFIG, db_path, dry_run, init_db, leads_source,
                 load_env, session)
from .routes_core import router as core_router
from .routes_stage import router as stage_router

# Before anything reads os.environ. Real environment variables still win, so a
# test that exports LEADS_SOURCE=seed is not overridden by the .env on disk.
load_env()

# After load_env(): the module reads AUTOPILOT_AM/PM at call time, but keeping
# the import here documents the ordering the rest of this file depends on.
from .autopilot import loop as autopilot_loop, router as autopilot_router  # noqa: E402


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    init_db().close()
    # The autopilot's clock. Inert unless a campaign has been switched on for it,
    # and under DRY_RUN=1 its dials are simulated like every other path here.
    task = asyncio.create_task(autopilot_loop())
    try:
        yield
    finally:
        task.cancel()


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
        # What is in the table, not what .env last claimed. LEADS_SOURCE is a
        # hand-set string nobody edits after a sync, so the banner an operator
        # checks before a live dial read "seed" over 22 real campaigns and 1848
        # real leads. Seed ids are 1-16, warehouse ids 1400+ (`purge_campaigns`),
        # so the data answers this without being asked.
        row = conn.execute("SELECT COUNT(*) AS n, MAX(id) AS top FROM campaigns").fetchone()
    source = leads_source() if not row["n"] else \
        ("warehouse" if row["top"] >= 1000 else "seed")
    return {"ok": True, "dry_run": dry_run(), "db": Path(db_path()).name,
            "leads_source": source, "agents": agents,
            "test_numbers": list(DEFAULT_CONFIG["test_numbers"])}


class DryRunBody(BaseModel):
    enabled: bool
    # Only checked when switching dialling ON. Going back to a dry run is the safe
    # direction and takes one click.
    confirm: str = ""


@app.post("/api/config/dry-run")
def set_dry_run(body: DryRunBody = Body(...)) -> dict[str, object]:
    """Turn live dialling on or off without a restart.

    `db.dry_run()` reads the environment at call time, and every dialling helper
    checks it as its first statement, so writing os.environ here reaches all
    fifteen call sites immediately -- no restart, no second source of truth.

    Deliberately NOT written to .env. A restart returns to whatever the file says,
    which means the blast radius of leaving this switched on is one process life
    rather than forever. The UI says so; do not "fix" it by persisting without
    deciding that is what you want.

    Turning dialling ON costs the typed word GO LIVE, matching the approve dialog
    which costs DIAL. Turning it off is free -- a switch that is hard to flip back
    to safe is a switch nobody flips in a hurry.
    """
    going_live = not body.enabled
    if going_live and body.confirm.strip().upper() != "GO LIVE":
        raise HTTPException(400, "Type GO LIVE to enable live dialling")
    os.environ["DRY_RUN"] = "0" if going_live else "1"
    # Printed, not just returned: this is the one control that decides whether real
    # customers get called, and the journal is where that question gets answered later.
    print(f"DRY_RUN set to {os.environ['DRY_RUN']} "
          f"({'LIVE DIALLING' if going_live else 'dry run'}) via /api/config/dry-run",
          flush=True)
    return {"dry_run": dry_run()}


app.include_router(core_router)
app.include_router(stage_router)
app.include_router(autopilot_router)
