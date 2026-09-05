"""Per-campaign autopilot: switch it on once, it runs until the policies run out.

The console could always plan and approve a run; nothing ever *started* one. This
is the missing scheduler. Turn `autopilot` on for a campaign and twice a day it
re-syncs that campaign's leads, dials the urgent buckets itself, and leaves the
rest as a plan for a human to approve. It switches itself off when there is
nothing left to call.

Three passes, three run kinds, because `_write_run` refuses to replace a run for
the same (campaign, date, kind) once it has been acted on — which is exactly the
"already ran today" guard, so no extra bookkeeping column is needed:

    auto      morning   URGENT buckets, dialled automatically
    auto_pm   afternoon URGENT buckets again, AFTER a re-sync so the second call
                        only goes to leads that genuinely did not pick up
    review    morning   everything else, planned and left for approval

It stops when the campaign is paused, when it is killed in Formi (see
`sync.upsert_campaign`), or when no lead is left with a RED inside the grace
window and a stage that is not terminal.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import replace
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from engine.red_engine import EXCLUDED, config_from_settings

from .db import current_config, now_ist, session
from .routes_core import _campaign, _campaign_json, _commit, _evaluate, _floor_min, _write_run

router = APIRouter()

# The approval line, in one place. Buckets on the left dial themselves; buckets
# on the right are planned and wait for a human. RED-7 .. RED+3 is the window
# where a missed day cannot be made up, so it is the one that runs unattended.
URGENT = ("M0", "E0", "F6", "F5")
REVIEW_BUCKETS = ("F4", "F3", "F2", "F1", "D0")

AM, PM, REVIEW = "auto", "auto_pm", "review"


def pass_times() -> list[tuple[str, str]]:
    """[(run kind, "HH:MM")] for the day. Both overridable from the environment."""
    return [(AM, (os.environ.get("AUTOPILOT_AM") or "10:00").strip()),
            (PM, (os.environ.get("AUTOPILOT_PM") or "15:00").strip())]


# ---------------------------------------------------------------------------
# The stop rule
# ---------------------------------------------------------------------------

def remaining_leads(conn: sqlite3.Connection, campaign_id: int,
                    day: Optional[date] = None) -> Optional[int]:
    """How many leads this campaign could still legitimately dial. None = unknown.

    Asked of the WAREHOUSE, not the local store, on purpose: a sync only pulls
    the leads inside today's RED window, so a campaign whose cohort renews next
    month is locally empty and would be retired on its first morning. The
    warehouse sees the leads that are still ahead of the window too.

    None (a warehouse we could not reach) is not zero — it never stops anything.
    """
    from engine import metabase_source as ms          # noqa: PLC0415 — heavy import

    red = config_from_settings(current_config(conn, campaign_id))
    slugs = sorted(s for s, rule in red.disposition_rules.items() if rule.klass == EXCLUDED)
    terminal = ", ".join("'" + s.replace("'", "''") + "'" for s in slugs) or "''"
    today = (day or now_ist().date()).isoformat()
    try:
        rows = ms.run_sql(f"""
SELECT COUNT(*) AS n
FROM public.leads_outlet_chola_v v
JOIN public.leads l ON l.id = v.id
CROSS JOIN LATERAL (SELECT {ms.red_parse_expression("v.red")} AS d) red
WHERE l.campaign_id = {int(campaign_id)}
  AND red.d IS NOT NULL
  AND (red.d - DATE '{today}') >= {int(red.dte_min)}
  AND LOWER(COALESCE(v.stage, '')) NOT IN ({terminal})
