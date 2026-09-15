"""The clock: switch a campaign on once and its plans are ready every day.

`campaigns.autopilot` means "include this campaign in the daily plan". Twice a
day this module re-syncs those campaigns' leads and PREPARES a plan for each.
The FIRST pass stops there: it waits for an operator to approve it on the day
screen (api/day.py), and if nobody does, no call goes out.

The RECALL pass does not wait. Asked for on 15 Sep 2026 — "Recall pass i wont
approve send it should automatically schedule the call based on the previous
today's call only for the required one" — so `run_recall` both prepares and
dials it, one campaign at a time, each once its own first pass is
`same_day_gap_hours` old. That is the only thing in this module that reaches
Formi; `run_recall` lists what still holds it back.

Two passes, two run kinds, because `_write_run` refuses to replace a run for the
same (campaign, date, kind) once it has been acted on — which is exactly the
"already prepared today" guard, and for the recall it doubles as the "already
chased today" guard, so no extra bookkeeping column is needed:

    auto      first pass   the day's plan, every schedulable bucket
    auto_pm   recall pass  a second plan, built AFTER a re-sync so it only
                           reaches leads whose last call still says nobody was
                           reached — and dialled with no approval in front of it

The two preparation times below are when each plan is BUILT, not hours either
pass may dial in. Both dial anywhere in the campaign's own window; what makes
the recall pass a recall is the re-sync in front of it, which is why it is
prepared later rather than earlier. See api/day.py's PASS_LABEL.

A campaign leaves the daily plan when it is paused here, when it is paused or
killed in Formi (see `sync.upsert_campaign`), or when no lead is left with a RED
inside the grace window and a stage that is not terminal.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from engine.red_engine import EXCLUDED, config_from_settings

from .db import current_config, now_ist, session
# Not a cycle: `api.day` imports `.db`, `.routes_core` and `engine` at module
# level and reaches back into this module only from inside functions, so this
# direction can be a plain import.
from .day import _scope
from .routes_core import _campaign, _campaign_json

log = logging.getLogger("redial.autopilot")

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
    month is locally empty and would be retired on its first day. The
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
    """Leave the campaign's one-line explanation of what just happened.

    Best effort on purpose. `_approve_one`'s `failing()` calls this from inside
    an exception handler, so a note that raises replaces a reportable failure
    with an unreportable one: on 14 Sep 2026 four campaigns came back with no
    name, no run_id and no reason, because this UPDATE hit the same lock that
    had just failed their dial. The note is bookkeeping; the result is the
    truth, and losing the first must not cost the second.
    """
    try:
        conn.execute("UPDATE campaigns SET autopilot_note=? WHERE id=?",
                     (text[:300], campaign_id))
        conn.commit()
    except Exception as exc:            # noqa: BLE001 -- see above
        log.warning("could not note campaign %s (%s): %s", campaign_id, exc, text[:80])


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

def _resync(campaign_id: int, day: date) -> int:
    """Re-pull this campaign's leads from the warehouse. Raises on any failure.

    The recall pass depends on this: the whole point of two passes is that the
    second one sees how the day's earlier calls went, and a stale local copy
    would dial everyone a second time regardless of whether they answered.
    """
    from engine import metabase_source as ms          # noqa: PLC0415 — heavy import
    from engine.sync import refresh_campaign_leads    # noqa: PLC0415

    config = ms.load_config()
    schema = ms.describe_schema(config)
    with session() as conn:
        return refresh_campaign_leads(conn, campaign_id, config, schema, today=day)


def _resync_status(day: date, agent_id: Optional[int] = None) -> list[int]:
    """Re-read Formi's campaign status before a pass is planned. Raises on failure.

    Without this the console only learns about a pause on the next full sync: a
    campaign paused in Formi at 11:00 was still in the 15:00 plan, and approving
    that plan dialled customers of a campaign the client had stopped. Returns the
    campaign ids this call stopped.

    `agent_id` narrows it to one agent, for the same reason every list on the day
    screen is narrowed: a prepare scoped to Hindi that stops Tamil campaigns and
    reports them in `stopped_in_formi` is the cross-language bleed the scoping
    exists to remove. None -- what the unattended passes send -- re-syncs every
    armed agent, exactly as before.
    """
    from engine import metabase_source as ms          # noqa: PLC0415 — heavy import
    from engine.sync import refresh_campaign_status   # noqa: PLC0415

    config = ms.load_config()
    schema = ms.describe_schema(config)
    where, params = _scope("autopilot=1 AND enabled=1 AND hidden=0", agent_id)
    with session() as conn:
        agents = [r["agent_id"] for r in conn.execute(
            f"SELECT DISTINCT agent_id FROM campaigns WHERE {where}", params)]
        return refresh_campaign_status(conn, agents, config, schema, today=day)


def run_pass(kind: str, day: Optional[date] = None) -> dict[str, Any]:
    """Prepare one pass across every campaign in the daily plan. Dials nothing.

    Delegates the whole of it to `day.prepare_day`, which is also what the
    operator's Prepare button calls — one code path, so a pass fired by the clock
    and a pass fired by hand cannot drift apart.
    """
    from .day import prepare_day                 # noqa: PLC0415 — avoids an import cycle

    return prepare_day(day or now_ist().date(), kind, resync=True)


# ---------------------------------------------------------------------------
# The automatic recall
# ---------------------------------------------------------------------------

_OFF = {"0", "false", "no", "off"}

# How often a due campaign is looked at again. NOT a time of day: a campaign is
# due `same_day_gap_hours` after its own first pass, which is a different minute
# for each of them. This is only the floor on how often we go back and check, so
# a campaign that was due but had nobody eligible yet is not re-synced against
# the warehouse every single minute until somebody is.
RECALL_EVERY_MIN = 10
_recalled_at: Optional[datetime] = None


def auto_recall_on() -> bool:
    """Whether the recall pass dials itself. On unless switched off.

    The one kill switch, and the only thing here that is not already a knob
    somewhere else: everything the recall obeys — the campaign's dial window,
    DRY_RUN, pause, `max_per_minute`, the per-lead RED gate — it obeys because it
    goes out through `day._approve_one`, the same function the operator's own
    Approve calls.
    """
    return (os.environ.get("AUTO_RECALL") or "1").strip().lower() not in _OFF


def _recall_due(conn: sqlite3.Connection, campaign_id: int, day: date,
                now: datetime) -> bool:
    """Has this campaign earned its second call of the day yet?

    `same_day_gap_hours` after its FIRST PASS went out — read per campaign, so it
    is the operator's own knob answering the operator's own example: "if you call
    them at 11:30 and they do not pick, call them after 3 hrs". Per campaign
    rather than at one clock time on purpose: twenty-two campaigns chased at
    twenty-two different minutes is twenty-two small posts to Formi instead of
    one thundering herd, which is the whole of "do not overload the system".

    This is only the COARSE gate — it decides when to look, never who to call.
    Whether any individual lead has earned a second call stays the engine's
    question: `wants_second_call` reads the disposition that lead's first call
    recorded, `same_day_gap_hours` is applied again to that lead's own last call,
    and only the two-calls-a-day buckets F5/E0/F6 — the operator's "red 0-7 and
    -1 to -3" — can hold a second slot at all. A campaign that is due can very
    reasonably dial nobody.
    """
    first = conn.execute(
        "SELECT created_at FROM runs WHERE campaign_id=? AND run_date=? AND kind=? "
        "AND posted>0 ORDER BY id DESC LIMIT 1",
        (campaign_id, day.isoformat(), AM)).fetchone()
    # No first pass, no recall. A campaign that has not called anybody today has
    # nothing to chase, whatever the hour — the recall is defined against the
    # previous call, not against the clock.
    if first is None:
        return False
    # The day's one chase, already out. `_write_run`'s (campaign, date, kind)
    # guard would refuse a second one anyway; asking here is what keeps a settled
    # campaign from being re-synced against the warehouse every ten minutes for a
    # plan that cannot be written.
    if conn.execute("SELECT 1 FROM runs WHERE campaign_id=? AND run_date=? AND kind=? "
                    "AND status<>'planned' LIMIT 1",
                    (campaign_id, day.isoformat(), PM)).fetchone():
        return False
    gap = config_from_settings(current_config(conn, campaign_id)).same_day_gap_hours
    try:
        dialled = datetime.fromisoformat(first["created_at"])
    except (TypeError, ValueError):          # a hand-edited row; never chase on a guess
        return False
    return now >= dialled + timedelta(hours=float(gap))


def run_recall(day: Optional[date] = None) -> dict[str, Any]:
    """Chase today's unanswered calls. THIS DIALS — there is no approval in front.

    Asked for in as many words: "Recall pass i wont approve send it should
    automatically schedule the call based on the previous today's call only for
    the required one." So the recall pass stopped waiting for a person.

    It is still exactly the two steps an operator performs by hand, in the same
    order and through the same two functions:

        `_prepare_one(resync=True)`  re-pulls the campaign's leads, so the plan is
                                     built against how the day's earlier calls
                                     actually went and not yesterday's guess at it
        `_approve_one`               re-plans from this minute and commits

    Which is the point: nothing about a dial is re-implemented here, so nothing
    about a dial can drift. A recall that comes due at 21:00 answers
    `window_closed` and dials nobody, because `_approve_one` checks the window. A
    paused campaign never reaches it, because `_armed` and `_prepare_one` both
    ask. DRY_RUN is honoured because `_commit` honours it. `max_per_minute` and
    the RED bands hold because `_write_run` and the engine hold them.

    Two things are this function's own:

      * it never runs beside the operator's own dial walk — two writers posting
        to Formi at once is a customer called twice in a minute;
      * it re-reads Formi's campaign status first, for the same reason
        `prepare_day` does. A campaign the client paused at 11:00 must drop out
        of the chase, and a pass that dials with nobody watching needs that check
        more than one that waits for a button, not less.
    """
    from .day import _approve_one, _armed, _day_result, _dial_state, _prepare_one  # noqa: PLC0415

    day = day or now_ist().date()
    now = now_ist()

    def nothing(why: str, due: int = 0) -> dict[str, Any]:
        return {**_day_result([], day.isoformat(), PM, []), "skipped": why, "due": due}

    if not auto_recall_on():
        return nothing("AUTO_RECALL is off")
    if _dial_state["running"]:
        return nothing("a dial is already running")

    with session() as conn:
        where, params = _armed()
        due = [r["id"] for r in conn.execute(
            f"SELECT id FROM campaigns WHERE {where} ORDER BY id", params)
            if _recall_due(conn, r["id"], day, now)]
    if not due:
        return nothing("no campaign is due")

    try:
        _resync_status(day)
    except Exception as exc:                 # noqa: BLE001 — reported, never fatal
        log.warning("could not re-read campaign status before the recall: %s", exc)

    results: list[dict[str, Any]] = []
    for campaign_id in due:
        # One campaign at a time, each opening its own connection for as long as
        # it needs it. The queue IS the overload protection: twenty-two campaigns
        # preparing and posting at once is what this shape exists to prevent.
        prepared = _prepare_one(campaign_id, day, PM, resync=True)
        if prepared["status"] != "prepared" or not prepared.get("ready"):
            # resync_failed, not_in_daily_plan, finished, window_closed,
            # already_ran, or simply nobody eligible. Carried through rather than
            # dropped: a campaign missing from its own result is the one thing
            # this must never do.
            results.append(prepared)
            continue
        try:
            with session() as conn:
                where, params = _armed()
                campaign = conn.execute(
                    f"SELECT * FROM campaigns WHERE id=? AND {where}",
                    (campaign_id, *params)).fetchone()
                if campaign is None:         # disarmed while we were preparing
                    results.append({**prepared, "status": "not_in_daily_plan"})
                    continue
                results.append(_approve_one(conn, campaign, day, PM, []))
        except Exception as exc:             # noqa: BLE001 — one campaign, not the pass
            log.exception("automatic recall failed on campaign %s", campaign_id)
            results.append({"campaign_id": campaign_id, "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"[:200]})

    out = {**_day_result(results, day.isoformat(), PM, []), "skipped": "", "due": len(due)}
    log.info("automatic recall %s: %s due, %s posted, %s refused",
             day, len(due), out["posted"], out["failed"])
    return out


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

    The same tick chases the day's unanswered calls and settles the dial log.
    The chase is the one thing on this tick that dials; see `run_recall`.
    Verification is read-only — SELECTs against the warehouse, writes only to the
    local log — so it is unaffected by DRY_RUN and can never place a call.
    """
    while True:
        now = now_ist()
        for kind, at in pass_times():
            key = (now.date(), kind)
            if key in _fired or now.strftime("%H:%M") < at:
                continue
            _fired.add(key)
            await asyncio.to_thread(run_pass, kind, now.date())
        # The recall chases today's unanswered calls, and unlike the passes above
        # it DIALS. It gets no pass time of its own because it does not want one:
        # each campaign comes due `same_day_gap_hours` after its OWN first pass,
        # so the tick's job is only to come back and look often enough.
        global _recalled_at
        if auto_recall_on() and (_recalled_at is None or
                                 (now - _recalled_at) >= timedelta(minutes=RECALL_EVERY_MIN)):
            # Stamped before the await, not after: a recall that takes twenty
            # minutes to place its calls must not be re-entered at minute ten.
            _recalled_at = now
            await asyncio.to_thread(run_recall, now.date())
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
            # No longer always false. The first pass still only prepares and still
            # waits for the day screen; the recall pass dials itself.
            "dials": auto_recall_on(),
            "recall": {"on": auto_recall_on(), "every_min": RECALL_EVERY_MIN,
                       "last_run": _recalled_at.isoformat(timespec="seconds")
                                   if _recalled_at else ""},
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


@router.post("/api/autopilot/recall")
def trigger_recall(date_: Optional[str] = Body(None, embed=True, alias="date")) -> dict[str, Any]:
    """Chase today's unanswered calls now. THIS DIALS — nothing follows it.

    The same work the tick does, on demand: for running the chase early, and for
    seeing what it would do without waiting ten minutes to find out. It answers
    `skipped` rather than dialling when AUTO_RECALL is off, deliberately — a kill
    switch with a manual bypass beside it is not a kill switch. The day screen's
    own Dial button is still there for a chase somebody wants to make by hand.
    """
    return run_recall(date.fromisoformat(date_) if date_ else None)


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
