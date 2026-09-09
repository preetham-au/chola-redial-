"""The two things that were silently broken: stage writes, and nothing running.

Everything here stays under the suite's DRY_RUN=1 except one test that flips it
to exercise the stage-write routing — and that one replaces `bulk_update` first,
so no test in this file can reach the network.
"""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from api.db import db_path, now_ist

# IST, not `date.today()`: a pass files its run under the IST calendar day, so on
# a UTC host after 18:30 UTC a suite asking `date.today()` prepares one day and
# asserts against another.
TODAY = now_ist().date()


def _db():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# "It can't mark anything"
# ---------------------------------------------------------------------------

def test_stage_commit_posts_real_leads_and_never_seed_ids(client, monkeypatch):
    """A synced lead goes to Formi; a seeded one goes to the local table only.

    `engine.sync` makes the warehouse id the local id, so a real campaign has
    id == warehouse_id. `engine.seed` invents ids 1-16 with a different
    warehouse_id — posting one of those would mark a stranger's lead.
    """
    from api import routes_stage

    conn = _db()
    conn.execute("INSERT OR REPLACE INTO campaigns (id, agent_id, warehouse_id, name) "
                 "VALUES (9001, 125, 9001, 'synced campaign')")
    conn.execute("INSERT OR REPLACE INTO leads (id, campaign_id, lead_uuid, stage) "
                 "VALUES (90011, 9001, 'uuid-real', 'new')")
    conn.commit()
    seed_lead = conn.execute("SELECT id FROM leads WHERE campaign_id=1 LIMIT 1").fetchone()["id"]

    sent: list[tuple[int, list[int], str]] = []

    def _fake_bulk(agent_id, lead_ids, stage, reason, dry_run=True):
        sent.append((agent_id, list(lead_ids), stage))
        return len(lead_ids), 0

    monkeypatch.setattr(routes_stage, "bulk_update", _fake_bulk)
    monkeypatch.setenv("DRY_RUN", "0")

    result = {"target_stage": "renewed", "lead_ids": [90011, seed_lead]}
    applied = routes_stage._commit(conn, result)

    assert sent == [(125, [90011], "renewed")], "only the synced lead may be posted"
    assert result["applied_formi"] == 1 and result["applied_local"] == 1
    assert applied == 2
    # The whole bug in one assertion: the seed lead moved locally, the real one
    # did not — the local table is never the destination for a synced lead.
    stages = dict(conn.execute("SELECT id, stage FROM leads WHERE id IN (?,?)",
                               (90011, seed_lead)))
    assert stages[seed_lead] == "renewed" and stages[90011] == "new"
    conn.close()


def test_stage_commit_is_a_no_op_under_dry_run(client):
    from api import routes_stage

    conn = _db()
    assert routes_stage._commit(conn, {"target_stage": "renewed", "lead_ids": [1, 2]}) == 0
    conn.close()


# ---------------------------------------------------------------------------
# "It doesn't run by itself"
# ---------------------------------------------------------------------------

@pytest.fixture
def armed(monkeypatch):
    """Campaign 16 in the daily plan, with the warehouse calls stubbed out."""
    from api import autopilot

    monkeypatch.setattr(autopilot, "_resync", lambda campaign_id, day: 0)
    monkeypatch.setattr(autopilot, "_resync_status", lambda day: [])
    monkeypatch.setattr(autopilot, "remaining_leads", lambda conn, campaign_id, day=None: 42)
    conn = _db()
    conn.execute("UPDATE campaigns SET autopilot=0")
    conn.execute("UPDATE campaigns SET autopilot=1, enabled=1, paused=0 WHERE id=16")
    conn.execute("DELETE FROM plan_items WHERE run_id IN "
                 "(SELECT id FROM runs WHERE campaign_id=16)")
    conn.execute("DELETE FROM runs WHERE campaign_id=16")
    conn.execute("DELETE FROM dial_log WHERE campaign_id=16")
    conn.commit()
    conn.close()
    return 16


def _first(result: dict) -> dict:
    campaigns = result["campaigns"]
    assert campaigns, "the pass looked at no campaigns"
    return campaigns[0]


def _skip_if_shut(entry: dict) -> None:
    if entry.get("status") == "window_closed":
        pytest.skip("today's dial window has already closed")


