"""The daily approval gate: one plan for the day, one decision, one screen.

Nothing in this console dials on its own. Each morning a pass PREPARES a plan for
every campaign that is in the daily plan (`campaigns.autopilot`) and leaves it
`planned`; this module is what the operator opens. One page holds the whole day —
how many leads are ready, split by RED band and by bucket — and one Approve puts
it on Formi's clock. If nobody approves, nothing goes out that day.

Ordering is the client's, and it is not the bucket order. Their schedule's two
2-calls/day rows lead it, in the order they named them: the three days AFTER
expiry first (their "1 to 3", dte -1..-3), then the week running up to it (their
"-7 to 0", dte 0..7); everything else follows. That is `red_priority` in the
config, applied by the dispatcher ahead of `bucket_priority`, so it is what
decides who survives when a day is capped or approved with few hours left.

Their table is signed the other way round from our dte -- negative means before
RED, per their own "calls needs to be initiated on RED - 1 and RED date" -- so
both bands are negated on the way in. See dispatcher.DEFAULT_RED_PRIORITY.

Approving late does not dial into the night. Approve RE-PLANS each campaign from
the current minute, so only what genuinely fits before the window shuts is
scheduled; the rest is not dialled today and comes back in tomorrow's plan.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from engine.dispatcher import (
    DEFAULT_RED_PRIORITY, WINDOW_CEIL, WINDOW_FLOOR, DispatchConfig, hhmm, parse_hhmm,
    red_rank,
)

from .db import dry_run, now_ist, session
from .routes_core import (
    _campaign_json, _commit, _earliest_dialable, _evaluate, _floor_min, _parse_day,
    _run_json, _write_run,
)

router = APIRouter()
log = logging.getLogger("redial.day")

# The run kinds a day is made of. Two waves, because the client's rule is "second
# call only if the first is not answered": the afternoon plan is built AFTER a
# re-sync, so it only reaches leads whose disposition still says nobody picked up.
# Both are prepared, neither dials — each needs its own approval.
#
# There is no per-wave call cap any more. A `_evaluate_wave` wrapper used to pin
# `calls_per_day_cap=1` on its way out of `_evaluate` — but `_evaluate` has
# already run `decide` by then, so the engine never saw it; the only thing it
# reached was the dispatcher's habit of pre-booking the day's second slot, and
# that is gone (see the note where it used to be booked, in dispatcher.py). One
# call per lead per wave is now structural, so the operator's `calls_per_day_cap`
# is left alone and means what it says: calls per lead per DAY.
MORNING, AFTERNOON = "auto", "auto_pm"
KINDS = (MORNING, AFTERNOON)

WAVE_LABEL = {MORNING: "morning", AFTERNOON: "afternoon"}

# Where the morning stops and the afternoon starts. One boundary for the whole
# console, not one per campaign: the screen has to be able to SAY which band it
# is approving ("the morning band, 09:00-13:30"), and a per-campaign boundary
# makes that sentence unwritable.
#
# Until 13 Sep 2026 the two waves were labels with no clock behind them. Approve
# re-plans from the current minute, so the morning wave approved at noon dialled
# into the evening -- on 12 Sep the `auto` wave's calls landed between 12:00 and
# 20:00 and the `auto_pm` wave's between 13:00 and 20:00, which is the same day
# twice. The band is what makes the name true.
#
# 13:30 sits between autopilot's own two preparation times (AUTOPILOT_AM 10:00,
# AUTOPILOT_PM 15:00, see autopilot.py) so each wave is still prepared inside the
# band it dials into.
_WAVE_BOUNDARY_RAW = (os.environ.get("WAVE_BOUNDARY") or "13:30").strip()
WAVE_BOUNDARY = parse_hhmm(_WAVE_BOUNDARY_RAW, "WAVE_BOUNDARY")
# `parse_hhmm` only bounds the TOTAL, so it reads `13:70` as 14:10 rather than
# refusing it. A boundary quietly set to a time nobody typed is the same failure
# as one set outside the hours, and it is the likelier typo of the two.
#
# The SHAPE of the whole value is checked, in ASCII digits, because `int()` is
# far more forgiving than an operator typing a clock time means it to be: it eats
# a sign (`+13:30`), leading zeros (`013:30`, `13:005`) and any Unicode digit --
# `13:3` ending in an Arabic-Indic zero is `isdigit()` to Python and 810 minutes
# to `int()`. Every one of those booted as a time nobody typed. Checking only the
# minute field left all four through.
#
# Unpadded stays legal: `9:30` and `13:5` name a real time and booted fine before
# any of these guards existed. Comparing `hhmm(WAVE_BOUNDARY)` against the raw
# string refused them, and a guard that stops a live dialler on a legal value is
# worse than the bug it prevents. The hour needs no range check of its own:
# `parse_hhmm` bounds the total at 24:00 and the dialling-hours check below is
# stricter still.
if not re.fullmatch(r"[0-9]{1,2}:[0-9]{1,2}", _WAVE_BOUNDARY_RAW):
    raise ValueError(f"WAVE_BOUNDARY must be HH:MM in plain ASCII digits, got "
                     f"{_WAVE_BOUNDARY_RAW!r} (it would be read as "
                     f"{hhmm(WAVE_BOUNDARY)})")
_WAVE_BOUNDARY_MM = _WAVE_BOUNDARY_RAW.partition(":")[2]
if int(_WAVE_BOUNDARY_MM) >= 60:
    raise ValueError(f"WAVE_BOUNDARY must be HH:MM, got {_WAVE_BOUNDARY_RAW!r} "
                     f"(minutes {_WAVE_BOUNDARY_MM!r} are not 00-59; it would be "
                     f"read as {hhmm(WAVE_BOUNDARY)})")
# Inside the dialling hours, and strictly: a boundary ON or outside either edge
# gives one wave the whole day and the other an empty band for EVERY campaign,
# and nothing downstream says so -- `_clip` just returns a band with no minutes
# in it. `WAVE_BOUNDARY=00:00` booted cleanly and shut the morning down across
# the board. Raised here, at import, so a typo stops the API where the operator
# can see it rather than on a box with DRY_RUN=0.
if not WINDOW_FLOOR < WAVE_BOUNDARY < WINDOW_CEIL:
    raise ValueError(
        f"WAVE_BOUNDARY {hhmm(WAVE_BOUNDARY)} must be strictly between "
        f"{hhmm(WINDOW_FLOOR)} and {hhmm(WINDOW_CEIL)}, the permitted dialling hours")
WAVE_BAND = {MORNING: (None, WAVE_BOUNDARY), AFTERNOON: (WAVE_BOUNDARY, None)}


def _clip(start: int, end: int, kind: str) -> tuple[int, int]:
    """(start, end) clipped to this wave's half of the day. Never widened.

    `None` on a side of the band means "this wave does not move that edge", so
    the campaign's own opening (morning) or close (afternoon) is kept.
    """
    lo, hi = WAVE_BAND[kind]
    return (max(start, lo if lo is not None else 0),
            min(end, hi if hi is not None else 24 * 60))


def _band(kind: str, dcfg: DispatchConfig) -> DispatchConfig:
    """The campaign's own dial window, clipped to this wave's half of the day.

    Narrowing the config is the whole implementation: `dispatch` already receives
    a DispatchConfig and honours start_min/end_min, so nothing in the dispatcher
    or in `_write_run` needs to know a band exists.

    CLIPPING, never widening. A campaign that shuts at 13:00 gets an afternoon
    band whose start is at or past its end -- an empty band, caught by the
    explicit `start_min >= end_min` check in `_prepare_one` / `_approve_one`.
    NOT by `floor >= end_min`: on a date that is not today `_floor_min` returns
    None and that guard never runs.
    """
    start, end = _clip(dcfg.start_min, dcfg.end_min, kind)
    return replace(dcfg, start_min=start, end_min=end)


# Who is in today's plan. One string because the question is asked three times —
# the day view, the prepare pass and the approve — and a campaign that answers
# yes to one but not the others is a campaign that gets planned off-screen or
# dialled after it was taken out. Widening the roster means editing this line.
ARMED = "autopilot=1 AND enabled=1 AND paused=0 AND hidden=0"


def _scope(where: str, agent_id: Optional[int]) -> tuple[str, list[Any]]:
    """`where` narrowed to one agent, or left exactly as it was. One place, so a
    roster and the `stopped` list beside it cannot end up scoped differently.

    `where` is PARENTHESISED before the AND. Both of today's callers are safe
    without it -- ARMED is all-AND, STOPPED is already bracketed -- but a future
    clause holding a top-level OR would bind the AND to its last branch only, and
    the scoping call would WIDEN the roster instead of narrowing it, silently and
    in the one direction this helper exists to make impossible.
    """
    return (where, []) if agent_id is None else (f"({where}) AND agent_id=?", [agent_id])


def _armed(agent_id: Optional[int] = None) -> tuple[str, list[Any]]:
    """The roster clause, optionally narrowed to one agent.

    Agent scoping is NOT a campaign filter: a campaign carries its agent already,
    so a newly created one still auto-arms and appears under its own agent with
    nothing to configure. What it buys is two languages that stop being one
    number -- agents 125 and 127 hold mirrored campaigns and the day screen used
    to sum them, so 4,271 Hindi slots and 481 Tamil ones were shown as 4,752.
    """
    return _scope(ARMED, agent_id)


# Armed once and now held. Keyed on the LATCH as well as the switch, because a
# stop disarms `autopilot` and moves the fact that it was armed into
# `autopilot_latched` -- reading only the switch loses the campaign the operator
# is looking for.
STOPPED = "(autopilot=1 OR autopilot_latched=1) AND (paused=1 OR enabled=0)"

# How far back `_stranded` looks. Older than this the leads have been re-planned
# several times over and the row is history, not a thing to act on.
STRANDED_DAYS = 14


class PrepareBody(BaseModel):
    date: Optional[str] = None
    kind: str = MORNING
    # Off by default: the hourly sync timer already keeps the local copy fresh,
    # and a warehouse round-trip per campaign turns a button press into minutes.
    # The scheduled pass sets it — the afternoon wave is worthless without it.
    resync: bool = False
    # Narrows the pass to one agent — one language. None = every armed campaign,
    # which is what the scheduled passes use.
    agent_id: Optional[int] = None


class ApproveBody(BaseModel):
    date: Optional[str] = None
    kind: str = MORNING
    # Which buckets to actually call. Empty = every bucket in the plan. The rest
    # are not dialled today; they come back in tomorrow's plan.
    buckets: list[str] = Field(default_factory=list)
    # Empty = every campaign with a plan waiting. Named ids narrow it.
    campaign_ids: list[int] = Field(default_factory=list)
    # Narrows the approval to one agent — one language. None = every armed
    # campaign, which is what an unscoped console has always dialled.
    agent_id: Optional[int] = None


# ---------------------------------------------------------------------------
# RED bands
# ---------------------------------------------------------------------------

def _bands(config: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    raw = config.get("red_priority") or DEFAULT_RED_PRIORITY
    return tuple((int(a), int(b)) for a, b in raw)


def _band_label(first: int, second: int) -> str:
    """Say the band in renewal terms, derived from the numbers rather than fixed.

    dte is (RED date - today), so a positive dte is a renewal still ahead and a
    negative one is a policy already past its RED date.
    """
    lo, hi = min(first, second), max(first, second)
    if lo > 0:                      # wholly ahead of RED
        return f"renewal due in {lo}-{hi} days"
    if hi < 0:                      # wholly past it — the client's "1 to 3"
        return (f"{-hi} days past RED" if lo == hi
                else f"{-hi}-{-lo} days past RED")
    if lo == 0 and hi == 0:
        return "RED day"
    if lo == 0:                     # RED day and the run-up — the client's "-7 to 0"
        return f"RED day and the {hi} days before it"
    if hi == 0:
        return f"RED day and the {-lo} days after it"
    return f"{hi} days before RED to {-lo} days after"


def _band_rows(per_campaign: dict[int, tuple], counts: dict[int, int]) -> list[dict[str, Any]]:
    """One row per priority position, over EVERY armed campaign's bands.

    `red_priority` is per-campaign, so a rank means "that campaign's Nth band"
    and nothing more. Reading one campaign's table and calling it the day's was
    wrong twice: it named days the other campaigns may not treat as priority at
    all, and it ranked their leads by a table they do not use.

    Where the campaigns agree — the case today, and the one the client asked for
    — each row reads exactly as it always did. Where they disagree the range is
    left empty and the row says so, because no single pair of days covers that
    position across the day.
    """
    defs: dict[int, set[Optional[tuple[int, int]]]] = {}
    # With nothing armed there is no config to read, and the response still owes
    # its caller the shape it always had.
    for bands in (per_campaign or {0: DEFAULT_RED_PRIORITY}).values():
        for rank, (first, second) in enumerate(bands):
            defs.setdefault(rank, set()).add((min(first, second), max(first, second)))
        defs.setdefault(len(bands), set()).add(None)

    rows = []
    for rank in sorted(defs):
        seen = defs[rank]
        if seen == {None}:
            row = {"dte_from": None, "dte_to": None, "label": "outside the priority bands"}
        elif len(seen) == 1 and (band := next(iter(seen))) is not None:
            lo, hi = band
            row = {"dte_from": hi, "dte_to": lo, "label": _band_label(lo, hi)}
        else:
            row = {"dte_from": None, "dte_to": None,
                   "label": f"priority {rank + 1} — differs between campaigns"}
        rows.append({"rank": rank, **row, "ready": counts.get(rank, 0)})
    return rows


# ---------------------------------------------------------------------------
# The day's window
# ---------------------------------------------------------------------------

# Only reached when no campaign is armed, so there is no config to read a window
# from. Every stored config carries its own — `with_defaults` fills it in.
DEFAULT_WINDOW = {"start": "09:00", "end": "20:00"}


def _day_window(configs: dict[int, dict[str, Any]], ready: dict[int, int],
                floor: int, today: bool, kind: str) -> dict[str, Any]:
    """The day's dialling window and its ceiling, from every armed campaign.

    `dial_window` and `max_per_minute` are per-campaign, and each is one PUT away
    from being edited on its own, so no single campaign's config is the day's.
    Reading the first armed one and labelling it "the window" showed nothing while
    every campaign happened to agree — all 69 land on 09:00-20:00 once
    `with_defaults` retires the stale 09:30-19:00 snapshot — and would have named
    a close time the other 68 do not keep the moment one was narrowed.

    The window reported is the ENVELOPE of the campaigns' windows CLIPPED TO THIS
    WAVE'S BAND: no call goes out before its start or after its end, whichever
    campaign places it, and this approval cannot reach past the band whatever a
    campaign's own close says. `varies` says the campaigns do not agree, so the
    screen can say so instead of implying a shared close.

    `open` is true while ANY campaign can still dial — approving is worth doing
    for the campaigns still open even once the others have shut.

    The ceiling is capped PER campaign before it is summed: a campaign that shuts
    at 19:00 cannot absorb another's leads, so one total capacity against one
    total ready would promise a day that does not exist.
    """
    if not configs:
        return {"window": DEFAULT_WINDOW, "varies": False, "open": False, "capacity": 0}

    spans, capacity = set(), 0
    for campaign_id, config in configs.items():
        window = config.get("dial_window") or DEFAULT_WINDOW
        # Clipped to the wave's band, so the screen names the hours this approval
        # can actually reach rather than the campaign's whole day.
        start, end = _clip(parse_hhmm(window.get("start", DEFAULT_WINDOW["start"])),
                           parse_hhmm(window.get("end", DEFAULT_WINDOW["end"])), kind)
        # No hours in this wave: a point at the boundary (every inverted clip
        # lands there), not a backwards "13:30-13:00" on the operator's screen.
        if start > end:
            start = end = WAVE_BOUNDARY
        spans.add((start, end))
        waiting = ready.get(campaign_id, 0)
        if not today:
            capacity += waiting
            continue
        room = max(0, end - max(start, floor)) * int(config.get("max_per_minute") or 1)
        capacity += min(room, waiting)

    return {
        "window": {"start": hhmm(min(s for s, _ in spans)), "end": hhmm(max(e for _, e in spans))},
        "varies": len(spans) > 1,
        "open": any((max(start, floor) if today else start) < end for start, end in spans),
        "capacity": capacity,
    }


# ---------------------------------------------------------------------------
# Reading the day
# ---------------------------------------------------------------------------

def _plan_rows(conn: sqlite3.Connection, campaign_ids, day: date, kind: str):
    """The `planned`/`committed` run per campaign for this day and wave."""
    if not campaign_ids:
        return {}
    marks = ",".join("?" * len(campaign_ids))
    rows = conn.execute(
        f"SELECT * FROM runs WHERE run_date=? AND kind=? AND campaign_id IN ({marks}) "
        f"ORDER BY id", (day.isoformat(), kind, *campaign_ids)).fetchall()
    return {r["campaign_id"]: r for r in rows}


def _slot_counts(conn: sqlite3.Connection, run_ids):
    """(run_id, bucket, dte) -> how many slots. One GROUP BY for the whole day."""
    if not run_ids:
        return []
    marks = ",".join("?" * len(run_ids))
    return conn.execute(
        f"SELECT run_id, bucket, bucket_label, dte, COUNT(*) AS n FROM plan_items "
        f"WHERE run_id IN ({marks}) AND status='planned' "
        f"GROUP BY run_id, bucket, bucket_label, dte", list(run_ids)).fetchall()


def _dialled_today(conn: sqlite3.Connection, day: date,
                   agent_id: Optional[int] = None) -> dict[str, int]:
    """What the log says actually happened, by verify state. Answers 'did it run?'.

    Scoped like every other field on this page. It is the ONE number that says
    whether the day actually ran, so two language panels side by side both
    reporting the whole day's dials is the exact confusion agent scoping exists
    to remove -- and a scoped day that reported the other agent's calls would be
    lying about the only figure the operator checks after approving.

    `dial_log` carries its own `agent_id` (written from the campaign when the row
    is logged), so this is the same one-clause narrowing as every other list.
    """
    where, params = _scope("substr(scheduled_time,1,10)=?", agent_id)
    rows = conn.execute(
        f"SELECT verified, COUNT(*) AS n FROM dial_log WHERE {where} "
        f"GROUP BY verified", (day.isoformat(), *params)).fetchall()
    return {r["verified"]: r["n"] for r in rows}


def _stranded(conn: sqlite3.Connection, day: date,
              agent_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Runs prepared on an EARLIER day and never dialled.

    A run stays `planned` until somebody approves it. On 12 Sep 2026 eight
    campaigns holding 491 slots sat like that until the day ended, and no screen
    in this console said so -- the day view only ever looked at the date it was
    asked about. Those leads were not dropped, re-queued or reported; they simply
    did not get called.

    Bounded to the last STRANDED_DAYS days.
    """
    # "Earlier" means earlier than TODAY, not earlier than the day asked about.
    # The date input has no upper bound, so tomorrow is one click away -- and
    # bounded by the requested day, this morning's `planned` run would be
    # reported as never dialled while it is in fact queued and awaiting
    # approval. A plan for a day that has not arrived is waiting, not abandoned.
    #
    # BOTH bounds come off the one clamped date. Clamping only the upper one left
    # the lower keyed to the requested day, so a date far enough in the future put
    # `since` past `upper` and the window collapsed to nothing -- the warning then
    # reported no stranded run at all, through the same unbounded date input.
    today = min(day, now_ist().date())
    since = (today - timedelta(days=STRANDED_DAYS)).isoformat()
    upper = today.isoformat()
    # The roster predicate is scoped to its own SELECT so every bare column in
    # ARMED resolves against `campaigns` by construction -- qualifying only the
    # first of the four left the rest to SQLite's search across the join.
    where, params = _armed(agent_id)
    rows = conn.execute(
        f"SELECT r.campaign_id, c.name, r.run_date, r.kind, r.slots "
        f"FROM runs r JOIN campaigns c ON c.id=r.campaign_id "
        f"WHERE r.status='planned' AND r.run_date < ? AND r.run_date >= ? "
        f"AND r.slots > 0 "
        f"AND r.campaign_id IN (SELECT id FROM campaigns WHERE {where}) "
        f"ORDER BY r.run_date DESC, r.campaign_id", (upper, since, *params)).fetchall()
    return [{"campaign_id": r["campaign_id"], "name": r["name"], "run_date": r["run_date"],
             "kind": r["kind"], "slots": r["slots"]} for r in rows]


