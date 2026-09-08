"""Append-only dial log: what this console sent, and what actually happened.

Two separate facts, deliberately kept apart, because conflating them is exactly
why the console could not answer "was the call placed?":

  * `outcome`  -- what Formi's API answered when we posted. Written on the dial
                  path, as ONE batched INSERT per commit however large the run.
  * `verified` -- whether the call is really on the clock, and whether it was
                  dialled. Written LATER, by reading the warehouse back. A 2xx
                  says the request was accepted; only this says a call happened.

The verify pass never runs on the dial path and never runs per call: it is one
warehouse query per (campaign, day) settling every open row of that day at once,
it is a no-op when nothing is open, and it runs on a background thread behind a
lock so two passes can never pile up. `plan_items` is unaffected -- it stays the
current state of a slot, this is the history.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from fastapi import APIRouter, HTTPException, Query

from .db import leads_source, now_ist, now_iso, session

router = APIRouter()
log = logging.getLogger("redial.dial_log")

FIELDS = ("created_at", "campaign_id", "agent_id", "run_id", "item_id", "source",
          "lead_uuid", "policy_no", "contact_id", "lead_name", "phone", "bucket",
          "disposition", "dte", "scheduled_time", "dry_run", "url", "request_body",
          "attempts", "http_status", "response", "outcome", "verified")

_INSERT = (f"INSERT INTO dial_log ({', '.join(FIELDS)}) "
           f"VALUES ({', '.join('?' * len(FIELDS))})")

# Rows the verifier still has something to learn about. `dialled` is final, and
# so is `simulated` -- a dry run put nothing on anyone's clock, so there is
# nothing to read back. `missing` is deliberately NOT final: the warehouse lags,
# and a row missing at 10:01 is routinely there at 10:06.
OPEN = ("pending", "queued", "missing")

# How much history to keep. A day of every campaign dialling is a few thousand
# rows; this table is the audit trail, not a queue, so it is pruned by age
# rather than by size and only ever on the (already asynchronous) verify pass.
def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("DIAL_LOG_DAYS") or 90))
    except ValueError:
        return 90


def _text(value: Any, limit: int) -> Optional[str]:
    text = str(value or "").strip()
    return text[:limit] or None


def row(campaign: Any, item: Any, *, source: str, url: str, body: dict[str, Any],
        dry: bool, outcome: str, http_status: Optional[int] = None,
        response: Any = None, attempts: int = 1,
        run_id: Optional[int] = None) -> tuple:
    """One dial_log tuple. Pure: the dial path pays for the INSERT and nothing else.

    `campaign` and `item` are anything dict()-able -- a sqlite3.Row from
    campaigns/plan_items, or a plain dict for a test call, which belongs to no run.
    """
    item, campaign = dict(item or {}), dict(campaign or {})
    return (
        now_iso(),
        int(campaign.get("id") or item.get("campaign_id") or 0),
        campaign.get("agent_id"),
        run_id if run_id is not None else item.get("run_id"),
        item.get("id"),
        source,
        item.get("lead_uuid"), item.get("policy_no"), item.get("contact_id"),
        item.get("lead_name"), item.get("phone"), item.get("bucket"),
        item.get("disposition"), item.get("dte"), item.get("scheduled_time"),
        int(dry), url, json.dumps(body, separators=(",", ":")), int(attempts),
        http_status, _text(response, 1000), outcome,
        "simulated" if outcome == "simulated" else "pending",
    )


def write(conn: sqlite3.Connection, rows: Iterable[tuple]) -> int:
    """Append. One executemany whatever the run size; never raises on the dial path."""
    rows = list(rows)
    if not rows:
        return 0
    try:
        conn.executemany(_INSERT, rows)
    except sqlite3.Error:                       # a log must never lose a call
        log.exception("dial_log write failed for %d rows", len(rows))
        return 0
    return len(rows)


# ---------------------------------------------------------------------------
# Verification — asynchronous, batched, read-only
# ---------------------------------------------------------------------------

_verifying = threading.Lock()
_CHUNK = 800            # lead ids per warehouse query, so the SQL stays sane


def _today() -> str:
    return now_ist().date().isoformat()


def _day(text: Optional[str]) -> str:
    if not text:
        return _today()
    try:
        return date.fromisoformat(str(text).strip()).isoformat()
    except ValueError:
        raise HTTPException(422, f"date must be YYYY-MM-DD, got {text!r}") from None


def _classify(interaction: Optional[dict[str, Any]]) -> str:
    if interaction is None:
        return "missing"
    # The app's own convention (engine/metabase_source.py): an empty call_stage
    # is an interaction sitting on the clock that was never dialled.
    return "dialled" if str(interaction.get("call_stage") or "").strip() else "queued"


def _verify_campaign(conn: sqlite3.Connection, campaign_id: int, day: str,
                     rows: list[sqlite3.Row]) -> list[tuple]:
    """Settle one campaign's open rows with ONE warehouse query. Read-only."""
    from engine import metabase_source as ms          # noqa: PLC0415 — heavy import

    uuids = {r["lead_uuid"] for r in rows if r["lead_uuid"]}
    if not uuids:
        return []
    marks = ",".join("?" * len(uuids))
    lead_id_by_uuid = {r["lead_uuid"]: int(r["id"]) for r in conn.execute(
        f"SELECT id, lead_uuid FROM leads WHERE campaign_id=? AND lead_uuid IN ({marks})",
        [campaign_id, *uuids])}
    if not lead_id_by_uuid:
        return []

    ids = sorted(set(lead_id_by_uuid.values()))
    found: list[dict[str, Any]] = []
    for start in range(0, len(ids), _CHUNK):
        chunk = ",".join(str(i) for i in ids[start:start + _CHUNK])
        found += ms.run_sql(f"""
SELECT i.id,
       i.lead_id,
       COALESCE(i.call_stage, '') AS call_stage,
       to_char(i.scheduled_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata',
               'YYYY-MM-DD"T"HH24:MI:00') AS slot_ist,
       {ms.DISPOSITION_SQL} AS disposition,
       COALESCE((i.interaction_metadata->>'call_duration')::numeric, 0) AS duration_sec
FROM public.interactions i
WHERE i.campaign_id = {int(campaign_id)}
  AND i.lead_id IN ({chunk})
  AND (i.scheduled_time AT TIME ZONE 'UTC'
       AT TIME ZONE 'Asia/Kolkata')::date = DATE '{day}'
""".strip(), timeout=180)

    # Exact minute first: that is the slot we asked for. Falling back to any
    # interaction the lead had that day matters because Formi moves a call when
    # the agent is busy -- without the fallback a dialled call reads as missing.
    exact = {(int(f["lead_id"]), str(f["slot_ist"])): f for f in found}
    by_lead: dict[int, dict[str, Any]] = {}
    for f in found:
        best = by_lead.get(int(f["lead_id"]))
        if best is None or (str(f["call_stage"]).strip() and not str(best["call_stage"]).strip()):
            by_lead[int(f["lead_id"])] = f

    checked, updates = now_iso(), []
    for r in rows:
        lead_id = lead_id_by_uuid.get(r["lead_uuid"])
        if lead_id is None:
            continue
        hit = exact.get((lead_id, str(r["scheduled_time"]))) or by_lead.get(lead_id)
        updates.append((checked, _classify(hit),
                        int(hit["id"]) if hit else None,
                        (str(hit["call_stage"]) or None) if hit else None,
                        _text(hit.get("disposition"), 60) if hit else None,
                        int(float(hit.get("duration_sec") or 0)) if hit else None,
                        r["id"]))
    return updates