def test_a_pass_prepares_a_plan_and_dials_nothing(armed):
    """The whole of the approval gate: the clock plans, it never calls."""
    from api.autopilot import AM, run_pass

    entry = _first(run_pass(AM, TODAY))
    _skip_if_shut(entry)
    assert entry["status"] == "prepared"
    assert entry["ready"] > 0

    conn = _db()
    run = conn.execute("SELECT * FROM runs WHERE campaign_id=? AND kind=? AND run_date=?",
                       (armed, AM, TODAY.isoformat())).fetchone()
    assert run["status"] == "planned", "a pass must never commit a run"
    assert run["posted"] == 0
    # Nothing was sent, so nothing was logged as sent — not even a simulation.
    assert conn.execute("SELECT COUNT(*) c FROM dial_log WHERE campaign_id=?",
                        (armed,)).fetchone()["c"] == 0
    conn.close()


def test_no_wave_books_both_calls_of_the_day_up_front(armed):
    """"2nd call only if the 1st is not answered" — so one slot per lead per wave.

    Booking slot 2 in the morning commits the afternoon call before anyone has
    picked up the phone, which is the rule inverted.
    """
    from api.autopilot import AM, run_pass

    _skip_if_shut(_first(run_pass(AM, TODAY)))

    conn = _db()
    slots = [r["slot_no"] for r in conn.execute(
        "SELECT p.slot_no FROM plan_items p JOIN runs r ON r.id=p.run_id "
        "WHERE r.campaign_id=? AND r.kind=? AND r.run_date=?",
        (armed, AM, TODAY.isoformat()))]
    conn.close()
    assert slots, "the morning pass planned nothing to check"
    assert set(slots) == {1}, f"morning pass pre-booked a second call: slots={set(slots)}"


def test_a_second_pass_is_a_no_op_rather_than_a_second_plan(armed):
    from api.autopilot import AM, run_pass

    _skip_if_shut(_first(run_pass(AM, TODAY)))
    assert _first(run_pass(AM, TODAY))["status"] == "prepared", \
        "re-preparing an unapproved plan is allowed — it is still `planned`"

    # Once approved it is not: replacing a run that has been acted on would
    # rewrite history, and re-approving would dial the same leads twice.
    from api.day import approve_day, ApproveBody
    approve_day(ApproveBody(date=TODAY.isoformat()))
    assert _first(run_pass(AM, TODAY))["status"] == "already_ran"


def test_it_switches_itself_off_when_nothing_is_left(armed, monkeypatch):
    from api import autopilot

    monkeypatch.setattr(autopilot, "remaining_leads", lambda conn, campaign_id, day=None: 0)
    assert _first(autopilot.run_pass(autopilot.AM, TODAY))["status"] == "finished"

    conn = _db()
    row = conn.execute("SELECT autopilot, autopilot_note FROM campaigns WHERE id=?",
                       (armed,)).fetchone()
    conn.close()
    assert row["autopilot"] == 0 and "finished" in row["autopilot_note"]


def test_an_unreachable_warehouse_never_counts_as_finished(armed, monkeypatch):
    """None is not zero. A Metabase outage must not retire a live campaign."""
    from api import autopilot

    monkeypatch.setattr(autopilot, "remaining_leads", lambda conn, campaign_id, day=None: None)
    autopilot.run_pass(autopilot.AM, TODAY)

    conn = _db()
    assert conn.execute("SELECT autopilot FROM campaigns WHERE id=?", (armed,)).fetchone()[0] == 1
    conn.close()


def test_a_failed_resync_skips_the_campaign_rather_than_planning_stale_leads(armed, monkeypatch):
    from api import autopilot

    def _boom(campaign_id, day):
        raise RuntimeError("Could not reach Metabase")

    monkeypatch.setattr(autopilot, "_resync", _boom)
    assert _first(autopilot.run_pass(autopilot.AM, TODAY))["status"] == "resync_failed"

    conn = _db()
    assert not conn.execute("SELECT 1 FROM runs WHERE campaign_id=? AND run_date=?",
                            (armed, TODAY.isoformat())).fetchone()
    assert "re-sync failed" in conn.execute(
        "SELECT autopilot_note FROM campaigns WHERE id=?", (armed,)).fetchone()[0]
    conn.close()


