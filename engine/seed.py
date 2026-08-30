"""Deterministic offline dataset so the whole app runs with zero credentials.

`python -m engine.seed` creates redial.db, fourteen campaigns and ~8,000 leads
whose RED dates are anchored to *today*, so every bucket F1-F6 is populated
whenever you run it. Same seed -> byte-identical dataset, every time.

`load_leads()` is the single place the rest of the app asks for leads. With
LEADS_SOURCE=seed (the default) it reads this table; with LEADS_SOURCE=metabase
it delegates to `metabase_source`, which needs live credentials — that import is
lazy so the seed path never touches it.
"""
from __future__ import annotations

import os
import random
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Any

SEED = 20260828

# ---------------------------------------------------------------------------
# THE agent roster. This one line is the only place agent ids are declared --
# every campaign below refers to AGENTS[0] / AGENTS[1], the API derives the
# agent list from the campaigns table, and nothing anywhere hardcodes a number.
# 125 and 127 are the real Chola voice agents in the warehouse (verified against
# public.campaigns: 40 and 24 campaigns respectively). The earlier "15" was a
# typo for 125. `engine.sync` pulls the same two agents' real campaigns.
AGENTS = [125, 127]

# Numbers the test-call button may dial. Seeded onto a real lead in each TEST
# campaign so the rehearsal actually resolves to something.
TEST_NUMBERS = ["9379747274"]

# Real campaign names carry a DDMM prefix, a product code (PV private vehicle,
# CV commercial vehicle, TW two-wheeler) and the agent's language. `leads` is the
# campaign's own size; `total` in generate()/populate() scales them all.
# Agent 15 runs the PV book, agent 127 the CV/TW book -- a clean split, because
# mixing them in one view is how a Hindi script reaches a Tamil cohort.
CAMPAIGNS = [
    {"id": 1,  "agent_id": AGENTS[0], "warehouse_id": 1650, "name": "0308Redial -PV Hindi",     "leads": 1200},
    {"id": 2,  "agent_id": AGENTS[0], "warehouse_id": 1651, "name": "0308Redial -PV Tamil",     "leads": 900},
    {"id": 3,  "agent_id": AGENTS[1], "warehouse_id": 1660, "name": "1008Redial -CV",           "leads": 950},
    {"id": 4,  "agent_id": AGENTS[0], "warehouse_id": 1652, "name": "0308Redial -PV Telugu",    "leads": 700},
    {"id": 5,  "agent_id": AGENTS[1], "warehouse_id": 1661, "name": "0308Redial -CV Hindi",     "leads": 800},
    {"id": 6,  "agent_id": AGENTS[0], "warehouse_id": 1653, "name": "1008Redial -PV Kannada",   "leads": 600},
    {"id": 7,  "agent_id": AGENTS[1], "warehouse_id": 1670, "name": "1008Redial -TW Hindi",     "leads": 550},
    {"id": 8,  "agent_id": AGENTS[1], "warehouse_id": 1662, "name": "1008Redial -CV Marathi",   "leads": 500,
     "paused": True},
    {"id": 9,  "agent_id": AGENTS[0], "warehouse_id": 1654, "name": "1008Redial -PV Malayalam", "leads": 450},
    {"id": 10, "agent_id": AGENTS[1], "warehouse_id": 1671, "name": "1008Redial -TW Tamil",     "leads": 350},
    {"id": 11, "agent_id": AGENTS[0], "warehouse_id": 1655, "name": "1808Redial -PV Marathi",   "leads": 250},
    # Off: finished cohorts the operator has switched off but not deleted.
    {"id": 12, "agent_id": AGENTS[0], "warehouse_id": 1656, "name": "0308Redial -PV English",   "leads": 400,
     "enabled": False},
    {"id": 13, "agent_id": AGENTS[1], "warehouse_id": 1672, "name": "1808Redial -TW Telugu",    "leads": 300,
     "enabled": False},
    {"id": 14, "agent_id": AGENTS[1], "warehouse_id": 1663, "name": "1808Redial -CV English",   "leads": 200,
     "enabled": False},
    # Rehearsal cohorts, one per agent: tiny, always parked in a live bucket, and
    # holding the allow-listed test number so `/api/test-call` resolves a lead.
    # `test: True` keeps their size fixed when `total` scales everything else.
    {"id": 15, "agent_id": AGENTS[0], "warehouse_id": 1690, "name": "TEST -Pipeline Check",     "leads": 6,
     "test": True},
    {"id": 16, "agent_id": AGENTS[1], "warehouse_id": 1691, "name": "TEST -Pipeline Check",     "leads": 6,
     "test": True},
]

