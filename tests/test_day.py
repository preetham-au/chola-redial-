"""The daily gate: stranded runs, which pass a plan is, agent scoping, proof."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from api import day as day_module
from api.day import ARMED, STRANDED_DAYS
from api.db import now_ist, session
from api.routes_core import FORMI_LEAD_MINUTES
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
        # Seeded dial-log rows go the same way, and for the same reason: the
        # `dial_log` counts on the day screen are a GROUP BY over the whole
        # table, so a row left behind moves another test's total. Tagged
        # source='test'; nothing in api/ or engine/ writes that value.
        conn.execute("DELETE FROM dial_log WHERE source='test'")
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


def _seed_run(campaign_id: int, run_date: str, kind: str, status: str, slots: int,
              leads: list[str] | None = None) -> int:
    """A run with `slots` planned items, exactly as _write_run would leave it.

    `leads` names the lead uuids to plan, for a test that needs two runs to hold
    the SAME people -- the default keys them to the run id, which no two runs can
    share, and a re-planned backlog is the same leads on a later day.
    """
    assert leads is None or len(leads) == slots, "one uuid per slot"
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
            [(run_id, leads[i] if leads else f"lead-{run_id}-{i}", f"P{run_id}{i}", "9" * 10,
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


def _seed_dial(campaign_id: int, agent_id: int | None, day: str, n: int,
               kind: str = "auto") -> int:
    """`n` dial-log rows for one campaign, as the dialler would have left them.

    Every row this console writes comes off a plan item, so it carries the id of
    the run it was dialled from (`api/dial_log.py`) -- the run is seeded here for
    the same reason, and it is what says which pass those calls belonged to.

    `agent_id` is written from the campaign at log time, and the column is
    NULLABLE: passing None is the row this console wrote before that column
    existed, which a day scoped through `dial_log.agent_id` would drop.
    """
    run_id = _seed_run(campaign_id, day, kind, "committed", 0)
    with session() as conn:
        conn.executemany(
            "INSERT INTO dial_log (created_at, campaign_id, agent_id, run_id, source, "
            "scheduled_time, dry_run, url, request_body, outcome, verified) "
            "VALUES (?,?,?,?,'test',?,1,'','{}','simulated','dialled')",
            [(f"{day}T10:00:00", campaign_id, agent_id, run_id, f"{day}T10:{i:02d}:00")
             for i in range(n)])
        conn.commit()
    return run_id


def _arm_two_agents() -> list[tuple[int, int]]:
    """Arm one campaign on each of two agents; return [(agent_id, campaign_id), ...].

    The agent-scoping tests need the AGENT ids, which `_arm` does not return --
    it answers in campaign ids because every other test here seeds runs against
    a campaign. Read back rather than hardcoded: `_arm` picks from the same
    table, so which campaign belongs to which agent is the DB's answer.
    """
    ids = _arm(2)
    marks = ",".join("?" * len(ids))
    with session() as conn:
        agent_of = {r["id"]: r["agent_id"] for r in conn.execute(
            f"SELECT id, agent_id FROM campaigns WHERE id IN ({marks})", ids)}
    pairs = [(agent_of[i], i) for i in ids]
    assert len({a for a, _ in pairs}) == 2, "_arm must pick one campaign per agent"
    return pairs


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
    """Asking for tomorrow must not report today's queued plan as abandoned.

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


def test_stranded_counts_a_lead_once_however_many_days_it_sat(client):
    """544 people never called, not 7,616 calls.

    An unapproved plan is rebuilt for the same leads the next day, and the
    one after that, so summing `slots` across the stranded runs multiplied one
    backlog by the number of days it sat -- 544 leads over a fortnight rendered
    as "7,616 calls never dialled". The number the operator has to act on is how
    many PEOPLE were not called.
    """
    campaign_id = _arm()[0]
    day = now_ist().date()
    leads = ["stuck-a", "stuck-b"]
    # Read before seeding: `stranded_leads` is the page's total and other armed
    # campaigns carry their own backlog, so the assertion is on the delta. `_arm`
    # goes first for the same reason -- it changes the roster that total covers.
    before = client.get("/api/day").json()
    for back in (1, 2, 3):
        _seed_run(campaign_id, (day - timedelta(days=back)).isoformat(),
                  "auto", "planned", len(leads), leads=leads)

    body = client.get("/api/day").json()

    mine = [s for s in body["stranded"] if s["campaign_id"] == campaign_id]
    assert sum(s["slots"] for s in mine) == 6, \
        "each run still reports the slots it holds; only the total is de-duplicated"
    assert body["stranded_leads"] - before["stranded_leads"] == 2, \
        "the same two leads on three days are two people, not six calls"


# ---------------------------------------------------------------------------
# Which pass a plan belongs to
# ---------------------------------------------------------------------------
# Until 14 Sep 2026 the clock answered this: before 13:30 was the morning wave,
# after it the afternoon, and each could only dial its own half of the day. The
# client's rule was never a time of day -- "call again the same day if the first
# call did not reach them" -- so the boundary is gone, both passes dial the
# campaign's whole window, and `kind_for_campaign` asks the CALL LOG instead:
# has this campaign already dialled today? Who is IN the recall pass is decided
# per lead by `wants_second_call` (engine/red_engine.py), which these do not
# re-test; what is pinned here is only which pass a fresh plan is filed under.


def _posted_run(campaign_id: int, run_date: str, kind: str, status: str,
                posted: int) -> int:
    """A run that reached `_commit` and put `posted` calls on the wire."""
    run_id = _seed_run(campaign_id, run_date, kind, status, 1)
    with session() as conn:
        conn.execute("UPDATE runs SET posted=? WHERE id=?", (posted, run_id))
        conn.commit()
    return run_id


@pytest.fixture
def untouched():
    """Campaigns nobody else in the suite has dialled today.

    Not `_arm`, which hands back the lowest id on each agent: `client` is
    session-scoped and tests/test_autopilot.py commits REAL runs against those
    campaigns for TODAY -- runs `clean_runs` rightly leaves alone, because it
    only sweeps rows it seeded itself. `kind_for_campaign` reads exactly that
    table, so an `_arm` campaign already answers "has dialled today" before one
    of these tests writes anything, and the four that expect a first pass fail
    on the suite while passing on the file. A campaign of their own is what
    makes the question askable.
    """
    made: list[int] = []

    def make(count: int = 1) -> list[int]:
        with session() as conn:
            for _ in range(count):
                cid = 90010 + len(made)
                conn.execute(
                    "INSERT INTO campaigns (id, agent_id, warehouse_id, name, autopilot) "
                    "VALUES (?, 90130, ?, 'pass fixture', 0)", (cid, 99000 + cid))
                made.append(cid)
            conn.commit()
        return made[-count:]

    yield make
    # Every table that REFERENCES campaigns(id) first, or the FK refuses.
    with session() as conn:
        for cid in made:
            runs = "(SELECT id FROM runs WHERE campaign_id=?)"
            conn.execute(f"DELETE FROM plan_items WHERE run_id IN {runs}", (cid,))
            conn.execute(f"DELETE FROM decisions WHERE run_id IN {runs}", (cid,))
            conn.execute("DELETE FROM config WHERE campaign_id=?", (cid,))
            conn.execute("DELETE FROM runs WHERE campaign_id=?", (cid,))
            conn.execute("DELETE FROM leads WHERE campaign_id=?", (cid,))
            conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
        conn.commit()


def test_a_campaign_that_has_not_dialled_today_gets_the_first_pass(untouched):
    campaign_id = untouched()[0]
    today = now_ist().date()
    with session() as conn:
        assert day_module.kind_for_campaign(conn, campaign_id, today) == day_module.FIRST_PASS


def test_a_campaign_that_already_dialled_today_gets_the_recall_pass(untouched):
    """The whole rule: the second plan of the day is a recall, not an afternoon."""
    campaign_id = untouched()[0]
    today = now_ist().date()
    _posted_run(campaign_id, today.isoformat(), day_module.FIRST_PASS, "committed", 12)
    with session() as conn:
        assert day_module.kind_for_campaign(conn, campaign_id, today) == day_module.RECALL_PASS


def test_a_first_pass_that_was_paused_after_dialling_still_counts_as_dialled(untouched):
    """`posted > 0`, not `status`. Pausing does not un-call the calls it made.

    Read off the status instead and a campaign paused mid-dial is handed a
    SECOND first pass, which re-plans the leads it has already rung.
    """
    campaign_id = untouched()[0]
    today = now_ist().date()
    _posted_run(campaign_id, today.isoformat(), day_module.FIRST_PASS, "paused", 8)
    with session() as conn:
        assert day_module.kind_for_campaign(conn, campaign_id, today) == day_module.RECALL_PASS


def test_a_plan_that_was_built_but_never_approved_is_still_the_first_pass(untouched):
    """A `planned` run has dialled nobody, so the day has not started.

    This is the common case the boundary used to get wrong on its own: a plan
    prepared at 10:00 and left unapproved until 14:00 became an "afternoon"
    wave, half its hours already gone, for leads no one had called yet.
    """
    campaign_id = untouched()[0]
    today = now_ist().date()
    _seed_run(campaign_id, today.isoformat(), day_module.FIRST_PASS, "planned", 5)
    with session() as conn:
        assert day_module.kind_for_campaign(conn, campaign_id, today) == day_module.FIRST_PASS


def test_another_campaigns_calls_do_not_use_up_this_campaigns_first_pass(untouched):
    """Per campaign, not per day. Panels hold many campaigns and they start apart."""
    dialled, quiet = untouched(2)
    today = now_ist().date()
    _posted_run(dialled, today.isoformat(), day_module.FIRST_PASS, "committed", 30)
    with session() as conn:
        assert day_module.kind_for_campaign(conn, quiet, today) == day_module.FIRST_PASS


def test_yesterdays_calls_do_not_carry_into_todays_first_pass(untouched):
    """Every day starts over, and a back-dated or future plan is always the first.

    Keyed on `run_date`, so nothing posted on another date can answer for this
    one -- which is also what makes a plan built for tomorrow a first pass.
    """
    campaign_id = untouched()[0]
    today = now_ist().date()
    yesterday = today - timedelta(days=1)
    _posted_run(campaign_id, yesterday.isoformat(), day_module.FIRST_PASS, "committed", 40)
    with session() as conn:
        assert day_module.kind_for_campaign(conn, campaign_id, today) == day_module.FIRST_PASS
        assert day_module.kind_for_campaign(
            conn, campaign_id, today + timedelta(days=1)) == day_module.FIRST_PASS


