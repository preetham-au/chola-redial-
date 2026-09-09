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
import sqlite3
from dataclasses import replace
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from engine.dispatcher import DEFAULT_RED_PRIORITY, hhmm, parse_hhmm, red_rank

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
MORNING, AFTERNOON = "auto", "auto_pm"
KINDS = (MORNING, AFTERNOON)

WAVE_LABEL = {MORNING: "morning", AFTERNOON: "afternoon"}

# Who is in today's plan. One string because the question is asked three times —
# the day view, the prepare pass and the approve — and a campaign that answers
# yes to one but not the others is a campaign that gets planned off-screen or
# dialled after it was taken out. Widening the roster means editing this line.
ARMED = "autopilot=1 AND enabled=1 AND paused=0 AND hidden=0"


class PrepareBody(BaseModel):
    date: Optional[str] = None
    kind: str = MORNING
    # Off by default: the hourly sync timer already keeps the local copy fresh,
    # and a warehouse round-trip per campaign turns a button press into minutes.
    # The scheduled pass sets it — the afternoon wave is worthless without it.
    resync: bool = False


class ApproveBody(BaseModel):
    date: Optional[str] = None
    kind: str = MORNING
    # Which buckets to actually call. Empty = every bucket in the plan. The rest
    # are not dialled today; they come back in tomorrow's plan.
    buckets: list[str] = Field(default_factory=list)
    # Empty = every campaign with a plan waiting. Named ids narrow it.
    campaign_ids: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# RED bands
# ---------------------------------------------------------------------------

def _evaluate_wave(conn: sqlite3.Connection, campaign: sqlite3.Row, day: date):
    """`_evaluate`, capped at one call per lead per wave.

    "2nd call only if the 1st is not answered" — so no wave may book both calls
    of the day up front. One slot per lead; the afternoon call is earned in the
    afternoon, by a lead whose re-synced disposition still says nobody picked up.
    A connected lead is CALLBACK class by then and `decide` drops it before it
    ever reaches the dispatcher. Prepare AND approve both go through here, so an
    approval cannot re-introduce the second slot the plan deliberately left out.
    """
    cfg, red, dcfg, now, leads, pairs = _evaluate(conn, campaign, day)
    return cfg, replace(red, calls_per_day_cap=1), dcfg, now, leads, pairs


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

    The window reported is the ENVELOPE: no call goes out before its start or
    after its end, whichever campaign places it. `varies` says the campaigns do
    not agree, so the screen can say so instead of implying a shared close.

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


def _dialled_today(conn: sqlite3.Connection, day: date) -> dict[str, int]:
    """What the log says actually happened, by verify state. Answers 'did it run?'."""
    rows = conn.execute(
        "SELECT verified, COUNT(*) AS n FROM dial_log WHERE substr(scheduled_time,1,10)=? "
        "GROUP BY verified", (day.isoformat(),)).fetchall()
    return {r["verified"]: r["n"] for r in rows}


@router.get("/api/day")
def get_day(date: Optional[str] = Query(None), kind: str = Query(MORNING)) -> dict[str, Any]:
    """The whole day on one page: what is ready, in what order, and what it did.

    Cheap by construction — two GROUP BYs over rows this console already wrote.
    It never re-runs the engine, so it can be polled by a screen that is open all
    day without costing a warehouse query.
    """
    if kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {list(KINDS)}, got {kind!r}")
    day = _parse_day(date)
    now = now_ist()
    today = day == now.date()

    with session() as conn:
        campaigns = conn.execute(f"SELECT * FROM campaigns WHERE {ARMED} ORDER BY id").fetchall()
        # Stopped campaigns are still shown: "why is nothing happening for X" is
        # the question this screen exists to answer, and an empty list answers it
        # with silence. Keyed on the LATCH as well as the switch, because a stop
        # disarms `autopilot` and moves the fact that it was armed into
        # `autopilot_latched` — reading only the switch loses the campaign the
        # operator is looking for.
        stopped = conn.execute(
            "SELECT * FROM campaigns WHERE (autopilot=1 OR autopilot_latched=1) "
            "AND (paused=1 OR enabled=0) ORDER BY id").fetchall()
        # A hidden campaign is gone from every list in the console, and hiding
        # deliberately leaves today's queued calls on Formi's clock. Those two
        # together would put calls on the wire with nothing on any screen saying
        # so, which is the one thing this console must never do. It appears here
        # while — and only while — it still has calls to place, then drops off.
        dialling_while_hidden = conn.execute(
            "SELECT DISTINCT c.* FROM campaigns c "
            "JOIN runs r ON r.campaign_id=c.id JOIN plan_items i ON i.run_id=r.id "
            "WHERE c.hidden=1 AND r.run_date=? AND r.status='committed' "
            "AND i.status IN ('posted','simulated') AND i.scheduled_time > ? "
            "ORDER BY c.id",
            (day.isoformat(), now.strftime("%Y-%m-%dT%H:%M:00"))).fetchall()
        runs = _plan_rows(conn, [c["id"] for c in campaigns], day, kind)
        counts = _slot_counts(conn, [r["id"] for r in runs.values()])
        log = _dialled_today(conn, day)

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
                       first_free.hour * 60 + first_free.minute, today)

    return {
        "date": day.isoformat(), "kind": kind, "wave": WAVE_LABEL[kind],
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
    }


