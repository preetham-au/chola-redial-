"""The daily gate: stranded runs, wave bands, agent scoping, proof."""
from __future__ import annotations

from datetime import timedelta

import pytest

from api.day import ARMED, STRANDED_DAYS, WAVE_BOUNDARY, _band
from api.db import now_ist, session
from engine.dispatcher import DispatchConfig


@pytest.fixture(autouse=True)
def clean_runs():
    """Drop every seeded run after each test.

    `client` in tests/conftest.py is scope="session" (line 21), so one database
    serves the whole file. Without this, a run seeded by an earlier test is still
    there for a later one -- and `last_dialled` is a MAX over all of a campaign's
    runs, so a leaked row silently changes another test's answer. Seeded runs are
    tagged note='seeded'; nothing else in the suite writes that value.
    """
    yield
    with session() as conn:
        conn.execute("DELETE FROM decisions WHERE run_id IN "
                     "(SELECT id FROM runs WHERE note='seeded')")
        conn.execute("DELETE FROM plan_items WHERE run_id IN "
                     "(SELECT id FROM runs WHERE note='seeded')")
        conn.execute("DELETE FROM runs WHERE note='seeded'")
        conn.commit()


@pytest.fixture(autouse=True)
def restore_campaigns():
    """Put every campaign's flags back after each test.

    These tests have to arm campaigns to have anything to assert on, and the
    `client` fixture is session-scoped (tests/conftest.py:21), so one database
    serves every file. Without this, arming here would leak into whatever runs
    next -- which is exactly the bug this fixture exists to stop repeating.
    """
    cols = "autopilot, enabled, paused, hidden"
    with session() as conn:
        before = [tuple(r) for r in conn.execute(f"SELECT {cols}, id FROM campaigns")]
    yield
    with session() as conn:
        conn.executemany("UPDATE campaigns SET autopilot=?, enabled=?, paused=?, "
                         "hidden=? WHERE id=?", before)
        conn.commit()


def _seed_run(campaign_id: int, run_date: str, kind: str, status: str, slots: int) -> int:
    """A run with `slots` planned items, exactly as _write_run would leave it."""
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO runs (campaign_id, run_date, kind, status, config_version, "
            "created_at, dry_run, evaluated, planned, slots, posted, failed, dropped, note) "
            "VALUES (?,?,?,?,1,?,1,?,?,?,0,0,0,'seeded')",
            (campaign_id, run_date, kind, status, f"{run_date}T09:00:00", slots, slots, slots))
        run_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO plan_items (run_id, lead_uuid, policy_no, phone, disposition, "
            "disposition_class, dte, bucket, bucket_label, priority, slot_no, "
            "scheduled_time, status) VALUES (?,?,?,?,'','',0,'M0','M0',0,1,?,'planned')",
            [(run_id, f"lead-{run_id}-{i}", f"P{run_id}{i}", "9" * 10,
              f"{run_date}T10:0{i % 10}:00") for i in range(slots)])
        conn.commit()
    return run_id


def _arm(count: int = 1) -> list[int]:
    """Arm one campaign on each of `count` distinct agents; return their ids.

    engine/seed.py ships every campaign disarmed and the day endpoints only look
    at armed ones, so a test that does not arm anything is asserting against an
    empty roster. One campaign per agent, because the agent-scoping tests need
    two agents that are genuinely separable.
    """
    with session() as conn:
        rows = conn.execute(
            "SELECT id, agent_id FROM campaigns ORDER BY agent_id, id").fetchall()
    first_of_agent: dict[int, int] = {}
    for row in rows:
        first_of_agent.setdefault(row["agent_id"], row["id"])
    ids = list(first_of_agent.values())[:count]
    if len(ids) < count:
        pytest.skip(f"fixture DB has fewer than {count} agents")
    with session() as conn:
        conn.executemany("UPDATE campaigns SET autopilot=1, enabled=1, paused=0, "
                         "hidden=0 WHERE id=?", [(i,) for i in ids])
        conn.commit()
    return ids


