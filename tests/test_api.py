"""API contract behaviour, including the DRY_RUN no-network guarantee."""
from __future__ import annotations

import datetime
import json
import sqlite3

import pytest
import requests

from api.db import NO_TOKEN, db_path, formi_token

# The seed anchors every RED to the day it was written, so bucket-sensitive
# assertions have to ask about today.
TODAY = datetime.date.today().isoformat()
# A slot the dial-window and past-time checks always accept, whatever o'clock
# the suite runs at. Tomorrow morning is inside 09:30-19:00 and never behind us.
TOMORROW_AM = f"{(datetime.date.today() + datetime.timedelta(days=1)).isoformat()}T10:05:00"


def _db():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _plan_today(client, campaign_id: int, **body):
    """Plan `campaign_id` for today, or skip once the dial window has shut.

    After 19:00 IST there is no legal slot left on today's clock, so the server
    answers 422 by design. A suite that runs in the evening should say "not
    applicable now", not fail -- but it must still fail on any OTHER 422.
    """
    r = client.post(f"/api/campaigns/{campaign_id}/plan", json={"date": TODAY, **body})
    if r.status_code == 422 and "closed" in r.json().get("error", ""):
        pytest.skip("today's dial window has already closed")
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    from engine.seed import AGENTS, TEST_NUMBERS

    body = client.get("/api/health").json()
    assert body == {"ok": True, "dry_run": True, "db": "test.db", "leads_source": "seed",
                    "agents": sorted(AGENTS), "test_numbers": TEST_NUMBERS}


def test_campaigns_and_pause(client):
    from engine.seed import CAMPAIGNS

    campaigns = client.get("/api/campaigns").json()
    assert len(campaigns) == len(CAMPAIGNS) and campaigns[0]["paused"] is False
    # The seed's enabled/paused flags survive into the API, so the UI's filters
    # have something to filter.
    assert sum(not c["enabled"] for c in campaigns) == 3
    assert sum(c["paused"] for c in campaigns) == 1
    assert client.post("/api/campaigns/1/pause").json()["paused"] is True
    assert client.post("/api/campaigns/1/resume").json()["paused"] is False
    assert client.get("/api/campaigns/999/config").status_code == 404


# ---------------------------------------------------------------------------
# Config versioning
# ---------------------------------------------------------------------------

def test_put_config_inserts_a_new_version_and_never_mutates(client):
    before = client.get("/api/campaigns/3/config").json()
    new = {**before, "dial_window": {"start": "10:15", "end": "18:45"}, "max_per_minute": 7}
    after = client.put("/api/campaigns/3/config", json=new).json()

    assert after["version"] == before["version"] + 1
    assert client.get("/api/campaigns/3/config").json()["dial_window"]["start"] == "10:15"

    with _db() as conn:
        rows = conn.execute("SELECT version, body FROM config WHERE campaign_id=3 "
                            "ORDER BY version").fetchall()
    assert [r["version"] for r in rows] == list(range(1, after["version"] + 1))
    # The original row is byte-for-byte what it was: a PUT inserts, never updates.
    assert json.loads(rows[0]["body"])["dial_window"] == before["dial_window"]

    history = client.get("/api/campaigns/3/config/history").json()
    assert [h["version"] for h in history] == sorted(
        (r["version"] for r in rows), reverse=True)


@pytest.mark.parametrize("window", [{"start": "08:00", "end": "19:00"},
                                    {"start": "09:00", "end": "20:00"},
                                    {"start": "18:00", "end": "10:00"}])
def test_put_config_422_on_a_bad_dial_window(client, window):
    response = client.put("/api/campaigns/2/config", json={"dial_window": window})
    assert response.status_code == 422
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# Per-bucket disposition allow-list
# ---------------------------------------------------------------------------

def _f5_dispositions(client, campaign_id, day):
    """Dispositions that actually made it into an F5 slot of a fresh plan."""
    run = _plan_today(client, campaign_id) if day == TODAY else \
        client.post(f"/api/campaigns/{campaign_id}/plan", json={"date": day}).json()
    items = client.get(f"/api/runs/{run['id']}/items?bucket=F5&page_size=500").json()["items"]
    return run, {i["disposition"] for i in items}