TOTAL_LEADS = sum(c["leads"] for c in CAMPAIGNS if not c.get("test"))

# (slug, weight). Roughly the live mix: DNP family ~55%, fresh ~15%,
# connected/CALLBACK ~18%, terminal ~12%.
DISPOSITIONS: list[tuple[str, int]] = [
    ("did_not_pick", 200), ("hung_up", 90), ("unreachable", 80), ("voicemail", 60),
    ("beep_tone_number_busy_not_reachable_switched_off", 80), ("telephony_failed", 40),
    ("", 90), ("new", 60),
    ("positive_followup", 55), ("lead_link_sent_online", 35), ("lead_appointment_fixed", 30),
    ("lead_directed_to_branch", 25), ("lead_cmrl_interested", 20),
    ("lead_premium_quotation_required", 15),
    ("already_paid_to_chola", 45), ("not_interested", 30), ("lost", 15),
    ("wrong_number", 12), ("do_not_call", 10), ("other_language", 8),
]

# (label, weight, dte range). "outside" and the unparseable tail are deliberate:
# the UI has to show OUTSIDE_WINDOW and NO_EXPIRY skips as real numbers.
DTE_SPREAD: list[tuple[str, int, tuple[int, int]]] = [
    ("F1", 18, (32, 45)), ("F2", 14, (24, 31)), ("F3", 14, (16, 23)),
    ("F4", 16, (8, 15)), ("F5", 18, (0, 7)), ("F6", 8, (-3, -1)),
    ("future", 7, (46, 120)), ("lapsed", 2, (-40, -4)), ("bad_red", 3, (0, 0)),
]

BAD_RED = ["", "N/A", "TBD", "31-02-2026", "not available", "0000-00-00", "-"]

FIRST = ["Rajesh", "Priya", "Anil", "Sunita", "Vikram", "Meena", "Suresh", "Kavita",
         "Arjun", "Deepa", "Manoj", "Lakshmi", "Ravi", "Neha", "Sanjay", "Pooja",
         "Karthik", "Anita", "Ganesh", "Divya"]
LAST = ["Sharma", "Iyer", "Reddy", "Nair", "Patel", "Gupta", "Menon", "Rao",
        "Krishnan", "Verma", "Joshi", "Pillai", "Desai", "Mehta"]


def _fmt_red(rnd: random.Random, day: date) -> str:
    """Render a RED the way Formi actually stores it: free text, mixed formats.

    Day-first and slash forms are only emitted when the day component is > 12,
    i.e. when the value is genuinely unambiguous. Seeding an ambiguous value like
    `09-05-2026` would be *realistic* but it would also silently disagree with
    the bucket the generator intended, which makes the fixture useless as a
    reference for the UI.

    A bare `%Y-%m-%d` is NOT unambiguous when the day is <= 12: `parse_red`
    accepts yyyy-dd-mm too, and its renewal-month tie-break reads `2026-10-08`
    as 10 August, moving the lead ~40 days out of the bucket the generator
    intended. Only the timestamp form takes parse_red's ISO fast path, so that
    is what the low-day branch emits.
    """
    if day.day > 12:
        fmt = rnd.choice(["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"])
    else:
        fmt = rnd.choice(["%Y-%m-%d 00:00:00", "%d-%b-%Y"])
    return day.strftime(fmt)


def _weighted(rnd: random.Random, choices: list[tuple[Any, int]]) -> Any:
    total = sum(w for _, w in choices)
    pick = rnd.randrange(total)
    for value, weight in choices:
        pick -= weight
        if pick < 0:
            return value
    return choices[-1][0]