def test_pause_and_disable_both_stop_it(armed):
    from api.autopilot import AM, run_pass

    conn = _db()
    conn.execute("UPDATE campaigns SET paused=1 WHERE id=?", (armed,))
    conn.commit()
    assert run_pass(AM, TODAY)["campaigns"] == []
    conn.execute("UPDATE campaigns SET paused=0, enabled=0 WHERE id=?", (armed,))
    conn.commit()
    assert run_pass(AM, TODAY)["campaigns"] == []
    conn.close()


def test_a_pause_in_formi_is_seen_before_the_next_wave_is_planned(client, armed, monkeypatch):
    """The client pauses at 11:00; the 15:00 wave must not plan that campaign.

    Only a full sync used to re-read campaign status, so between two syncs a
    campaign paused in Formi kept `paused=0` here and was planned — and dialled —
    by the afternoon wave. Every pass now re-reads status first.
    """
    from api.autopilot import PM, run_pass
    from engine import sync
    from engine.sync import PLATFORM_PAUSE, refresh_campaign_status

    conn = _db()
    agent = conn.execute("SELECT agent_id FROM campaigns WHERE id=?", (armed,)).fetchone()[0]
    conn.execute("UPDATE campaigns SET platform_status='active' WHERE id=?", (armed,))
    conn.commit()

    warehouse = [
        {"campaign_id": armed, "agent_id": agent, "campaign_name": "paused one",
         "campaign_status": "paused"},
        # A campaign the console does not hold: capped out of the sync, or with no
        # parseable RED. Re-admitting it here would undo that filtering.
        {"campaign_id": 99123, "agent_id": agent, "campaign_name": "not ours",
         "campaign_status": "active"},
    ]
    monkeypatch.setattr(sync.ms, "fetch_agent_campaigns",
                        lambda agent_id, config=None, schema=None, today=None: warehouse)
    monkeypatch.setattr("api.autopilot._resync_status",
                        lambda day: refresh_campaign_status(_db(), [agent], None, None, day))

    assert run_pass(PM, TODAY)["campaigns"] == [], "a campaign paused in Formi was planned"

    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (armed,)).fetchone()
    assert row["paused"] == 1 and row["autopilot"] == 0
    assert row["stopped_reason"] == PLATFORM_PAUSE
    # Latched, so a resume in THIS console puts it back — a resume in Formi must not.
    assert row["autopilot_latched"] == 1
    assert conn.execute("SELECT 1 FROM campaigns WHERE id=99123").fetchone() is None
    conn.close()


def test_a_resume_in_formi_does_not_restart_calls_here(client, armed, monkeypatch):
    """paused -> active in the warehouse is deliberately not copied back."""
    from engine import sync
    from engine.sync import refresh_campaign_status

    conn = _db()
    agent = conn.execute("SELECT agent_id FROM campaigns WHERE id=?", (armed,)).fetchone()[0]
    conn.execute("UPDATE campaigns SET platform_status='paused', paused=1, autopilot=0 "
                 "WHERE id=?", (armed,))
    conn.commit()
    monkeypatch.setattr(sync.ms, "fetch_agent_campaigns",
                        lambda agent_id, config=None, schema=None, today=None: [
                            {"campaign_id": armed, "agent_id": agent, "campaign_name": "back",
                             "campaign_status": "active"}])

    assert refresh_campaign_status(_db(), [agent], None, None, TODAY) == []
    row = conn.execute("SELECT paused, autopilot FROM campaigns WHERE id=?", (armed,)).fetchone()
    assert row["paused"] == 1 and row["autopilot"] == 0, "Formi resumed it and calls restarted"
    conn.close()


def test_autopilot_switch_endpoints(client, armed):
    assert client.post("/api/campaigns/16/autopilot", json={"on": False}).json()["autopilot"] is False
    assert client.post("/api/campaigns/16/autopilot", json={"on": True}).json()["autopilot"] is True
    assert client.post("/api/campaigns/999/autopilot", json={"on": True}).status_code == 404
    body = client.get("/api/autopilot").json()
    assert [c["id"] for c in body["campaigns"]] == [16]
    # The switch decides who is IN the plan; it can never place a call.
    assert body["dials"] is False


