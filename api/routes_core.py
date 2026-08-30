"""Campaigns, config, planning, runs and manual redial."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Sequence

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from engine.dispatcher import (
    DispatchConfig, dispatch, dispatch_config_from_body, hhmm, manual_pairs,
    red_config_from_body,
)
from engine.red_engine import (
    CALLBACK_LABEL, EXCLUDED, MANDATORY_LABEL, RedConfig, SKIP_MANUAL_ONLY,
    classify_disposition, decide,
)
from engine.seed import load_leads

from .db import (
    DEFAULT_CONFIG, current_config, dry_run, insert_config, now_iso, now_ist, session,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class PlanBody(BaseModel):
    date: Optional[str] = None
    # Empty = plan every schedulable bucket. Supply e.g. ["M0","F6","F5"] to put
    # only the urgent buckets on the clock; the rest are still evaluated and
    # recorded in `decisions`, they just get no slot today.
    buckets: list[str] = Field(default_factory=list)
    # Per-run dial window override, "HH:MM". Absent = the campaign's configured
    # window. Narrowing it here does NOT edit the saved config — this run only.
    start: Optional[str] = None
    end: Optional[str] = None


class ManualBody(BaseModel):
    campaign_id: int
    dispositions: list[str] = Field(default_factory=list)
    buckets: list[str] = Field(default_factory=list)
    date: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_day(text: Optional[str]) -> date:
    if not text:
        # IST, not server-local: on a UTC host `date.today()` rolls over at
        # 05:30 IST, so an evening plan would be filed under yesterday.
        return now_ist().date()
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError:
        raise HTTPException(422, f"date must be YYYY-MM-DD, got {text!r}") from None


def _campaign(conn: sqlite3.Connection, campaign_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    return row


def _anchor(day: date, dcfg: DispatchConfig) -> datetime:
    """Evaluation clock for `day`: the real time for today, window start otherwise.

    Clamped into the dial window, so asking for today's plan at 07:00 does not
    schedule before opening and asking at 21:00 does not schedule after closing.
    """
    start = datetime.combine(day, time(dcfg.start_min // 60, dcfg.start_min % 60))
    if day != now_ist().date():
        return start
    end = datetime.combine(day, time(dcfg.end_min // 60, dcfg.end_min % 60))
    return min(max(now_ist(), start), end)


def _floor_min(now: datetime, day: date, dcfg: DispatchConfig) -> Optional[int]:
    """Earliest dialable minute-of-day, or None to mean 'the window start'."""
    if day != now_ist().date():
        return None
    return max(dcfg.start_min, now.hour * 60 + now.minute)


def _configs(body: dict[str, Any]) -> tuple[RedConfig, DispatchConfig]:
    try:
        return red_config_from_body(body), dispatch_config_from_body(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _run_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "campaign_id": row["campaign_id"], "run_date": row["run_date"],
        "kind": row["kind"], "status": row["status"], "config_version": row["config_version"],
        "created_at": row["created_at"], "dry_run": bool(row["dry_run"]),
        "note": row["note"],
        "counts": {"evaluated": row["evaluated"], "planned": row["planned"],
                   "slots": row["slots"], "posted": row["posted"], "failed": row["failed"],
                   "dropped": row["dropped"]},
    }


def _item_json(row: sqlite3.Row) -> dict[str, Any]:
    item = {k: row[k] for k in row.keys()}
    item["response"] = json.loads(row["response"]) if row["response"] else None
    return item


# ---------------------------------------------------------------------------
# Campaigns & config
# ---------------------------------------------------------------------------

def _campaign_json(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "agent_id": row["agent_id"], "warehouse_id": row["warehouse_id"],
            "name": row["name"], "enabled": bool(row["enabled"]), "paused": bool(row["paused"])}


@router.get("/api/campaigns")
def list_campaigns(agent_id: Optional[int] = Query(None)) -> list[dict[str, Any]]:
    """Every campaign, or only one agent's. The console is always agent-scoped."""
    sql, args = "SELECT * FROM campaigns", []
    if agent_id is not None:
        sql += " WHERE agent_id=?"
        args.append(agent_id)
    with session() as conn:
        return [_campaign_json(r) for r in conn.execute(sql + " ORDER BY id", args)]


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
# There is no `agents` table on purpose: an agent IS the set of campaigns that
# carry its id, so the roster is derived and can never disagree with the data.

