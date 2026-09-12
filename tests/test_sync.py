"""What makes a sync skip a campaign's leads, and what must never let it.

`campaign_fingerprint` is a performance optimisation that can go wrong in only
one direction that matters: claiming nothing has changed when something has, so
the console shows yesterday's stages over a campaign that has been dialled all
morning. Every test here is about that direction. The speed it buys is not
asserted anywhere -- it is the reason the code exists, not a property worth
pinning a test to.
"""
from __future__ import annotations

import sqlite3

import pytest

from api.db import db_path, now_ist
from engine.sync import campaign_fingerprint

TODAY = now_ist().date()

# The columns the campaigns query hands back that say something about a campaign's
# leads. Named once so a test can move exactly one of them.
ROW = {"campaign_id": 16, "agent_id": 125, "campaign_name": "fp", "campaign_status": "active",
       "leads": 400, "leads_with_red": 120, "leads_in_red_window": 30,
       "dials": 88, "connected_dials": 41, "queued_today": 12,
       "last_dial_at": "2026-09-12T09:30:00+05:30"}


@pytest.fixture()
def conn(client):
    """A real store. `client` builds it; the fingerprint reads config and leads."""
    db = sqlite3.connect(db_path())
    db.row_factory = sqlite3.Row
    yield db
    db.close()


def _fp(conn, **moved):
    return campaign_fingerprint(conn, {**ROW, **moved}, TODAY, 5000, False)


def test_the_same_warehouse_row_twice_is_the_same_fingerprint(conn):
    """The whole point: an unchanged campaign is recognised as unchanged.

    If this is ever flaky the optimisation is worse than useless -- it would
    re-pull everything AND carry the cost of asking.
    """
    assert _fp(conn) == _fp(conn)


@pytest.mark.parametrize("field, value", [
    # A lead was added or removed in the warehouse.
    ("leads", 401),
    ("leads_with_red", 121),
    # The RED window's population changed under a fixed `today` -- an edited RED.
    ("leads_in_red_window", 31),
    # Somebody dialled. This is the case a COUNT on its own would miss: the lead
    # count stands perfectly still while stage and disposition move underneath it,
    # and stage is most of what the console actually shows.
    ("dials", 89),
    ("connected_dials", 42),
    ("queued_today", 13),
    # A re-dial that left all three counters equal still moves the clock.
    ("last_dial_at", "2026-09-12T09:31:00+05:30"),
    # Killed or paused in Formi.
    ("campaign_status", "paused"),
])
def test_anything_that_moves_a_lead_moves_the_fingerprint(conn, field, value):
    assert _fp(conn, **{field: value}) != _fp(conn), f"{field} changed and the sync would skip it"


def test_the_day_is_in_it_so_a_rollover_always_re_pulls(conn):
    """The RED window is relative to today, so two days cannot share a verdict.

    Without this a midnight rollover that happens to leave the same number of
    leads in the window is invisible, and the campaign keeps a window computed
    against yesterday. It also buys one unconditional full pull per day, which is
    the bound on everything the fingerprint cannot see.
    """
    from datetime import timedelta

    row = dict(ROW)
    assert (campaign_fingerprint(conn, row, TODAY, 5000, False)
            != campaign_fingerprint(conn, row, TODAY + timedelta(days=1), 5000, False))


def test_a_wider_window_set_here_re_pulls_even_when_the_warehouse_is_silent(conn):
    """`refresh_campaign_leads` reads the window from `current_config`, not defaults.

    So an operator widening the frequency table changes which leads a pull
    returns while every warehouse counter stands still. The fingerprint has to
    carry the campaign's OWN window or that edit shows up nowhere until tomorrow.

    `dte_min`/`dte_max` are the whole of it because they are the whole of what
    the pull uses -- `refresh_campaign_leads` passes those two and nothing else.
    A window edit that leaves both bounds where they were changes which leads get
    DIALLED but not which get fetched, so skipping is still correct.
    """
    from api.db import current_config, insert_config

    campaign_id = ROW["campaign_id"]
    before = _fp(conn)
    was = current_config(conn, campaign_id)
    insert_config(conn, campaign_id, {**was, "frequency_table": [
        # from_dte is the edge FURTHER from RED, so it is the larger number.
        {"bucket": "F1", "label": "wide", "from_dte": 30, "to_dte": -30,
         "calls_per_week": 3, "calls_per_day": 1}]})
    try:
        assert _fp(conn) != before, "the window was widened and the sync would skip it"
    finally:
        # `config` is append-only, so the tidy-up is dropping the version we added
        # rather than editing one. The client fixture is session-scoped: a campaign
        # left on a 60-day window here is a wrong answer in some later test.
        conn.execute("DELETE FROM config WHERE campaign_id=? AND version>?",
                     (campaign_id, was["version"]))
        conn.commit()