# ---------------------------------------------------------------------------
# "Nothing dials until I approve, every day"
# ---------------------------------------------------------------------------

def _prepare(client, armed) -> dict:
    body = client.post("/api/day/prepare", json={"date": TODAY.isoformat()}).json()
    _skip_if_shut(_first(body))
    return body


def test_the_day_waits_for_an_approval_and_dials_nothing_before_it(client, armed):
    prepared = _prepare(client, armed)
    assert prepared["ready"] > 0

    day = client.get(f"/api/day?date={TODAY.isoformat()}").json()
    assert day["status"] == "awaiting_approval"
    assert day["totals"]["ready"] == prepared["ready"]
    assert day["totals"]["posted"] == 0
    assert day["dry_run"] is True, "the suite must never dial for real"
    assert [c["id"] for c in day["campaigns"]] == [armed]
    assert day["campaigns"][0]["run_status"] == "planned"


def test_red_bands_lead_the_order_and_the_buckets_follow_them(client, armed):
    """Just-lapsed (RED+1..+3) first, then the run-up (RED-7..RED).

    Ahead of the bucket order, not inside it. The pair is the client's two
    2-calls/day rows negated out of their sign convention into dte.
    """
    _prepare(client, armed)
    day = client.get(f"/api/day?date={TODAY.isoformat()}").json()

    bands = day["red_bands"]
    assert [(b["dte_from"], b["dte_to"]) for b in bands[:2]] == [(-1, -3), (7, 0)]
    assert bands[0]["label"] == "1-3 days past RED"
    assert bands[1]["label"] == "RED day and the 7 days before it"
    assert bands[-1]["rank"] == len(bands) - 1, "the catch-all band is always last"
    assert sum(b["ready"] for b in bands) == day["totals"]["ready"]

    ranks = [b["best_rank"] for b in day["buckets"]]
    assert ranks == sorted(ranks), "buckets are listed in RED-band order"


# ---------------------------------------------------------------------------
# No campaign's config is the day's
# ---------------------------------------------------------------------------
# `dial_window`, `max_per_minute` and `red_priority` are per-campaign and each is
# editable per campaign, but the day view read `campaigns[0]`'s and presented it
# as the whole day's. Nothing showed today: all 69 live campaigns land on
# 09:00-20:00 once `with_defaults` retires the stale 09:30-19:00 snapshot, so the
# borrowed value happened to be right. It stops being right the first time an
# operator narrows one campaign's window — which is one PUT away, and is what
# `test_one_campaigns_edited_window_does_not_become_the_days` exercises.

def _cfg(start: str, end: str, per_minute: int = 10, red=None):
    body = {"dial_window": {"start": start, "end": end}, "max_per_minute": per_minute}
    return {**body, "red_priority": red} if red else body


def test_the_days_window_is_the_envelope_and_says_when_it_is_not_shared():
    from api.day import _day_window

    same = _day_window({1: _cfg("09:30", "19:00"), 2: _cfg("09:30", "19:00")},
                       {1: 0, 2: 0}, floor=600, today=True)
    assert same["window"] == {"start": "09:30", "end": "19:00"}
    assert same["varies"] is False

    mixed = _day_window({1: _cfg("09:30", "19:00"), 2: _cfg("09:00", "20:00")},
                        {1: 0, 2: 0}, floor=600, today=True)
    assert mixed["window"] == {"start": "09:00", "end": "20:00"}, "earliest start, latest end"
    assert mixed["varies"] is True, "the screen must not imply a shared close time"


def test_the_day_is_open_while_any_campaign_can_still_dial():
    from api.day import _day_window

    late = 19 * 60 + 30           # 19:30 — past the 19:00 campaign, inside the 20:00 one
    configs = {1: _cfg("09:30", "19:00"), 2: _cfg("09:00", "20:00")}
    assert _day_window(configs, {}, floor=late, today=True)["open"] is True
    assert _day_window({1: configs[1]}, {}, floor=late, today=True)["open"] is False

    shut = 20 * 60
    assert _day_window(configs, {}, floor=shut, today=True)["open"] is False