def test_bucket_dispositions_round_trip(client):
    base = client.get("/api/campaigns/6/config").json()
    assert base["bucket_dispositions"] == {}          # default: every bucket inherits

    wanted = {"F5": ["did_not_pick", "voicemail"], "F1": ["did_not_pick"]}
    saved = client.put("/api/campaigns/6/config", json={**base,
                                                        "bucket_dispositions": wanted}).json()
    assert saved["bucket_dispositions"] == wanted
    assert client.get("/api/campaigns/6/config").json()["bucket_dispositions"] == wanted
    # ...and the engine can still be built from it, so a plan runs.
    assert _plan_today(client, 6)["status"] == "planned"


def test_put_config_422_on_an_unknown_bucket(client):
    response = client.put("/api/campaigns/2/config",
                          json={"bucket_dispositions": {"F9": ["did_not_pick"]}})
    assert response.status_code == 422
    assert "F9" in response.json()["error"]


def test_bucket_allow_list_narrows_only_its_own_bucket(client):
    base = client.get("/api/campaigns/5/config").json()
    _run, before = _f5_dispositions(client, 5, TODAY)
    assert "hung_up" in before and "did_not_pick" in before

    client.put("/api/campaigns/5/config",
               json={**base, "bucket_dispositions": {"F5": ["did_not_pick"]}})
    run, after = _f5_dispositions(client, 5, TODAY)
    assert after == {"did_not_pick"}                  # hung_up is off in F5 now

    # F3 has no list of its own, so it still schedules hung_up.
    f3 = client.get(f"/api/runs/{run['id']}/items?bucket=F3&page_size=500").json()["items"]
    assert "hung_up" in {i["disposition"] for i in f3}

    # The skip is reported, and reported under its own code.
    buckets = client.get(f"/api/campaigns/5/buckets?date={TODAY}").json()
    assert buckets["skips"].get("BUCKET_DISPOSITION_OFF", 0) > 0
    with _db() as conn:
        rows = conn.execute(
            "SELECT bucket, disposition FROM decisions WHERE run_id=? AND action=?",
            (run["id"], "BUCKET_DISPOSITION_OFF")).fetchall()
    assert rows and {r["bucket"] for r in rows} == {"F5"}
    assert "did_not_pick" not in {r["disposition"] for r in rows}

    # An empty list means "inherit", not "dial nothing": F5 goes back to normal.
    client.put("/api/campaigns/5/config", json={**base, "bucket_dispositions": {"F5": []}})
    _run, restored = _f5_dispositions(client, 5, TODAY)
    assert restored == before


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_plan_produces_a_prioritised_non_empty_run(client):
    run = client.post("/api/campaigns/1/plan", json={"date": "2026-08-28"}).json()
    assert run["status"] == "planned" and run["kind"] == "auto"
    assert run["counts"]["planned"] > 0 and run["counts"]["slots"] >= run["counts"]["planned"]

    items = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    assert items and all(i["status"] == "planned" for i in items)
    # Everything is inside the dial window, and priority tracks the bucket order.
    for item in items:
        hour = int(item["scheduled_time"][11:13])
        assert 9 <= hour <= 19
    # Read from the campaign's own config rather than a copy of it, so adding a
    # bucket to the default table does not silently make this a no-op.
    priority = client.get("/api/campaigns/1/config").json()["bucket_priority"]
    order = {b: i for i, b in enumerate(priority)}
    assert "E0" in order
    assert all(item["priority"] == order[item["bucket"]] for item in items)
    # No connected lead reached an auto plan.
    assert all(item["disposition_class"] in ("dnp", "fresh") or item["bucket"] == "M0"
               for item in items)


def test_replanning_replaces_the_planned_run(client):
    first = client.post("/api/campaigns/2/plan", json={"date": "2026-09-01"}).json()
    second = client.post("/api/campaigns/2/plan", json={"date": "2026-09-01"}).json()
    assert second["id"] != first["id"]
    assert client.get(f"/api/runs/{first['id']}").status_code == 404
    with _db() as conn:
        orphans = conn.execute("SELECT COUNT(*) c FROM plan_items WHERE run_id=?",
                               (first["id"],)).fetchone()["c"]
    assert orphans == 0