def test_the_shape_of_the_pull_is_in_it(conn):
    """`--leads` and a force-sync's all_leads change what a pull would return."""
    row = dict(ROW)
    base = campaign_fingerprint(conn, row, TODAY, 5000, False)
    assert campaign_fingerprint(conn, row, TODAY, 100, False) != base
    # all_leads=True skips the RED window entirely -- same warehouse, different rows.
    assert campaign_fingerprint(conn, row, TODAY, 5000, True) != base


def test_a_store_that_lost_its_leads_re_pulls(conn):
    """The local half. The warehouse has not moved; our copy of it has.

    A purge, a restore from an older db, a pull that died halfway: the campaign
    has fewer leads here than it should and nothing upstream will ever say so.
    """
    campaign_id = ROW["campaign_id"]
    before = _fp(conn)
    removed = conn.execute("SELECT * FROM leads WHERE campaign_id=? LIMIT 1",
                           (campaign_id,)).fetchone()
    assert removed is not None, "fixture campaign has no leads to drop"
    conn.execute("DELETE FROM leads WHERE id=?", (removed["id"],))
    conn.commit()
    try:
        assert _fp(conn) != before, "leads went missing locally and the sync would skip it"
    finally:
        cols = ",".join(removed.keys())
        conn.execute(f"INSERT INTO leads ({cols}) VALUES ({','.join('?' * len(cols.split(',')))})",
                     tuple(removed))
        conn.commit()


# ---------------------------------------------------------------------------
# The two ways a NEW campaign used to arrive broken
#
# Both bite only a campaign nobody has dialled yet, which is why they read as
# one complaint -- "new campaigns never sync properly" -- and needed two fixes.
# ---------------------------------------------------------------------------

def _warehouse_row(**over):
    return {**ROW, "campaign_id": 9500, "campaign_name": "brand new", **over}


def _campaign(conn, campaign_id):
    return conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()


@pytest.fixture()
def fresh_campaign(conn):
    """An id no fixture owns, removed again afterwards.

    `conn` comes from the session-scoped `client`, so a row left behind here is
    a row every later test sees.
    """
    ids = (9500, 9501)
    yield ids
    for campaign_id in ids:
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
    conn.commit()


def test_a_new_active_campaign_arrives_armed(conn, fresh_campaign):
    """Created in Formi, dialling here, with nobody having to find a switch.

    This is the whole of the operator's complaint: before it, `autopilot` took
    its schema default of 0 and no sync ever raised it, so a new campaign synced
    with all its leads and then sat there. On 12 Sep 2026 campaigns 1818 and
    1819 were the only two of 101 still at 0.
    """
    from engine.sync import upsert_campaign

    upsert_campaign(conn, _warehouse_row(campaign_status="active"))
    conn.commit()
    row = _campaign(conn, 9500)
    assert row["autopilot"] == 1
    assert "armed on arrival" in row["autopilot_note"], "the operator is told why"


@pytest.mark.parametrize("status", ["paused", "killed"])
def test_a_new_campaign_that_is_not_running_in_formi_does_not_arm_itself(
        conn, fresh_campaign, status):
    """Arriving armed is for campaigns Formi says are live. Nothing else."""
    from engine.sync import upsert_campaign

    upsert_campaign(conn, _warehouse_row(campaign_status=status))
    conn.commit()
    assert _campaign(conn, 9500)["autopilot"] == 0