""".strip(), timeout=180)
    except Exception:
        return None
    return int(rows[0]["n"]) if rows else None


def _stop(conn: sqlite3.Connection, campaign_id: int, why: str) -> None:
    conn.execute("UPDATE campaigns SET autopilot=0, autopilot_note=? WHERE id=?",
                 (why[:300], campaign_id))
    conn.commit()


def _note(conn: sqlite3.Connection, campaign_id: int, text: str) -> None:
    conn.execute("UPDATE campaigns SET autopilot_note=? WHERE id=?", (text[:300], campaign_id))
    conn.commit()


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def _resync(campaign_id: int, day: date) -> int:
    """Re-pull this campaign's leads from the warehouse. Raises on any failure.

    The afternoon pass depends on this: the whole point of two passes is that the
    second one sees the morning's dispositions, and a stale local copy would dial
    everyone a second time regardless of whether they answered.
    """
    from engine import metabase_source as ms          # noqa: PLC0415 — heavy import
    from engine.sync import refresh_campaign_leads    # noqa: PLC0415

    config = ms.load_config()
    schema = ms.describe_schema(config)
    with session() as conn:
        return refresh_campaign_leads(conn, campaign_id, config, schema, today=day)


def _plan(conn: sqlite3.Connection, campaign: sqlite3.Row, day: date, kind: str,
          buckets, dial: bool) -> dict[str, Any]:
    """Plan one bucket set, and (when `dial`) put it straight on Formi's clock."""
    cfg, red, dcfg, now, leads, pairs = _evaluate(conn, campaign, day)
    floor = _floor_min(now, day, dcfg)
    if floor is not None and floor >= dcfg.end_min:
        return {"status": "window_closed"}
    # "2nd call only if the 1st is not answered" — so no pass may book both calls
    # of the day up front. One slot per lead per pass; the afternoon call is
    # earned in the afternoon, by a lead whose re-synced disposition still says
    # nobody picked up. A connected lead is CALLBACK class by then and `decide`
    # drops it before it ever reaches the dispatcher.
    red = replace(red, calls_per_day_cap=1)
    try:
        run_id = _write_run(conn, campaign, day, kind, cfg["version"], pairs, red, dcfg,
                            evaluated=len(leads), note=f"autopilot {kind}",
                            floor_min=floor, buckets=list(buckets))
    except HTTPException as exc:
        # 409 = a run of this kind already went out today. That IS the "already
        # ran" check; re-running the pass is a no-op rather than a double dial.
        return {"status": "already_ran", "detail": str(exc.detail)}
    if not dial:
        return {"status": "planned", "run_id": run_id, "awaiting_approval": True}
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    try:
        out = _commit(conn, run, campaign, "approving")
    except HTTPException as exc:
        return {"status": "not_dialled", "run_id": run_id, "detail": str(exc.detail)}
    return {"status": "committed", "run_id": run_id, "dry_run": out["dry_run"],
            "slots": out["counts"]["slots"], "posted": out["counts"]["posted"],
            "failed": out["counts"]["failed"]}


def _one(campaign_id: int, kind: str, day: date) -> dict[str, Any]:
    """One campaign, one pass. Never raises — a bad campaign must not stop the rest."""
    result: dict[str, Any] = {"campaign_id": campaign_id, "kind": kind}
    try:
        result["resynced"] = _resync(campaign_id, day)
    except Exception as exc:                     # noqa: BLE001 — reported, not swallowed
        # Planning off a stale local copy is worse than not planning: yesterday's
        # counters would re-dial leads that already answered. Skip and say so.
        with session() as conn:
            _note(conn, campaign_id, f"{day} {kind}: skipped, re-sync failed: {exc}")
        return {**result, "status": "resync_failed", "detail": str(exc)[:200]}

    with session() as conn:
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None or not campaign["autopilot"] or campaign["paused"] \
                or not campaign["enabled"]:
            return {**result, "status": "not_on_autopilot"}

        left = remaining_leads(conn, campaign_id, day)
        result["remaining"] = left
        if left == 0:
            _stop(conn, campaign_id,
                  f"finished {day}: no lead left with a RED in the window and a "
                  f"non-terminal stage")
            return {**result, "status": "finished"}

        result["urgent"] = _plan(conn, campaign, day, kind, URGENT, dial=True)
        if kind == AM:
            result["review"] = _plan(conn, campaign, day, REVIEW, REVIEW_BUCKETS, dial=False)
        _note(conn, campaign_id, f"{day} {kind}: {result['urgent'].get('status')}")
    return {**result, "status": "ran"}


