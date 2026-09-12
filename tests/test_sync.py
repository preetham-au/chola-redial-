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