def test_item_filters_and_paging(client):
    run = client.post("/api/campaigns/1/plan", json={"date": "2026-08-28"}).json()
    page = client.get(f"/api/runs/{run['id']}/items?bucket=F5&page=1&page_size=5").json()
    assert page["page_size"] == 5 and len(page["items"]) <= 5
    assert all(i["bucket"] == "F5" for i in page["items"])
    assert client.get(f"/api/runs/{run['id']}/items?status=posted").json()["total"] == 0


def test_buckets_matrix(client):
    body = client.get("/api/campaigns/1/buckets?date=2026-08-28").json()
    assert body["date"] == "2026-08-28" and body["total_leads"] > 0
    assert {"buckets", "dispositions", "matrix", "skips"} <= set(body)
    manual_only = sum(b["manual_only"] for b in body["buckets"])
    assert manual_only == body["skips"].get("MANUAL_ONLY", 0)
    assert any(d["disposition"] == "positive_followup" and d["auto"] is False
               for d in body["dispositions"])


def test_plan_can_be_narrowed_to_urgent_buckets(client):
    urgent = ["M0", "F6", "F5"]
    wide = client.post("/api/campaigns/1/plan", json={"date": "2026-08-28"}).json()
    narrow = client.post("/api/campaigns/1/plan",
                         json={"date": "2026-08-28", "buckets": urgent}).json()

    # Same book evaluated either way — only the slots are narrowed.
    assert narrow["counts"]["evaluated"] == wide["counts"]["evaluated"]
    assert 0 < narrow["counts"]["slots"] < wide["counts"]["slots"]

    items = client.get(f"/api/runs/{narrow['id']}/items?page_size=500").json()["items"]
    assert items and {i["bucket"] for i in items} <= set(urgent)
    # A dialable number came along for the ride, or the operator cannot call anyone.
    assert all(i["phone"] and i["phone"].isdigit() for i in items)

    assert client.post("/api/campaigns/1/plan",
                       json={"date": "2026-08-28", "buckets": ["F9"]}).status_code == 422


def test_planning_today_never_schedules_into_the_past(client):
    from api.db import now_ist

    run = _plan_today(client, 1)
    items = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    if not items:
        pytest.skip("nothing due today in the seed")
    now = now_ist()
    assert min(i["scheduled_time"] for i in items) >= now.strftime("%Y-%m-%dT%H:%M")


def test_plan_honours_a_narrowed_dial_window(client):
    """A per-run window confines the slots without touching the saved config."""
    before = client.get("/api/campaigns/1/config").json()["dial_window"]

    run = client.post("/api/campaigns/1/plan",
                      json={"date": "2026-09-03", "start": "14:00", "end": "16:00"}).json()
    items = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    assert items, "the seed should have something due"
    assert all("14:00" <= i["scheduled_time"][11:16] <= "16:00" for i in items)
    assert "window=14:00-16:00" in run["note"]

    # The override is for this run only — the campaign still dials its own hours.
    assert client.get("/api/campaigns/1/config").json()["dial_window"] == before

    # And it cannot be used to escape the permitted dialling hours.
    assert client.post("/api/campaigns/1/plan",
                       json={"date": "2026-09-03", "start": "03:00", "end": "23:00"}
                       ).status_code == 422
    assert client.post("/api/campaigns/1/plan",
                       json={"date": "2026-09-03", "start": "16:00", "end": "14:00"}
                       ).status_code == 422


def test_todays_plan_refuses_a_window_that_has_already_closed(client):
    from api.db import now_ist

    if now_ist().hour < 11:
        pytest.skip("the window used here has not closed yet")
    r = client.post("/api/campaigns/1/plan",
                    json={"date": TODAY, "start": "09:30", "end": "10:00"})
    assert r.status_code == 422 and "already" in r.json()["error"]