def run_pass(kind: str, day: Optional[date] = None) -> list[dict[str, Any]]:
    """Run one pass across every campaign currently on autopilot."""
    day = day or now_ist().date()
    with session() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM campaigns WHERE autopilot=1 AND enabled=1 AND paused=0 ORDER BY id")]
    return [_one(campaign_id, kind, day) for campaign_id in ids]


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_fired: set[tuple[date, str]] = set()


async def loop() -> None:
    """Fire each pass once a day, at or after its configured time.

    In-memory only: a restart re-fires, and the (campaign, date, kind) guard in
    `_write_run` turns that into a no-op. One attempt per pass per day — if the
    warehouse was down at 10:00, re-fire it by hand with POST /api/autopilot/run
    rather than have the box retry silently every minute.
    """
    while True:
        now = now_ist()
        for kind, at in pass_times():
            key = (now.date(), kind)
            if key in _fired or now.strftime("%H:%M") < at:
                continue
            _fired.add(key)
            await asyncio.to_thread(run_pass, kind, now.date())
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class AutopilotBody(BaseModel):
    on: bool


@router.get("/api/autopilot")
def autopilot_status() -> dict[str, Any]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
    return {"passes": [{"kind": k, "at": at} for k, at in pass_times()],
            "urgent_buckets": list(URGENT), "review_buckets": list(REVIEW_BUCKETS),
            # Pass times are IST and the browser is on whatever the operator's
            # laptop says, so "has 10:00 gone by?" is only answerable here.
            "now": now_ist().strftime("%H:%M"),
            "fired_today": sorted(k for d, k in _fired if d == now_ist().date()),
            "campaigns": [_campaign_json(r) for r in rows if r["autopilot"]]}


@router.post("/api/campaigns/{campaign_id}/autopilot")
def set_autopilot(campaign_id: int, body: AutopilotBody) -> dict[str, Any]:
    """Start or stop the autopilot for one campaign. The operator's only switch."""
    with session() as conn:
        campaign = _campaign(conn, campaign_id)
        if body.on and not campaign["enabled"]:
            raise HTTPException(409, f"campaign {campaign_id} is disabled")
        conn.execute("UPDATE campaigns SET autopilot=?, autopilot_note=? WHERE id=?",
                     (int(body.on), f"{'started' if body.on else 'stopped'} by operator "
                                    f"{now_ist().isoformat(timespec='minutes')}", campaign_id))
        conn.commit()
        return _campaign_json(_campaign(conn, campaign_id))


@router.post("/api/autopilot/run")
def trigger(kind: str = Body(AM, embed=True),
            date_: Optional[str] = Body(None, embed=True, alias="date")) -> list[dict[str, Any]]:
    """Fire a pass now — the manual re-try for a pass the warehouse ate."""
    if kind not in (AM, PM):
        raise HTTPException(422, f"kind must be {AM!r} or {PM!r}, got {kind!r}")
    day = date.fromisoformat(date_) if date_ else None
    return run_pass(kind, day)


@router.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int) -> dict[str, Any]:
    """Remove a campaign from the console. Stops its autopilot by construction.

    Runs and their history go with it: keeping dial history for a campaign that
    is no longer listed leaves an audit trail nobody can attribute.
    """
    from .db import purge_campaigns                   # noqa: PLC0415 — avoids a cycle

    with session() as conn:
        _campaign(conn, campaign_id)
        keep = [r["id"] for r in conn.execute("SELECT id FROM campaigns WHERE id<>?",
                                              (campaign_id,))]
        purge_campaigns(conn, keep)
        return {"deleted": campaign_id}