def verify_day(day: Optional[str] = None) -> dict[str, Any]:
    """Settle every open row scheduled on `day`. Safe to call on any schedule.

    Read-only against the warehouse: it issues SELECTs and writes only to the
    local log, so it is unaffected by DRY_RUN and can never place a call.
    """
    day = _day(day)
    if leads_source() == "seed":
        return {"date": day, "checked": 0, "skipped": "no warehouse (LEADS_SOURCE=seed)"}
    if not _verifying.acquire(blocking=False):
        return {"date": day, "checked": 0, "skipped": "a verify pass is already running"}
    try:
        with session() as conn:
            open_rows = conn.execute(
                "SELECT id, campaign_id, lead_uuid, scheduled_time FROM dial_log "
                f"WHERE dry_run=0 AND verified IN ({','.join('?' * len(OPEN))}) "
                "AND substr(scheduled_time, 1, 10)=? ORDER BY campaign_id",
                [*OPEN, day]).fetchall()
            if not open_rows:
                _prune(conn)
                return {"date": day, "checked": 0, "campaigns": 0}

            by_campaign: dict[int, list[sqlite3.Row]] = {}
            for r in open_rows:
                by_campaign.setdefault(int(r["campaign_id"]), []).append(r)

            updates, failures = [], []
            for campaign_id, rows in by_campaign.items():
                try:
                    updates += _verify_campaign(conn, campaign_id, day, rows)
                except Exception as exc:            # one bad campaign must not stop the rest
                    log.warning("dial_log verify failed for campaign %s: %s", campaign_id, exc)
                    failures.append(campaign_id)

            if updates:
                conn.executemany(
                    "UPDATE dial_log SET checked_at=?, verified=?, interaction_id=?, "
                    "call_stage=?, call_disposition=?, duration_sec=? WHERE id=?", updates)
            _prune(conn)
            conn.commit()
            settled = conn.execute(
                "SELECT verified, COUNT(*) c FROM dial_log WHERE dry_run=0 "
                "AND substr(scheduled_time, 1, 10)=? GROUP BY verified", (day,)).fetchall()
        return {"date": day, "checked": len(updates), "campaigns": len(by_campaign),
                "failed_campaigns": failures,
                "by_state": {r["verified"]: r["c"] for r in settled}}
    finally:
        _verifying.release()