def test_hand_edited_slot_obeys_the_same_rules_as_the_planner(client):
    run = client.post("/api/campaigns/1/plan", json={"date": "2026-08-28"}).json()
    item = client.get(f"/api/runs/{run['id']}/items?page_size=1").json()["items"][0]
    patch = f"/api/runs/{run['id']}/items/{item['id']}"

    assert client.patch(patch, json={"scheduled_time": "2026-08-29T11:00"}).status_code == 422
    assert client.patch(patch, json={"scheduled_time": "2026-08-28T04:00"}).status_code == 422
    assert client.patch(patch, json={"scheduled_time": "2026-08-28T23:30"}).status_code == 422
    assert client.patch(patch, json={"scheduled_time": "nonsense"}).status_code == 422

    ok = client.patch(patch, json={"scheduled_time": "2026-08-28T15:07"})
    assert ok.status_code == 200 and ok.json()["scheduled_time"] == "2026-08-28T15:07:00"


def test_approving_a_stale_plan_retires_the_slots_that_have_passed(client):
    """A plan left sitting must never post a time that has already gone."""
    from api.db import db_path, now_ist

    if now_ist().hour < 12:
        pytest.skip("needs a time of day with room to put slots behind the clock")
    run = _plan_today(client, 3)
    items = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    if len(items) < 2:
        pytest.skip("nothing due today in the seed")

    # Drag the first slot into the past, exactly as the clock would have.
    conn = sqlite3.connect(db_path())
    conn.execute("UPDATE plan_items SET scheduled_time=? WHERE id=?",
                 (f"{TODAY}T09:31:00", items[0]["id"]))
    conn.commit()
    conn.close()

    done = client.post(f"/api/runs/{run['id']}/approve").json()
    assert done["expired"] >= 1
    assert done["counts"]["posted"] == len(items) - done["expired"]

    after = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    retired = [i for i in after if i["id"] == items[0]["id"]][0]
    assert retired["status"] == "expired" and retired["response"] is None
    # Nothing that survived was left behind, and nothing stale was sent.
    assert all(i["scheduled_time"][:16] >= f"{TODAY}T09:31" or i["status"] == "expired"
               for i in after)
    assert all(i["status"] == "expired" for i in after if i["scheduled_time"] < f"{TODAY}T09:32")


def test_second_daily_call_can_be_limited_by_disposition(client):
    """F5/F6/M0 offer two calls; the disposition decides who earns the second."""
    cfg = client.get("/api/campaigns/4/config").json()

    def slot_twos(config):
        assert client.put("/api/campaigns/4/config", json=config).status_code == 200
        run = client.post("/api/campaigns/4/plan", json={"date": "2026-09-04"}).json()
        items = client.get(f"/api/runs/{run['id']}/items?page_size=2000").json()["items"]
        return [i for i in items if i["slot_no"] == 2]

    both = slot_twos({**cfg, "second_call_dispositions": []})
    if not both:
        pytest.skip("no intensive-bucket leads due on that date")

    # Narrow it to one slug that is present, and only those leads keep slot 2.
    keep = both[0]["disposition"]
    narrowed = slot_twos({**cfg, "second_call_dispositions": [keep]})
    assert narrowed, "the allowed slug should still earn its second call"
    assert len(narrowed) < len(both)
    assert {i["disposition"] for i in narrowed} == {keep}

    # Nobody qualifies -> single-call day, but slot 1 is untouched.
    none = slot_twos({**cfg, "second_call_dispositions": ["no_such_disposition"]})
    assert none == []

    client.put("/api/campaigns/4/config", json=cfg)