@router.get("/api/agents")
def list_agents() -> list[dict[str, Any]]:
    with session() as conn:
        return [{"agent_id": r["agent_id"], "name": f"Agent {r['agent_id']}",
                 "campaigns": r["campaigns"], "enabled": r["enabled"],
                 "paused_campaigns": r["paused_campaigns"],
                 # Paused only when there is something running to pause and all
                 # of it is paused. A disabled campaign is not "running".
                 "paused": bool(r["enabled"]) and r["paused_campaigns"] == r["enabled"]}
                for r in conn.execute(
                    "SELECT agent_id, COUNT(*) AS campaigns, "
                    "  SUM(enabled) AS enabled, "
                    "  SUM(enabled AND paused) AS paused_campaigns "
                    "FROM campaigns GROUP BY agent_id ORDER BY agent_id")]


@router.post("/api/agents/{agent_id}/pause")
def pause_agent(agent_id: int) -> dict[str, Any]:
    return _set_agent_paused(agent_id, True)


@router.post("/api/agents/{agent_id}/resume")
def resume_agent(agent_id: int) -> dict[str, Any]:
    return _set_agent_paused(agent_id, False)


def _set_agent_paused(agent_id: int, paused: bool) -> dict[str, Any]:
    """Pause/resume every campaign on the agent — the kill switch for one voice."""
    with session() as conn:
        if conn.execute("SELECT 1 FROM campaigns WHERE agent_id=?", (agent_id,)).fetchone() is None:
            raise HTTPException(404, f"agent {agent_id} has no campaigns")
        conn.execute("UPDATE campaigns SET paused=? WHERE agent_id=?", (int(paused), agent_id))
        conn.commit()
    agent = next(a for a in list_agents() if a["agent_id"] == agent_id)
    return {**agent, "campaigns_changed": list_campaigns(agent_id)}


@router.post("/api/campaigns/{campaign_id}/pause")
def pause(campaign_id: int) -> dict[str, Any]:
    return _set_paused(campaign_id, True)


@router.post("/api/campaigns/{campaign_id}/resume")
def resume(campaign_id: int) -> dict[str, Any]:
    return _set_paused(campaign_id, False)


def _set_paused(campaign_id: int, paused: bool) -> dict[str, Any]:
    with session() as conn:
        _campaign(conn, campaign_id)
        conn.execute("UPDATE campaigns SET paused=? WHERE id=?", (int(paused), campaign_id))
        conn.commit()
        return _campaign_json(_campaign(conn, campaign_id))


@router.get("/api/campaigns/{campaign_id}/config")
def get_config(campaign_id: int) -> dict[str, Any]:
    with session() as conn:
        _campaign(conn, campaign_id)
        return current_config(conn, campaign_id)


