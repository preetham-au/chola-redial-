"""Bulk stage updates. Preview always; commit only when DRY_RUN=0."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from engine.stage_ops import (
    DEFAULT_KEEP, apply_stage, bulk_update, preview_expired, preview_policies, read_policies,
)

from .db import dry_run, leads_source, now_iso, session

router = APIRouter()


class ExpiredBody(BaseModel):
    campaign_ids: list[int] = Field(default_factory=list)
    red_before: str
    target_stage: str = "policy_expired"
    keep: Optional[list[str]] = None


async def _policy_request(request: Request) -> tuple[list[str], str, Optional[list[str]]]:
    """Accept either `{policies: [...], target_stage}` JSON or a multipart upload."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type == "application/json":
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "body must be JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "body must be a JSON object")
        policies = body.get("policies") or read_policies(body.get("policies_text") or "")
        target = body.get("target_stage")
        keep = body.get("keep")
    else:
        form = await request.form()
        upload = form.get("file") or form.get("policy_file")
        text = ""
        if upload is not None and hasattr(upload, "read"):
            text = (await upload.read()).decode("utf-8", "replace")
        policies = read_policies(text or str(form.get("policies") or ""))
        target = form.get("target_stage")
        keep = None

    policies = [str(p).strip() for p in (policies or []) if str(p).strip()]
    if not policies:
        raise HTTPException(422, "no policy numbers supplied")
    if not target:
        raise HTTPException(422, "target_stage is required")
    return policies, str(target), keep


def _record(conn: sqlite3.Connection, kind: str, mode: str, result: dict[str, Any],
            params: dict[str, Any], applied: int) -> int:
    cursor = conn.execute(
        "INSERT INTO stage_jobs (kind, mode, target_stage, params, would_change, unchanged, "
        "committed, dry_run, by_stage, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, mode, result["target_stage"], json.dumps(params), result["would_change"],
         result["unchanged"], applied, int(dry_run()), json.dumps(result["by_stage"]), now_iso()))
    conn.commit()
    return int(cursor.lastrowid)


def _public(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out.pop("lead_ids", None)
    return out


def _commit(conn: sqlite3.Connection, result: dict[str, Any]) -> int:
    """Apply the stage change. Refuses outright while DRY_RUN is set.

    With LEADS_SOURCE=seed there is no warehouse to write to, so the local
    dataset is the target. With LEADS_SOURCE=metabase the write goes to Formi's
    bulk endpoint, grouped by agent exactly as mark_stage_by_policy did.
    """
    if dry_run():
        return 0
    lead_ids = result.get("lead_ids") or []
    if leads_source() == "seed":
        return apply_stage(conn, lead_ids, result["target_stage"])

    by_agent: dict[int, list[int]] = defaultdict(list)
    marks = ",".join("?" * len(lead_ids)) or "NULL"
    for row in conn.execute(
            f"SELECT l.id, c.agent_id FROM leads l JOIN campaigns c ON c.id=l.campaign_id "
            f"WHERE l.id IN ({marks})", lead_ids):
        by_agent[row["agent_id"]].append(row["id"])
    applied = 0
    reason = f"chola-redial console: {result['target_stage']}"
    for agent_id, ids in by_agent.items():
        ok, _failed = bulk_update(agent_id, ids, result["target_stage"], reason, dry_run=False)
        applied += ok
    return applied


@router.post("/api/stage/policies/preview")
async def policies_preview(request: Request) -> dict[str, Any]:
    policies, target, keep = await _policy_request(request)
    with session() as conn:
        result = preview_policies(conn, policies, target, keep)
        _record(conn, "policies", "preview", result,
                {"policies": len(policies), "target_stage": target}, applied=0)
    return _public(result)


@router.post("/api/stage/policies/commit")
async def policies_commit(request: Request) -> dict[str, Any]:
    policies, target, keep = await _policy_request(request)
    with session() as conn:
        result = preview_policies(conn, policies, target, keep)
        applied = _commit(conn, result)
        job_id = _record(conn, "policies", "commit", result,
                         {"policies": len(policies), "target_stage": target}, applied)
    return {**_public(result), "job_id": job_id, "dry_run": dry_run(), "applied": applied}


@router.post("/api/stage/expired/preview")
def expired_preview(body: ExpiredBody) -> dict[str, Any]:
    with session() as conn:
        result = _expired(conn, body)
        _record(conn, "expired", "preview", result, body.model_dump(), applied=0)
    return _public(result)


@router.post("/api/stage/expired/commit")
def expired_commit(body: ExpiredBody) -> dict[str, Any]:
    with session() as conn:
        result = _expired(conn, body)
        applied = _commit(conn, result)
        job_id = _record(conn, "expired", "commit", result, body.model_dump(), applied)
    return {**_public(result), "job_id": job_id, "dry_run": dry_run(), "applied": applied}


def _expired(conn: sqlite3.Connection, body: ExpiredBody) -> dict[str, Any]:
    if not body.campaign_ids:
        raise HTTPException(422, "campaign_ids cannot be empty")
    try:
        # already_paid_to_chola / renewed / policy_expired are never overwritten.
        return preview_expired(conn, body.campaign_ids, body.red_before,
                               body.target_stage, body.keep or DEFAULT_KEEP)
    except ValueError as exc:
        raise HTTPException(422, f"red_before must be YYYY-MM-DD: {exc}") from None


@router.get("/api/stage/jobs")
def stage_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with session() as conn:
        return [{"id": r["id"], "kind": r["kind"], "mode": r["mode"],
                 "target_stage": r["target_stage"], "params": json.loads(r["params"]),
                 "would_change": r["would_change"], "unchanged": r["unchanged"],
                 "committed": r["committed"], "dry_run": bool(r["dry_run"]),
                 "by_stage": json.loads(r["by_stage"]), "created_at": r["created_at"]}
                for r in conn.execute("SELECT * FROM stage_jobs ORDER BY id DESC LIMIT ?",
                                      (max(1, min(int(limit), 500)),))]