def test_capacity_is_capped_per_campaign_before_it_is_summed():
    """A campaign that shuts at 19:00 cannot absorb another campaign's leads.

    One total capacity against one total ready would promise a day that does not
    exist: the roomy campaign's spare minutes would silently cover the shut one's
    backlog.
    """
    from api.day import _day_window

    # 18:00. Campaign 1 has 60 minutes left at 10/min = 600 slots for 5000 leads;
    # campaign 2 has 120 minutes at 10/min = 1200 slots but only 10 leads waiting.
    span = _day_window({1: _cfg("09:30", "19:00"), 2: _cfg("09:00", "20:00")},
                       {1: 5000, 2: 10}, floor=18 * 60, today=True)
    assert span["capacity"] == 600 + 10

    # Its own max_per_minute, not the first campaign's.
    slow = _day_window({1: _cfg("09:30", "19:00", per_minute=1)},
                       {1: 5000}, floor=18 * 60, today=True)
    assert slow["capacity"] == 60


def test_a_day_with_nothing_armed_still_answers_with_a_window():
    from api.day import _day_window

    empty = _day_window({}, {}, floor=600, today=True)
    assert empty["open"] is False and empty["capacity"] == 0
    assert empty["window"] == {"start": "09:00", "end": "20:00"}


def test_each_campaigns_leads_are_ranked_by_its_own_red_bands():
    """Rank is a position, so it stays comparable — the LABEL is what cannot be
    borrowed. Where the campaigns disagree the row says so rather than naming
    days most of them do not treat as priority."""
    from api.day import _band_rows

    shared = ((-1, -3), (0, 7))
    agreed = _band_rows({1: shared, 2: shared}, {0: 5, 1: 3, 2: 1})
    assert [(b["dte_from"], b["dte_to"]) for b in agreed] == [(-1, -3), (7, 0), (None, None)]
    assert agreed[0]["label"] == "1-3 days past RED"
    assert agreed[-1]["label"] == "outside the priority bands"
    assert [b["ready"] for b in agreed] == [5, 3, 1]

    split = _band_rows({1: shared, 2: ((0, 7), (-1, -3))}, {0: 5})
    assert split[0]["label"] == "priority 1 — differs between campaigns"
    assert (split[0]["dte_from"], split[0]["dte_to"]) == (None, None)
    assert split[0]["ready"] == 5, "the count is still the whole day's"
    assert split[-1]["label"] == "outside the priority bands"


def test_a_campaign_with_more_bands_does_not_swallow_the_catch_all_row():
    """Rank len(bands) is 'outside' for one campaign and a real band for another,
    so that position holds both and cannot be labelled either."""
    from api.day import _band_rows

    rows = _band_rows({1: ((-1, -3), (0, 7)), 2: ((-1, -3), (0, 7), (8, 15))}, {})
    assert len(rows) == 4
    assert rows[2]["label"] == "priority 3 — differs between campaigns"
    assert rows[3]["label"] == "outside the priority bands"


def test_one_campaigns_edited_window_does_not_become_the_days(client):
    """The whole path, not the helper: an operator narrows ONE campaign's hours.

    This is how the borrowed config surfaces. Every live campaign shares
    09:00-20:00 today, so reading the first one's was accidentally right; one PUT
    on one campaign is all it takes for the header to name a close time the other
    campaigns do not keep.
    """
    conn = _db()
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM campaigns WHERE enabled=1 AND paused=0 ORDER BY id LIMIT 2")]
    conn.execute("UPDATE campaigns SET autopilot=0")
    conn.executemany("UPDATE campaigns SET autopilot=1 WHERE id=?", [(i,) for i in ids])
    conn.commit()
    conn.close()
    assert len(ids) == 2, "this test needs two campaigns to disagree"

    shared = client.get(f"/api/day?date={TODAY.isoformat()}").json()
    assert shared["window_varies"] is False
    assert shared["window"] == {"start": "09:00", "end": "20:00"}

    # The LOWER id is edited, so a run reading campaigns[0] would report 18:00 for
    # both campaigns — the failure this is here to catch — and the higher one is
    # left alone so there is a campaign the edit must not speak for.
    config = client.get(f"/api/campaigns/{ids[0]}/config").json()
    saved = client.put(f"/api/campaigns/{ids[0]}/config",
                       json={**config, "dial_window": {"start": "09:00", "end": "18:00"}})
    assert saved.status_code == 200, saved.text

    try:
        day = client.get(f"/api/day?date={TODAY.isoformat()}").json()
        assert day["window_varies"] is True, "the screen must say the campaigns disagree"
        assert day["window"] == {"start": "09:00", "end": "20:00"}, (
            "the envelope — no call goes out before 09:00 or after 20:00, and the "
            "edited campaign's 18:00 is not the day's close")
    finally:
        # The `client` fixture is session-scoped and a config PUT appends a
        # version rather than replacing one, so a narrowed window left behind
        # here would silently shorten this campaign's day for every test after
        # it — and only after 18:00 IST, which is the worst kind of flake.
        client.put(f"/api/campaigns/{ids[0]}/config", json=config)