def test_stranded_lists_a_past_planned_run(client):
    """A run left `planned` on an earlier date is 491 calls nobody dialled."""
    campaign_id = _arm()[0]
    yesterday = (now_ist().date() - timedelta(days=1)).isoformat()
    _seed_run(campaign_id, yesterday, "auto", "planned", 3)

    body = client.get("/api/day").json()
    stranded = {s["campaign_id"]: s for s in body["stranded"]}

    assert campaign_id in stranded, "yesterday's undialled plan must be reported"
    assert stranded[campaign_id]["slots"] == 3
    assert stranded[campaign_id]["run_date"] == yesterday


def test_stranded_ignores_today_and_committed_runs(client):
    """One seeded run per predicate the query is made of, each of which must fall out.

    Scanned per (date, kind) and filtered to the campaign under test: `_arm`
    leaves other campaigns armed and `clean_runs` only removes note='seeded'
    rows, so a bare `all(...)` over every stranded row would be answering about
    somebody else's campaign.
    """
    campaign_id = _arm()[0]
    # now_ist, not date.today: the server clamps against ITS today, and on a
    # non-IST host the two are a day apart for part of every day.
    day = now_ist().date()
    today = day.isoformat()
    yesterday = (day - timedelta(days=1)).isoformat()
    two_days_ago = (day - timedelta(days=2)).isoformat()
    long_ago = (day - timedelta(days=20)).isoformat()
    _seed_run(campaign_id, today, "auto", "planned", 5)
    _seed_run(campaign_id, yesterday, "auto_pm", "committed", 7)
    _seed_run(campaign_id, long_ago, "auto", "planned", 9)
    _seed_run(campaign_id, two_days_ago, "auto", "planned", 0)

    seen = {(s["run_date"], s["kind"]) for s in client.get("/api/day").json()["stranded"]
            if s["campaign_id"] == campaign_id}

    assert (today, "auto") not in seen, "today's plan is awaiting approval, not abandoned"
    assert (yesterday, "auto_pm") not in seen, "a committed run did dial"
    assert (long_ago, "auto") not in seen, \
        f"older than the {STRANDED_DAYS}-day bound is history, not a thing to act on"
    assert (two_days_ago, "auto") not in seen, "a plan holding no slots dialled nothing"


def test_stranded_reports_only_campaigns_in_the_daily_plan(client):
    """A stranded plan on a campaign nobody armed is not this screen's business.

    Every other test here seeds into the campaign `_arm` just armed, so none of
    them touched the roster predicate: deleting it from the query outright left
    all three green. It is the line that decides whose runs reach the warning, so
    it gets its own test -- a campaign that is NOT in the daily plan was never
    going to be dialled today and is not a plan somebody forgot to approve.

    The unarmed campaign is read back out of the DB rather than hardcoded: `_arm`
    picks its ids from the same table, and `restore_campaigns` puts every flag
    back afterwards, so an id that is disarmed today may not be tomorrow.
    """
    armed = _arm()[0]
    with session() as conn:
        # Genuinely failing ARMED as the request will evaluate it -- asked AFTER
        # `_arm` has run, so whatever it just armed cannot come back from here.
        unarmed = [r["id"] for r in conn.execute(
            f"SELECT id FROM campaigns WHERE NOT ({ARMED}) ORDER BY id")]
    assert unarmed, "the seed DB must hold a campaign that is not in the daily plan"

    yesterday = (now_ist().date() - timedelta(days=1)).isoformat()
    _seed_run(armed, yesterday, "auto", "planned", 3)
    _seed_run(unarmed[0], yesterday, "auto", "planned", 5)

    reported = {s["campaign_id"] for s in client.get("/api/day").json()["stranded"]}

    # Both halves, so a seeding mistake cannot pass this off as a clean exclusion.
    assert armed in reported, "an armed campaign's undialled plan is still reported"
    assert unarmed[0] not in reported, \
        "a campaign outside the daily plan must not appear in the stranded warning"