def test_both_passes_dial_the_campaigns_whole_window(client):
    """No half-days. The recall pass may place a call at 09:05, the first at 19:55.

    The 13:30 band is what the client said was still on screen after they asked
    for the previous call to decide the second one. Its absence is the fix, so
    it is asserted rather than left to the deleted code not coming back.
    """
    from api.day import DEFAULT_WINDOW, _day_window

    config = {1: {"dial_window": dict(DEFAULT_WINDOW), "max_per_minute": 10}}
    span = _day_window(config, {1: 500}, floor=None, today=False)

    assert span["window"] == DEFAULT_WINDOW, span["window"]
    assert not hasattr(day_module, "WAVE_BOUNDARY"),         "the clock boundary is gone; a pass is decided by the previous call"
    assert not hasattr(day_module, "WAVE_BAND"), "and so are the per-pass bands"


def test_pin_clock_can_be_called_twice(client, pin_clock):
    """A second pin must move every module the first one moved.

    The fixture finds its targets by identity against the original `now_ist`, and
    each pin installs a DISTINCT lambda -- so a sweep re-run per call matches only
    api.db the second time and leaves api.day and api.routes_core on the FIRST
    hour. That fails green, which is precisely the "the fixture patched the wrong
    target" failure pinning the clock exists to prevent.
    """
    import api.day as day_module
    import api.db
    import api.routes_core as routes_core

    pin_clock(10)
    pin_clock(15)

    for module in (api.db, day_module, routes_core):
        assert module.now_ist().hour == 15, \
            f"{module.__name__} is still on the first pinned hour: {module.now_ist()}"


# ---------------------------------------------------------------------------
# Agent scoping
# ---------------------------------------------------------------------------
# Agents 125 and 127 hold mirrored campaigns in two languages, and every one of
# these endpoints used to sum them. Both halves are asserted throughout: a test
# that only ever passes `agent_id` says nothing about the default path, and one
# that never passes it says nothing about the filter.

def test_day_without_agent_id_covers_every_agent_and_scoping_narrows_it(client):
    """The scoped day must be a PROPER subset of the unscoped one.

    This is the assertion the feature lives or dies on. `agent_id` that is read
    and then ignored leaves both calls identical, and a subset check alone
    passes on that -- a set is a subset of itself.
    """
    (first, first_c), (second, second_c) = _arm_two_agents()
    whole = {c["id"]: c["agent_id"] for c in client.get("/api/day").json()["campaigns"]}

    assert {first, second} <= set(whole.values()), \
        "an unscoped day must still show every armed agent, exactly as before"

    parts = {}
    for agent in (first, second):
        body = client.get(f"/api/day?agent_id={agent}").json()
        ids = {c["id"] for c in body["campaigns"]}
        assert ids, f"agent {agent} has an armed campaign and must still show it"
        assert ids < set(whole), \
            f"the day scoped to agent {agent} equals the unscoped day - it is not scoped"
        assert body["agent_id"] == agent, "the response must say what it was scoped to"
        assert body["totals"]["campaigns"] == len(body["campaigns"]), \
            "the totals must count the scoped roster, not the whole day"
        parts[agent] = ids

    assert parts[first].isdisjoint(parts[second]), "two agents cannot share a campaign"
    assert parts[first] | parts[second] == set(whole), \
        "between them the agents must account for the whole unscoped day"
    assert client.get("/api/day").json()["agent_id"] is None, \
        "an unscoped day is not scoped to anybody"


def test_day_scoped_to_one_agent_excludes_the_other(client):
    (first, _), (second, _) = _arm_two_agents()
    body = client.get(f"/api/day?agent_id={first}").json()

    assert body["campaigns"], "the scoped agent must still have its campaigns"
    assert all(c["agent_id"] == first for c in body["campaigns"]), \
        "a scoped day must never show another agent's campaigns"
    assert all(c["agent_id"] != second for c in body["campaigns"])


def test_day_for_an_agent_with_nothing_armed_is_empty_not_an_error(client):
    _arm_two_agents()
    res = client.get("/api/day?agent_id=999999")

    assert res.status_code == 200, "a quiet agent is a real state, not a 404"
    assert res.json()["campaigns"] == []
    assert res.json()["status"] == "no_campaigns"


def test_day_scopes_the_stopped_list_to_the_agent(client):
    """A scoped panel showing the other language's stopped campaigns is the same
    bug as showing its armed ones -- both land in the operator's `stopped` list."""
    (first, _), (second, second_c) = _arm_two_agents()
    with session() as conn:
        conn.execute("UPDATE campaigns SET paused=1 WHERE id=?", (second_c,))
        conn.commit()

    scoped = {c["id"] for c in client.get(f"/api/day?agent_id={first}").json()["stopped"]}
    whole = {c["id"] for c in client.get("/api/day").json()["stopped"]}

    assert second_c in whole, "an unscoped day still reports every stopped campaign"
    assert second_c not in scoped, \
        "a scoped panel must not show another agent's stopped campaigns"


def test_day_scopes_a_hidden_campaign_that_is_still_dialling(client, pin_clock):
    """The hidden-but-dialling warning lands in the same `stopped` array.

    Scoped with `stopped` or not at all: half a scoped list is worse than none,
    because the operator cannot tell which half they are looking at.
    """
    (first, _), (second, second_c) = _arm_two_agents()
    now = pin_clock(9)
    today = now.date().isoformat()
    run_id = _seed_run(second_c, today, "auto", "committed", 2)
    with session() as conn:
        conn.execute("UPDATE campaigns SET hidden=1 WHERE id=?", (second_c,))
        conn.execute("UPDATE plan_items SET status='simulated', scheduled_time=? "
                     "WHERE run_id=?", (f"{today}T23:59:00", run_id))
        conn.commit()

    scoped = {c["id"] for c in client.get(f"/api/day?agent_id={first}").json()["stopped"]}
    own = {c["id"] for c in client.get(f"/api/day?agent_id={second}").json()["stopped"]}
    whole = {c["id"] for c in client.get("/api/day").json()["stopped"]}

    assert second_c in whole, "an unscoped day must still warn about it"
    assert second_c not in scoped, \
        "a scoped panel must not warn about another agent's hidden campaign"
    # The half that actually holds the warning up. Absence from the OTHER panel
    # is also what a warning that reaches NO panel looks like: bind `agent_id=?`
    # to the timestamp instead of the agent and this query returns nothing for
    # every scoped fetch, with the whole suite still green. Task 4's UI is always
    # scoped, so that is live calls with nothing on any screen -- the one thing
    # this warning exists to prevent.
    assert second_c in own, (
        f"campaign {second_c} is hidden and still putting calls on Formi's clock, "
        f"and agent {second}'s own panel does not warn about it -- scoped is the "
        f"only way Task 4 ever asks")


def test_day_scopes_the_stranded_warning_to_the_agent(client):
    (first, first_c), (second, second_c) = _arm_two_agents()
    yesterday = (now_ist().date() - timedelta(days=1)).isoformat()
    _seed_run(first_c, yesterday, "auto", "planned", 3)
    _seed_run(second_c, yesterday, "auto", "planned", 5)

    scoped = {s["campaign_id"] for s in
              client.get(f"/api/day?agent_id={first}").json()["stranded"]}
    whole = {s["campaign_id"] for s in client.get("/api/day").json()["stranded"]}

    assert {first_c, second_c} <= whole, "an unscoped day reports both agents' plans"
    assert first_c in scoped, "the scoped agent's own stranded plan is still reported"
    assert second_c not in scoped, \
        "a scoped panel must not report another agent's stranded plan"


def test_prepare_is_scoped_to_its_agent(client, pin_clock):
    """The roster the prepare pass walks is the scoped one.

    Pinned past the boundary so every campaign answers `window_closed` and no run
    is written: `client` is session-scoped, and what is under test is WHICH
    campaigns the pass visits, not what it plans for them.
    """
    import api.day as day_module

    (first, first_c), (_, second_c) = _arm_two_agents()
    now = pin_clock(15)

    scoped = day_module.prepare_day(now.date(), "auto", False, first)
    whole = day_module.prepare_day(now.date(), "auto", False)

    visited = {c["campaign_id"] for c in scoped["campaigns"]}
    assert visited == {first_c}, f"the scoped pass visited {visited}"
    assert {first_c, second_c} <= {c["campaign_id"] for c in whole["campaigns"]}, \
        "an unscoped pass must still prepare every armed campaign"


def test_prepare_endpoint_passes_agent_id_through(client, pin_clock):
    """`PrepareBody.agent_id` must actually reach `prepare_day`.

    The field can be declared, accepted and dropped on the floor, and every
    direct-call test above still passes.
    """
    (first, first_c), (_, second_c) = _arm_two_agents()
    now = pin_clock(15)
    body = {"date": now.date().isoformat(), "kind": "auto", "agent_id": first}

    visited = {c["campaign_id"] for c in
               client.post("/api/day/prepare", json=body).json()["campaigns"]}

    assert visited == {first_c}, f"the endpoint prepared {visited}"
    assert second_c not in visited


def test_approve_is_scoped_to_its_agent(client):
    """Approve's roster is the scoped one too -- the roster that reaches Formi.

    Dated tomorrow with no plan prepared, so every campaign answers
    `not_prepared` and nothing dials. What is asserted is which campaigns are in
    the answer at all, which is exactly what the roster query decides.
    """
    (first, first_c), (_, second_c) = _arm_two_agents()
    tomorrow = (now_ist().date() + timedelta(days=1)).isoformat()

    scoped = client.post("/api/day/approve",
                         json={"date": tomorrow, "agent_id": first}).json()
    whole = client.post("/api/day/approve", json={"date": tomorrow}).json()

    visited = {c["campaign_id"] for c in scoped["campaigns"]}
    assert visited == {first_c}, f"the scoped approve visited {visited}"
    assert {first_c, second_c} <= {c["campaign_id"] for c in whole["campaigns"]}, \
        "an unscoped approve must still visit every armed campaign"