def test_a_lead_booked_in_formi_is_never_double_dialled(client):
    """`queued_today` is a call scheduled in the main system, not by this one."""
    from api.db import db_path

    day = "2026-09-08"
    run = client.post("/api/campaigns/5/plan", json={"date": day}).json()
    items = client.get(f"/api/runs/{run['id']}/items?page_size=2000").json()["items"]
    if len(items) < 5:
        pytest.skip("nothing due on that date in the seed")
    booked = sorted({i["lead_uuid"] for i in items})[:3]

    conn = sqlite3.connect(db_path())
    conn.executemany("UPDATE leads SET queued_today=1 WHERE lead_uuid=?", [(u,) for u in booked])
    conn.commit()
    try:
        after = client.post("/api/campaigns/5/plan", json={"date": day}).json()
        assert after["counts"]["planned"] == run["counts"]["planned"] - len(booked)

        still = client.get(f"/api/runs/{after['id']}/items?page_size=2000").json()["items"]
        assert not ({i["lead_uuid"] for i in still} & set(booked)), \
            "a lead Formi already holds must not be put on this console's clock"

        # The skip is auditable, not silent.
        skips = client.get(f"/api/campaigns/5/buckets?date={day}").json()["skips"]
        assert skips.get("ALREADY_SCHEDULED_TODAY") == len(booked)

        # Manual overrides it on purpose -- but says so.
        seen = client.post("/api/manual/preview", json={
            "campaign_id": 5, "dispositions": [], "buckets": [], "date": day}).json()
        assert seen["already_scheduled"] >= 1
    finally:
        conn.executemany("UPDATE leads SET queued_today=0 WHERE lead_uuid=?",
                         [(u,) for u in booked])
        conn.commit()
        conn.close()


def test_a_committed_run_can_be_paused_edited_and_resumed(client):
    """Pause hands the rest of the day back as editable slots; resume re-sends it."""
    run = _plan_today(client, 5)
    before = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    if len(before) < 2:
        pytest.skip("nothing due today in the seed")

    assert client.post(f"/api/runs/{run['id']}/approve").status_code == 200
    # Only a committed run can be paused, and only a paused one resumed.
    assert client.post(f"/api/runs/{run['id']}/resume").status_code == 409

    paused = client.post(f"/api/runs/{run['id']}/pause")
    assert paused.status_code == 200, paused.text
    paused = paused.json()
    assert paused["status"] == "paused"
    assert client.post(f"/api/runs/{run['id']}/pause").status_code == 409

    items = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    live = [i for i in items if i["status"] == "planned"]
    assert live, "pausing must return the un-dialled slots to planned"
    # Everything still on the clock came back; nothing in the past was disturbed.
    assert all(i["status"] in ("posted", "simulated", "expired")
               for i in items if i["status"] != "planned")

    # Editable while paused -- that is the whole point of pausing.
    moved = live[-1]
    new_time = moved["scheduled_time"][:11] + "18:45:00"
    edit = client.patch(f"/api/runs/{run['id']}/items/{moved['id']}",
                        json={"scheduled_time": new_time})
    if edit.status_code == 422:
        pytest.skip("18:45 is outside this campaign's window or already gone")
    assert edit.status_code == 200, edit.text

    done = client.post(f"/api/runs/{run['id']}/resume")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "committed"
    after = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    assert not [i for i in after if i["status"] == "planned"]


def test_paused_campaign_cannot_be_approved(client):
    run = client.post("/api/campaigns/2/plan", json={"date": "2026-09-02"}).json()
    client.post("/api/campaigns/2/pause")
    try:
        assert client.post(f"/api/runs/{run['id']}/approve").status_code == 409
    finally:
        client.post("/api/campaigns/2/resume")


# ---------------------------------------------------------------------------
# DRY_RUN: no network, ever
# ---------------------------------------------------------------------------

