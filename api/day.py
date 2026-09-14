"""The daily approval gate: one plan for the day, one decision, one screen.

Nothing in this console dials on its own. A scheduled pass PREPARES a plan for
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
import sqlite3
import threading
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from engine.dispatcher import (
    DEFAULT_RED_PRIORITY, WINDOW_CEIL, WINDOW_FLOOR, hhmm, parse_hhmm, red_rank,
)

from .db import dry_run, now_ist, now_iso, session
from .routes_core import (
    _campaign_json, _commit, _earliest_dialable, _evaluate, _floor_min, _parse_day,
    _run_json, _write_run,
)

router = APIRouter()
log = logging.getLogger("redial.day")

# The two passes a day is made of. The client's rule is "second call only if the
# first is not answered", so the recall pass is built AFTER a re-sync and only
# reaches leads whose last call still says nobody was reached. Both are prepared,
# neither dials — each needs its own approval.
#
# WHICH LEADS, NOT WHICH HOURS. Until 14 Sep 2026 these were a morning and an
# afternoon, split at a 13:30 clock boundary: the first pass could only dial
# 09:00-13:30 and the second only 13:30-20:00. That band is gone. It was never
# what made the second call correct -- on 12 Sep the engine gave 200 leads a
# second call out of 3,553, entirely through `wants_second_call` and
# `same_day_gap_hours`, and the boundary contributed nothing but a ceiling on
# when each pass could dial. Both passes now dial anywhere in the campaign's own
# window, and the recall pass holds exactly the leads the PREVIOUS CALL earned it
# for: no pick, hung up, under `short_call_seconds`, or a disposition named in
# `second_call_dispositions`, and at least `same_day_gap_hours` after that call.
# See engine/red_engine.py's `wants_second_call`.
#
# The stored strings are unchanged so the `runs` rows written under the old names
# are still this day's rows and still readable.
FIRST_PASS, RECALL_PASS = "auto", "auto_pm"
KINDS = (FIRST_PASS, RECALL_PASS)

PASS_LABEL = {FIRST_PASS: "first pass", RECALL_PASS: "recall pass"}

def kind_for_campaign(conn: sqlite3.Connection, campaign_id: int, day: date) -> str:
    """Which pass a fresh plan for this campaign belongs to: the day's first, or
    the recall after it.

    Asked of the CALL LOG, not of the clock. A campaign that has not put a call
    out today cannot be on its recall pass however late in the day it is planned,
    and one that dialled at 09:10 is on its recall pass at 09:40 -- the engine,
    not this function, then decides whether any individual lead has earned that
    second call and whether `same_day_gap_hours` has elapsed.

    `posted > 0` rather than the run's status, so a first pass that was dialled
    and later paused still counts as having happened. Its calls are on Formi's
    clock; pausing the run does not take them back.

    On a date that is not today there is nothing posted, so a back-dated or
    forward-dated plan is always the first pass. That is right: the recall pass
    only means anything against calls that exist.
    """
    row = conn.execute(
        "SELECT 1 FROM runs WHERE campaign_id=? AND run_date=? AND kind=? AND posted>0 "
        "LIMIT 1", (campaign_id, day.isoformat(), FIRST_PASS)).fetchone()
    return RECALL_PASS if row else FIRST_PASS


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
    kind: str = FIRST_PASS
    # Off by default: the hourly sync timer already keeps the local copy fresh,
    # and a warehouse round-trip per campaign turns a button press into minutes.
    # The scheduled pass sets it — the recall pass is worthless without it, as it
    # is the re-sync that tells it how the day's earlier calls actually went.
    resync: bool = False
    # Narrows the pass to one agent — one language. None = every armed campaign,
    # which is what the scheduled passes use.
    agent_id: Optional[int] = None


class ApproveBody(BaseModel):
    date: Optional[str] = None
    kind: str = FIRST_PASS
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
                floor: int, today: bool) -> dict[str, Any]:
    """The day's dialling window and its ceiling, from every armed campaign.

    `dial_window` and `max_per_minute` are per-campaign, and each is one PUT away
    from being edited on its own, so no single campaign's config is the day's.
    Reading the first armed one and labelling it "the window" showed nothing while
    every campaign happened to agree — all 69 land on 09:00-20:00 once
    `with_defaults` retires the stale 09:30-19:00 snapshot — and would have named
    a close time the other 68 do not keep the moment one was narrowed.

    The window reported is the ENVELOPE of the campaigns' own windows: no call
    goes out before its start or after its end, whichever campaign places it.
    `varies` says the campaigns do not agree, so the screen can say so instead of
    implying a shared close. Both passes get the same envelope -- neither owns
    half the day any more.

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
        start = parse_hhmm(window.get("start", DEFAULT_WINDOW["start"]))
        end = parse_hhmm(window.get("end", DEFAULT_WINDOW["end"]))
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
    """The `planned`/`committed` run per campaign for this day and pass."""
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