def test_approve_dials_the_intersection_of_the_agent_scope_and_campaign_ids(client):
    """Both narrowings at once must INTERSECT -- neither one wins.

    `approve_day` takes an `agent_id` scope and a `campaign_ids` list, and this
    is the only endpoint in the console that reaches Formi. The per-panel Approve
    button sends both at once, so "what do they mean together" is answered on a
    live dialler unless it is answered here. An agent scope that overrode the
    named ids, ids that overrode the agent scope, and a union of the two are all
    different sets of customers, and all three passed the suite before this test.

    Three campaigns, so the three wrong answers are all distinguishable from the
    right one: two armed on the first agent and one on the second, with only one
    of the first agent's two named. Dated tomorrow with no plan prepared, so every
    campaign visited answers `not_prepared` and nothing dials -- what is asserted
    is which campaigns the approve reached at all, which is the roster decision.
    """
    (first, first_c), (second, second_c) = _arm_two_agents()
    with session() as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE agent_id=? AND id<>? "
                           "ORDER BY id", (first, first_c)).fetchone()
        assert row is not None, \
            f"agent {first} needs a second campaign for this test to separate the cases"
        also_first = row["id"]
        conn.execute("UPDATE campaigns SET autopilot=1, enabled=1, paused=0, hidden=0 "
                     "WHERE id=?", (also_first,))
        conn.commit()
    tomorrow = (now_ist().date() + timedelta(days=1)).isoformat()

    dialled = {c["campaign_id"] for c in client.post(
        "/api/day/approve",
        json={"date": tomorrow, "agent_id": first,
              "campaign_ids": [first_c, second_c]}).json()["campaigns"]}

    assert dialled == {first_c}, (
        f"a scoped approve naming campaigns {[first_c, second_c]} must dial their "
        f"intersection with agent {first} -- campaign {first_c} alone. It dialled "
        f"{sorted(dialled)}. Campaign {also_first} is agent {first}'s but was not "
        f"named (the agent scope alone would add it); campaign {second_c} was named "
        f"but belongs to agent {second} (the id list alone would add it); both "
        f"together is the union.")


def test_armed_helper_leaves_the_unscoped_clause_exactly_as_it_was(client):
    """`_armed(None)` is the literal roster string, with no parameters.

    Every caller interpolates the fragment into SQL it also passes parameters
    for, so a helper that quietly appended `AND agent_id=?` with nothing to bind
    would fail at the driver rather than return the wrong rows.
    """
    from api.day import _armed, _scope

    assert _armed() == (ARMED, [])
    assert _armed(None) == (ARMED, [])
    where, params = _armed(125)
    assert where == f"({ARMED}) AND agent_id=?" and params == [125]
    # The brackets are the point, not decoration: `a=1 OR b=1 AND agent_id=?`
    # binds the AND to the last branch alone, so scoping a clause with a
    # top-level OR would WIDEN it -- the opposite of what this helper is for.
    assert _scope("a=1 OR b=1", 125)[0] == "(a=1 OR b=1) AND agent_id=?", \
        "_scope must parenthesise the clause it narrows"


# ---------------------------------------------------------------------------
# Agent language labels
# ---------------------------------------------------------------------------

def test_agents_carry_a_language_label(client, monkeypatch):
    """The label comes from AGENT_LANGUAGES, never from a constant in the code."""
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", "125:Hindi,127:Tamil")
    labels = {a["agent_id"]: a["language"] for a in core.list_agents()}

    assert labels, "the fixture DB must hold at least one agent"
    assert labels.get(125) == "Hindi", labels
    assert labels.get(127) == "Tamil", labels


def test_an_unlabelled_agent_gets_no_language_rather_than_a_guess(client, monkeypatch):
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", "125:Hindi")
    labels = {a["agent_id"]: a["language"] for a in core.list_agents()}

    assert labels.get(125) == "Hindi"
    assert labels.get(127) is None, "an agent nobody labelled has no language"


def test_agents_carry_the_field_even_with_nothing_configured(client, monkeypatch):
    """The key is always present, so the client never has to feature-detect it."""
    import api.routes_core as core

    monkeypatch.delenv("AGENT_LANGUAGES", raising=False)
    agents = core.list_agents()

    assert agents, "the fixture DB must hold at least one agent"
    assert all("language" in a and a["language"] is None for a in agents)


def test_an_agent_named_twice_is_refused_rather_than_last_wins(client, monkeypatch):
    """`125:Hindi,125:Tamil` is a contradiction, not an override.

    It used to boot clean and label 125 Tamil, on the one variable whose entire
    justification is that a wrong label means a script read to the wrong cohort.
    The refusal has to name the id and BOTH labels -- neither is more likely to be
    the intended one, so the operator is the only one who can choose.
    """
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", "125:Hindi,125:Tamil")
    with pytest.raises(ValueError, match="125") as raised:
        core.list_agents()

    assert "Hindi" in str(raised.value) and "Tamil" in str(raised.value), \
        f"the refusal must name both labels: {raised.value}"


def test_a_malformed_agent_language_is_refused_rather_than_guessed(client, monkeypatch):
    """`127` with no label must raise, not silently label agent 127 with ''."""
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", "125:Hindi,127")
    with pytest.raises(ValueError, match="127"):
        core.list_agents()


@pytest.mark.parametrize("value,label", [
    # The whole tail of the line becomes one agent's label: 125 is labelled
    # "Hindi;127:Tamil" and 127 is left unlabelled, silently. A semicolon for a
    # comma is the likeliest hand-edit slip in this variable.
    ("125:Hindi;127:Tamil", "Hindi;127:Tamil"),
    ("125:Hindi:Extra", "Hindi:Extra"),
])
def test_a_label_that_is_not_a_language_is_refused(client, monkeypatch, value, label):
    """Non-empty was not enough: the label has to LOOK like a language.

    This is the variable whose entire justification is that a wrong label means a
    script read to the wrong cohort of customers, and both of these booted clean.
    """
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", value)
    with pytest.raises(ValueError) as raised:
        core.list_agents()

    assert "125" in str(raised.value) and label in str(raised.value), \
        f"the refusal must name the agent and the offending label: {raised.value}"


@pytest.mark.parametrize("label", [
    # The two this console actually dials. Every Indic vowel sign and virama is
    # a combining mark, and `str.isalpha()` is False for those, so the label
    # guard refused both at boot on a value that is exactly right.
    "हिन्दी", "தமிழ்", "ਪੰਜਾਬੀ", "తెలుగు",
    # These booted even then -- they carry no combining marks. The guard was
    # never a Latin-only rule, which is why refusing the four above was a bug.
    "日本語", "Français", "Русский", "العربية",
    # Punctuation that belongs to a language name, not a separator.
    "N'Ko", "Brazilian Portuguese", "Serbo-Croatian",
])
def test_a_language_name_in_any_script_is_accepted(client, monkeypatch, label):
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", f"125:{label}")
    labels = {a["agent_id"]: a["language"] for a in core.list_agents()}

    assert labels.get(125) == label, labels


def test_a_padded_duplicate_agent_id_is_still_a_duplicate(client, monkeypatch):
    """`0125` and `125` are the same agent, and the label guard must not mask it."""
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", "0125:Tamil,125:Hindi")
    with pytest.raises(ValueError, match="twice"):
        core.list_agents()


def test_a_non_numeric_agent_id_names_the_variable_and_the_entry(client, monkeypatch):
    """`int(agent)` alone raised `invalid literal for int()` and nothing else.

    The operator's one line to debug from has to say WHICH variable and WHICH
    entry; a bare int() failure says neither.
    """
    import api.routes_core as core

    monkeypatch.setenv("AGENT_LANGUAGES", "x:Hindi")
    with pytest.raises(ValueError, match="AGENT_LANGUAGES.*x:Hindi"):
        core.list_agents()


# ---------------------------------------------------------------------------
# The dial log — "did it actually run?"
# ---------------------------------------------------------------------------

def test_dial_log_is_scoped_to_its_agent(client):
    """A scoped day must not report the other agent's calls as its own.

    `dial_log` was the one unscoped field left on a scoped page: one row logged
    for a campaign owned by agent 127 showed up in `GET /api/day?agent_id=125`.
    It is also the field that matters most, because it is the number the operator
    checks after approving to find out whether the day actually ran -- and the
    two languages sit in side-by-side panels, so both would show the same
    whole-day figure.

    Asserted as a DELTA rather than an absolute: `client` is session-scoped, so
    other tests in this run may have logged dials of their own.
    """
    (first, first_c), (second, second_c) = _arm_two_agents()
    today = now_ist().date().isoformat()

    def dialled(agent=None) -> int:
        url = "/api/day" + (f"?agent_id={agent}" if agent is not None else "")
        return client.get(url).json()["dial_log"].get("dialled", 0)

    before = {first: dialled(first), second: dialled(second), None: dialled()}
    _seed_dial(first_c, first, today, 2)
    _seed_dial(second_c, second, today, 5)

    assert dialled(first) - before[first] == 2, (
        f"agent {first} dialled 2 and agent {second} dialled 5; agent {first}'s day "
        f"moved by {dialled(first) - before[first]} — it is counting the other "
        f"agent's calls")
    assert dialled(second) - before[second] == 5, (
        f"agent {second}'s day moved by {dialled(second) - before[second]}, not 5")
    assert dialled() - before[None] == 7, (
        f"an unscoped day must still count both agents: it moved by "
        f"{dialled() - before[None]}, not 7")