@pytest.fixture
def no_network(monkeypatch):
    """Any HTTP call at all becomes a test failure."""
    def boom(*args, **kwargs):
        raise AssertionError(f"network call attempted under DRY_RUN: {args[:1]}")
    for verb in ("post", "get", "put", "patch", "request"):
        monkeypatch.setattr(requests, verb, boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    return boom


def test_approve_under_dry_run_simulates_and_never_dials(client, no_network):
    # TODAY, not a fixed date: approving a run whose slots are all in the past is
    # a 409, so a hardcoded date turns this into a failure the day after it lands.
    run = client.post("/api/campaigns/3/plan", json={"date": TODAY}).json()
    approved = client.post(f"/api/runs/{run['id']}/approve").json()

    assert approved["dry_run"] is True
    assert approved["counts"]["posted"] == run["counts"]["slots"]
    assert approved["counts"]["failed"] == 0

    items = client.get(f"/api/runs/{run['id']}/items?page_size=5").json()["items"]
    assert all(i["status"] == "simulated" for i in items)
    # It recorded exactly what it would have sent.
    assert items[0]["response"]["would_post"]["body"]["scheduled_time"] == items[0]["scheduled_time"]
    assert "/schedule" in items[0]["response"]["would_post"]["url"]

    assert client.post(f"/api/runs/{run['id']}/approve").status_code == 409


def test_stage_commit_under_dry_run_never_dials(client, no_network):
    with _db() as conn:
        policies = [r["policy_no"] for r in conn.execute(
            "SELECT policy_no FROM leads LIMIT 5")]

    preview = client.post("/api/stage/policies/preview",
                          json={"policies": policies, "target_stage": "renewed"}).json()
    assert preview["would_change"] + preview["unchanged"] == 5
    assert {"would_change", "unchanged", "by_stage", "sample"} <= set(preview)

    commit = client.post("/api/stage/policies/commit",
                         json={"policies": policies, "target_stage": "renewed"}).json()
    assert commit["dry_run"] is True and commit["applied"] == 0

    expired = client.post("/api/stage/expired/commit",
                          json={"campaign_ids": [1], "red_before": "2026-08-25"}).json()
    assert expired["dry_run"] is True and expired["applied"] == 0

    # Nothing was actually written to the dataset.
    with _db() as conn:
        marks = ",".join("?" * len(policies))
        stages = [r["stage"] for r in conn.execute(
            f"SELECT stage FROM leads WHERE policy_no IN ({marks})", policies)]
    assert "renewed" not in stages

    jobs = client.get("/api/stage/jobs").json()
    assert jobs and all(j["dry_run"] is True and j["committed"] == 0 for j in jobs)


def test_expired_preview_keeps_renewed_and_paid_untouched(client):
    body = client.post("/api/stage/expired/preview",
                       json={"campaign_ids": [1, 2, 3], "red_before": "2026-08-25",
                             "target_stage": "policy_expired"}).json()
    assert body["would_change"] > 0
    for kept in ("already_paid_to_chola", "renewed", "policy_expired"):
        assert kept not in body["by_stage"]


def test_manual_bypasses_the_allow_list_but_not_exclusions(client):
    body = {"campaign_id": 1, "dispositions": ["positive_followup", "do_not_call"],
            "buckets": [], "date": "2026-08-28"}
    preview = client.post("/api/manual/preview", json=body).json()
    assert preview["count"] > 0
    assert all(s["disposition"] == "positive_followup" for s in preview["sample"])

    run = client.post("/api/manual/schedule", json=body).json()
    assert run["kind"] == "manual" and run["status"] == "planned"
    items = client.get(f"/api/runs/{run['id']}/items?page_size=500").json()["items"]
    assert items and all(i["disposition"] != "do_not_call" for i in items)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def test_agents_are_derived_from_campaigns_and_scope_the_list(client):
    from engine.seed import AGENTS, CAMPAIGNS

    agents = client.get("/api/agents").json()
    assert [a["agent_id"] for a in agents] == sorted(AGENTS)
    assert all(a["name"] == f"Agent {a['agent_id']}" for a in agents)
    assert sum(a["campaigns"] for a in agents) == len(CAMPAIGNS)

    for agent in agents:
        scoped = client.get(f"/api/campaigns?agent_id={agent['agent_id']}").json()
        assert scoped and all(c["agent_id"] == agent["agent_id"] for c in scoped)
        assert len(scoped) == agent["campaigns"]
        assert agent["enabled"] == sum(c["enabled"] for c in scoped)

    # Scoping partitions: no campaign belongs to two agents, none is orphaned.
    everything = client.get("/api/campaigns").json()
    assert len(everything) == sum(a["campaigns"] for a in agents)
    assert client.get("/api/campaigns?agent_id=999").json() == []


def test_agent_pause_pauses_every_campaign_on_that_agent(client):
    from engine.seed import AGENTS

    agent_id, other = AGENTS[0], AGENTS[1]
    before = {c["id"]: c["paused"] for c in client.get(f"/api/campaigns?agent_id={other}").json()}
    try:
        paused = client.post(f"/api/agents/{agent_id}/pause").json()
        assert paused["paused"] is True
        assert paused["paused_campaigns"] == paused["enabled"]
        assert all(c["paused"] for c in client.get(f"/api/campaigns?agent_id={agent_id}").json())
        # The other agent is untouched: pausing one voice is not a global stop.
        assert {c["id"]: c["paused"]
                for c in client.get(f"/api/campaigns?agent_id={other}").json()} == before

        # One campaign resumed is enough to stop the agent reading as paused.
        first = client.get(f"/api/campaigns?agent_id={agent_id}").json()[0]
        client.post(f"/api/campaigns/{first['id']}/resume")
        agent = next(a for a in client.get("/api/agents").json() if a["agent_id"] == agent_id)
        assert agent["paused"] is False
    finally:
        client.post(f"/api/agents/{agent_id}/resume")

    resumed = next(a for a in client.get("/api/agents").json() if a["agent_id"] == agent_id)
    assert resumed["paused"] is False and resumed["paused_campaigns"] == 0
    assert client.post("/api/agents/999/pause").status_code == 404


# ---------------------------------------------------------------------------
# Test call
# ---------------------------------------------------------------------------

def test_the_test_number_resolves_to_a_real_lead_in_a_test_campaign(client):
    from engine.seed import AGENTS, TEST_NUMBERS

    phone = TEST_NUMBERS[0]
    listing = client.get("/api/test-call/numbers").json()
    assert [n["phone"] for n in listing] == TEST_NUMBERS
    assert listing[0]["found"] is True and listing[0]["lead_uuid"]

    with _db() as conn:
        rows = conn.execute(
            "SELECT l.id, l.campaign_id, l.stage, c.name, c.agent_id FROM leads l "
            "JOIN campaigns c ON c.id=l.campaign_id WHERE l.phone=? ORDER BY l.campaign_id",
            (phone,)).fetchall()
    # One rehearsal lead per agent, both in a TEST campaign.
    assert [r["agent_id"] for r in rows] == sorted(AGENTS)
    assert all(r["name"].startswith("TEST ") for r in rows)

    # It really is schedulable: today's plan for its campaign contains it.
    # rows[-1] is the newest campaign, which is the one /api/test-call resolves to.
    campaign_id = rows[-1]["campaign_id"]
    run = _plan_today(client, campaign_id)
    items = client.get(f"/api/runs/{run['id']}/items?page_size=50").json()["items"]
    assert listing[0]["lead_uuid"] in {i["lead_uuid"] for i in items}
    assert {i["bucket"] for i in items} == {"F5"}

    # Every seeded lead carries a plausible 10-digit Indian mobile.
    with _db() as conn:
        phones = [r["phone"] for r in conn.execute("SELECT phone FROM leads")]
    assert all(len(p) == 10 and p[0] in "6789" and p.isdigit() for p in phones)


def test_trigger_422s_for_a_number_that_is_not_allow_listed(client, no_network):
    for number in ("9876543210", "+91 90000 00001", "", "1234"):
        response = client.post("/api/test-call/trigger", json={"phone": number})
        assert response.status_code == 422, number
        assert "allow-list" in response.json()["error"]

    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM test_calls WHERE phone=?",
                            ("9876543210",)).fetchone()["c"] == 0


