"""The clock: switch a campaign on once and its plan is ready every morning.

`campaigns.autopilot` means "include this campaign in the daily plan". Twice a
day this module re-syncs those campaigns' leads and PREPARES a plan for each —
and stops there. **Nothing here dials.** The plan waits for an operator to
approve it on the day screen (api/day.py); if nobody does, no call goes out.

Two passes, two run kinds, because `_write_run` refuses to replace a run for the
same (campaign, date, kind) once it has been acted on — which is exactly the
"already prepared today" guard, so no extra bookkeeping column is needed:

    auto      morning   the day's plan, every schedulable bucket
    auto_pm   afternoon a second plan, built AFTER a re-sync so it only reaches
                        leads whose disposition still says nobody picked up

A campaign leaves the daily plan when it is paused here, when it is paused or
killed in Formi (see `sync.upsert_campaign`), or when no lead is left with a RED
inside the grace window and a stage that is not terminal.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from engine.red_engine import EXCLUDED, config_from_settings

from .db import current_config, now_ist, session
from .routes_core import _campaign, _campaign_json

router = APIRouter()

AM, PM = "auto", "auto_pm"


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


def _resync_status(day: date) -> list[int]:
    """Re-read Formi's campaign status before a wave is planned. Raises on failure.

    Without this the console only learns about a pause on the next full sync: a
    campaign paused in Formi at 11:00 was still in the 15:00 plan, and approving
    that plan dialled customers of a campaign the client had stopped. Returns the
    campaign ids this call stopped.
    """
    from engine import metabase_source as ms          # noqa: PLC0415 — heavy import
    from engine.sync import refresh_campaign_status   # noqa: PLC0415

    config = ms.load_config()
    schema = ms.describe_schema(config)
    with session() as conn:
        agents = [r["agent_id"] for r in conn.execute(
            "SELECT DISTINCT agent_id FROM campaigns WHERE autopilot=1 AND enabled=1 "
            "AND hidden=0")]
        return refresh_campaign_status(conn, agents, config, schema, today=day)


def run_pass(kind: str, day: Optional[date] = None) -> dict[str, Any]:
    """Prepare one wave across every campaign in the daily plan. Dials nothing.

    Delegates the whole of it to `day.prepare_day`, which is also what the
    operator's Prepare button calls — one code path, so a pass fired by the clock
    and a pass fired by hand cannot drift apart.
    """
    from .day import prepare_day                 # noqa: PLC0415 — avoids an import cycle

    return prepare_day(day or now_ist().date(), kind, resync=True)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_fired: set[tuple[date, str]] = set()

# How often the log is read back against the warehouse, and until when. Calls
# booked at 19:59 are dialled after the window shuts, so verification runs an
# hour past it — otherwise the last hour of every day stays "not checked yet".
VERIFY_EVERY_MIN = 10
VERIFY_UNTIL_HOUR = 21
_verified_at: Optional[datetime] = None


async def loop() -> None:
    """Fire each pass once a day, at or after its configured time.

    In-memory only: a restart re-fires, and the (campaign, date, kind) guard in
    `_write_run` turns that into a no-op. One attempt per pass per day — if the
    warehouse was down at 10:00, re-fire it by hand with POST /api/autopilot/run
    rather than have the box retry silently every minute.

    The same tick settles the dial log. Verification is read-only — SELECTs
    against the warehouse, writes only to the local log — so it is unaffected by
    DRY_RUN and can never place a call.
    """
    while True:
        now = now_ist()
        for kind, at in pass_times():
            key = (now.date(), kind)
            if key in _fired or now.strftime("%H:%M") < at:
                continue
            _fired.add(key)
            await asyncio.to_thread(run_pass, kind, now.date())
        global _verified_at
        due = _verified_at is None or (now - _verified_at) >= timedelta(minutes=VERIFY_EVERY_MIN)
        # Elapsed time, not "minute % 10": a tick that drifts past the tenth
        # minute would skip the whole slot and leave the log unchecked for twenty.
        if due and 9 <= now.hour < VERIFY_UNTIL_HOUR:
            _verified_at = now
            # Off-thread and self-skipping: a slow warehouse delays the next
            # verify, never the next pass. No open row means no warehouse query
            # and no log line, so an idle day costs nothing.
            from .dial_log import verify_async         # noqa: PLC0415 — avoids a cycle
            verify_async(now.date().isoformat())
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
            # Said out loud so a screen can say it: a pass prepares, it never dials.
            "dials": False,
            # Pass times are IST and the browser is on whatever the operator's
            # laptop says, so "has 10:00 gone by?" is only answerable here.
            "now": now_ist().strftime("%H:%M"),
            "fired_today": sorted(k for d, k in _fired if d == now_ist().date()),
            "campaigns": [_campaign_json(r) for r in rows if r["autopilot"]]}


@router.post("/api/campaigns/{campaign_id}/autopilot")
def set_autopilot(campaign_id: int, body: AutopilotBody) -> dict[str, Any]:
    """Put one campaign in, or out of, the daily plan. The operator's one switch.

    Switching it ON never places a call: it only decides whose leads appear in
    tomorrow's plan. The call happens when the day is approved.
    """
    with session() as conn:
        campaign = _campaign(conn, campaign_id)
        if body.on and not campaign["enabled"]:
            raise HTTPException(409, f"campaign {campaign_id} is disabled")
        # Refused at the API, not hidden in the picker: a stale tab holding the
        # campaign list from before it was hidden would otherwise re-arm it on
        # the operator's next save, and they would never see which one.
        if body.on and campaign["hidden"]:
            raise HTTPException(409, f"campaign {campaign_id} is hidden — un-hide it first")
        conn.execute("UPDATE campaigns SET autopilot=?, autopilot_note=? WHERE id=?",
                     (int(body.on), f"{'started' if body.on else 'stopped'} by operator "
                                    f"{now_ist().isoformat(timespec='minutes')}", campaign_id))
        conn.commit()
        return _campaign_json(_campaign(conn, campaign_id))


@router.post("/api/autopilot/run")
def trigger(kind: str = Body(AM, embed=True),
            date_: Optional[str] = Body(None, embed=True, alias="date")) -> dict[str, Any]:
    """Fire a pass now — the manual re-try for a pass the warehouse ate.

    Prepares plans. Approving them is a separate, deliberate act.
    """
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