def test_the_dial_log_is_scoped_to_its_pass(client):
    """The recall card must not present the first pass's calls as its own proof.

    These counts are rendered inside the pass's proof card, under that pass's own
    title and eyebrow. Scoped by date and agent but not by kind, a first pass that
    dialled 1,900 read as the recall's the moment the recall card was opened -- a
    number that looks pass-scoped and is not.

    A delta, not an absolute: `client` is session-scoped.
    """
    agent, campaign_id = _arm_two_agents()[0]
    today = now_ist().date().isoformat()

    def dialled(kind: str) -> int:
        body = client.get(f"/api/day?kind={kind}&agent_id={agent}").json()
        return body["dial_log"].get("dialled", 0)

    before = {k: dialled(k) for k in ("auto", "auto_pm")}
    _seed_dial(campaign_id, agent, today, 4, kind="auto")
    _seed_dial(campaign_id, agent, today, 1, kind="auto_pm")

    assert dialled("auto") - before["auto"] == 4, (
        f"the first pass dialled 4 and the recall 1; the first pass's card moved by "
        f"{dialled('auto') - before['auto']} — it is counting the other pass")
    assert dialled("auto_pm") - before["auto_pm"] == 1, (
        f"the recall card moved by {dialled('auto_pm') - before['auto_pm']}, not 1")


def test_a_dial_log_row_with_no_agent_still_counts_for_its_campaigns_agent(client):
    """`dial_log.agent_id` is nullable, and a scoped panel dropped those rows.

    Written from the campaign at log time, so every row this console writes today
    has one -- but the column was added after the table, and a row from before it
    was populated is a real call that really happened. Scoping on it made a
    scoped panel under-report the one number the operator checks after approving.
    The campaign's own agent is never null, so the narrowing goes through there.
    """
    agent, campaign_id = _arm_two_agents()[0]
    today = now_ist().date().isoformat()

    def dialled() -> int:
        return client.get(f"/api/day?agent_id={agent}").json()["dial_log"].get("dialled", 0)

    before = dialled()
    _seed_dial(campaign_id, None, today, 3)

    assert dialled() - before == 3, (
        f"3 calls logged before dial_log.agent_id was stamped moved agent {agent}'s "
        f"day by {dialled() - before} — they are its campaign's calls either way")


def test_resync_status_is_scoped_to_its_agent(client, pin_clock, monkeypatch):
    """A prepare scoped to one language must not re-sync — or stop — the other.

    `_resync_status` selected DISTINCT agent_id across every armed campaign, so
    `prepare_day(resync=True, agent_id=125)` could pause Tamil campaigns and
    report them back in `stopped_in_formi`. No dialling risk, but it is the same
    cross-language bleed the scoping exists to remove.

    Both paths are covered: scoped sees one agent, unscoped still sees them all,
    which is what the unattended autopilot pass sends.
    """
    import api.autopilot as autopilot_module
    import api.day as day_module
    from engine import metabase_source as ms
    from engine import sync as sync_module

    (first, _), (second, _) = _arm_two_agents()
    now = pin_clock(15)
    seen: list[list[int]] = []

    def _capture(conn, agents, config, schema, today=None):
        seen.append(list(agents))
        return []

    monkeypatch.setattr(ms, "load_config", lambda *a, **k: None)
    monkeypatch.setattr(ms, "describe_schema", lambda *a, **k: None)
    monkeypatch.setattr(sync_module, "refresh_campaign_status", _capture)
    monkeypatch.setattr(autopilot_module, "_resync", lambda campaign_id, day: 0)

    day_module.prepare_day(now.date(), "auto", True, first)
    day_module.prepare_day(now.date(), "auto", True)

    assert len(seen) == 2, (
        f"_resync_status did not run on both passes (it is called inside a "
        f"try/except that only logs): {seen}")
    scoped, whole = seen
    assert scoped == [first], f"a prepare scoped to agent {first} re-synced {scoped}"
    assert {first, second} <= set(whole), (
        f"an unscoped prepare must still re-sync every armed agent, it saw {whole}")


# ---------------------------------------------------------------------------
# Malformed schedule env vars stop the API at boot
# ---------------------------------------------------------------------------