def _dialled_today(conn: sqlite3.Connection, day: date, kind: str,
                   agent_id: Optional[int] = None) -> dict[str, int]:
    """What the log says THIS PASS actually did, by verify state. 'Did it run?'.

    Scoped like every other field on this page -- and that has to include the
    pass. These counts are rendered inside the proof card, under the pass's own
    title and eyebrow: a recall card reading "2,080 dialled" when the recall pass
    posted 300 is the first pass's work presented as this one's proof, which is
    the same confusion agent scoping exists to remove.

    Both narrowings go through the run rather than through `dial_log`'s own
    columns. `run_id` says which pass a call belonged to as a fact -- the run it
    was dialled from. There is nothing else that could say it: both passes now
    dial the campaign's whole window, so the hour a call was booked for carries
    no information about which pass booked it.
    The agent comes off the campaign for the plainer reason that
    `dial_log.agent_id` is nullable, so scoping on it drops every row written
    before that column was populated.

    A row with no run is not this pass's: a test call belongs to no run and is
    nobody's proof that the day ran.
    """
    where = ["substr(d.scheduled_time,1,10)=?", "r.kind=?"]
    params: list[Any] = [day.isoformat(), kind]
    if agent_id is not None:
        where.append("c.agent_id=?")
        params.append(agent_id)
    rows = conn.execute(
        "SELECT d.verified AS verified, COUNT(*) AS n FROM dial_log d "
        "JOIN runs r ON r.id=d.run_id JOIN campaigns c ON c.id=r.campaign_id "
        f"WHERE {' AND '.join(where)} GROUP BY d.verified", params).fetchall()
    return {r["verified"]: r["n"] for r in rows}


def _stranded(conn: sqlite3.Connection, day: date,
              agent_id: Optional[int] = None) -> tuple[list[dict[str, Any]], int]:
    """Runs prepared on an EARLIER day and never dialled, and how many leads that is.

    A run stays `planned` until somebody approves it. On 12 Sep 2026 eight
    campaigns holding 491 slots sat like that until the day ended, and no screen
    in this console said so -- the day view only ever looked at the date it was
    asked about. Those leads were not dropped, re-queued or reported; they simply
    did not get called.

    The second return value counts PEOPLE, not slots. An unapproved plan is built
    again for the same leads the next day, so summing `slots` over the runs
    multiplies one backlog by the days it sat: 544 leads over a fortnight read as
    "7,616 calls never dialled". The rows keep their own `slots` -- that is what
    each run holds, and it is true per row -- but the headline is the distinct
    count, because 544 people is the thing an operator can act on.

    Bounded to the last STRANDED_DAYS days.
    """
    # "Earlier" means earlier than TODAY, not earlier than the day asked about.
    # The date input has no upper bound, so tomorrow is one click away -- and
    # bounded by the requested day, today's own `planned` run would be
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
        f"SELECT r.id, r.campaign_id, c.name, r.run_date, r.kind, r.slots "
        f"FROM runs r JOIN campaigns c ON c.id=r.campaign_id "
        f"WHERE r.status='planned' AND r.run_date < ? AND r.run_date >= ? "
        f"AND r.slots > 0 "
        f"AND r.campaign_id IN (SELECT id FROM campaigns WHERE {where}) "
        f"ORDER BY r.run_date DESC, r.campaign_id", (upper, since, *params)).fetchall()
    leads = 0
    if rows:
        marks = ",".join("?" * len(rows))
        leads = int(conn.execute(
            f"SELECT COUNT(DISTINCT lead_uuid) FROM plan_items WHERE run_id IN ({marks})",
            [r["id"] for r in rows]).fetchone()[0])
    return [{"campaign_id": r["campaign_id"], "name": r["name"], "run_date": r["run_date"],
             "kind": r["kind"], "slots": r["slots"]} for r in rows], leads


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