def test_a_later_sync_never_re_arms_a_campaign_the_operator_switched_off(
        conn, fresh_campaign):
    """The reason this is an INSERT-only write, and the test that keeps it one.

    Arming on every sync would take the switch away from the operator entirely:
    disarm a campaign at 11:00 and the 11:05 sync starts planning it again. The
    old rule -- a sync never writes `autopilot` -- was protecting exactly this,
    and it still holds for every sync after the first.
    """
    from engine.sync import upsert_campaign

    row = _warehouse_row(campaign_status="active")
    upsert_campaign(conn, row)
    conn.commit()
    conn.execute("UPDATE campaigns SET autopilot=0, autopilot_note='operator stopped it' "
                 "WHERE id=9500")
    conn.commit()

    upsert_campaign(conn, row)              # the next sync, same active campaign
    conn.commit()
    assert _campaign(conn, 9500)["autopilot"] == 0, "a sync re-armed a disarmed campaign"


def test_a_campaign_killed_in_formi_is_still_disarmed_by_a_sync(conn, fresh_campaign):
    """The other direction, which was already true and must stay true."""
    from engine.sync import upsert_campaign

    upsert_campaign(conn, _warehouse_row(campaign_status="active"))
    conn.commit()
    assert _campaign(conn, 9500)["autopilot"] == 1

    upsert_campaign(conn, _warehouse_row(campaign_status="killed"))
    conn.commit()
    row = _campaign(conn, 9500)
    assert row["autopilot"] == 0 and row["enabled"] == 0


def test_fresh_leads_pages_past_the_metabase_row_cap(monkeypatch):
    """The silent half: a never-dialled campaign bigger than the cap lost leads.

    Metabase's /api/dataset stops at `ROW_CAP` rows whatever the SQL asks for,
    and `fetch_fresh_leads` asked once. For a campaign with dial history that
    barely showed -- most of its leads come from the paging `fetch_redial_leads`
    -- but a NEW campaign has no history at all, so this query is its entire
    lead list. Campaign 1818 stored 2,000 of its 2,530 leads and said nothing:
    `sync` only warns at `--leads` (5,000), so 530 customers were never dialled
    and no log line existed to notice it.
    """
    import re

    import engine.metabase_source as ms
    from engine.sync import fetch_fresh_leads

    monkeypatch.setattr(ms, "ROW_CAP", 10)
    warehouse = [{"warehouse_lead_id": i, "lead_uuid": f"u{i}"} for i in range(1, 26)]
    pages: list[int] = []

    def fake_run_sql(sql, config=None, **kwargs):
        after = int(re.search(r"v\.id > (\d+)", sql).group(1))
        limit = int(re.search(r"LIMIT (\d+)", sql).group(1))
        assert "ORDER BY v.id" in sql, "a keyset cursor without an order is not a cursor"
        page = [r for r in warehouse if r["warehouse_lead_id"] > after][:limit]
        pages.append(len(page))
        return page

    monkeypatch.setattr(ms, "run_sql", fake_run_sql)
    rows = fetch_fresh_leads(9500, None)

    assert [r["warehouse_lead_id"] for r in rows] == list(range(1, 26)), \
        "every lead in the campaign, not just the first page"
    assert pages == [10, 10, 5], f"expected three pages ending short, got {pages}"


def test_fresh_leads_stops_asking_once_a_page_comes_back_short(monkeypatch):
    """The cheap case must stay cheap: one page when one page is the whole thing.

    Paging that always asks twice would double the cost of every small campaign,
    and most of them are small.
    """
    import engine.metabase_source as ms
    from engine.sync import fetch_fresh_leads

    monkeypatch.setattr(ms, "ROW_CAP", 10)
    calls = []

    def fake_run_sql(sql, config=None, **kwargs):
        calls.append(sql)
        return [{"warehouse_lead_id": i} for i in range(1, 4)]

    monkeypatch.setattr(ms, "run_sql", fake_run_sql)
    assert len(fetch_fresh_leads(9500, None)) == 3
    assert len(calls) == 1, "a short first page is the last page"