def _boot(tmp_path, line: str) -> "object":
    """Import the API in a subprocess with one line in its .env.

    A subprocess because IMPORT is the thing under test and this session
    imported `api.main` long ago. The value goes in the .env FILE rather than an
    export, because the file is the path the operator actually uses and the one
    `load_env` has to reach before the routers are imported. The variable is
    stripped from the inherited environment first -- `load_env` uses setdefault,
    so a key already present would shadow the file.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    name = line.split("=", 1)[0]
    env_file = tmp_path / f"{name.lower()}.env"
    env_file.write_text(line + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != name}
    env.update(REDIAL_ENV_FILE=str(env_file), PYTHONPATH=str(root))
    return subprocess.run([sys.executable, "-c", "import api.main"], cwd=str(root),
                          env=env, capture_output=True, text=True)


def test_a_malformed_agent_language_stops_the_api_at_boot(client, tmp_path):
    """Raising only from the request was not fail-fast.

    `/api/agents` answered 500, the client caught it and fell back to a roster
    derived from the campaign list -- which carries no language -- so the labels
    quietly vanished with no error anywhere on a live dialler.
    """
    missing = _boot(tmp_path, "AGENT_LANGUAGES=125:Hindi,127")

    assert missing.returncode != 0, (
        "a malformed AGENT_LANGUAGES booted; the labels will silently disappear")
    assert "AGENT_LANGUAGES" in missing.stderr and "127" in missing.stderr, (
        f"the error must name the variable and the offending entry: {missing.stderr}")

    bad_id = _boot(tmp_path, "AGENT_LANGUAGES=x:Hindi")

    assert bad_id.returncode != 0, "a non-numeric agent id booted"
    assert "AGENT_LANGUAGES" in bad_id.stderr and "x:Hindi" in bad_id.stderr, bad_id.stderr

    # Only the comma splits entries, so this labels 125 with the rest of the
    # line and leaves 127 unlabelled -- the wrong-language failure this variable
    # exists to prevent, reached by the likeliest typo in it.
    separator = _boot(tmp_path, "AGENT_LANGUAGES=125:Hindi;127:Tamil")

    assert separator.returncode != 0, (
        "a semicolon-separated AGENT_LANGUAGES booted; agent 125 is now labelled "
        "'Hindi;127:Tamil' and agent 127 has no language at all")
    assert "AGENT_LANGUAGES" in separator.stderr and "Hindi;127:Tamil" in separator.stderr, \
        separator.stderr

    good = _boot(tmp_path, "AGENT_LANGUAGES=125:Hindi,127:Tamil")

    assert good.returncode == 0, (
        f"the documented form must still boot: {good.stderr}")


# ---------------------------------------------------------------------------
# Has a call already been placed?
# ---------------------------------------------------------------------------

def test_already_booked_counts_leads_formi_had_already_queued(client):
    """The engine skips them at plan time; the screen has to say how many."""
    campaign_id = _arm()[0]
    today = now_ist().date().isoformat()
    run_id = _seed_run(campaign_id, today, "auto", "planned", 2)
    with session() as conn:
        conn.executemany(
            "INSERT INTO decisions (run_id, lead_uuid, action, reason, scheduled, created_at) "
            "VALUES (?,?,'SKIP',?,0,?)",
            [(run_id, f"booked-{i}", "ALREADY_SCHEDULED_TODAY queued_today=1", f"{today}T09:00:00")
             for i in range(3)]
            + [(run_id, "waited", "CADENCE_WAIT", f"{today}T09:00:00")])
        conn.commit()

    body = client.get("/api/day").json()
    # `_campaign_json` names it `id`; `campaign_id` is what the APPROVE response
    # calls the same identity.
    row = next(c for c in body["campaigns"] if c["id"] == campaign_id)

    assert row["already_booked"] == 3, "only the ALREADY_SCHEDULED_TODAY rows count"
    assert row["plan_built_at"], "the age of the plan is what makes the count meaningful"


def test_last_dialled_is_the_most_recent_day_that_posted(client):
    # This test needs a campaign with NO run history. `last_dialled` is a MAX over
    # a campaign's WHOLE history and the `client` fixture is session-scoped
    # (tests/conftest.py:21), so one database serves every file: by the time this
    # one runs, tests/test_autopilot.py has already committed a real run for TODAY
    # against campaigns 1-11. Those rows are not tagged 'seeded', so `clean_runs`
    # rightly leaves them alone -- and today beats every date seeded below, which
    # would answer this test with somebody else's approve.
    #
    # So it seeds its own rather than hunting the shared corpus for a free one.
    # Hunting worked until it didn't: 3 of 17 campaigns were still free, and the
    # next test to seed a run against the last of them would have retired this
    # assertion. api/schema.sql:19-52 gives every column but these three a
    # default, so one INSERT is the whole fixture -- and the DELETE is required,
    # because `restore_campaigns` restores flags and never removes rows.
    campaign_id = 90001
    with session() as conn:
        conn.execute("INSERT INTO campaigns (id, agent_id, warehouse_id, name, autopilot) "
                     "VALUES (?, 125, 99001, 'last-dialled fixture', 1)", (campaign_id,))
        conn.commit()
    today = now_ist().date()
    older = (today - timedelta(days=4)).isoformat()
    newer = (today - timedelta(days=2)).isoformat()
    try:
        with session() as conn:
            for run_date, posted in ((older, 5), (newer, 9),
                                     ((today - timedelta(days=1)).isoformat(), 0)):
                conn.execute(
                    "INSERT INTO runs (campaign_id, run_date, kind, status, config_version, "
                    "created_at, dry_run, evaluated, planned, slots, posted, failed, dropped, "
                    "note) VALUES (?,?,'auto','committed',1,?,1,0,0,0,?,0,0,'seeded')",
                    (campaign_id, run_date, f"{run_date}T09:00:00", posted))
            conn.commit()

        body = client.get("/api/day").json()
        # `_campaign_json` names it `id`; `campaign_id` is what the APPROVE
        # response calls the same identity.
        row = next(c for c in body["campaigns"] if c["id"] == campaign_id)

        assert row["last_dialled"] == newer, \
            "a run that posted nothing did not dial, however recent it is"
    finally:
        # Every table that REFERENCES campaigns(id) first (api/schema.sql), or the
        # FK refuses: `clean_runs` only sweeps at teardown so the runs are still
        # here, and reading the day view wrote this campaign a default `config`
        # row. Three deletes because the schema has three referrers -- adding a
        # fourth would fail here loudly rather than leak a row.
        with session() as conn:
            conn.execute("DELETE FROM runs WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM config WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM leads WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
            conn.commit()


# ---------------------------------------------------------------------------
# Where did the calls actually land?
# ---------------------------------------------------------------------------

def test_spread_reports_the_hours_posted_calls_actually_landed_in(client):
    """"Is it scheduling properly" is answered by the hours, not by the pass name.

    Seeds its own campaign on its own agent and asks for that agent only. The
    `client` fixture is session-scoped (tests/conftest.py:21) and
    tests/test_autopilot.py has already committed real runs for TODAY against
    campaigns 1-11; `spread` is a sum over every armed campaign, so an unscoped
    read here would be counting somebody else's hours alongside these four rows.
    """
    campaign_id, agent_id = 90002, 90125
    with session() as conn:
        conn.execute("INSERT INTO campaigns (id, agent_id, warehouse_id, name, autopilot) "
                     "VALUES (?, ?, 99002, 'spread fixture', 1)", (campaign_id, agent_id))
        conn.commit()
    today = now_ist().date().isoformat()
    try:
        run_id = _seed_run(campaign_id, today, "auto", "committed", 0)
        with session() as conn:
            conn.executemany(
                "INSERT INTO plan_items (run_id, lead_uuid, policy_no, phone, disposition, "
                "disposition_class, dte, bucket, bucket_label, priority, slot_no, "
                "scheduled_time, status) VALUES (?,?,?,'9999999999','','',0,'M0','M0',0,1,?,?)",
                [(run_id, "a", "PA", f"{today}T10:15:00", "posted"),
                 (run_id, "b", "PB", f"{today}T10:45:00", "posted"),
                 (run_id, "c", "PC", f"{today}T19:30:00", "posted"),
                 (run_id, "d", "PD", f"{today}T11:00:00", "planned")])
            conn.commit()

        spread = client.get(f"/api/day?agent_id={agent_id}").json()["spread"]

        assert spread["hours"]["10"] == 2, "both 10:xx calls belong to the 10:00 hour"
        assert spread["hours"]["19"] == 1
        assert "11" not in spread["hours"], "a planned slot has not been scheduled anywhere yet"
        assert spread["band"] == {"start": "09:00", "end": "20:00"}, \
            "the band is what the spread has to be judged against"
    finally:
        # Every table that REFERENCES campaigns(id) first (api/schema.sql), or the
        # FK refuses -- reading the day view wrote this campaign a default config
        # row. Same four deletes as the last-dialled fixture above.
        with session() as conn:
            conn.execute("DELETE FROM config WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM runs WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM leads WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
            conn.commit()


def test_both_passes_are_judged_against_the_same_band(client):
    """The recall pass is not a time of day, so it does not get a narrower band.

    Until 14 Sep 2026 the two passes were a morning and an afternoon split at
    13:30, and each was judged against its own half. The pass is decided by the
    previous call now, so both dial the campaign's whole window and the spread
    has one band to report. A recall pass answering 13:30-20:00 would be the old
    split surviving under the new name.
    """
    first = client.get("/api/day?kind=auto").json()["spread"]["band"]
    recall = client.get("/api/day?kind=auto_pm").json()["spread"]["band"]
    assert first == recall == {"start": "09:00", "end": "20:00"}

# ---------------------------------------------------------------------------
# Which pass a per-campaign build files itself under
#
# `_write_run` replaces every `planned` run for (campaign, date), so the kind a
# writer picks decides which pass the SURVIVING plan is filed as. The per-campaign
# `POST /api/campaigns/{id}/plan` used to hardcode `kind="auto"`: opening Plan
# Review and pressing Build deleted the day screen's run whichever pass it was,
# and filed the replacement as the first pass even on a campaign that had
# already dialled one. It asks `kind_for_campaign`, same as the day screen.
# ---------------------------------------------------------------------------

def _tomorrow() -> str:
    """A date `_floor_min` returns None for, so the clock cannot decide the band.

    Tomorrow rather than today on purpose: a run this file does not tag 'seeded'
    is not swept by `clean_runs`, and the `client` fixture is session-scoped, so a
    stray run dated TODAY would answer another file's day view. Nothing in the
    suite reads a future run except through `stranded`, which is past-only.
    """
    return (now_ist().date() + timedelta(days=1)).isoformat()


def _drop_run(run_id: int) -> None:
    """Remove a run the API wrote. `clean_runs` only sweeps note='seeded' rows."""
    with session() as conn:
        conn.execute("DELETE FROM plan_items WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM decisions WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        conn.commit()


def _items(client, run_id: int) -> list[dict]:
    return client.get(f"/api/runs/{run_id}/items?page_size=2000").json()["items"]


def test_a_per_campaign_plan_for_a_day_with_no_calls_is_the_first_pass(client):
    """Build on Plan Review must file itself under the pass the day screen would."""
    campaign_id = _arm()[0]
    day = _tomorrow()
    replaced = _seed_run(campaign_id, day, "auto", "planned", 3)

    built = client.post(f"/api/campaigns/{campaign_id}/plan", json={"date": day})
    assert built.status_code == 200, built.text
    built = built.json()
    try:
        # Before the empty-plan skip below: a plan filed under the wrong pass can
        # come back with no slots at all, and a test that skips on that is a test
        # that goes quiet exactly when the bug is present.
        assert built["kind"] == "auto", \
            "nothing has been dialled tomorrow, so tomorrow's plan is a first pass"
        # It really does replace the day screen's run -- which is why the run it
        # leaves behind has to be filed the same way.
        assert client.get(f"/api/runs/{replaced}").status_code == 404
        items = _items(client, built["id"])
        if not items:
            pytest.skip("nothing due tomorrow in the seed")
        latest = max(i["scheduled_time"][11:16] for i in items)
        assert latest <= "20:00", f"a plan put a call at {latest}, past the window"
    finally:
        _drop_run(built["id"])


def test_a_per_campaign_plan_after_a_first_pass_is_the_recall_pass(client):
    """Build on a campaign that has already dialled today must not overwrite it.

    The two passes of a day are two runs. Filing this one as `auto` does not
    merely mislabel it: `_write_run` deletes the `planned` run for the kind it is
    given, and the first pass's row is the record of what already went out.
    """
    campaign_id = _arm()[0]
    day = now_ist().date().isoformat()
    first = _posted_run(campaign_id, day, "auto", "committed", 4)

    built = client.post(f"/api/campaigns/{campaign_id}/plan", json={"date": day})
    assert built.status_code == 200, built.text
    built = built.json()
    try:
        assert built["kind"] == "auto_pm", \
            "this campaign has dialled today already, so its next plan is a recall"
        assert client.get(f"/api/runs/{first}").status_code == 200, \
            "planning the recall must not delete the first pass's run"
    finally:
        _drop_run(built["id"])
        _drop_run(first)


def test_rebuilding_a_plan_clears_the_other_passs_stale_plan(client):
    """One plan per campaign per day. A rebuild is "this is the plan now".

    Scoped to the same kind, the delete leaves the other pass's unapproved plan
    sitting beside the new one. On 15 Sep that was ~4,000 slots under "recall
    pass, awaiting approval" -- written at 15:00 for campaigns whose first call
    had not gone out -- still on the day screen at 20:19 next to the first pass
    that HAD gone out. Two plans on screen, one of them dead, no way to tell
    which from the row.
    """
    campaign_id = _arm()[0]
    day = _tomorrow()
    stale = _seed_run(campaign_id, day, "auto_pm", "planned", 3)

    built = client.post(f"/api/campaigns/{campaign_id}/plan", json={"date": day})
    assert built.status_code == 200, built.text
    built = built.json()
    try:
        assert built["kind"] == "auto", \
            "nothing has been dialled tomorrow, so tomorrow's plan is a first pass"
        assert client.get(f"/api/runs/{stale}").status_code == 404, \
            "a recall plan nobody approved survived a rebuild of the same day"
    finally:
        _drop_run(built["id"])


def test_a_recall_is_not_prepared_for_a_campaign_that_has_not_dialled(untouched, monkeypatch):
    """The 15:00 pass must not write a recall for a campaign with no calls yet.

    A recall is defined against today's earlier calls, so without one there is
    nothing to recall -- and now that a rebuild clears the day's other stale plan
    (above), writing one would DELETE the morning's real plan and leave Approve
    with nothing to dial. The two changes only work together.
    """
    from api import autopilot                      # noqa: PLC0415 — patched per test
    monkeypatch.setattr(autopilot, "remaining_leads",
                        lambda conn, campaign_id, day=None: 42)
    campaign_id = untouched()[0]
    today = now_ist().date()
    with session() as conn:
        conn.execute("UPDATE campaigns SET autopilot=1, enabled=1, paused=0, hidden=0 "
                     "WHERE id=?", (campaign_id,))
        conn.commit()

    out = day_module._prepare_one(campaign_id, today, day_module.RECALL_PASS, resync=False)
    assert out["status"] == "no_first_pass_yet", out
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM runs WHERE campaign_id=?",
                            (campaign_id,)).fetchone()["n"] == 0, \
            "the refused recall wrote a plan anyway"

    # Scoped to the recall. The first pass is exactly what this campaign is owed,
    # and refusing that too would leave it with no plan at all.
    first = day_module._prepare_one(campaign_id, today, day_module.FIRST_PASS, resync=False)
    assert first["status"] != "no_first_pass_yet", first

    # And it lifts the moment a call has actually gone out.
    _posted_run(campaign_id, today.isoformat(), day_module.FIRST_PASS, "committed", 7)
    again = day_module._prepare_one(campaign_id, today, day_module.RECALL_PASS, resync=False)
    assert again["status"] != "no_first_pass_yet", again


# ---------------------------------------------------------------------------
# Approving re-plans
# ---------------------------------------------------------------------------

def _arm_replannable(day: str, kind: str) -> int:
    """Arm a campaign whose `kind` pass has not been acted on yet on `day`.

    Not `_arm`: `client` is session-scoped, so by the time this file runs the
    earlier ones have already committed today's first pass for the campaign
    `_arm` picks -- and `_write_run` rightly refuses to rewrite a run somebody
    has dialled. A test about re-planning needs a campaign that can be re-planned.

    It also needs one with leads: the re-plan runs the engine against the local
    warehouse copy, and a campaign holding none of them would come back with an
    empty plan, which is not the thing under test.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE enabled=1 "
            "AND id IN (SELECT campaign_id FROM leads) AND id NOT IN "
            "(SELECT campaign_id FROM runs WHERE run_date=? AND kind=? AND status!='planned') "
            "ORDER BY id", (day, kind)).fetchone()
        if row is None:
            pytest.skip(f"every campaign has already dialled its {kind} pass for {day}")
        conn.execute("UPDATE campaigns SET autopilot=1, enabled=1, paused=0, hidden=0 "
                     "WHERE id=?", (row["id"],))
        conn.commit()
    return int(row["id"])