def _plan_facts(conn: sqlite3.Connection, runs: dict[int, sqlite3.Row],
                campaign_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per campaign: when the plan was built, when it last dialled, what was booked.

    All three answer the same operator question -- "has a call already been
    placed for these leads, by me or by anybody?" The engine does check: it skips
    every lead with `queued_today > 0` (red_engine.SKIP_ALREADY_SCHEDULED). But
    it checks at PLAN time, and on 12 Sep 2026 the plans were built in the
    morning and approved six hours later, so anything booked in Formi in between
    was invisible to the approval.

    `already_booked` is therefore both a count and a clock: N leads were already
    on Formi's clock when this plan was built, and the older the plan the less
    that number can be trusted. `last_dialled` is the other half -- a campaign
    that dialled two hours ago reads very differently from one last called on
    Tuesday.

    `plan_built_at` is `runs.created_at`, which is `now_iso()` -- naive IST, no
    offset (see the note on IST in api/db.py). The client must not hand that
    string to a bare `new Date(...)`: an ISO date-TIME with no offset is parsed
    as the BROWSER's local time, so on a laptop in UTC a plan reads 5h30m
    YOUNGER than it is -- a six-hour-old plan looks half an hour old and raises
    nothing, which is the exact failure of 12 Sep 2026 wearing a timezone.
    `planAge` in web/src/screens/Today.tsx pins the +05:30 back on; anything
    else reading this field owes the same.
    """
    facts = {cid: {"plan_built_at": None, "last_dialled": None, "already_booked": 0}
             for cid in campaign_ids}
    if not campaign_ids:
        return facts

    for campaign_id, run in runs.items():
        if campaign_id in facts:
            facts[campaign_id]["plan_built_at"] = run["created_at"]

    marks = ",".join("?" * len(campaign_ids))
    for row in conn.execute(
            f"SELECT campaign_id, MAX(run_date) AS last FROM runs "
            f"WHERE campaign_id IN ({marks}) AND posted > 0 GROUP BY campaign_id",
            campaign_ids):
        facts[row["campaign_id"]]["last_dialled"] = row["last"]

    run_ids = [r["id"] for r in runs.values()]
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        owner = {r["id"]: cid for cid, r in runs.items()}
        for row in conn.execute(
                f"SELECT run_id, COUNT(*) AS n FROM decisions "
                f"WHERE run_id IN ({marks}) AND reason LIKE 'ALREADY_SCHEDULED_TODAY%' "
                f"GROUP BY run_id", run_ids):
            facts[owner[row["run_id"]]]["already_booked"] = row["n"]
    return facts


def _spread(conn: sqlite3.Connection, runs: dict[int, sqlite3.Row],
            kind: str) -> dict[str, Any]:
    """Which hours the day's calls actually went onto, against the band approved.

    This is the honest answer to "is it scheduling properly". On 12 Sep 2026 the
    `auto` wave's posted slots were spread across 12:00-20:00 and the `auto_pm`
    wave's across 13:00-20:00 -- the same evening twice, under two names -- and
    nothing in this console showed it. A count per hour beside the band the
    operator approved makes a wave that dialled outside its half of the day
    visible at a glance instead of needing a database query.

    Only `posted` and `simulated` items count: a `planned` slot has not been
    scheduled anywhere yet, and an `expired` one never will be.

    `runs` arrives already narrowed to the scope of the page (`_plan_rows` over
    the armed campaigns, get_day), so there is no agent filter here -- a second
    one would be a second chance to disagree with the rest of the answer.
    """
    lo, hi = WAVE_BAND[kind]
    band = {"start": hhmm(lo if lo is not None else parse_hhmm(DEFAULT_WINDOW["start"])),
            "end": hhmm(hi if hi is not None else parse_hhmm(DEFAULT_WINDOW["end"]))}
    run_ids = [r["id"] for r in runs.values()]
    if not run_ids:
        return {"band": band, "hours": {}}

    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT substr(scheduled_time, 12, 2) AS hour, COUNT(*) AS n FROM plan_items "
        f"WHERE run_id IN ({marks}) AND status IN ('posted','simulated') "
        f"GROUP BY hour ORDER BY hour", run_ids).fetchall()
    return {"band": band, "hours": {str(int(r["hour"])): r["n"] for r in rows}}


@router.get("/api/day")
def get_day(date: Optional[str] = Query(None), kind: str = Query(MORNING),
            agent_id: Optional[int] = Query(None)) -> dict[str, Any]:
    """The whole day on one page: what is ready, in what order, and what it did.

    Cheap by construction — two GROUP BYs over rows this console already wrote.
    It never re-runs the engine, so it can be polled by a screen that is open all
    day without costing a warehouse query.

    `agent_id` narrows every list here to one agent — one language. Omitted, the
    answer is the whole day across every armed campaign, exactly as before.
    """
    if kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}, got {kind!r}")
    day = _parse_day(date)
    now = now_ist()
    today = day == now.date()

    with session() as conn:
        where, params = _armed(agent_id)
        campaigns = conn.execute(
            f"SELECT * FROM campaigns WHERE {where} ORDER BY id", params).fetchall()
        # Stopped campaigns are still shown: "why is nothing happening for X" is
        # the question this screen exists to answer, and an empty list answers it
        # with silence. Scoped with the roster above, or a panel showing one
        # language would list the other one's stopped campaigns.
        stopped_where, stopped_params = _scope(STOPPED, agent_id)
        stopped = conn.execute(
            f"SELECT * FROM campaigns WHERE {stopped_where} ORDER BY id",
            stopped_params).fetchall()
        # A hidden campaign is gone from every list in the console, and hiding
        # deliberately leaves today's queued calls on Formi's clock. Those two
        # together would put calls on the wire with nothing on any screen saying
        # so, which is the one thing this console must never do. It appears here
        # while — and only while — it still has calls to place, then drops off.
        # Scoped too: it joins the `stopped` list below, and half a scoped list is
        # worse than none because the operator cannot tell which half they see.
        hidden_where, hidden_params = _scope(
            "c.hidden=1 AND r.run_date=? AND r.status='committed' "
            "AND i.status IN ('posted','simulated') AND i.scheduled_time > ?",
            agent_id)
        dialling_while_hidden = conn.execute(
            "SELECT DISTINCT c.* FROM campaigns c "
            "JOIN runs r ON r.campaign_id=c.id JOIN plan_items i ON i.run_id=r.id "
            f"WHERE {hidden_where} ORDER BY c.id",
            (day.isoformat(), now.strftime("%Y-%m-%dT%H:%M:00"),
             *hidden_params)).fetchall()
        runs = _plan_rows(conn, [c["id"] for c in campaigns], day, kind)
        counts = _slot_counts(conn, [r["id"] for r in runs.values()])
        log = _dialled_today(conn, day, agent_id)
        stranded_runs = _stranded(conn, day, agent_id)
        facts = _plan_facts(conn, runs, [c["id"] for c in campaigns])
        spread = _spread(conn, runs, kind)

        from .db import current_config                  # noqa: PLC0415 — avoids a cycle
        # Every armed campaign's own config. There is no campaign whose settings
        # are the day's — window, max_per_minute and red_priority are all
        # per-campaign — and reading only the first one's made this screen
        # describe a day the other campaigns were not having.
        configs = {c["id"]: current_config(conn, c["id"]) for c in campaigns}

    bands = {cid: _bands(config) for cid, config in configs.items()}
    campaign_of_run = {run["id"]: campaign_id for campaign_id, run in runs.items()}
    per_run: dict[int, dict[str, Any]] = {}
    buckets: dict[str, dict[str, Any]] = {}
    band_counts: dict[int, int] = {}
    for row in counts:
        entry = per_run.setdefault(row["run_id"], {"ready": 0, "buckets": {}})
        entry["ready"] += row["n"]
        entry["buckets"][row["bucket"]] = entry["buckets"].get(row["bucket"], 0) + row["n"]
        # Ranked by the bands of the campaign the lead belongs to, not by one
        # borrowed table. Rank stays comparable across campaigns because it is a
        # position — rank 0 is whatever that campaign calls first.
        rank = red_rank(row["dte"], bands[campaign_of_run[row["run_id"]]])
        band_counts[rank] = band_counts.get(rank, 0) + row["n"]
        bucket = buckets.setdefault(row["bucket"], {
            "bucket": row["bucket"], "label": row["bucket_label"] or row["bucket"], "ready": 0,
            "best_rank": rank})
        bucket["ready"] += row["n"]
        bucket["best_rank"] = min(bucket["best_rank"], rank)

    listed = []
    for campaign in campaigns:
        run = runs.get(campaign["id"])
        stats = per_run.get(run["id"] if run is not None else -1, {"ready": 0, "buckets": {}})
        listed.append({
            **_campaign_json(campaign),
            "run_id": run["id"] if run is not None else None,
            "run_status": run["status"] if run is not None else "not_prepared",
            "ready": stats["ready"], "by_bucket": stats["buckets"],
            "posted": run["posted"] if run is not None else 0,
            "failed": run["failed"] if run is not None else 0,
            "dropped": run["dropped"] if run is not None else 0,
            **facts[campaign["id"]],
        })

    ready = sum(c["ready"] for c in listed)
    statuses = {c["run_status"] for c in listed}
    if not listed:
        status = "no_campaigns"
    elif statuses == {"not_prepared"}:
        status = "not_prepared"
    elif "planned" in statuses and ready:
        status = "awaiting_approval"
    else:
        status = "approved"

    # A ceiling, not a promise: minutes left in each campaign's own window times
    # how many calls a minute it may hold, summed. Approve re-plans, so the real
    # number is decided then — but an operator opening this at 18:00 needs to see
    # that the day no longer fits BEFORE they approve, not in the drop count
    # afterwards.
    first_free = _earliest_dialable(now)
    span = _day_window(configs, {c["id"]: c["ready"] for c in listed},
                       first_free.hour * 60 + first_free.minute, today, kind)

    return {
        "date": day.isoformat(), "kind": kind, "wave": WAVE_LABEL[kind],
        # What this answer is narrowed to. None = every armed campaign, so a
        # screen can tell "one agent's day" from "the whole day" without keeping
        # its own copy of what it asked for.
        "agent_id": agent_id,
        "now": now.strftime("%H:%M"), "dry_run": dry_run(),
        # The envelope across the armed campaigns, not one campaign's own hours.
        "window": span["window"], "window_varies": span["varies"],
        "window_open": span["open"],
        "status": status,
        "totals": {"campaigns": len(listed), "ready": ready,
                   "posted": sum(c["posted"] for c in listed),
                   "failed": sum(c["failed"] for c in listed),
                   "dropped": sum(c["dropped"] for c in listed)},
        "capacity_before_close": span["capacity"],
        # Best RED band first, then the bucket order inside it — the same order
        # `approve` will dial in, so the screen cannot promise a sequence the
        # dispatcher will not honour.
        "buckets": sorted(buckets.values(), key=lambda b: (b["best_rank"], b["bucket"])),
        "red_bands": _band_rows(bands, band_counts),
        "campaigns": listed,
        "stopped": [{**_campaign_json(c),
                     "why": c["stopped_reason"] or ("disabled" if not c["enabled"] else "paused")}
                    for c in stopped]
                   + [{**_campaign_json(c), "why": "hidden — the calls it already "
                       "put on Formi's clock today are still going out"}
                      for c in dialling_while_hidden],
        # The honest half of "did the call happen": counts straight off the dial
        # log, where `dialled` means the warehouse showed a real interaction.
        "dial_log": log,
        # Plans from earlier days that nobody ever approved. Not history: those
        # leads were never called and nothing else in this console says so.
        "stranded": stranded_runs,
        # Which hours the calls actually landed in, against the band approved.
        "spread": spread,
    }


# ---------------------------------------------------------------------------
# Preparing
# ---------------------------------------------------------------------------

def prepare_day(day: Optional[date] = None, kind: str = MORNING,
                resync: bool = False, agent_id: Optional[int] = None) -> dict[str, Any]:
    """Build (or rebuild) today's plan for every campaign in the daily plan.

    Plan only. This function cannot dial: it never calls `_commit`, and the runs
    it writes are `planned`. That is the whole of the approval gate — there is no
    path from here to Formi.

    Never raises for one bad campaign: a warehouse that ate one campaign's leads
    must not cost the operator the other twenty-one.
    """
    if kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}, got {kind!r}")
    day = day or now_ist().date()
    stopped: list[int] = []
    if resync:
        # Before anything is planned, not after: a campaign paused in Formi since
        # the morning must drop out of this wave, and its queued calls come off
        # Formi's clock. One warehouse read for the whole pass, not one per
        # campaign. A warehouse we cannot reach is reported, never fatal — the
        # per-campaign lead re-sync below fails loudly enough on its own.
        from .autopilot import _resync_status         # noqa: PLC0415 — avoids a cycle
        try:
            stopped = _resync_status(day, agent_id)
        except Exception as exc:                     # noqa: BLE001 — reported, not swallowed
            log.warning("could not re-read campaign status before the %s wave: %s", kind, exc)

    with session() as conn:
        where, params = _armed(agent_id)
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM campaigns WHERE {where} ORDER BY id", params)]

    results = [_prepare_one(campaign_id, day, kind, resync) for campaign_id in ids]
    return {"date": day.isoformat(), "kind": kind, "wave": WAVE_LABEL[kind],
            "campaigns": results, "stopped_in_formi": stopped,
            "ready": sum(r.get("ready", 0) for r in results),
            "prepared": sum(1 for r in results if r["status"] == "prepared")}


def _prepare_one(campaign_id: int, day: date, kind: str, resync: bool) -> dict[str, Any]:
    from .autopilot import _note, _resync, _stop, remaining_leads   # noqa: PLC0415 — cycle

    out: dict[str, Any] = {"campaign_id": campaign_id}
    if resync:
        try:
            out["resynced"] = _resync(campaign_id, day)
        except Exception as exc:                 # noqa: BLE001 — reported, not swallowed
            # Planning off a stale copy is worse than not planning: yesterday's
            # counters would re-offer leads that already answered this morning.
            with session() as conn:
                _note(conn, campaign_id, f"{day} {kind}: skipped, re-sync failed: {exc}")
            return {**out, "status": "resync_failed", "detail": str(exc)[:200]}

    with session() as conn:
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None or not campaign["autopilot"] or campaign["paused"] \
                or not campaign["enabled"]:
            return {**out, "status": "not_in_daily_plan"}
        out["name"] = campaign["name"]

        left = remaining_leads(conn, campaign_id, day)
        if left == 0:
            _stop(conn, campaign_id,
                  f"finished {day}: no lead left with a RED in the window and a "
                  f"non-terminal stage")
            return {**out, "status": "finished"}

        try:
            cfg, red, dcfg, _now, leads, pairs = _evaluate(conn, campaign, day)
            # The wave's half of the day, not the campaign's whole window: a
            # 'morning' plan that dials at 19:00 is not a morning plan.
            dcfg = _band(kind, dcfg)
            # An empty band is not a closed one, and it is not caught by the
            # guard below: on a past or future date `_floor_min` returns None and
            # nothing else looks at the clock.
            if dcfg.start_min >= dcfg.end_min:
                return {**out, "status": "window_closed",
                        "detail": f"this campaign's window has no "
                                  f"{WAVE_LABEL[kind]} band"}
            floor = _floor_min(now_ist(), day, dcfg)
            if floor is not None and floor >= dcfg.end_min:
                return {**out, "status": "window_closed",
                        "detail": f"the {hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)} "
                                  f"{WAVE_LABEL[kind]} band has closed"}
            run_id = _write_run(conn, campaign, day, kind, cfg["version"], pairs, red, dcfg,
                                evaluated=len(leads), note=f"{WAVE_LABEL[kind]} plan, awaiting "
                                                           f"approval", floor_min=floor)
        except HTTPException as exc:
            # 409 = this wave already went out today. That IS the "already ran"
            # guard; preparing again is a no-op rather than a second plan.
            return {**out, "status": "already_ran", "detail": str(exc.detail)}
        except Exception as exc:                 # noqa: BLE001 — one campaign, not the day
            return {**out, "status": "error", "detail": f"{type(exc).__name__}: {exc}"[:200]}

        ready = conn.execute("SELECT COUNT(*) AS n FROM plan_items WHERE run_id=?",
                             (run_id,)).fetchone()["n"]
        _note(conn, campaign_id, f"{day} {WAVE_LABEL[kind]}: {ready} ready, awaiting approval")
    return {**out, "status": "prepared", "run_id": run_id, "ready": ready}


@router.post("/api/day/prepare")
def post_prepare(body: PrepareBody = Body(default_factory=PrepareBody)) -> dict[str, Any]:
    """Build today's plan now. Writes `planned` runs and dials nothing."""
    return prepare_day(_parse_day(body.date), body.kind, body.resync, body.agent_id)


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------

@router.post("/api/day/approve")
def approve_day(body: ApproveBody = Body(default_factory=ApproveBody)) -> dict[str, Any]:
    """Dial the day. The only path in this module that reaches Formi.

    Re-plans each campaign from the current minute with the buckets the operator
    ticked, then commits it. Re-planning is what makes a late approval behave the
    way the client asked: the plan is repacked into the hours that are left, so
    what fits goes out and what does not is simply not dialled today.

    Under DRY_RUN nothing leaves the process; the runs are marked simulated and
    the dial log records exactly what would have been sent.
    """
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}, got {body.kind!r}")
    day = _parse_day(body.date)
    wanted = set(body.campaign_ids)
    buckets = list(body.buckets)

    results: list[dict[str, Any]] = []
    with session() as conn:
        where, params = _armed(body.agent_id)
        campaigns = conn.execute(
            f"SELECT * FROM campaigns WHERE {where} ORDER BY id", params).fetchall()
        for campaign in campaigns:
            if wanted and campaign["id"] not in wanted:
                continue
            results.append(_approve_one(conn, campaign, day, body.kind, buckets))

    posted = sum(r.get("posted", 0) for r in results)
    return {"date": day.isoformat(), "kind": body.kind, "wave": WAVE_LABEL[body.kind],
            "dry_run": dry_run(), "buckets": buckets or "all",
            "approved": sum(1 for r in results if r["status"] == "approved"),
            "posted": posted, "failed": sum(r.get("failed", 0) for r in results),
            "not_dialled": sum(r.get("expired", 0) + r.get("dropped", 0) for r in results),
            "campaigns": results}