@pytest.fixture
def armed_all(monkeypatch):
    """Every campaign in the daily plan — one plan across all of them, as asked."""
    from api import autopilot

    monkeypatch.setattr(autopilot, "_resync", lambda campaign_id, day: 0)
    monkeypatch.setattr(autopilot, "_resync_status", lambda day: [])
    monkeypatch.setattr(autopilot, "remaining_leads", lambda conn, campaign_id, day=None: 42)
    conn = _db()
    conn.execute("UPDATE campaigns SET autopilot=1, paused=0, autopilot_latched=0 "
                 "WHERE enabled=1")
    conn.execute("DELETE FROM plan_items")
    conn.execute("DELETE FROM decisions")
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM dial_log")
    conn.commit()
    ids = [r["id"] for r in conn.execute("SELECT id FROM campaigns WHERE autopilot=1")]
    conn.close()
    return ids


def test_approving_dials_only_the_ticked_buckets(client, armed_all):
    prepared = client.post("/api/day/prepare", json={"date": TODAY.isoformat()}).json()
    if not prepared["ready"]:
        pytest.skip("today's dial window has already closed")

    day = client.get(f"/api/day?date={TODAY.isoformat()}").json()
    offered = [b["bucket"] for b in day["buckets"]]
    assert len(offered) >= 2, f"nothing to narrow: the whole plan is {offered}"
    pick = offered[:1]

    result = client.post("/api/day/approve",
                         json={"date": TODAY.isoformat(), "buckets": pick}).json()
    assert result["dry_run"] is True
    assert result["posted"] > 0

    conn = _db()
    dialled = {r["bucket"] for r in conn.execute("SELECT DISTINCT bucket FROM dial_log")}
    outcomes = {r["outcome"] for r in conn.execute("SELECT DISTINCT outcome FROM dial_log")}
    conn.close()
    assert dialled == set(pick), f"an un-ticked bucket was dialled: {dialled - set(pick)}"
    assert outcomes == {"simulated"}, "DRY_RUN must never produce a real outcome"


def test_an_unticked_bucket_is_not_dialled_today_and_is_not_lost(client, armed_all):
    """The un-ticked leads are still evaluated and recorded — just not called."""
    prepared = client.post("/api/day/prepare", json={"date": TODAY.isoformat()}).json()
    if not prepared["ready"]:
        pytest.skip("today's dial window has already closed")
    day = client.get(f"/api/day?date={TODAY.isoformat()}").json()
    pick = [day["buckets"][0]["bucket"]]
    dropped = sum(b["ready"] for b in day["buckets"][1:])

    client.post("/api/day/approve", json={"date": TODAY.isoformat(), "buckets": pick})

    conn = _db()
    # `decisions` stays the full audit whatever was ticked, so tomorrow's plan
    # rebuilds from the same leads rather than from a narrowed copy.
    audited = conn.execute(
        "SELECT COUNT(DISTINCT d.bucket) c FROM decisions d JOIN runs r ON r.id=d.run_id "
        "WHERE r.run_date=?", (TODAY.isoformat(),)).fetchone()["c"]
    conn.close()
    assert dropped > 0 and audited > len(pick)