def test_trigger_under_dry_run_simulates_and_never_dials(client, no_network):
    from engine.seed import TEST_NUMBERS

    phone = TEST_NUMBERS[0]
    preview = client.post("/api/test-call/preview", json={"phone": phone}).json()
    assert preview["found"] is True and preview["lead"]["phone"] == phone
    assert preview["would_post"]["url"].endswith("/schedule")

    triggered = client.post("/api/test-call/trigger",
                            json={"phone": phone, "scheduled_time": TOMORROW_AM}).json()
    assert triggered["status"] == "simulated" and triggered["dry_run"] is True
    assert triggered["http_status"] is None and triggered["response"] is None
    lead = triggered["lead"]
    assert triggered["would_post"] == {
        "url": f"/v2/campaign/leads/{lead['agent_id']}/{lead['lead_uuid']}/schedule",
        "body": {"scheduled_time": TOMORROW_AM}}

    history = client.get("/api/test-call/history").json()
    assert history[0]["phone"] == phone and history[0]["status"] == "simulated"
    assert history[0]["dry_run"] is True
    assert history[0]["would_post"] == triggered["would_post"]


def test_a_hand_picked_test_call_time_obeys_the_dial_window(client, no_network):
    """Picking the time must not become a way around dialling hours."""
    from engine.seed import TEST_NUMBERS

    phone = TEST_NUMBERS[0]
    day = TOMORROW_AM[:10]
    for bad in (f"{day}T04:00", f"{day}T23:30", "2020-01-01T10:00", "nonsense"):
        r = client.post("/api/test-call/preview",
                        json={"phone": phone, "scheduled_time": bad})
        assert r.status_code == 422, bad

    ok = client.post("/api/test-call/preview",
                     json={"phone": phone, "scheduled_time": f"{day}T15:07"}).json()
    assert ok["would_post"]["body"]["scheduled_time"] == f"{day}T15:07:00"

    # Omitted still means "next minute inside the window", not an error.
    auto = client.post("/api/test-call/preview", json={"phone": phone}).json()
    assert "09:30" <= auto["would_post"]["body"]["scheduled_time"][11:16] <= "19:00"

    # A rejected time is never recorded as an attempt.
    with _db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM test_calls WHERE scheduled_time LIKE ?",
            (f"{day}T04:%",)).fetchone()["c"] == 0