def _approve_one(conn: sqlite3.Connection, campaign: sqlite3.Row, day: date, kind: str,
                 buckets: list[str]) -> dict[str, Any]:
    """One campaign: re-plan from now with the ticked buckets, then commit.

    Every outcome that is not a clean dial leaves a note on the campaign. The
    operator reported on 13 Sep 2026 that a campaign they had ticked could fail
    to start and say nothing: the reason was returned to the browser, shown in a
    toast that summed the whole day into one line, and then dropped when the
    modal closed. For `window_closed` and `error` there is no `runs` row either,
    so at that point the reason was gone for good. `autopilot_note` is where the
    console already explains itself, so it is where this belongs too.
    """
    from .autopilot import _note                          # noqa: PLC0415 — import cycle

    out: dict[str, Any] = {"campaign_id": campaign["id"], "name": campaign["name"]}

    def failing(status: str, detail: str = "", **extra: Any) -> dict[str, Any]:
        """Record why this campaign did not dial, then report it.

        `already_committed` is the exception: that campaign DID dial, on an
        earlier approve, and it already has the note saying how it went. Marking
        it "NOT dialled" would overwrite a true record with a false one.
        """
        if status != "already_committed":
            _note(conn, campaign["id"],
                  f"{day} {kind}: NOT dialled — {status}" + (f": {detail}" if detail else ""))
        return {**out, "status": status, **({"detail": detail} if detail else {}), **extra}

    run = conn.execute(
        "SELECT * FROM runs WHERE campaign_id=? AND run_date=? AND kind=? ORDER BY id DESC",
        (campaign["id"], day.isoformat(), kind)).fetchone()
    if run is None:
        return failing("not_prepared")
    if run["status"] != "planned":
        # Already committed or paused. Approving twice must not dial twice.
        # `run_id` goes back so the caller can still retry the calls that this
        # run had refused, which is a different act from approving it again.
        return failing("already_" + run["status"], run_id=run["id"])

    try:
        cfg, red, dcfg, now, leads, pairs = _evaluate(conn, campaign, day)
        dcfg = _band(kind, dcfg)
        if dcfg.start_min >= dcfg.end_min:
            return failing("window_closed", run_id=run["id"],
                           detail=f"this campaign's window has no {WAVE_LABEL[kind]} band")
        floor = _floor_min(now, day, dcfg)
        if floor is not None and floor >= dcfg.end_min:
            return failing("window_closed", run_id=run["id"],
                           detail=f"the {hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)} "
                                  f"{WAVE_LABEL[kind]} band has closed "
                                  f"(it is {now_ist().strftime('%H:%M')})")
        note = f"approved {now_ist().strftime('%H:%M')}"
        if buckets:
            note += " buckets=" + ",".join(buckets)
        if floor is not None:
            note += f" from={hhmm(floor)}"
        run_id = _write_run(conn, campaign, day, kind, cfg["version"], pairs, red, dcfg,
                            evaluated=len(leads), note=note, floor_min=floor, buckets=buckets)
        fresh = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not fresh["slots"]:
            return failing("nothing_to_dial", run_id=run_id, posted=0)
        result = _commit(conn, fresh, campaign, "approving", source="approve")
    except HTTPException as exc:
        return failing("not_dialled", detail=str(exc.detail))
    except Exception as exc:                     # noqa: BLE001 — one campaign, not the day
        return failing("error", detail=f"{type(exc).__name__}: {exc}"[:200])

    posted, failed = result["counts"]["posted"], result["counts"]["failed"]
    # A partial is not a failure, but it is not silence either: those leads were
    # refused by Formi and will not be called unless somebody sends them again.
    _note(conn, campaign["id"],
          f"{day} {kind}: dialled {posted}, {failed} refused by Formi" if failed
          else f"{day} {kind}: dialled {posted}")
    return {**out, "status": "approved", "run_id": result["id"],
            "posted": posted, "failed": failed,
            "dropped": result["counts"]["dropped"], "expired": result["expired"],
            "simulated": result["simulated"], "run": _run_json(
                conn.execute("SELECT * FROM runs WHERE id=?", (result["id"],)).fetchone())}