def test_stranded_ignores_today_when_a_future_day_is_requested(client):
    """Asking for tomorrow must not report this morning's queued plan as abandoned.

    The date input has no upper bound, so this is one click away. `now_ist`
    rather than `date.today`: the clamp is against the server's IST today.
    """
    campaign_id = _arm()[0]
    today = now_ist().date()
    _seed_run(campaign_id, today.isoformat(), "auto", "planned", 4)

    body = client.get(f"/api/day?date={(today + timedelta(days=1)).isoformat()}").json()

    assert all(s["run_date"] != today.isoformat() for s in body["stranded"]
               if s["campaign_id"] == campaign_id), \
        "today's plan is awaiting approval, not abandoned"


def test_stranded_still_looks_back_when_a_far_future_day_is_requested(client):
    """The lower bound is clamped with the upper, so the window cannot invert.

    Same unbounded date input as the test above. Keyed off the REQUESTED day, a
    date more than STRANDED_DAYS out pushed `since` past `upper` and the window
    collapsed to nothing -- the screen then said no plan was stranded, which is
    the one answer this warning must never give wrongly.
    """
    campaign_id = _arm()[0]
    today = now_ist().date()
    yesterday = (today - timedelta(days=1)).isoformat()
    _seed_run(campaign_id, yesterday, "auto", "planned", 6)

    far = (today + timedelta(days=STRANDED_DAYS + 1)).isoformat()
    body = client.get(f"/api/day?date={far}").json()

    assert any(s["run_date"] == yesterday for s in body["stranded"]
               if s["campaign_id"] == campaign_id), \
        "yesterday's undialled plan is stranded whatever date the screen asks for"


# ---------------------------------------------------------------------------
# Wave bands
# ---------------------------------------------------------------------------
# `_band` is pure, but the autouse fixtures above are not: both open the DB, and
# only the session-scoped `client` fixture creates it. These take `client` so the
# section can be run on its own (`-k band`) rather than only after a test that
# happens to have built the database first.

def test_band_clips_morning_to_the_first_half_of_the_day(client):
    dcfg = DispatchConfig(start_min=9 * 60, end_min=20 * 60)
    band = _band("auto", dcfg)
    assert band.start_min == 9 * 60, "morning keeps the campaign's own opening"
    assert band.end_min == WAVE_BOUNDARY, "morning must stop at the boundary"


def test_band_clips_afternoon_to_the_second_half_of_the_day(client):
    dcfg = DispatchConfig(start_min=9 * 60, end_min=20 * 60)
    band = _band("auto_pm", dcfg)
    assert band.start_min == WAVE_BOUNDARY, "afternoon must not start before the boundary"
    assert band.end_min == 20 * 60, "afternoon keeps the campaign's own close"


def test_band_never_widens_a_narrow_campaign_window(client):
    """A campaign that shuts at 13:00 has no afternoon at all."""
    dcfg = DispatchConfig(start_min=10 * 60, end_min=13 * 60)
    morning = _band("auto", dcfg)
    assert (morning.start_min, morning.end_min) == (10 * 60, 13 * 60), \
        "the band must never open earlier or close later than the campaign itself"
    afternoon = _band("auto_pm", dcfg)
    assert afternoon.start_min >= afternoon.end_min, \
        "an empty band is how 'this wave cannot run here' is expressed"


def test_band_leaves_other_config_untouched(client):
    dcfg = DispatchConfig(start_min=9 * 60, end_min=20 * 60, max_per_minute=7, max_per_run=99)
    band = _band("auto", dcfg)
    assert band.max_per_minute == 7 and band.max_per_run == 99
    assert band.red_priority == dcfg.red_priority