def test_trigger_ignores_pause_but_not_exclusions(client, no_network):
    from engine.seed import TEST_NUMBERS

    phone = TEST_NUMBERS[0]
    campaign_id = client.get("/api/test-call/numbers").json()[0]["campaign_id"]
    client.post(f"/api/campaigns/{campaign_id}/pause")
    try:
        response = client.post("/api/test-call/trigger",
                               json={"phone": phone, "campaign_id": campaign_id})
        assert response.status_code == 200 and response.json()["status"] == "simulated"

        # Same lead, now excluded: the rehearsal must refuse it.
        with _db() as conn:
            conn.execute("UPDATE leads SET stage='do_not_call' WHERE phone=? AND campaign_id=?",
                         (phone, campaign_id))
            conn.commit()
        blocked = client.post("/api/test-call/trigger",
                              json={"phone": phone, "campaign_id": campaign_id})
        assert blocked.status_code == 409 and "exclusion" in blocked.json()["error"]
    finally:
        with _db() as conn:
            conn.execute("UPDATE leads SET stage='did_not_pick' WHERE phone=? AND campaign_id=?",
                         (phone, campaign_id))
            conn.commit()
        client.post(f"/api/campaigns/{campaign_id}/resume")


class TestFormiToken:
    """Deploys have shipped the Formi key under either name; accept both.

    A live dial used to read FORMI_TOKEN only, so a .env that set
    FORMI_API_KEY turned every approve into a 500 and nothing was dialled.
    """

    def test_formi_token_wins_when_both_are_set(self, monkeypatch):
        monkeypatch.setenv("FORMI_TOKEN", "from-token")
        monkeypatch.setenv("FORMI_API_KEY", "from-key")
        assert formi_token() == "from-token"

    def test_falls_back_to_formi_api_key(self, monkeypatch):
        monkeypatch.delenv("FORMI_TOKEN", raising=False)
        monkeypatch.setenv("FORMI_API_KEY", "from-key")
        assert formi_token() == "from-key"

    def test_none_when_neither_is_set_and_the_message_names_both(self, monkeypatch):
        # The lookup reports "nothing configured" rather than raising, so each
        # caller can fail in its own vocabulary -- an HTTP 500 from the dial
        # route, a RuntimeError from the bulk-stage engine. What must not vary
        # is the name an operator is told to set.
        monkeypatch.delenv("FORMI_TOKEN", raising=False)
        monkeypatch.delenv("FORMI_API_KEY", raising=False)
        assert formi_token() is None
        assert "FORMI_API_KEY" in NO_TOKEN and "FORMI_TOKEN" in NO_TOKEN