def test_approving_replans_from_the_current_minute_not_the_plan_on_file(client, pin_clock):
    """Approve rebuilds the plan from NOW; a 10:0x slot is not re-dialled at noon.

    This is `approve_day`'s central promise -- "approving late does not dial into
    the night", because each campaign is re-planned from the current minute and
    only what still fits goes out. The whole of it is `_approve_one`'s
    `_write_run(...)` and the `fresh` row it reads back: drop those two lines for
    `fresh = run` and the console dials the stale plan instead, sending the
    10:0x slots at noon. Every other approve test was still green.
    """
    today = pin_clock(9).date().isoformat()
    campaign_id = _arm_replannable(today, "auto")
    # The plan on file, built at 09:0x: slots at 10:0x, all of them now past.
    stale = _seed_run(campaign_id, today, "auto", "planned", 3)

    # Noon: still inside the dial window, and two hours past every slot the plan
    # on file holds.
    pin_clock(12)
    body = client.post("/api/day/approve",
                       json={"date": today, "campaign_ids": [campaign_id]}).json()

    one = next(c for c in body["campaigns"] if c["campaign_id"] == campaign_id)
    assert one["status"] == "approved", f"approve did not dial: {one}"
    run_id = one["run_id"]
    try:
        assert run_id != stale, "the plan on file was dialled as it stood"
        times = sorted(i["scheduled_time"][11:16] for i in _items(client, run_id))
        assert times, "an approved run with no slots is not an approval"
        assert times[0] >= "12:00", \
            f"approving at noon put a call at {times[0]} — that minute has gone"
        assert one["posted"] == len(times), "every slot of the fresh plan goes out"
    finally:
        _drop_run(run_id)


