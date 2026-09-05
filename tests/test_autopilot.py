"""The two things that were silently broken: stage writes, and nothing running.

Everything here stays under the suite's DRY_RUN=1 except one test that flips it
to exercise the stage-write routing — and that one replaces `bulk_update` first,
so no test in this file can reach the network.
"""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from api.db import db_path

TODAY = datetime.date.today()


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
    """Campaign 16 on autopilot, with the warehouse calls stubbed out."""
    from api import autopilot

    monkeypatch.setattr(autopilot, "_resync", lambda campaign_id, day: 0)
    monkeypatch.setattr(autopilot, "remaining_leads", lambda conn, campaign_id, day=None: 42)
    conn = _db()
    conn.execute("UPDATE campaigns SET autopilot=0")
    conn.execute("UPDATE campaigns SET autopilot=1, enabled=1, paused=0 WHERE id=16")
    conn.execute("DELETE FROM runs WHERE campaign_id=16")
    conn.commit()
    conn.close()
    return 16


def _buckets(conn, campaign_id: int, kind: str) -> set[str]:
    return {r["bucket"] for r in conn.execute(
        "SELECT DISTINCT bucket FROM plan_items p JOIN runs r ON r.id=p.run_id "
        "WHERE r.campaign_id=? AND r.kind=? AND r.run_date=?",
        (campaign_id, kind, TODAY.isoformat()))}


def test_morning_pass_dials_urgent_and_leaves_the_rest_for_approval(armed):
    from api.autopilot import AM, REVIEW, REVIEW_BUCKETS, URGENT, run_pass

    result = run_pass(AM, TODAY)[0]
    if result["urgent"].get("status") == "window_closed":
        pytest.skip("today's dial window has already closed")

    assert result["status"] == "ran"
    assert result["urgent"]["status"] == "committed"
    assert result["urgent"]["dry_run"] is True, "the suite must never dial for real"
    assert result["review"]["status"] == "planned"
    assert result["review"]["awaiting_approval"] is True

    conn = _db()
    assert _buckets(conn, armed, AM) <= set(URGENT)
    assert _buckets(conn, armed, REVIEW) <= set(REVIEW_BUCKETS)
    # The review run is a plan, not a dial: nothing in it was put on the clock.
    assert conn.execute("SELECT status FROM runs WHERE campaign_id=? AND kind=? AND run_date=?",
                        (armed, REVIEW, TODAY.isoformat())).fetchone()["status"] == "planned"
    conn.close()


def test_no_pass_books_both_calls_of_the_day_up_front(armed):
    """"2nd call only if the 1st is not answered" — so one slot per lead per pass.

    Booking slot 2 in the morning commits the afternoon call before anyone has
    picked up the phone, which is the rule inverted.
    """
    from api.autopilot import AM, run_pass

    if run_pass(AM, TODAY)[0]["urgent"].get("status") == "window_closed":
        pytest.skip("today's dial window has already closed")

    conn = _db()
    slots = [r["slot_no"] for r in conn.execute(
        "SELECT p.slot_no FROM plan_items p JOIN runs r ON r.id=p.run_id "
        "WHERE r.campaign_id=? AND r.kind=? AND r.run_date=?",
        (armed, AM, TODAY.isoformat()))]
    conn.close()
    assert slots, "the morning pass planned nothing to check"
    assert set(slots) == {1}, f"morning pass pre-booked a second call: slots={set(slots)}"


def test_a_second_morning_pass_does_not_dial_again(armed):
    from api.autopilot import AM, run_pass

    if run_pass(AM, TODAY)[0]["urgent"].get("status") == "window_closed":
        pytest.skip("today's dial window has already closed")
    assert run_pass(AM, TODAY)[0]["urgent"]["status"] == "already_ran"


def test_it_switches_itself_off_when_nothing_is_left(armed, monkeypatch):
    from api import autopilot

    monkeypatch.setattr(autopilot, "remaining_leads", lambda conn, campaign_id, day=None: 0)
    assert autopilot.run_pass(autopilot.AM, TODAY)[0]["status"] == "finished"

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


def test_a_failed_resync_skips_the_campaign_rather_than_dialling_stale_leads(armed, monkeypatch):
    from api import autopilot

    def _boom(campaign_id, day):
        raise RuntimeError("Could not reach Metabase")

    monkeypatch.setattr(autopilot, "_resync", _boom)
    result = autopilot.run_pass(autopilot.AM, TODAY)[0]
    assert result["status"] == "resync_failed"

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
    assert run_pass(AM, TODAY) == []
    conn.execute("UPDATE campaigns SET paused=0, enabled=0 WHERE id=?", (armed,))
    conn.commit()
    assert run_pass(AM, TODAY) == []
    conn.close()


def test_autopilot_switch_endpoints(client, armed):
    assert client.post("/api/campaigns/16/autopilot", json={"on": False}).json()["autopilot"] is False
    assert client.post("/api/campaigns/16/autopilot", json={"on": True}).json()["autopilot"] is True
    assert client.post("/api/campaigns/999/autopilot", json={"on": True}).status_code == 404
    body = client.get("/api/autopilot").json()
    assert [c["id"] for c in body["campaigns"]] == [16]
    assert body["urgent_buckets"] == ["M0", "E0", "F6", "F5"]