def campaign_sizes(total: int = TOTAL_LEADS) -> dict[int, int]:
    """Per-campaign lead counts, scaled so they sum to roughly `total`.

    TEST campaigns keep their literal size: a rehearsal cohort that shrinks with
    the dataset would eventually lose the lead holding the test number.
    """
    return {c["id"]: (c["leads"] if c.get("test")
                      else max(1, round(c["leads"] * total / TOTAL_LEADS)))
            for c in CAMPAIGNS}


def generate(today: date | None = None, total: int = TOTAL_LEADS,
             seed: int = SEED) -> list[dict[str, Any]]:
    """The dataset, as plain dicts. No I/O — importable from a test."""
    today = today or date.today()
    rnd = random.Random(seed)
    # Phones draw from their OWN stream so adding the column did not reshuffle
    # every other field of the existing fixture.
    phones = random.Random(seed + 1)
    spread = [(name, weight) for name, weight, _ in DTE_SPREAD]
    ranges = {name: bounds for name, _, bounds in DTE_SPREAD}
    sizes = campaign_sizes(total)

    leads: list[dict[str, Any]] = []
    test_seen: dict[int, int] = {}
    roster = [c for c in CAMPAIGNS for _ in range(sizes[c["id"]])]
    for index, campaign in enumerate(roster, start=1):
        stage = _weighted(rnd, DISPOSITIONS)

        group = _weighted(rnd, spread)
        if group == "bad_red":
            red, dte = rnd.choice(BAD_RED), None
        else:
            low, high = ranges[group]
            dte = rnd.randint(low, high)
            red = _fmt_red(rnd, today + timedelta(days=dte))

        # Call history. Fresh/blank leads have none; everything else was dialled
        # at some point in the last three weeks, at a plausible hour of the day.
        if stage in ("", "new"):
            attempts = 0
            last: datetime | None = None
        else:
            attempts = rnd.randint(1, 9)
            days_ago = rnd.choice([1, 1, 2, 2, 3, 4, 5, 6, 8, 11, 14, 20])
            last = datetime.combine(today - timedelta(days=days_ago),
                                    datetime.min.time()).replace(
                hour=rnd.randint(9, 18), minute=rnd.randrange(0, 60))

        # calls_today is rare (the auto run fires in the morning), calls_last_7d
        # is bounded so the weekly budget does not veto the entire sparse cohort.
        calls_today = 1 if (attempts and rnd.random() < 0.05) else 0
        calls_last_7d = calls_today + (rnd.randint(0, 2) if attempts else 0)

        lead = {
            "id": index,
            "campaign_id": campaign["id"],
            "lead_uuid": str(uuid.UUID(int=rnd.getrandbits(128), version=4)),
            "policy_no": f"33{62 + campaign['id']}/{index:06d}/00",
            "contact_id": str(500000 + index),
            "lead_name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
            # Indian mobiles are 10 digits starting 6-9.
            "phone": f"{phones.choice('6789')}{phones.randrange(10 ** 8, 10 ** 9)}",
            "stage": stage,
            "red": red,
            "last_interaction_time": last.isoformat(sep=" ", timespec="seconds") if last else None,
            "total_interactions": attempts,
            "calls_today": calls_today,
            "calls_last_7d": calls_last_7d,
            # A callback slug sometimes carries a customer-named date.
            "callback_date": ((today + timedelta(days=rnd.randint(0, 6))).isoformat()
                              if stage.startswith(("positive", "lead_")) and rnd.random() < 0.4
                              else None),
            "appointment_date": ((today + timedelta(days=rnd.randint(0, 4))).isoformat()
                                 if stage == "lead_appointment_fixed" else None),
        }

        if campaign.get("test"):
            lead.update(_rehearsal(campaign, today, test_seen))
        leads.append(lead)
    return leads