# ---------------------------------------------------------------------------
# Preparing
# ---------------------------------------------------------------------------

def prepare_day(day: Optional[date] = None, kind: str = MORNING,
                resync: bool = False) -> dict[str, Any]:
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
            stopped = _resync_status(day)
        except Exception as exc:                     # noqa: BLE001 — reported, not swallowed
            log.warning("could not re-read campaign status before the %s wave: %s", kind, exc)

    with session() as conn:
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM campaigns WHERE {ARMED} ORDER BY id")]

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
            cfg, red, dcfg, _now, leads, pairs = _evaluate_wave(conn, campaign, day)
            floor = _floor_min(now_ist(), day, dcfg)
            if floor is not None and floor >= dcfg.end_min:
                return {**out, "status": "window_closed",
                        "detail": f"the {hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)} window "
                                  f"has closed"}
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
    return prepare_day(_parse_day(body.date), body.kind, body.resync)


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
        campaigns = conn.execute(f"SELECT * FROM campaigns WHERE {ARMED} ORDER BY id").fetchall()
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
    """One campaign: re-plan from now with the ticked buckets, then commit."""
    out: dict[str, Any] = {"campaign_id": campaign["id"], "name": campaign["name"]}
    run = conn.execute(
        "SELECT * FROM runs WHERE campaign_id=? AND run_date=? AND kind=? ORDER BY id DESC",
        (campaign["id"], day.isoformat(), kind)).fetchone()
    if run is None:
        return {**out, "status": "not_prepared"}
    if run["status"] != "planned":
        # Already committed or paused. Approving twice must not dial twice.
        return {**out, "status": "already_" + run["status"], "run_id": run["id"]}

    try:
        cfg, red, dcfg, now, leads, pairs = _evaluate_wave(conn, campaign, day)
        floor = _floor_min(now, day, dcfg)
        if floor is not None and floor >= dcfg.end_min:
            return {**out, "status": "window_closed", "run_id": run["id"],
                    "detail": f"the {hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)} window has "
                              f"closed (it is {now_ist().strftime('%H:%M')})"}
        note = f"approved {now_ist().strftime('%H:%M')}"
        if buckets:
            note += " buckets=" + ",".join(buckets)
        if floor is not None:
            note += f" from={hhmm(floor)}"
        run_id = _write_run(conn, campaign, day, kind, cfg["version"], pairs, red, dcfg,
                            evaluated=len(leads), note=note, floor_min=floor, buckets=buckets)
        fresh = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not fresh["slots"]:
            return {**out, "status": "nothing_to_dial", "run_id": run_id, "posted": 0}
        result = _commit(conn, fresh, campaign, "approving", source="approve")
    except HTTPException as exc:
        return {**out, "status": "not_dialled", "detail": str(exc.detail)}
    except Exception as exc:                     # noqa: BLE001 — one campaign, not the day
        return {**out, "status": "error", "detail": f"{type(exc).__name__}: {exc}"[:200]}

    return {**out, "status": "approved", "run_id": result["id"],
            "posted": result["counts"]["posted"], "failed": result["counts"]["failed"],
            "dropped": result["counts"]["dropped"], "expired": result["expired"],
            "simulated": result["simulated"], "run": _run_json(
                conn.execute("SELECT * FROM runs WHERE id=?", (result["id"],)).fetchone())}