def test_a_day_approve_counts_each_lead_it_did_not_dial_exactly_once(client, pin_clock,
                                                                     monkeypatch):
    """`not_dialled` is the operator's only measure of how much of a day never went out.

    It was `expired + dropped`, and `_commit` writes `dropped = dropped + stale +
    strays` while ALSO reporting `expired = stale` -- so every slot retired for
    being in the past was counted twice in the one number the approve modal shows
    as "not scheduled". A pass with 340 stale slots reported 680: inflated by
    exactly the commonest reason a slot does not go out.

    Getting a stale slot into a day-level approve takes the same kind of push as
    the stray above, because `_approve_one` RE-PLANS before it commits: the fresh
    plan starts at `_floor_min`, so at the instant it is written no slot of it is
    in the past. What makes one stale is the CLOCK MOVING between the write and
    the post -- the operator reading the modal -- so that is what is staged here.

    The clock is set from the plan's own LAST slot, five minutes before it: that
    is Formi's lead time, so the last minute of the plan is the only one still
    dialable and every earlier slot is retired. Anchored on the plan rather than
    on an hour picked in advance, because which campaign `_arm_replannable` hands
    over depends on what the rest of the suite has already dialled, and a plan of
    6 leads and a plan of 461 land nothing alike -- any fixed hour retired either
    all of one or none of the other.

    `max_per_minute=1` for the same reason, and it is the only push at the plan's
    shape: slots are rotated off each lead's last interaction, and six leads that
    were all last called at the same time are all placed on ONE minute -- a plan
    with no inside for the cutoff to fall in. One call a minute is a setting the
    operator has, it is written nowhere (the config row is untouched), and it
    guarantees what this test needs and nothing more: a run holding both kinds of
    slot at once.
    """
    today = pin_clock(9, 45).date().isoformat()
    campaign_id = _arm_replannable(today, "auto")
    # `_approve_one` approves a PLANNED run; with nothing on file it answers
    # `not_prepared` and never reaches `_commit`.
    _seed_run(campaign_id, today, "auto", "planned", 3)

    real_evaluate, real_commit = day_module._evaluate, day_module._commit

    def evaluate_one_call_a_minute(*args, **kwargs):
        cfg, red, dcfg, now, leads, pairs = real_evaluate(*args, **kwargs)
        return cfg, red, replace(dcfg, max_per_minute=1), now, leads, pairs

    monkeypatch.setattr(day_module, "_evaluate", evaluate_one_call_a_minute)

    def commit_with_one_dialable_minute_left(conn, run, campaign, *args, **kwargs):
        last = conn.execute(
            "SELECT MAX(scheduled_time) AS t FROM plan_items WHERE run_id=? "
            "AND status='planned'", (run["id"],)).fetchone()["t"]
        if last:
            minute = int(last[11:13]) * 60 + int(last[14:16]) - FORMI_LEAD_MINUTES
            pin_clock(minute // 60, minute % 60)
        return real_commit(conn, run, campaign, *args, **kwargs)

    monkeypatch.setattr(day_module, "_commit", commit_with_one_dialable_minute_left)

    body = client.post("/api/day/approve",
                       json={"date": today, "campaign_ids": [campaign_id]}).json()

    one = next(c for c in body["campaigns"] if c["campaign_id"] == campaign_id)
    try:
        if one["status"] != "approved":
            # Every remaining slot in the past is a 409 by design; a plan that
            # fits entirely before the cutoff has none to spare. This branch used
            # to `pytest.skip`, which is how the count came to be asserted ONLY
            # for a campaign that dialled -- and the campaigns that dial are the
            # only ones it was ever right for. A campaign that dialled nothing is
            # holding its whole plan, and the day has to say so.
            assert one["dropped"] == body["not_dialled"] > 0, (
                f"a campaign that never reached the dialler still has to be "
                f"counted: not_dialled={body['not_dialled']}, {one}")
            return
        items = _items(client, one["run_id"])
        missed = [i for i in items if i["status"] in ("expired", "skipped")]
        dialled = [i for i in items if i["status"] == "simulated"]
        if not (missed and dialled):
            pytest.skip(f"the fresh plan did not straddle the cutoff: {one}")
        assert body["not_dialled"] == len(missed), (
            f"the day says {body['not_dialled']} leads were not scheduled, but only "
            f"{len(missed)} of its {len(items)} slots did not dial")
    finally:
        if one.get("run_id"):
            _drop_run(one["run_id"])


def test_a_day_approve_counts_the_leads_of_a_campaign_that_never_dialled(client, pin_clock):
    """A closed window is not zero leads; it is every lead, un-dialled.

    `not_dialled` summed `dropped`, which only the APPROVED return carried. Every
    other outcome -- `window_closed`, `not_prepared`, `already_*`, `error` --
    answered with no such key, so `.get(..., 0)` scored it zero however many
    leads it was holding. A 19:45 approve is past every campaign's `end_min`, so
    all twelve answer `window_closed` and the status bar the operator asked for
    read "0 scheduled · 0 not scheduled" over 2,000 leads that were never called.

    Approving at 21:00 is that state exactly, with nothing else pushed: the
    window shuts at 20:00, so `_approve_one` returns before it re-plans and
    before it dials.
    """
    today = pin_clock(21).date().isoformat()
    campaign_id = _arm(1)[0]
    _seed_run(campaign_id, today, "auto", "planned", 7)

    body = client.post("/api/day/approve",
                       json={"date": today, "campaign_ids": [campaign_id]}).json()

    one = next(c for c in body["campaigns"] if c["campaign_id"] == campaign_id)
    assert one["status"] == "window_closed", one
    assert body["posted"] == 0 and body["approved"] == 0
    assert body["not_dialled"] == 7, (
        f"7 planned leads went nowhere and the day says {body['not_dialled']} "
        f"were not scheduled: {one}")


# ---------------------------------------------------------------------------
# "Approved" has to mean somebody approved it
# ---------------------------------------------------------------------------

def test_a_planned_pass_with_nothing_ready_is_not_reported_as_approved(client):
    """Plans that came back empty are not an approval -- nobody dialled anything.

    A recall pass after a first pass that booked every lead: every run is
    `planned` and every one holds zero slots. The ladder fell straight through to
    `approved`, so the screen said "this pass has been approved" and offered
    neither Build nor Approve -- and the leads a 16:00 re-sync pulled in could
    then never be planned or approved at all. `_stranded` does not catch it
    either: it only reports runs with `slots > 0`.

    Its own campaign on its own agent, scoped to that agent: `status` is a
    statement about the WHOLE page, so it can only be asserted on a page whose
    contents this test decides. The `client` fixture is session-scoped and
    earlier files leave real runs against campaigns 1-11.
    """
    campaign_id, agent_id = 90003, 90126
    with session() as conn:
        conn.execute("INSERT INTO campaigns (id, agent_id, warehouse_id, name, autopilot) "
                     "VALUES (?, ?, 99003, 'empty pass fixture', 1)", (campaign_id, agent_id))
        conn.commit()
    today = now_ist().date().isoformat()
    try:
        _seed_run(campaign_id, today, "auto", "planned", 0)
        body = client.get(f"/api/day?agent_id={agent_id}").json()

        assert [c["id"] for c in body["campaigns"]] == [campaign_id], \
            "the scope has to hold this campaign and nothing else"
        assert body["totals"]["ready"] == 0 and body["campaigns"][0]["run_status"] == "planned"
        assert body["status"] == "nothing_to_dial", \
            "a plan nobody has approved must never be reported as approved"
    finally:
        # Every table that REFERENCES campaigns(id) first (api/schema.sql), or the
        # FK refuses -- reading the day view wrote this campaign a default config
        # row. Same four deletes as the fixtures above.
        with session() as conn:
            conn.execute("DELETE FROM config WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM runs WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM leads WHERE campaign_id=?", (campaign_id,))
            conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
            conn.commit()


def test_a_pass_holding_one_unbuilt_campaign_is_not_reported_as_approved(client):
    """Eight committed runs and one campaign that never built is not an approval.

    `statuses` is a SET over the whole panel, so {committed, not_prepared} matched
    no arm of the ladder and fell through to `approved` -- the hero then read
    "this pass has been approved", offered only the call log, and the unbuilt
    campaign's leads had no route onto the clock at all: the picker's Save is
    greyed out for a campaign that is already armed.

    Two campaigns on one agent, one committed and one with no run, scoped to that
    agent for the same reason as the test above: `status` is a statement about the
    whole page.
    """
    committed_id, unbuilt_id, agent_id = 90004, 90005, 90127
    with session() as conn:
        conn.executemany(
            "INSERT INTO campaigns (id, agent_id, warehouse_id, name, autopilot) VALUES (?,?,?,?,1)",
            [(committed_id, agent_id, 99004, "built fixture"),
             (unbuilt_id, agent_id, 99005, "unbuilt fixture")])
        conn.commit()
    today = now_ist().date().isoformat()
    try:
        _seed_run(committed_id, today, "auto", "committed", 2)
        body = client.get(f"/api/day?agent_id={agent_id}").json()

        assert sorted(c["run_status"] for c in body["campaigns"]) == ["committed", "not_prepared"]
        assert body["status"] == "part_prepared", \
            "a pass holding a campaign with no plan has not been approved"
    finally:
        # Same four deletes, in the same order, for both fixture campaigns.
        with session() as conn:
            for cid in (committed_id, unbuilt_id):
                conn.execute("DELETE FROM config WHERE campaign_id=?", (cid,))
                conn.execute("DELETE FROM runs WHERE campaign_id=?", (cid,))
                conn.execute("DELETE FROM leads WHERE campaign_id=?", (cid,))
                conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
            conn.commit()


# ---------------------------------------------------------------------------
# What the approve did NOT include
# ---------------------------------------------------------------------------

def test_approve_names_a_campaign_stopped_after_its_plan_was_built(client):
    """The 15 Sep shape: plan built at 10:01, campaign stopped at 16:33.

    The approve walks ARMED campaigns, so a stopped one is skipped with no
    result row at all — not a failure, not a zero, simply absent. Three of them
    took 2,280 ready leads off that evening's books and the approve a minute
    later mentioned none of it. Approving must not dial a campaign somebody
    stopped on purpose; it must say the campaign is there.
    """
    campaign_id = _arm()[0]
    today = now_ist().date().isoformat()
    _seed_run(campaign_id, today, "auto", "planned", 6)
    with session() as conn:
        conn.execute("UPDATE campaigns SET paused=1 WHERE id=?", (campaign_id,))
        conn.commit()

    body = client.post("/api/day/approve",
                       json={"date": today, "campaign_ids": [campaign_id]}).json()

    assert not any(c["campaign_id"] == campaign_id for c in body["campaigns"]), \
        "an approve dialled a campaign the operator had stopped"
    mine = [c for c in body["left_behind"] if c["campaign_id"] == campaign_id]
    assert len(mine) == 1, body["left_behind"]
    assert mine[0]["leads"] == 6, "the shelved plan's leads are the number that matters"
    assert mine[0]["reason"] == "paused"
    assert body["not_dialled"] >= 6, \
        "leads nobody sent are not dialled, and have to be counted as such"


def test_a_campaign_the_operator_unticked_is_not_reported_as_a_surprise(client):
    """Left out on purpose is not left behind. They already know; it is noise."""
    stopped, other = _arm(2)
    today = now_ist().date().isoformat()
    _seed_run(stopped, today, "auto", "planned", 4)
    with session() as conn:
        conn.execute("UPDATE campaigns SET paused=1 WHERE id=?", (stopped,))
        conn.commit()

    body = client.post("/api/day/approve",
                       json={"date": today, "campaign_ids": [other]}).json()

    assert not any(c["campaign_id"] == stopped for c in body["left_behind"]), \
        "a campaign the operator did not tick was reported as a surprise"


def test_the_dial_queue_reports_what_it_left_behind_too(client):
    """The queue is the path the button takes, so it is the path that must say so.

    `/api/day/approve` and `/api/day/dial` do the same work; only the second one
    is what the console presses. Reporting the shelved campaigns on the first
    alone would put the answer somewhere the operator never looks.
    """
    stopped, other = _arm(2)
    day = now_ist().date().isoformat()
    _seed_run(stopped, day, "auto", "planned", 5)
    _seed_run(other, day, "auto", "planned", 2)
    with session() as conn:
        conn.execute("UPDATE campaigns SET enabled=0 WHERE id=?", (stopped,))
        conn.commit()

    client.post("/api/day/dial",
                json={"date": day, "kind": "auto", "campaign_ids": [stopped, other]})
    done = _await_dial(client)

    mine = [c for c in done["result"]["left_behind"] if c["campaign_id"] == stopped]
    assert len(mine) == 1, done["result"]["left_behind"]
    assert mine[0]["leads"] == 5
    assert mine[0]["reason"] == "switched off"
    assert not any(c["campaign_id"] == other for c in done["result"]["left_behind"]), \
        "a campaign the walk dialled was also reported as left behind"


# ---------------------------------------------------------------------------
# A rehearsal to your own handset has no hours
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hour", [4, 23])
def test_an_auto_timed_test_call_is_free_of_the_dial_window(client, pin_clock, hour):
    """"Test call now" at 04:00 or 23:00 books THAT minute, not the next morning.

    The operator's own constraint: the only number this endpoint will dial is one
    they listed as their own handset, so the customer-protection window does not
    apply to it. `_next_slot` in api/routes_core.py is already right; nothing held
    it there. Clamping it to 09:00-20:00, or unconditionally to 09:00 tomorrow,
    left the whole suite green -- so the answer is asserted here to the exact
    minute, date included, which is the only form either clamp cannot survive.

    tests/test_api.py's rehearsal test cannot see this: it runs on the real clock
    and asserts `>= now`, which both clamps satisfy. The hour has to be pinned for
    the question to mean anything.
    """
    from engine.seed import TEST_NUMBERS

    now = pin_clock(hour)
    body = client.post("/api/test-call/preview", json={"phone": TEST_NUMBERS[0]}).json()
    assert body["found"], body

    # Formi's five-minute floor is ours to respect; the dial window is not.
    expected = (now + timedelta(minutes=FORMI_LEAD_MINUTES)).strftime("%Y-%m-%dT%H:%M:00")
    assert body["would_post"]["body"]["scheduled_time"] == expected, \
        f"a {hour:02d}:00 rehearsal must go out at {hour:02d}:{FORMI_LEAD_MINUTES:02d} today"


def test_a_stored_never_dial_list_we_used_to_ship_is_brought_up_to_date():
    """A saved config is a snapshot, so a new protected slug reaches nobody.

    On 14 Sep 2026, 85 of the 101 saved configs held the eight-slug list this
    app shipped before `policy_expired` was added -- and `with_defaults` merges
    the defaults UNDERNEATH the body, so every one of those campaigns would have
    let a mandatory day override that exclusion. Adding a slug to NEVER_DIAL is
    only half the change; this is the other half.

    An operator's own list is not one of the shipped ones, so it is left alone.
    That is the line between a migration and overwriting somebody's decision.
    """
    from api.db import DEFAULT_CONFIG, SUPERSEDED_NEVER_DIAL, with_defaults

    for shipped in SUPERSEDED_NEVER_DIAL:
        merged = with_defaults({"never_dial": list(shipped)})
        assert merged["never_dial"] == DEFAULT_CONFIG["never_dial"], shipped
        assert "not_interested" in merged["never_dial"]
        assert "policy_expired" in merged["never_dial"]

    mine = ["dnd", "renewed"]
    assert with_defaults({"never_dial": mine})["never_dial"] == mine, \
        "a list nobody shipped is the operator's own -- never replace it"


def test_verification_follows_the_data_not_the_env_var(client):
    """`LEADS_SOURCE` defaults to "seed", and that switched verification off.

    Found on 14 Sep 2026. The production box had never set `LEADS_SOURCE`, so
    `verify_day` took its "no warehouse" exit on every pass since the dial log
    shipped: 1,364 real calls that day sat at `verified='pending'` forever. The
    console could say what Formi ACCEPTED and could never say what Formi
    DIALLED -- the one question the dial log exists to answer.

    So the campaign table decides it, exactly as `/api/health` already did:
    seed ids are 1-16, warehouse ids 1400+ (`api.db.purge_campaigns`).
    """
    import os
    import sqlite3

    from api.db import leads_source_effective, session

    assert os.environ["LEADS_SOURCE"] == "seed", "this test is about that default"

    # A scratch table, not the suite's: whether a warehouse-id campaign is
    # already in the shared DB depends on which tests ran first, and the rule
    # under test is about ids, not about this file's fixtures.
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    scratch.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY)")
    assert leads_source_effective(scratch) == "seed", "an empty table falls back to the env"
    scratch.execute("INSERT INTO campaigns (id) VALUES (14)")
    assert leads_source_effective(scratch) == "seed", "seed ids are 1-16"
    scratch.execute("INSERT INTO campaigns (id) VALUES (1400)")
    assert leads_source_effective(scratch) == "warehouse", \
        "a real warehouse campaign must beat the env var's seed default"
    scratch.close()

    # And the endpoint an operator reads before a live dial agrees with it.
    with session() as conn:
        expected = leads_source_effective(conn)
    assert client.get("/api/health").json()["leads_source"] == expected