def test_approving_twice_does_not_dial_twice(client, armed):
    _prepare(client, armed)
    first = client.post("/api/day/approve", json={"date": TODAY.isoformat()}).json()
    assert first["approved"] == 1

    again = client.post("/api/day/approve", json={"date": TODAY.isoformat()}).json()
    assert again["approved"] == 0
    assert again["campaigns"][0]["status"] == "already_committed"

    conn = _db()
    sent = conn.execute("SELECT COUNT(*) c FROM dial_log WHERE campaign_id=?",
                        (armed,)).fetchone()["c"]
    conn.close()
    assert sent == first["posted"], "the second approval sent something"


def test_a_hidden_campaign_is_neither_planned_nor_approved(client, armed):
    """Hidden has to hold on the day path, not just in the picker.

    Un-ticking a campaign and hiding it look the same on screen; only one of them
    survives a stale client posting the id back, which is why approve is handed
    the id explicitly here.
    """
    _prepare(client, armed)
    before = client.get(f"/api/day?date={TODAY.isoformat()}").json()
    assert before["totals"]["ready"] > 0, "nothing was planned, so nothing is being tested"

    try:
        assert client.post(f"/api/campaigns/{armed}/hide").status_code == 200

        day = client.get(f"/api/day?date={TODAY.isoformat()}").json()
        assert day["campaigns"] == []
        assert day["totals"]["ready"] == 0 and day["totals"]["campaigns"] == 0
        assert day["capacity_before_close"] == 0

        again = client.post("/api/day/prepare", json={"date": TODAY.isoformat()}).json()
        assert [c for c in again["campaigns"] if c["campaign_id"] == armed] == []

        result = client.post("/api/day/approve",
                             json={"date": TODAY.isoformat(), "campaign_ids": [armed]}).json()
        assert result["approved"] == 0 and result["posted"] == 0
    finally:
        client.post(f"/api/campaigns/{armed}/unhide")


def test_a_hidden_campaign_still_dialling_today_is_never_off_screen(client, armed):
    """Hiding leaves today's queued calls running — so it stays listed while they run.

    Invisible everywhere plus still dialling is the one state this console must
    not have: a call goes out with nothing on any screen saying so. It shows under
    "held back" for as long as it has slots left on Formi's clock, then drops off.
    """
    _prepare(client, armed)
    conn = _db()
    run = conn.execute("SELECT id FROM runs WHERE campaign_id=? AND run_date=?",
                       (armed, TODAY.isoformat())).fetchone()["id"]
    conn.execute("UPDATE runs SET status='committed' WHERE id=?", (run,))
    # An hour out, so this does not depend on what time the suite runs -- except
    # after 23:00, when nothing can still be queued for today and the point is moot.
    later = (now_ist() + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:00")
    if later[:10] != TODAY.isoformat():
        conn.close()
        pytest.skip("past 23:00 IST: no slot can still be ahead of the clock today")
    conn.execute("UPDATE plan_items SET status='simulated', scheduled_time=? WHERE run_id=?",
                 (later, run))
    conn.commit()

    try:
        body = client.post(f"/api/campaigns/{armed}/hide").json()
        assert body["live_today"] > 0, "the count the operator is shown was wrong"

        day = client.get(f"/api/day?date={TODAY.isoformat()}").json()
        assert day["campaigns"] == [], "a hidden campaign was still offered for approval"
        held = [c for c in day["stopped"] if c["id"] == armed]
        assert len(held) == 1 and "hidden" in held[0]["why"]
        assert "still going out" in held[0]["why"]
    finally:
        client.post(f"/api/campaigns/{armed}/unhide")
        conn.execute("UPDATE runs SET status='planned' WHERE id=?", (run,))
        conn.execute("UPDATE plan_items SET status='planned' WHERE run_id=?", (run,))
        conn.commit()
        conn.close()


def test_a_paused_campaign_is_neither_planned_nor_approved(client, armed):
    _prepare(client, armed)
    assert client.post(f"/api/campaigns/{armed}/pause").status_code == 200

    day = client.get(f"/api/day?date={TODAY.isoformat()}").json()
    assert day["campaigns"] == []
    assert [c["id"] for c in day["stopped"]] == [armed]

    result = client.post("/api/day/approve", json={"date": TODAY.isoformat()}).json()
    assert result["approved"] == 0 and result["posted"] == 0