def _spread(conn: sqlite3.Connection, runs: dict[int, sqlite3.Row]) -> dict[str, Any]:
    """Which hours this pass's calls actually went onto, against the hours allowed.

    This is the honest answer to "is it scheduling properly": a count per hour,
    beside the permitted dialling hours, so a pass that piled every call into one
    hour or ran up against the close is visible at a glance instead of needing a
    database query.

    `band` is the permitted dialling hours, the same for both passes. It used to
    be the pass's half of a day split at 13:30; there is no such split any more,
    so the only line worth drawing the hours against is the one no call may cross.

    Only `posted` and `simulated` items count -- the two statuses that mean a
    call really went onto Formi's clock (or would have, outside DRY_RUN). Every
    other status is left out, and for the same reason: a `planned` slot has not
    been scheduled anywhere yet, and `failed`, `expired` and `skipped` ones never
    were. Counting any of them would put an hour on this chart that nobody dialled.

    `runs` arrives already narrowed to the scope of the page (`_plan_rows` over
    the armed campaigns, get_day), so there is no agent filter here -- a second
    one would be a second chance to disagree with the rest of the answer.
    """
    band = {"start": hhmm(max(WINDOW_FLOOR, parse_hhmm(DEFAULT_WINDOW["start"]))),
            "end": hhmm(min(WINDOW_CEIL, parse_hhmm(DEFAULT_WINDOW["end"])))}
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
def get_day(date: Optional[str] = Query(None), kind: str = Query(FIRST_PASS),
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
        log = _dialled_today(conn, day, kind, agent_id)
        stranded_runs, stranded_leads = _stranded(conn, day, agent_id)
        facts = _plan_facts(conn, runs, [c["id"] for c in campaigns])
        spread = _spread(conn, runs)

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
    elif "planned" in statuses:
        # Prepared, but the plans came back empty -- the recall pass after a
        # first pass that booked every lead reaching it. `approved` was the answer
        # here until 14 Sep 2026, and it is a lie twice over: nobody approved
        # anything, and the screen that believed it offered neither Build nor
        # Approve, so the leads a later re-sync pulled in could not be planned at
        # all. `nothing_to_dial` is what `_approve_one` already calls this exact
        # state for one campaign; the day says it in the same words.
        status = "nothing_to_dial"
    elif "not_prepared" in statuses:
        # The same lie as the branch above, one state over: a day of eight
        # committed runs and one campaign that never built answered `approved`,
        # and the approved hero offers only "Open the call log" -- no Build
        # anywhere on the panel, and the picker's Save is greyed out because that
        # campaign is already armed. Its leads had no route onto the clock at all.
        # `_prepare_one` answers `already_ran` for the committed ones and writes
        # them no note, so building from here costs them nothing.
        status = "part_prepared"
    else:
        status = "approved"

    # A ceiling, not a promise: minutes left in each campaign's own window times
    # how many calls a minute it may hold, summed. Approve re-plans, so the real
    # number is decided then — but an operator opening this at 18:00 needs to see
    # that the day no longer fits BEFORE they approve, not in the drop count
    # afterwards.
    first_free = _earliest_dialable(now)
    span = _day_window(configs, {c["id"]: c["ready"] for c in listed},
                       first_free.hour * 60 + first_free.minute, today)

    return {
        "date": day.isoformat(), "kind": kind, "pass_label": PASS_LABEL[kind],
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
        # How many distinct leads those runs hold. Summing the rows' `slots`
        # counts the same lead once per day it waited; this is the headline.
        "stranded_leads": stranded_leads,
        # Which hours the calls actually landed in, against the band approved.
        "spread": spread,
    }


# ---------------------------------------------------------------------------
# Preparing
# ---------------------------------------------------------------------------

def prepare_day(day: Optional[date] = None, kind: str = FIRST_PASS,
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
        # earlier today must drop out of this pass, and its queued calls come off
        # Formi's clock. One warehouse read for the whole pass, not one per
        # campaign. A warehouse we cannot reach is reported, never fatal — the
        # per-campaign lead re-sync below fails loudly enough on its own.
        from .autopilot import _resync_status         # noqa: PLC0415 — avoids a cycle
        try:
            stopped = _resync_status(day, agent_id)
        except Exception as exc:                     # noqa: BLE001 — reported, not swallowed
            log.warning("could not re-read campaign status before the %s: %s",
                        PASS_LABEL.get(kind, kind), exc)

    with session() as conn:
        where, params = _armed(agent_id)
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM campaigns WHERE {where} ORDER BY id", params)]

    results = [_prepare_one(campaign_id, day, kind, resync) for campaign_id in ids]
    return {"date": day.isoformat(), "kind": kind, "pass_label": PASS_LABEL[kind],
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
            # counters would re-offer leads that already answered earlier today.
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
            # Both passes get the campaign's whole window. The pass is decided by
            # the leads' previous call, not by the hour, so there is nothing left
            # here to narrow -- the engine has already withheld every lead that
            # has not earned a second call today.
            floor = _floor_min(now_ist(), day, dcfg)
            if floor is not None and floor >= dcfg.end_min:
                return {**out, "status": "window_closed",
                        "detail": f"the dial window {hhmm(dcfg.start_min)}-"
                                  f"{hhmm(dcfg.end_min)} has closed"}
            run_id = _write_run(conn, campaign, day, kind, cfg["version"], pairs, red, dcfg,
                                evaluated=len(leads), note=f"{PASS_LABEL[kind]}, awaiting "
                                                           f"approval", floor_min=floor)
        except HTTPException as exc:
            # 409 = this pass already went out today. That IS the "already ran"
            # guard; preparing again is a no-op rather than a second plan.
            return {**out, "status": "already_ran", "detail": str(exc.detail)}
        except Exception as exc:                 # noqa: BLE001 — one campaign, not the day
            return {**out, "status": "error", "detail": f"{type(exc).__name__}: {exc}"[:200]}

        ready = conn.execute("SELECT COUNT(*) AS n FROM plan_items WHERE run_id=?",
                             (run_id,)).fetchone()["n"]
        _note(conn, campaign_id, f"{day} {PASS_LABEL[kind]}: {ready} ready, awaiting approval")
    return {**out, "status": "prepared", "run_id": run_id, "ready": ready}


@router.post("/api/day/prepare")
def post_prepare(body: PrepareBody = Body(default_factory=PrepareBody)) -> dict[str, Any]:
    """Build today's plan now. Writes `planned` runs and dials nothing."""
    return prepare_day(_parse_day(body.date), body.kind, body.resync, body.agent_id)


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------

def _day_result(results: list[dict[str, Any]], day: str, kind: str,
                buckets: list[str]) -> dict[str, Any]:
    """A day's dial, added up from its per-campaign rows. The only shape.

    `/api/day/approve` and the background walk both finish holding a list of
    `_approve_one` rows, and the console renders whichever it gets through the
    same component. Two places summing the same rows is two places to sum them
    differently, so they sum them here.
    """
    return {"date": day, "kind": kind, "pass_label": PASS_LABEL.get(kind, ""),
            "dry_run": dry_run(), "buckets": buckets or "all",
            "approved": sum(1 for r in results if r["status"] == "approved"),
            "posted": sum(r.get("posted", 0) for r in results),
            "failed": sum(r.get("failed", 0) for r in results),
            # `dropped` is on every row now, not just the approved ones: a
            # campaign that never reached `_commit` still held leads, and scoring
            # it zero is what made a pass of 12 closed windows read "0 scheduled ·
            # 0 not scheduled" over 2,000 leads. See `_unspent`.
            #
            # `dropped` is already the whole of it. `_commit` adds the slots it
            # retired for being in the past to the count `_write_run` left behind
            # for the leads `max_per_run` shed, so the run row carries one number
            # meaning "did not dial". `expired` is that number's BREAKDOWN, not
            # extra to it: adding `expired` back on top charged every retired
            # slot twice, and a pass with 340 stale slots told the operator 680
            # leads were not scheduled -- inflated by exactly the commonest
            # reason a slot does not go out.
            "not_dialled": sum(r.get("dropped", 0) for r in results),
            "campaigns": results}


# ---------------------------------------------------------------------------
# Dialling the day in the background
# ---------------------------------------------------------------------------

# One campaign's approve is one long request: campaign 1744 took 11:09:09 to
# 11:19:05 on 14 Sep 2026 to place 1,364 calls, and uvicorn logged no access
# line for it at all -- nobody was still on the other end of the socket when it
# answered. The browser had split the day into one request per campaign already,
# which is what stopped a whole day riding on one connection; it does not help
# when a SINGLE campaign is a ten-minute request. The queue lived in the page,
# so when that request died the remaining twenty-one campaigns were never asked
# for. The operator pressed Dial on twenty-two campaigns and one dialled.
#
# So the walk moves server-side, exactly as `/api/sync` already does it: plain
# module state and a daemon thread. POST starts it and returns at once, GET
# polls it. Nothing now depends on the browser staying on the page -- closing
# the modal, a reload, a laptop lid, none of them can stop the day mid-way.
#
# Module state rather than a table for the same reason `/api/sync` uses it: one
# process owns this console, the answer is only interesting while the walk runs,
# and a restart losing it is correct -- a restart also kills the thread.
_dial_lock = threading.Lock()


def _idle_dial() -> dict[str, Any]:
    return {"running": False, "date": "", "kind": "", "buckets": [], "agent_id": None,
            "total": 0, "done": 0, "current": None, "stopped": False,
            "results": [], "started_at": "", "finished_at": ""}


_dial_state: dict[str, Any] = _idle_dial()


def _dial_walk(day: date, kind: str, buckets: list[str], campaign_ids: list[int]) -> None:
    """Approve each campaign in turn. Runs on the thread, never in a request."""
    try:
        for campaign_id in campaign_ids:
            if _dial_state["stopped"]:
                break
            # Held outside the try so the error row below can still say which
            # campaign it was -- an unnamed failure in a list of twenty-two is
            # a number the operator has to go and look up.
            name = ""
            try:
                # A connection per campaign, not one held for the whole walk: an
                # approve can run ten minutes, and a writer holding SQLite open
                # that long is every other request in the console waiting on it.
                with session() as conn:
                    where, params = _armed(_dial_state["agent_id"])
                    campaign = conn.execute(
                        f"SELECT * FROM campaigns WHERE id=? AND {where}",
                        (campaign_id, *params)).fetchone()
                    if campaign is None:
                        # Disarmed since the walk began -- a 15:00 unattended
                        # pass, a hide, a Formi pause picked up by a resync.
                        # `approve_day` skips such a campaign in silence, and a
                        # campaign that vanishes from its own result is the one
                        # thing this screen must never do.
                        _dial_state["results"].append(
                            {"campaign_id": campaign_id, "name": "",
                             "status": "no_result"})
                        continue
                    name = campaign["name"]
                    _dial_state["current"] = {"campaign_id": campaign_id,
                                              "name": name}
                    result = _approve_one(conn, campaign, day, kind, buckets)
                _dial_state["results"].append(result)
            except Exception as exc:             # noqa: BLE001 — one campaign, not the day
                log.exception("dial walk failed on campaign %s", campaign_id)
                _dial_state["results"].append(
                    {"campaign_id": campaign_id, "name": name, "status": "error",
                     "detail": f"{type(exc).__name__}: {exc}"[:200]})
            finally:
                _dial_state["done"] += 1
                _dial_state["current"] = None
    finally:
        _dial_state["running"] = False
        _dial_state["finished_at"] = now_iso()


@router.post("/api/day/dial")
def start_dial(body: ApproveBody = Body(default_factory=ApproveBody)) -> dict[str, Any]:
    """Dial the day in the background. Returns at once; poll GET /api/day/dial.

    Same body as `/api/day/approve` and the same work, minus the wait. Pressing
    it twice is not an error and does not dial twice: the walk already in flight
    is returned unchanged, which is what a button pressed again because nothing
    visibly happened should do.
    """
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}, got {body.kind!r}")
    day = _parse_day(body.date)
    wanted = set(body.campaign_ids)
    buckets = list(body.buckets)

    with _dial_lock:
        if _dial_state["running"]:
            return dial_status()
        with session() as conn:
            where, params = _armed(body.agent_id)
            campaign_ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM campaigns WHERE {where} ORDER BY id", params)
                if not wanted or r["id"] in wanted]
        _dial_state.update(_idle_dial())
        _dial_state.update(running=True, date=day.isoformat(), kind=body.kind,
                           buckets=buckets, agent_id=body.agent_id,
                           total=len(campaign_ids), started_at=now_iso())
    threading.Thread(target=_dial_walk, args=(day, body.kind, buckets, campaign_ids),
                     daemon=True).start()
    return dial_status()


@router.get("/api/day/dial")
def dial_status() -> dict[str, Any]:
    """Where the walk has got to. Safe to poll from a screen that is open all day.

    `result` is the walk so far in the same shape `/api/day/approve` answers, so
    the console renders a finished walk through the component it already has and
    adds nothing up itself.
    """
    state = dict(_dial_state)
    state["dry_run"] = dry_run()
    state["result"] = _day_result(list(state["results"]), state["date"],
                                  state["kind"], list(state["buckets"]))
    return state


@router.post("/api/day/dial/stop")
def stop_dial() -> dict[str, Any]:
    """Stop between campaigns. The one in flight finishes -- its calls are posted.

    A campaign the walk never reached was never posted, so its plan items are
    still `planned` and dialling the day again sends exactly those.
    """
    _dial_state["stopped"] = True
    return dial_status()


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

    return _day_result(results, day.isoformat(), body.kind, buckets)


def _unspent(run: Optional[sqlite3.Row]) -> int:
    """Leads this run was owed a call for and did not send.

    Only a run still `planned` has any: its `slots` plan items are all undialled
    and its `dropped` is the leads the plan itself shed, which is the same
    "did not dial" `_commit` reports on the approved path. A run in any other
    state was acted on by an earlier approve that reported its own numbers, and a
    run that does not exist holds no leads to count -- an unprepared campaign is
    visible as `ready` on the day view, not here.
    """
    if run is None or run["status"] != "planned":
        return 0
    return int(run["slots"]) + int(run["dropped"])


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
        # `dropped` is the day's "not scheduled" number, and only the approved
        # return ever carried one -- so a 19:45 pass where every campaign answers
        # `window_closed` told the operator 0 leads had missed out over 2,000 that
        # had. Wrong in the reassuring direction, which is the worst of the two.
        extra.setdefault("dropped", _unspent(plan))
        return {**out, "status": status, **({"detail": detail} if detail else {}), **extra}

    run = conn.execute(
        "SELECT * FROM runs WHERE campaign_id=? AND run_date=? AND kind=? ORDER BY id DESC",
        (campaign["id"], day.isoformat(), kind)).fetchone()
    # The plan `failing` should answer for. `_write_run` below replaces the row,
    # and a failure after that point is about the FRESH plan, not the shelved one.
    plan = run
    if run is None:
        return failing("not_prepared")
    if run["status"] != "planned":
        # Already committed or paused. Approving twice must not dial twice.
        # `run_id` goes back so the caller can still retry the calls that this
        # run had refused, which is a different act from approving it again.
        return failing("already_" + run["status"], run_id=run["id"])

    try:
        cfg, red, dcfg, now, leads, pairs = _evaluate(conn, campaign, day)
        floor = _floor_min(now, day, dcfg)
        if floor is not None and floor >= dcfg.end_min:
            return failing("window_closed", run_id=run["id"],
                           detail=f"the dial window {hhmm(dcfg.start_min)}-"
                                  f"{hhmm(dcfg.end_min)} has closed "
                                  f"(it is {now_ist().strftime('%H:%M')})")
        note = f"approved {now_ist().strftime('%H:%M')}"
        if buckets:
            note += " buckets=" + ",".join(buckets)
        if floor is not None:
            note += f" from={hhmm(floor)}"
        run_id = _write_run(conn, campaign, day, kind, cfg["version"], pairs, red, dcfg,
                            evaluated=len(leads), note=note, floor_min=floor, buckets=buckets)
        fresh = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        plan = fresh
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