def _rehearsal(campaign: dict[str, Any], today: date, seen: dict[int, int]) -> dict[str, Any]:
    """Override a TEST-campaign lead so it is always genuinely schedulable.

    dte=3 lands in F5 (the live critical window, 2 calls/day) and misses the
    mandatory days [1, 0], the last call was yesterday so the 3h same-day gap is
    clear, and today's/this week's counters are zero. The RED is written as an
    ISO timestamp, the one form `parse_red` cannot misread.
    """
    n = seen[campaign["id"]] = seen.get(campaign["id"], 0) + 1
    fields: dict[str, Any] = {
        "stage": "did_not_pick",
        "red": (today + timedelta(days=3)).strftime("%Y-%m-%d 00:00:00"),
        "lead_name": f"TEST Rehearsal {n}",
        "last_interaction_time": datetime.combine(
            today - timedelta(days=1), datetime.min.time()).replace(hour=11).isoformat(
                sep=" ", timespec="seconds"),
        "total_interactions": 1,
        "calls_today": 0,
        "calls_last_7d": 0,
        "callback_date": None,
        "appointment_date": None,
    }
    if n <= len(TEST_NUMBERS):
        fields["phone"] = TEST_NUMBERS[n - 1]
    return fields


def populate(conn: sqlite3.Connection, today: date | None = None,
             total: int = TOTAL_LEADS) -> int:
    """Reset campaigns + leads. Runs, configs and audit rows are left alone."""
    for campaign in CAMPAIGNS:
        conn.execute(
            "INSERT INTO campaigns (id, agent_id, warehouse_id, name, enabled, paused) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "agent_id=excluded.agent_id, warehouse_id=excluded.warehouse_id, "
            "name=excluded.name, enabled=excluded.enabled, paused=excluded.paused",
            (campaign["id"], campaign["agent_id"], campaign["warehouse_id"], campaign["name"],
             int(campaign.get("enabled", True)), int(campaign.get("paused", False))))

    leads = generate(today, total)
    conn.execute("DELETE FROM leads")
    conn.executemany(
        "INSERT INTO leads (id, campaign_id, lead_uuid, policy_no, contact_id, lead_name, "
        "phone, stage, red, last_interaction_time, total_interactions, calls_today, "
        "calls_last_7d, callback_date, appointment_date) VALUES "
        "(:id,:campaign_id,:lead_uuid,:policy_no,:contact_id,:lead_name,:phone,:stage,:red,"
        ":last_interaction_time,:total_interactions,:calls_today,:calls_last_7d,"
        ":callback_date,:appointment_date)", leads)
    conn.commit()
    return len(leads)


# ---------------------------------------------------------------------------
# The one lead source the app uses
# ---------------------------------------------------------------------------

def load_leads(conn: sqlite3.Connection, campaign: dict[str, Any] | sqlite3.Row,
               today: date | None = None) -> list[dict[str, Any]]:
    """Every lead of one campaign, shaped for `red_engine.decide`."""
    source = (os.environ.get("LEADS_SOURCE") or "seed").strip().lower()
    if source == "metabase":
        from .metabase_source import fetch_redial_leads   # needs credentials
        return fetch_redial_leads([campaign["warehouse_id"]], today=today,
                                  require_red=False)
    if source != "seed":
        raise ValueError(f"LEADS_SOURCE must be 'seed' or 'metabase', got {source!r}")
    rows = conn.execute("SELECT * FROM leads WHERE campaign_id=? ORDER BY id",
                        (campaign["id"],)).fetchall()
    return [dict(row) for row in rows]


def main() -> int:
    from api.db import init_db, current_config, load_env, purge_campaigns

    load_env()
    conn = init_db()
    # A previous `engine.sync` leaves real campaigns in the file; they would sit
    # next to these invented ones with zero leads. Seeding means "the offline
    # dataset, and only that".
    purge_campaigns(conn, [c["id"] for c in CAMPAIGNS])
    count = populate(conn)
    for campaign in CAMPAIGNS:
        current_config(conn, campaign["id"])      # materialises version 1
    print(f"seeded {count} leads across {len(CAMPAIGNS)} campaigns "
          f"-> {conn.execute('PRAGMA database_list').fetchall()[0][2]}")
    by_stage = conn.execute(
        "SELECT stage, COUNT(*) c FROM leads GROUP BY stage ORDER BY c DESC LIMIT 6").fetchall()
    print("top dispositions:", {(r[0] or "(blank)"): r[1] for r in by_stage})
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
