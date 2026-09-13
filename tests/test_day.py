"""The daily gate: stranded runs, wave bands, agent scoping, proof."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.db import session


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


def _armed_campaign_id() -> int:
    with session() as conn:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE autopilot=1 AND enabled=1 AND paused=0 "
            "AND hidden=0 ORDER BY id").fetchone()
    assert row is not None, "the fixture DB must have at least one armed campaign"
    return int(row["id"])


def test_stranded_lists_a_past_planned_run(client):
    """A run left `planned` on an earlier date is 491 calls nobody dialled."""
    campaign_id = _armed_campaign_id()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _seed_run(campaign_id, yesterday, "auto", "planned", 3)

    body = client.get("/api/day").json()
    stranded = {s["campaign_id"]: s for s in body["stranded"]}

    assert campaign_id in stranded, "yesterday's undialled plan must be reported"
    assert stranded[campaign_id]["slots"] == 3
    assert stranded[campaign_id]["run_date"] == yesterday


def test_stranded_ignores_today_and_committed_runs(client):
    """Today's plan is awaiting approval, not stranded; a committed run dialled."""
    campaign_id = _armed_campaign_id()
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _seed_run(campaign_id, today, "auto", "planned", 5)
    _seed_run(campaign_id, yesterday, "auto_pm", "committed", 7)

    stranded = client.get("/api/day").json()["stranded"]

    assert all(s["run_date"] != today for s in stranded), \
        "today's plan is awaiting approval, not abandoned"
    assert all(not (s["run_date"] == yesterday and s["kind"] == "auto_pm")
               for s in stranded), "a committed run did dial"