def _prune(conn: sqlite3.Connection) -> int:
    cutoff = (now_ist() - timedelta(days=retention_days())).isoformat(timespec="seconds")
    return conn.execute("DELETE FROM dial_log WHERE created_at < ?", (cutoff,)).rowcount


def verify_async(day: Optional[str] = None) -> None:
    """Kick a verify pass off the caller's thread. Never raises, never blocks."""
    if _verifying.locked():
        return
    threading.Thread(target=lambda: _safe_verify(day), daemon=True,
                     name="dial-log-verify").start()


def _safe_verify(day: Optional[str]) -> None:
    try:
        verify_day(day)
    except Exception:
        log.exception("dial_log verify pass crashed")


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def _json(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: r[k] for k in r.keys()}
    out["dry_run"] = bool(out["dry_run"])
    return out


@router.get("/api/dial-log")
def list_log(campaign_id: Optional[int] = Query(None),
             date: Optional[str] = Query(None),
             verified: Optional[str] = Query(None),
             outcome: Optional[str] = Query(None),
             limit: int = Query(100, ge=1, le=500),
             offset: int = Query(0, ge=0)) -> dict[str, Any]:
    """One page of the log, newest first. Capped: this table is never read whole."""
    where, args = ["1=1"], []
    if campaign_id is not None:
        where.append("campaign_id=?")
        args.append(campaign_id)
    if date:
        where.append("substr(scheduled_time, 1, 10)=?")
        args.append(_day(date))
    if verified:
        where.append("verified=?")
        args.append(verified)
    if outcome:
        where.append("outcome=?")
        args.append(outcome)
    clause = " AND ".join(where)
    with session() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM dial_log WHERE {clause}",
                             args).fetchone()["c"]
        rows = conn.execute(f"SELECT * FROM dial_log WHERE {clause} "
                            "ORDER BY id DESC LIMIT ? OFFSET ?",
                            [*args, limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "rows": [_json(r) for r in rows]}


@router.get("/api/dial-log/summary")
def summary(date: Optional[str] = Query(None),
            campaign_id: Optional[int] = Query(None)) -> dict[str, Any]:
    """Counts only — what the campaign screen shows. One GROUP BY, no row bodies."""
    day = _day(date)
    where, args = ["substr(scheduled_time, 1, 10)=?"], [day]
    if campaign_id is not None:
        where.append("campaign_id=?")
        args.append(campaign_id)
    clause = " AND ".join(where)
    with session() as conn:
        rows = conn.execute(
            f"SELECT campaign_id, outcome, verified, COUNT(*) c, "
            f"  SUM(CASE WHEN duration_sec > 0 THEN duration_sec ELSE 0 END) secs "
            f"FROM dial_log WHERE {clause} GROUP BY campaign_id, outcome, verified",
            args).fetchall()
    per: dict[int, dict[str, Any]] = {}
    for r in rows:
        bucket = per.setdefault(int(r["campaign_id"]),
                                {"campaign_id": int(r["campaign_id"]), "sent": 0,
                                 "outcome": {}, "verified": {}, "talk_time_sec": 0})
        bucket["sent"] += r["c"]
        bucket["outcome"][r["outcome"]] = bucket["outcome"].get(r["outcome"], 0) + r["c"]
        bucket["verified"][r["verified"]] = bucket["verified"].get(r["verified"], 0) + r["c"]
        bucket["talk_time_sec"] += int(r["secs"] or 0)
    return {"date": day, "campaigns": sorted(per.values(), key=lambda b: b["campaign_id"])}


@router.post("/api/dial-log/verify")
def verify_now(date: Optional[str] = Query(None),
               wait: bool = Query(False)) -> dict[str, Any]:
    """Ask the warehouse what really happened. Backgrounded unless `wait=true`."""
    if wait:
        return verify_day(date)
    verify_async(date)
    return {"date": _day(date), "started": True}