@router.put("/api/campaigns/{campaign_id}/config")
def put_config(campaign_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Insert a new version. Nothing is ever mutated in place."""
    with session() as conn:
        _campaign(conn, campaign_id)
        # Layer over the CURRENT version, not over the factory defaults: a PUT
        # that omits a key means "leave it alone", not "reset it". DEFAULT_CONFIG
        # stays as the base so a key never saved before still resolves.
        live = {k: v for k, v in current_config(conn, campaign_id).items()
                if k not in ("version", "created_at")}
        merged = {**DEFAULT_CONFIG, **live, **{k: v for k, v in body.items()
                                               if k not in ("version", "created_at")}}
        _configs(merged)                    # 422 on a bad window / table / number
        return insert_config(conn, campaign_id, merged)


@router.get("/api/campaigns/{campaign_id}/config/history")
def config_history(campaign_id: int) -> list[dict[str, Any]]:
    with session() as conn:
        _campaign(conn, campaign_id)
        current_config(conn, campaign_id)   # ensure version 1 exists
        return [{"version": r["version"], "created_at": r["created_at"]}
                for r in conn.execute(
                    "SELECT version, created_at FROM config WHERE campaign_id=? "
                    "ORDER BY version DESC", (campaign_id,))]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _evaluate(conn: sqlite3.Connection, campaign: sqlite3.Row, day: date,
              window: Optional[tuple[Optional[str], Optional[str]]] = None):
    """Run the engine over one campaign for one day. No writes, no network.

    `window` narrows the dial window for this evaluation only; the saved config
    is untouched. Validation is the same one the config editor uses, so a run
    can never be placed outside the hours the campaign is allowed to dial.
    """
    cfg = current_config(conn, campaign["id"])
    if window and any(window):
        saved = cfg.get("dial_window") or {}
        cfg = {**cfg, "dial_window": {"start": window[0] or saved.get("start", "09:30"),
                                      "end": window[1] or saved.get("end", "19:00")}}
    red, dcfg = _configs(cfg)
    # A plan for a FUTURE date is evaluated at the start of its dial window, so it
    # is reproducible whatever time of day the operator asks for it. A plan for
    # TODAY is evaluated at the actual clock: anchoring it to 09:30 when it is
    # already 15:00 schedules the whole first wave into the past.
    now = _anchor(day, dcfg)
    leads = load_leads(conn, campaign, day)
    pairs = [(lead, decide(lead, now, red)) for lead in leads]
    return cfg, red, dcfg, now, leads, pairs


@router.get("/api/campaigns/{campaign_id}/buckets")
def buckets(campaign_id: int, date: Optional[str] = Query(None)) -> dict[str, Any]:
    day = _parse_day(date)
    with session() as conn:
        campaign = _campaign(conn, campaign_id)
        _cfg, red, _dcfg, _now, leads, pairs = _evaluate(conn, campaign, day)

    labels = {w.bucket: w.label for w in red.frequency_table}
    labels["M0"], labels["D0"] = MANDATORY_LABEL, CALLBACK_LABEL
    per_bucket: dict[str, dict[str, Any]] = {}
    per_disp: dict[str, dict[str, Any]] = {}
    matrix: Counter = Counter()
    skips: Counter = Counter()

    for lead, decision in pairs:
        stage = str(lead.get("stage") or "")
        entry = per_disp.setdefault(stage, {
            "disposition": stage, "class": decision.disposition_class,
            "auto": decision.disposition_class in red.auto_classes, "eligible": 0, "total": 0})
        entry["total"] += 1
        if decision.schedule:
            entry["eligible"] += 1
        else:
            skips[decision.action] += 1

        if not decision.bucket:
            continue
        bucket = per_bucket.setdefault(decision.bucket, {
            "bucket": decision.bucket, "label": labels.get(decision.bucket, decision.bucket_label),
            "eligible": 0, "waiting": 0, "manual_only": 0, "total": 0})
        bucket["total"] += 1
        if decision.schedule:
            bucket["eligible"] += 1
        elif decision.action == SKIP_MANUAL_ONLY:
            bucket["manual_only"] += 1
        else:
            bucket["waiting"] += 1
        matrix[(decision.bucket, stage)] += 1

    return {
        "date": day.isoformat(),
        "total_leads": len(leads),
        "buckets": sorted(per_bucket.values(), key=lambda b: red.priority_of(b["bucket"])),
        "dispositions": sorted(per_disp.values(), key=lambda d: -d["total"]),
        "matrix": [{"bucket": b, "disposition": d, "count": n}
                   for (b, d), n in sorted(matrix.items(), key=lambda kv: -kv[1])],
        "skips": dict(skips.most_common()),
    }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _write_run(conn: sqlite3.Connection, campaign: sqlite3.Row, day: date, kind: str,
               config_version: int, pairs, red: RedConfig, dcfg: DispatchConfig,
               evaluated: int, note: str = "", floor_min: Optional[int] = None,
               buckets: Optional[Sequence[str]] = None) -> int:
    """Replace any `planned` run for (campaign, date, kind) and store the plan."""
    existing = conn.execute(
        "SELECT id, status FROM runs WHERE campaign_id=? AND run_date=? AND kind=?",
        (campaign["id"], day.isoformat(), kind)).fetchall()
    for row in existing:
        if row["status"] != "planned":
            raise HTTPException(
                409, f"run {row['id']} for {day.isoformat()} is already {row['status']}; "
                     "re-planning would rewrite a run that has been acted on")
    for row in existing:
        conn.execute("DELETE FROM plan_items WHERE run_id=?", (row["id"],))
        conn.execute("DELETE FROM decisions WHERE run_id=?", (row["id"],))
        conn.execute("DELETE FROM runs WHERE id=?", (row["id"],))

    # `decisions` stays a full audit of every lead the engine looked at; only the
    # leads that get a SLOT are narrowed by `buckets`.
    schedulable = pairs if not buckets else [
        (lead, d) for lead, d in pairs if d.bucket in set(buckets)]
    result = dispatch(schedulable, day, red, dcfg, floor_min)
    created = now_iso()
    cursor = conn.execute(
        "INSERT INTO runs (campaign_id, run_date, kind, status, config_version, created_at, "
        "dry_run, evaluated, planned, slots, posted, failed, dropped, note) "
        "VALUES (?,?,?,'planned',?,?,?,?,?,?,0,0,?,?)",
        (campaign["id"], day.isoformat(), kind, config_version, created, int(dry_run()),
         evaluated, result.leads, len(result.slots), result.dropped, note))
    run_id = int(cursor.lastrowid)

    conn.executemany(
        "INSERT INTO decisions (run_id, lead_uuid, policy_no, disposition, disposition_class, "
        "dte, bucket, action, reason, scheduled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, lead.get("lead_uuid"), lead.get("policy_no"), str(lead.get("stage") or ""),
          d.disposition_class, d.dte, d.bucket, d.action, d.reason, int(d.schedule), created)
         for lead, d in pairs])

    conn.executemany(
        "INSERT INTO plan_items (run_id, lead_uuid, policy_no, contact_id, phone, lead_name, "
        "disposition, disposition_class, dte, bucket, bucket_label, priority, slot_no, "
        "scheduled_time, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'planned')",
        [(run_id, s.lead.get("lead_uuid"), s.lead.get("policy_no"), s.lead.get("contact_id"),
          _digits(s.lead.get("phone")) or None,
          s.lead.get("lead_name"), str(s.lead.get("stage") or ""), s.decision.disposition_class,
          s.decision.dte, s.decision.bucket, s.decision.bucket_label, s.priority, s.slot_no,
          s.scheduled_time) for s in result.slots])
    conn.commit()
    return run_id


@router.post("/api/campaigns/{campaign_id}/plan")
def make_plan(campaign_id: int, body: Optional[PlanBody] = None) -> dict[str, Any]:
    """Evaluate + place on the clock. Writes a run. Never dials."""
    day = _parse_day(body.date if body else None)
    buckets = list(body.buckets) if body and body.buckets else []
    with session() as conn:
        campaign = _campaign(conn, campaign_id)
        cfg, red, dcfg, now, leads, pairs = _evaluate(
            conn, campaign, day, (body.start if body else None, body.end if body else None))
        known = {w.bucket for w in red.frequency_table} | {"M0", "D0"}
        unknown = [b for b in buckets if b not in known]
        if unknown:
            raise HTTPException(422, f"unknown bucket(s) {unknown}; known: {sorted(known)}")
        floor = _floor_min(now, day, dcfg)
        # Today's window can already be over. Better a 422 than a run whose every
        # slot silently collapses onto the closing minute.
        if floor is not None and floor >= dcfg.end_min:
            raise HTTPException(
                422, f"the dial window {hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)} has already "
                     # `now` is the anchor, already clamped INTO the window — report
                     # the real clock or the message reads "it is 11:00" at 16:22.
                     f"closed (it is {now_ist().strftime('%H:%M')}); widen it or plan another date")
        note = ""
        if buckets:
            note = "buckets=" + ",".join(buckets)
        if body and (body.start or body.end):
            note = (note + " " if note else "") + f"window={hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)}"
        if floor is not None and floor > dcfg.start_min:
            note = (note + " " if note else "") + f"from={hhmm(floor)}"
        run_id = _write_run(conn, campaign, day, "auto", cfg["version"], pairs, red, dcfg,
                            evaluated=len(leads), note=note, floor_min=floor, buckets=buckets)
        return _run_json(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@router.get("/api/runs")
def list_runs(campaign_id: Optional[int] = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM runs"
    args: list[Any] = []
    if campaign_id is not None:
        sql += " WHERE campaign_id=?"
        args.append(campaign_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with session() as conn:
        return [_run_json(r) for r in conn.execute(sql, args)]


@router.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    with session() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"run {run_id} not found")
        return _run_json(row)


@router.get("/api/runs/{run_id}/items")
def run_items(run_id: int, bucket: Optional[str] = None, disposition: Optional[str] = None,
              status: Optional[str] = None, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 500))
    where = ["run_id=?"]
    args: list[Any] = [run_id]
    for column, value in (("bucket", bucket), ("disposition", disposition), ("status", status)):
        if value:
            where.append(f"{column}=?")
            args.append(value)
    clause = " AND ".join(where)
    with session() as conn:
        if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise HTTPException(404, f"run {run_id} not found")
        total = conn.execute(f"SELECT COUNT(*) c FROM plan_items WHERE {clause}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM plan_items WHERE {clause} ORDER BY scheduled_time, priority, id "
            f"LIMIT ? OFFSET ?", [*args, page_size, (page - 1) * page_size]).fetchall()
        return {"total": total, "page": page, "page_size": page_size,
                "items": [_item_json(r) for r in rows]}


@router.delete("/api/runs/{run_id}")
def delete_run(run_id: int) -> dict[str, Any]:
    """Discard a plan. Refuses committed runs — that's dial history, keep it."""
    with session() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"run {run_id} not found")
        if row["status"] != "planned":
            raise HTTPException(409, f"run {run_id} is {row['status']}, only planned runs can be deleted")
        conn.execute("DELETE FROM plan_items WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        conn.commit()
        return {"deleted": run_id}


class ItemPatch(BaseModel):
    scheduled_time: Optional[str] = None


@router.patch("/api/runs/{run_id}/items/{item_id}")
def patch_item(run_id: int, item_id: int, body: ItemPatch) -> dict[str, Any]:
    """Reschedule one slot. Refuses non-planned runs and non-planned items."""
    if not body.scheduled_time:
        raise HTTPException(422, "scheduled_time is required")
    try:
        # Validate ISO-ish input; accept "YYYY-MM-DDTHH:MM" or with seconds.
        text = body.scheduled_time.strip().replace(" ", "T")
        parsed = datetime.fromisoformat(text[:19] if len(text) >= 19 else text)
    except ValueError:
        raise HTTPException(422, f"scheduled_time must be ISO-8601; got {body.scheduled_time!r}")
    with session() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        if run["status"] != "planned":
            raise HTTPException(409, f"run {run_id} is {run['status']}, cannot edit items")
        item = conn.execute("SELECT * FROM plan_items WHERE id=? AND run_id=?",
                            (item_id, run_id)).fetchone()
        if item is None:
            raise HTTPException(404, f"item {item_id} not found in run {run_id}")
        if item["status"] != "planned":
            raise HTTPException(409, f"item {item_id} is {item['status']}, cannot reschedule")

        # A hand-edited slot has to survive the same checks the planner applies,
        # or the console becomes a way to schedule calls outside dialling hours,
        # onto the wrong day, or into the past. The planner is not the only writer
        # of scheduled_time, so the rules are enforced here too.
        _, dcfg = _configs(current_config(conn, run["campaign_id"]))
        if parsed.date().isoformat() != run["run_date"]:
            raise HTTPException(
                422, f"slot must stay on the run's date {run['run_date']}; "
                     f"got {parsed.date().isoformat()}")
        minute = parsed.hour * 60 + parsed.minute
        if not dcfg.start_min <= minute <= dcfg.end_min:
            raise HTTPException(
                422, f"{hhmm(minute)} is outside this campaign's dial window "
                     f"{hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)}")
        now = now_ist()
        if parsed.date() == now.date() and parsed < now:
            raise HTTPException(
                422, f"{hhmm(minute)} is in the past (it is {now.strftime('%H:%M')})")
        if dcfg.max_per_minute:
            taken = conn.execute(
                "SELECT COUNT(*) AS n FROM plan_items WHERE run_id=? AND id<>? "
                "AND scheduled_time=?",
                (run_id, item_id, parsed.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()["n"]
            if taken >= dcfg.max_per_minute:
                raise HTTPException(
                    422, f"{hhmm(minute)} already holds {taken} calls "
                         f"(max_per_minute={dcfg.max_per_minute})")

        conn.execute("UPDATE plan_items SET scheduled_time=? WHERE id=?",
                     (parsed.strftime("%Y-%m-%dT%H:%M:%S"), item_id))
        conn.commit()
        return _item_json(conn.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone())


@router.post("/api/runs/{run_id}/approve")
def approve(run_id: int) -> dict[str, Any]:
    """The only path that can dial. Under DRY_RUN it only marks items simulated."""
    with session() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        campaign = _campaign(conn, run["campaign_id"])
        if campaign["paused"]:
            raise HTTPException(409, f"campaign {campaign['id']} is paused")
        if run["status"] != "planned":
            raise HTTPException(409, f"run {run_id} is {run['status']}, not planned")

        every = conn.execute("SELECT * FROM plan_items WHERE run_id=? ORDER BY scheduled_time, id",
                             (run_id,)).fetchall()

        # A plan is a proposal, not a promise: it can sit unapproved while the
        # clock runs past its early slots. Posting those to Formi would ask for a
        # call at a time that has already gone, so they are retired here instead.
        # The rest of the run still goes out — one stale slot must not block the
        # afternoon. Re-plan to put the retired leads back on the clock.
        # Truncated to the minute: Formi schedules by the minute, so a slot at
        # 16:26 approved at 16:26:40 is still on time, not stale.
        cutoff = now_ist().strftime("%Y-%m-%dT%H:%M:00")
        stale = [r for r in every if (r["scheduled_time"] or "") < cutoff]
        items = [r for r in every if (r["scheduled_time"] or "") >= cutoff]
        if stale and not items:
            raise HTTPException(
                409, f"every slot in run {run_id} is in the past (it is "
                     f"{now_ist().strftime('%H:%M')}); re-plan before approving")
        if stale:
            conn.executemany("UPDATE plan_items SET status='expired' WHERE id=?",
                             [(r["id"],) for r in stale])

        if dry_run():
            # No network I/O whatsoever: record exactly what would have been sent.
            conn.executemany(
                "UPDATE plan_items SET status='simulated', response=? WHERE id=?",
                [(json.dumps({"would_post": {
                    "url": _schedule_path(campaign["agent_id"], r["lead_uuid"]),
                    "body": {"scheduled_time": r["scheduled_time"]}}}), r["id"]) for r in items])
            posted, failed = len(items), 0
        else:
            posted, failed = _dial_live(conn, campaign, items)
        conn.execute("UPDATE runs SET status='committed', posted=?, failed=?, dropped=?, "
                     "dry_run=? WHERE id=?",
                     (posted, failed, run["dropped"] + len(stale), int(dry_run()), run_id))
        conn.commit()
        out = _run_json(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        out["dry_run"] = dry_run()
        out["simulated"] = posted if dry_run() else 0
        out["expired"] = len(stale)
        return out


def _schedule_path(agent_id: Any, lead_uuid: Any) -> str:
    """The one place the Formi schedule URL is built."""
    return f"/v2/campaign/leads/{agent_id}/{lead_uuid}/schedule"


def _formi_post(agent_id: Any, lead_uuid: Any, scheduled_time: Any):
    """The single live POST in the app. Unreachable while DRY_RUN is set.

    The guard is the first statement and `requests` is imported after it, so a
    dry run cannot reach the network even if this were called by mistake.
    """
    if dry_run():
        raise RuntimeError("DRY_RUN is set — refusing to dial")
    import os                                        # noqa: PLC0415 — see docstring
    import requests                                  # noqa: PLC0415 — see docstring
    token = os.environ.get("FORMI_TOKEN")
    if not token:
        raise HTTPException(500, "FORMI_TOKEN is not set; cannot dial live")
    return requests.post(
        f"https://api.formi.co.in{_schedule_path(agent_id, lead_uuid)}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"scheduled_time": scheduled_time}, timeout=45)


def _dial_live(conn: sqlite3.Connection, campaign: sqlite3.Row, items) -> tuple[int, int]:
    """POST each slot to Formi. Unreachable while DRY_RUN is set."""
    posted = failed = 0
    for item in items:
        response = _formi_post(campaign["agent_id"], item["lead_uuid"], item["scheduled_time"])
        ok = 200 <= response.status_code < 300
        posted += int(ok)
        failed += int(not ok)
        conn.execute("UPDATE plan_items SET status=?, http_status=?, response=? WHERE id=?",
                     ("posted" if ok else "failed", response.status_code,
                      response.text[:300], item["id"]))
    return posted, failed


# ---------------------------------------------------------------------------
# Manual redial
# ---------------------------------------------------------------------------

def _manual(conn: sqlite3.Connection, body: ManualBody, day: date):
    campaign = _campaign(conn, body.campaign_id)
    cfg = current_config(conn, campaign["id"])
    red, dcfg = _configs(cfg)
    now = datetime.combine(day, time(dcfg.start_min // 60, dcfg.start_min % 60))
    leads = load_leads(conn, campaign, day)
    pairs = manual_pairs(leads, now, red, body.dispositions, body.buckets)
    return campaign, cfg, red, dcfg, leads, pairs


@router.post("/api/manual/preview")
def manual_preview(body: ManualBody) -> dict[str, Any]:
    day = _parse_day(body.date)
    with session() as conn:
        _campaign_row, _cfg, red, dcfg, _leads, pairs = _manual(conn, body, day)
    result = dispatch(pairs, day, red, dcfg)
    # The manual screen deliberately overrides the cadence guards, including the
    # one that stops a lead being booked twice in a day. That override is the
    # point, but it must not be silent: if Formi already holds a call for these
    # leads today, say how many before the operator commits.
    booked = sum(1 for s in result.slots
                 if s.slot_no == 1 and int(s.lead.get("queued_today") or 0))
    return {
        "count": result.leads,
        "slots": len(result.slots),
        "dropped": result.dropped,
        "already_scheduled": booked,
        "sample": [{"lead_uuid": s.lead.get("lead_uuid"), "policy_no": s.lead.get("policy_no"),
                    "lead_name": s.lead.get("lead_name"), "disposition": s.lead.get("stage"),
                    "disposition_class": s.decision.disposition_class, "dte": s.decision.dte,
                    "bucket": s.decision.bucket, "slot_no": s.slot_no,
                    "scheduled_time": s.scheduled_time} for s in result.slots[:25]],
    }


@router.post("/api/manual/schedule")
def manual_schedule(body: ManualBody) -> dict[str, Any]:
    day = _parse_day(body.date)
    with session() as conn:
        campaign, cfg, red, dcfg, leads, pairs = _manual(conn, body, day)
        note = f"manual dispositions={','.join(body.dispositions) or 'any'} " \
               f"buckets={','.join(body.buckets) or 'any'}"
        run_id = _write_run(conn, campaign, day, "manual", cfg["version"], pairs, red, dcfg,
                            evaluated=len(leads), note=note)
        return _run_json(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


# ---------------------------------------------------------------------------
# Test call — one rehearsal against one known number
# ---------------------------------------------------------------------------

class TestCallBody(BaseModel):
    phone: str
    campaign_id: Optional[int] = None
    scheduled_time: Optional[str] = None


def _digits(phone: Any) -> str:
    """'+91 93797-47274' -> '9379747274'. Compare numbers, not formatting."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) > 10 else digits


def _allow_list(conn: sqlite3.Connection, campaign_id: Optional[int]) -> list[str]:
    """The numbers that may be test-dialled: the campaign's config, else default."""
    cfg = current_config(conn, campaign_id) if campaign_id is not None else DEFAULT_CONFIG
    return [_digits(p) for p in (cfg.get("test_numbers") or []) if _digits(p)]


def _resolve_lead(conn: sqlite3.Connection, phone: str,
                  campaign_id: Optional[int]) -> Optional[sqlite3.Row]:
    """The lead holding `phone`. The NEWEST campaign wins when several hold it.

    A test number is re-uploaded every time someone rehearses, so it exists in
    every campaign it was ever pushed to (the real number 9379747274 is in ~20).
    Dialling the oldest of those would rehearse against a campaign that has been
    dead for a month; the freshest campaign id is the one just created for the
    rehearsal. Pass `campaign_id` to pin an exact one.
    """
    sql = ("SELECT l.*, c.agent_id AS agent_id, c.name AS campaign_name "
           "FROM leads l JOIN campaigns c ON c.id = l.campaign_id WHERE l.phone=?")
    args: list[Any] = [phone]
    if campaign_id is not None:
        sql += " AND l.campaign_id=?"
        args.append(campaign_id)
    return conn.execute(sql + " ORDER BY l.campaign_id DESC, l.id DESC LIMIT 1",
                        args).fetchone()


def _next_slot(dcfg: DispatchConfig) -> str:
    """The next minute inside the dial window; tomorrow's opening if it has shut."""
    now = now_ist()
    day, minute = now.date(), now.hour * 60 + now.minute
    if minute < dcfg.start_min:
        minute = dcfg.start_min
    elif minute >= dcfg.end_min:
        day, minute = day + timedelta(days=1), dcfg.start_min
    return f"{day.isoformat()}T{minute // 60:02d}:{minute % 60:02d}:00"


def _chosen_slot(text: str, dcfg: DispatchConfig) -> str:
    """Validate an operator-picked rehearsal time. Same rules a planned slot gets.

    A test call is still a real call to a real handset, so "pick your own time"
    cannot become a way around dialling hours or a way to post a time that has
    already gone. Any date is fine -- rehearsing tomorrow morning is legitimate.
    Truncated to the minute: Formi schedules by the minute, so choosing 16:26 at
    16:26:40 is on time, not late.
    """
    try:
        raw = text.strip().replace(" ", "T")
        parsed = datetime.fromisoformat(raw[:19] if len(raw) >= 19 else raw)
    except ValueError:
        raise HTTPException(422, f"scheduled_time must be ISO-8601; got {text!r}")
    minute = parsed.hour * 60 + parsed.minute
    if not dcfg.start_min <= minute <= dcfg.end_min:
        raise HTTPException(
            422, f"{hhmm(minute)} is outside this campaign's dial window "
                 f"{hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)}")
    now = now_ist()
    if parsed < now.replace(second=0, microsecond=0):
        raise HTTPException(
            422, f"{parsed.strftime('%Y-%m-%d %H:%M')} has already passed "
                 f"(it is {now.strftime('%Y-%m-%d %H:%M')})")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _test_call(conn: sqlite3.Connection, body: TestCallBody, commit: bool) -> dict[str, Any]:
    """Resolve, validate and (when `commit`) dispatch one rehearsal call.

    Order matters: the allow-list is checked before anything else, so an
    arbitrary customer number cannot even reach lead resolution.
    """
    phone = _digits(body.phone)
    if body.campaign_id is not None:
        _campaign(conn, body.campaign_id)

    allowed = _allow_list(conn, body.campaign_id)
    if phone not in allowed:
        raise HTTPException(422, f"{body.phone!r} is not on the test-call allow-list; "
                                 f"config.test_numbers = {allowed}")

    lead = _resolve_lead(conn, phone, body.campaign_id)
    if lead is None:
        out = {"found": False, "dry_run": dry_run(), "lead": None, "would_post": None,
               "status": "not_found", "http_status": None, "response": None}
        if commit:
            _record_test_call(conn, phone, None, out)
        return out

    cfg = current_config(conn, lead["campaign_id"])
    red, dcfg = _configs(cfg)
    stage = str(lead["stage"] or "")
    klass, rule = classify_disposition(stage, red)
    # A rehearsal may ignore a campaign pause -- that is exactly when you want
    # one -- but exclusions are TRAI/NCPR, so they hold here too.
    if klass == EXCLUDED:
        raise HTTPException(409, f"lead {lead['lead_uuid']} is {stage!r} "
                                 f"({rule.note if rule else 'excluded'}); a test call never "
                                 f"overrides an exclusion")

    scheduled = (_chosen_slot(body.scheduled_time, dcfg) if body.scheduled_time
                 else _next_slot(dcfg))
    out: dict[str, Any] = {
        "found": True, "dry_run": dry_run(),
        "lead": {"lead_uuid": lead["lead_uuid"], "phone": lead["phone"],
                 "lead_name": lead["lead_name"], "policy_no": lead["policy_no"],
                 "campaign_id": lead["campaign_id"], "campaign_name": lead["campaign_name"],
                 "agent_id": lead["agent_id"], "stage": stage,
                 "disposition_class": klass},
        "would_post": {"url": _schedule_path(lead["agent_id"], lead["lead_uuid"]),
                       "body": {"scheduled_time": scheduled}},
        "status": "preview", "http_status": None, "response": None,
    }
    if not commit:
        return out

    if dry_run():
        out["status"] = "simulated"
    else:
        response = _formi_post(lead["agent_id"], lead["lead_uuid"], scheduled)
        ok = 200 <= response.status_code < 300
        out["status"] = "posted" if ok else "failed"
        out["http_status"] = response.status_code
        out["response"] = response.text[:300]
    _record_test_call(conn, phone, lead, out, scheduled)
    return out


def _record_test_call(conn: sqlite3.Connection, phone: str, lead: Optional[sqlite3.Row],
                      out: dict[str, Any], scheduled: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO test_calls (created_at, phone, campaign_id, agent_id, lead_uuid, "
        "lead_name, disposition, scheduled_time, status, dry_run, would_post, http_status, "
        "response) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), phone,
         lead["campaign_id"] if lead else None, lead["agent_id"] if lead else None,
         lead["lead_uuid"] if lead else None, lead["lead_name"] if lead else None,
         str(lead["stage"] or "") if lead else None, scheduled, out["status"],
         int(dry_run()), json.dumps(out["would_post"]) if out["would_post"] else None,
         out["http_status"], json.dumps(out["response"]) if out["response"] else None))
    conn.commit()


@router.get("/api/test-call/numbers")
def test_call_numbers(campaign_id: Optional[int] = Query(None)) -> list[dict[str, Any]]:
    with session() as conn:
        if campaign_id is not None:
            _campaign(conn, campaign_id)
        out = []
        for phone in _allow_list(conn, campaign_id):
            lead = _resolve_lead(conn, phone, campaign_id)
            out.append({"phone": phone, "found": lead is not None,
                        "label": lead["campaign_name"] if lead else "no lead on this number",
                        "campaign_id": lead["campaign_id"] if lead else None,
                        "lead_uuid": lead["lead_uuid"] if lead else None,
                        "lead_name": lead["lead_name"] if lead else None})
        return out


@router.post("/api/test-call/preview")
def test_call_preview(body: TestCallBody) -> dict[str, Any]:
    """Resolve the lead and show the exact payload. Records nothing, dials nothing."""
    with session() as conn:
        return _test_call(conn, body, commit=False)


@router.post("/api/test-call/trigger")
def test_call_trigger(body: TestCallBody) -> dict[str, Any]:
    """Schedule that one lead. 422 for any number not in config.test_numbers."""
    with session() as conn:
        return _test_call(conn, body, commit=True)


@router.get("/api/test-call/history")
def test_call_history(limit: int = 50) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute("SELECT * FROM test_calls ORDER BY id DESC LIMIT ?",
                            (max(1, min(int(limit), 500)),)).fetchall()
    return [{**{k: r[k] for k in r.keys()}, "dry_run": bool(r["dry_run"]),
             "would_post": json.loads(r["would_post"]) if r["would_post"] else None,
             "response": json.loads(r["response"]) if r["response"] else None}
            for r in rows]