def test_a_call_formi_accepted_and_then_lost_is_sent_again(client):
    """A 2xx is not proof that a call exists, so verification repairs what it finds.

    On 14 Sep 2026 Formi answered `{"success": true, ..., "task_id": ...}` to all
    1,364 calls of one run, by customer name, and then created no interaction for
    206 of them. The console reported 1,364 posted / 0 failed and was right about
    every word of it. Nothing sent those calls again.

    Two rules, both asserted here: a lost slot still in the future goes back out,
    and a slot Formi has already lost `RESEND_LIMIT` times is left alone -- a
    console that keeps asking every ten minutes until the window shuts is a loop,
    not a repair.
    """
    from api.dial_log import RESEND_LIMIT, _resend_missing
    from api.db import now_ist, session

    day = now_ist().date().isoformat()
    slot = (now_ist() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:00")
    campaign_id = _arm_two_agents()[0][1]
    run_id = _seed_run(campaign_id, day, "auto", "committed", 2)

    with session() as conn:
        items = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM plan_items WHERE run_id=? ORDER BY id", (run_id,))]
        # Both went out and Formi lost both. The second has used up its resends.
        conn.executemany(
            "UPDATE plan_items SET status='posted', scheduled_time=? WHERE id=?",
            [(slot, i) for i in items])
        conn.executemany(
            "INSERT INTO dial_log (created_at, campaign_id, run_id, item_id, source, "
            "scheduled_time, dry_run, url, request_body, outcome, verified) "
            "VALUES (?,?,?,?,'test',?,0,'','{}','placed','missing')",
            [(f"{day}T10:00:00", campaign_id, run_id, items[0], slot)]
            + [(f"{day}T10:00:00", campaign_id, run_id, items[1], slot)] * (RESEND_LIMIT + 1))
        conn.commit()

        out = _resend_missing(conn, day)
        assert out["resent"] == 1, out
        status = dict(conn.execute(
            "SELECT id, status FROM plan_items WHERE run_id=?", (run_id,)).fetchall())
        # DRY_RUN is on for the whole suite, so a re-posted slot reads `simulated`.
        assert status[items[0]] == "simulated", "a lost slot must go back out"
        assert status[items[1]] == "posted", "a slot lost RESEND_LIMIT times is left alone"

        conn.execute("DELETE FROM dial_log WHERE run_id=?", (run_id,))
        conn.commit()


def test_a_lost_slot_whose_time_has_gone_is_not_sent_again(client):
    """`posted` + `missing` is the true record of a call the day has moved past.

    Re-posting it would ask Formi for a call at a time that has gone, and
    rewriting it `expired` would erase the fact that we did send it. Neither is
    an improvement on saying what happened. The lead returns in the next plan.
    """
    from api.dial_log import _resend_missing
    from api.db import now_ist, session

    day = now_ist().date().isoformat()
    gone = (now_ist() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:00")
    campaign_id = _arm_two_agents()[0][1]
    run_id = _seed_run(campaign_id, day, "auto", "committed", 1)

    with session() as conn:
        item = int(conn.execute("SELECT id FROM plan_items WHERE run_id=?",
                                (run_id,)).fetchone()["id"])
        conn.execute("UPDATE plan_items SET status='posted', scheduled_time=? WHERE id=?",
                     (gone, item))
        conn.execute(
            "INSERT INTO dial_log (created_at, campaign_id, run_id, item_id, source, "
            "scheduled_time, dry_run, url, request_body, outcome, verified) "
            "VALUES (?,?,?,?,'test',?,0,'','{}','placed','missing')",
            (f"{day}T10:00:00", campaign_id, run_id, item, gone))
        conn.commit()

        assert _resend_missing(conn, day)["resent"] == 0
        assert conn.execute("SELECT status FROM plan_items WHERE id=?",
                            (item,)).fetchone()["status"] == "posted"

        conn.execute("DELETE FROM dial_log WHERE run_id=?", (run_id,))
        conn.commit()


def _arm_many(count: int) -> list[int]:
    """Arm `count` campaigns, agent be damned -- what the dial walk is about.

    `_arm` gives one campaign per AGENT, and the seed DB has two, so `_arm(4)`
    skips. The bug these tests cover is a day of twenty-two campaigns on ONE
    agent that dialled one of them; the walk must be exercised over more
    campaigns than there are agents.
    """
    with session() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM campaigns ORDER BY id LIMIT ?", (count,))]
        assert len(ids) == count, "fixture DB is smaller than this test needs"
        conn.executemany("UPDATE campaigns SET autopilot=1, enabled=1, paused=0, "
                         "hidden=0 WHERE id=?", [(i,) for i in ids])
        conn.commit()
    return ids


def _await_dial(client, tries: int = 200) -> dict:
    """Poll the walk until it stops. It runs on a thread; the test must not race it."""
    import time
    for _ in range(tries):
        state = client.get("/api/day/dial").json()
        if not state["running"]:
            return state
        time.sleep(0.05)
    raise AssertionError(f"dial walk never finished: {state}")


def test_the_dial_walks_every_campaign_without_the_browser(client):
    """The day stopped after one campaign because the queue lived in the page.

    On 14 Sep 2026 the operator pressed Dial on 22 campaigns. Campaign 1744's
    approve was a single ten-minute request -- 11:09:09 to 11:19:05, 1,364 calls
    -- and uvicorn logged no access line for it: nobody was on the other end of
    the socket when it answered. The queue died with that request and the other
    21 campaigns were never asked for. One campaign dialled.

    So the walk is server-side and nothing about it depends on the browser
    staying on the page.
    """
    ids = _arm_many(4)
    day = now_ist().date().isoformat()
    for campaign_id in ids:
        _seed_run(campaign_id, day, "auto", "planned", 2)

    started = client.post("/api/day/dial",
                          json={"date": day, "kind": "auto", "campaign_ids": ids}).json()
    assert started["running"] is True, "the dial must start and return, not block"
    assert started["total"] == len(ids), "every named campaign belongs to the walk"

    done = _await_dial(client)
    assert done["done"] == len(ids), f"the walk stopped early: {done}"
    walked = {r["campaign_id"] for r in done["results"]}
    assert walked == set(ids), f"campaigns never reached: {set(ids) - walked}"


def test_pressing_dial_twice_does_not_dial_twice(client):
    """A button pressed again because nothing visibly happened must not re-dial."""
    ids = _arm_many(2)
    day = now_ist().date().isoformat()
    for campaign_id in ids:
        _seed_run(campaign_id, day, "auto", "planned", 2)

    first = client.post("/api/day/dial",
                        json={"date": day, "kind": "auto", "campaign_ids": ids}).json()
    again = client.post("/api/day/dial",
                        json={"date": day, "kind": "auto", "campaign_ids": ids}).json()
    # Either it is still running -- in which case the second call returns that
    # same walk -- or it finished between the two, which is not a second dial.
    assert again["started_at"] == first["started_at"], "a second press started a second walk"
    _await_dial(client)


def test_a_stopped_dial_leaves_the_rest_planned_and_approvable(client):
    """Stop breaks between campaigns; what it never reached was never posted.

    Asserted as the invariant rather than as a stopwatch: a walk over simulated
    campaigns can finish before a stop posted from the same thread is ever read,
    so "it stopped at campaign 2" is not a fact any test can hold. What must be
    true at every interleave is that each campaign is EITHER in the results OR
    still `planned` -- never dialled-but-unreported, and never dropped from the
    day with its plan spent.
    """
    ids = _arm_many(4)
    day = now_ist().date().isoformat()
    for campaign_id in ids:
        _seed_run(campaign_id, day, "auto", "planned", 2)

    client.post("/api/day/dial", json={"date": day, "kind": "auto", "campaign_ids": ids})
    assert client.post("/api/day/dial/stop").json()["stopped"] is True

    done = _await_dial(client)
    reached = {r["campaign_id"] for r in done["results"]}
    assert done["done"] == len(done["results"]), "a campaign was walked and not reported"
    with session() as conn:
        for campaign_id in set(ids) - reached:
            # The run this test seeded, not an older one for the same day left by
            # a test that ran earlier in this shared DB.
            run = conn.execute(
                "SELECT status FROM runs WHERE campaign_id=? AND run_date=? AND kind='auto' "
                "ORDER BY id DESC", (campaign_id, day)).fetchone()
            assert run["status"] == "planned", \
                "a campaign the walk never reached must still be approvable"


class _Ok:
    """The narrowest thing `_dial_live` will accept as a successful POST."""

    status_code = 200
    text = "{}"


def test_dialling_does_not_hold_the_database_while_it_posts(client, monkeypatch):
    """A dial in flight must leave the database writable by everyone else.

    `_dial_live` used to UPDATE each slot inside the posting loop, so Python
    opened a write transaction on the first POST of a batch and held it until
    the flush fifty paced posts later -- longer if any of them retried a 45s
    timeout. SQLite allows one writer, so for that whole stretch nothing else
    in the process could write.

    On 14 Sep 2026 that cost four campaigns of a dial walk. The background
    verify was re-sending the slots Formi had lost, down this same loop; the
    walk reached campaign 1800, waited out `connect()`'s fifteen seconds on
    `DELETE FROM plan_items`, and died -- as did 1804, 1805 and 1807 behind it.

    The probe below is the other writer. One second, not fifteen: the question
    is whether the lock is free WHILE a call is being placed, not whether it
    frees up eventually.
    """
    import sqlite3

    from api import routes_core
    from api.db import db_path

    blocked: list[str] = []
    with session() as conn:
        campaign = conn.execute("SELECT * FROM campaigns LIMIT 1").fetchone()

    def _post(*_args):
        other = sqlite3.connect(db_path(), timeout=1)
        try:
            other.execute("UPDATE campaigns SET autopilot_note='probe' WHERE id=?",
                          (campaign["id"],))
            other.commit()
        except sqlite3.OperationalError as exc:
            blocked.append(str(exc))
        finally:
            other.close()
        return _Ok(), 1

    monkeypatch.setenv("FORMI_POST_RATE_PER_SEC", "0")      # no pacing; this is not that test
    monkeypatch.setattr(routes_core, "_formi_post", _post)

    # plan_items ids that do not exist: the UPDATE matches nothing, which is all
    # this needs. It is the transaction the loop holds that is under test.
    items = [{"id": -900 - i, "lead_uuid": f"lock-{i}", "lead_name": "x",
              "phone": "9000000000", "scheduled_time": "2026-09-14T10:00:00",
              "slot_no": 1, "bucket": "F5", "campaign_id": campaign["id"]}
             for i in range(5)]
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        posted, failed = routes_core._dial_live(conn, campaign, items, "test")
    finally:
        conn.close()

    assert (posted, failed) == (5, 0), "the probe must not have broken the dial itself"
    assert not blocked, (
        f"{len(blocked)} of 5 posts ran with the write lock held: {blocked[0]}")