def test_prepare_reports_the_band_that_closed_not_the_whole_window(client, monkeypatch):
    """Preparing the morning wave after the boundary must name the BAND.

    Before bands, this said "the 09:00-20:00 window has closed" only after 20:00,
    and happily planned a 'morning' wave into the evening at any hour before it.
    """
    import api.day as day_module

    _arm()
    # Captured BEFORE the patch. Reading `day_module.now_ist` from inside the
    # replacement would be the replacement reading itself -- infinite recursion.
    real_now_ist = day_module.now_ist
    monkeypatch.setattr(day_module, "now_ist", lambda: real_now_ist().replace(
        hour=15, minute=0, second=0, microsecond=0))

    out = day_module.prepare_day(real_now_ist().date(), "auto")
    closed = [c for c in out["campaigns"] if c["status"] == "window_closed"]

    assert closed, "the morning band is shut at 15:00 - every campaign must say so"
    assert "13:30" in closed[0]["detail"], \
        f"the detail must name the band, got {closed[0]['detail']!r}"
    assert "20:00" not in closed[0]["detail"], \
        "naming the full window hides the fact that the morning band is what shut"


def test_a_campaign_with_no_afternoon_band_neither_prepares_nor_approves(client):
    """A campaign that shuts at 13:00 cannot run the afternoon wave at all.

    Dated TOMORROW on purpose: `_floor_min` returns None for a day that is not
    today, so the clock guard cannot fire and the empty band is the only thing
    left that can close this window. Without it `dispatch` would be handed a
    window whose start is past its end.

    Both halves, because they are two separate guards in two functions and the
    approve one is the one that reaches Formi. `_prepare_one`/`_approve_one`
    rather than the day-wide passes, which have no campaign filter and would
    write runs for every other armed campaign as a side effect.
    """
    import api.day as day_module

    campaign_id = _arm()[0]
    tomorrow = now_ist().date() + timedelta(days=1)
    config = client.get(f"/api/campaigns/{campaign_id}/config").json()
    saved = client.put(f"/api/campaigns/{campaign_id}/config",
                       json={**config, "dial_window": {"start": "09:00", "end": "13:00"}})
    assert saved.status_code == 200, saved.text
    try:
        prepared = day_module._prepare_one(campaign_id, tomorrow, "auto_pm", False)
        # An approve needs a `planned` run in front of it, or it stops at
        # `not_prepared` before ever reaching the band.
        _seed_run(campaign_id, tomorrow.isoformat(), "auto_pm", "planned", 3)
        with session() as conn:
            campaign = conn.execute("SELECT * FROM campaigns WHERE id=?",
                                    (campaign_id,)).fetchone()
            approved = day_module._approve_one(conn, campaign, tomorrow, "auto_pm", [])
    finally:
        # Session-scoped `client`: a narrowed window left behind would shorten
        # this campaign's day for every test after it.
        client.put(f"/api/campaigns/{campaign_id}/config", json=config)

    assert prepared["status"] == "window_closed", prepared
    assert "no afternoon band" in prepared["detail"], prepared["detail"]
    assert approved["status"] == "window_closed", approved
    assert "no afternoon band" in approved["detail"], approved["detail"]
    assert approved.get("posted") is None, "an empty band must never reach Formi"


def test_the_window_a_campaign_has_no_band_for_reports_no_hours(client):
    """The header must not invert when a campaign has no hours in this wave.

    Clipping alone leaves start past end. `capacity` and `open` both read that as
    zero either way, but `window` is a STRING on the operator's screen and
    "13:30-13:00" is not a window anybody can act on.
    """
    from api.day import AFTERNOON, _day_window

    span = _day_window({1: {"dial_window": {"start": "09:00", "end": "13:00"},
                            "max_per_minute": 10}},
                       {1: 500}, floor=10 * 60, today=True, kind=AFTERNOON)

    assert span["window"] == {"start": "13:30", "end": "13:30"}, \
        "a band with no hours in it must not be reported as a backwards window"
    assert span["open"] is False, "there is no afternoon here to open"
    assert span["capacity"] == 0, "no hours means no capacity, whatever is ready"
