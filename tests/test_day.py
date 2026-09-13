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


def _seed_dial(campaign_id: int, agent_id: int, day: str, n: int) -> None:
    """`n` dial-log rows for one campaign, as the dialler would have left them.

    `agent_id` is written from the campaign at log time in `api/dial_log.py`, so
    it is set here the same way -- a row whose agent is NULL is not a row this
    console produces.
    """
    with session() as conn:
        conn.executemany(
            "INSERT INTO dial_log (created_at, campaign_id, agent_id, source, "
            "scheduled_time, dry_run, url, request_body, outcome, verified) "
            "VALUES (?,?,?,'test',?,1,'','{}','simulated','dialled')",
            [(f"{day}T10:00:00", campaign_id, agent_id, f"{day}T10:{i:02d}:00")
             for i in range(n)])
        conn.commit()


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


def test_prepare_reports_the_band_that_closed_not_the_whole_window(client, pin_clock):
    """Preparing the morning wave after the boundary must name the BAND.

    Before bands, this said "the 09:00-20:00 window has closed" only after 20:00,
    and happily planned a 'morning' wave into the evening at any hour before it.

    `pin_clock` rather than a hand-rolled monkeypatch of `day_module.now_ist`:
    `_prepare_one` reads the clock twice, once for `_evaluate` and once for
    `_floor_min`, and patching the one name in one module left the other on the
    real clock -- working only while the two happened to agree on the date.
    """
    import api.day as day_module

    _arm()
    now = pin_clock(15)

    out = day_module.prepare_day(now.date(), "auto")
    closed = [c for c in out["campaigns"] if c["status"] == "window_closed"]

    assert closed, "the morning band is shut at 15:00 - every campaign must say so"
    assert "13:30" in closed[0]["detail"], \
        f"the detail must name the band, got {closed[0]['detail']!r}"
    assert "20:00" not in closed[0]["detail"], \
        "naming the full window hides the fact that the morning band is what shut"
    # The wording, not just the hours -- `_approve_one`'s twin is pinned the same
    # way in test_api.py. Without this the detail reverts to "window has closed"
    # with the band's hours in front of it, which reads as the campaign's whole
    # day having ended, and the suite stays green.
    assert "morning band has closed" in closed[0]["detail"], \
        f"the detail must say which BAND shut, got {closed[0]['detail']!r}"


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


def test_the_morning_header_never_reports_an_hour_past_the_boundary(client):
    """A campaign that opens after the boundary must not stretch the morning header.

    14:00-20:00 is a legal window (WINDOW_FLOOR/WINDOW_CEIL are 09:00/20:00), and
    in the morning it clips to start=14:00, end=13:30. Collapsing that forward
    onto `start` made the ENVELOPE end at 14:00 -- the morning wave's header
    promising half an hour past the boundary that defines it, which is the same
    lie ("morning" slots landing in the evening) the bands were added to kill.
    Collapsed toward the band, the campaign contributes 13:30 and the header
    stays inside the morning.
    """
    from api.day import MORNING, _day_window
    from engine.dispatcher import hhmm, parse_hhmm

    span = _day_window({1: {"dial_window": {"start": "14:00", "end": "20:00"},
                            "max_per_minute": 10},
                        2: {"dial_window": {"start": "09:00", "end": "20:00"},
                            "max_per_minute": 10}},
                       {1: 500, 2: 500}, floor=10 * 60, today=True, kind=MORNING)

    assert parse_hhmm(span["window"]["end"]) <= WAVE_BOUNDARY, \
        (f"the morning header says {span['window']['end']}, past the "
         f"{hhmm(WAVE_BOUNDARY)} boundary that defines the morning")
    assert span["window"] == {"start": "09:00", "end": hhmm(WAVE_BOUNDARY)}, span["window"]


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


def test_wave_boundary_set_in_the_env_file_reaches_the_band_logic(client, tmp_path):
    """A WAVE_BOUNDARY in the .env must reach `_band`, not merely os.environ.

    `api.main` used to import the routers BEFORE calling `load_env()`, and
    `api.day` freezes WAVE_BOUNDARY into a module constant at import. So the one
    tunable this feature has was inert: the operator moved the boundary, got no
    error and no warning, and both waves kept dialling to 13:30.

    A subprocess because import ORDER is the thing under test and this session
    imported `api.day` long ago. The value goes in the .env FILE, not a shell
    export: an export already worked before the fix, so exporting one tests the
    single path that was never broken.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env_file = tmp_path / "boundary.env"
    env_file.write_text("WAVE_BOUNDARY=11:15\n", encoding="utf-8")

    probe = (
        "import api.main;"                      # the import IS the thing under test
        "from api.day import AFTERNOON, MORNING, _band;"
        "from engine.dispatcher import DispatchConfig, hhmm;"
        "d = DispatchConfig(start_min=9*60, end_min=20*60);"
        "print(hhmm(_band(MORNING, d).end_min), hhmm(_band(AFTERNOON, d).start_min))"
    )
    env = {k: v for k, v in os.environ.items() if k != "WAVE_BOUNDARY"}
    # Not "" -- load_env uses setdefault, so an empty-but-present key would shadow
    # the file and this test would pass on the default for the wrong reason.
    env.update(REDIAL_ENV_FILE=str(env_file), PYTHONPATH=str(root))

    out = subprocess.run([sys.executable, "-c", probe], cwd=str(root), env=env,
                         capture_output=True, text=True)

    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["11:15", "11:15"], (
        f"{env_file.read_text().strip()} in the .env never reached the band "
        f"logic; the bands report {out.stdout.strip()!r}")


# WAVE_BOUNDARY -> may the API boot on it? `None` is the variable unset.
# Unpadded is legal and has always booted. Everything else here is a time nobody
# typed: `int()` eats a sign, leading zeros and any Unicode digit, so `+13:30`,
# `013:30` and `13:3` ending in an Arabic-Indic zero all used to boot as 13:30
# and `13:005` as 13:05 -- the operator sets one boundary and the day runs on
# another. `09:00`/`20:00` sit ON the dialling edges, which gives one wave the
# whole day and the other an empty band.
BOUNDARY_BOOTS = ((None, True), ("13:30", True), ("9:30", True), ("13:5", True),
                  ("13:70", False), ("00:00", False), ("25:00", False),
                  ("+13:30", False), ("013:30", False), ("13:005", False),
                  ("13:3٠", False), ("1330", False), ("13.30", False),
                  ("13:30:00", False), ("09:00", False), ("20:00", False))


def test_the_wave_boundary_guard_refuses_only_impossible_times(client, tmp_path):
    """`9:30` and `13:5` must boot; `13:70`, `00:00` and `25:00` must not.

    The first version of this guard compared `hhmm(parse_hhmm(raw))` against the
    raw string, so it refused every unpadded value too -- both of those name a
    real time and both booted before the guard existed. On a box running with
    DRY_RUN=0, an API that will not start on a legal value is worse than the
    silent 14:10 the guard was written to catch.

    One subprocess for the whole matrix, because the guard runs at MODULE level
    in api.day: each value needs its own fresh import chain, and this session
    imported api.day long ago.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # A file rather than `python -c`: the probe is a loop, and a multi-line -c
    # argument is a quoting question this test has no reason to ask.
    probe = tmp_path / "boundary_matrix.py"
    probe.write_text(
        "import os, sys\n"
        f"CASES = {[v for v, _ in BOUNDARY_BOOTS]!r}\n"
        "for value in CASES:\n"
        "    for name in [m for m in sys.modules if m.split('.')[0] in ('api', 'engine')]:\n"
        "        del sys.modules[name]\n"
        "    os.environ.pop('WAVE_BOUNDARY', None)\n"
        "    if value is not None:\n"
        "        os.environ['WAVE_BOUNDARY'] = value\n"
        "    try:\n"
        "        import api.main\n"
        "        from api.day import WAVE_BOUNDARY\n"
        "        from engine.dispatcher import hhmm\n"
        "        print(f'{value}\\tBOOT\\t{hhmm(WAVE_BOUNDARY)}')\n"
        "    except Exception as exc:\n"
        "        print(f'{value}\\tREFUSED\\t{type(exc).__name__}: {exc}')\n",
        encoding="utf-8")
    # A .env that does not exist, so the unset case is genuinely unset: `load_env`
    # uses setdefault, and a file carrying WAVE_BOUNDARY would answer for it.
    env = {k: v for k, v in os.environ.items() if k != "WAVE_BOUNDARY"}
    # UTF-8 on both ends of the pipe: one case is a non-ASCII digit, and on a
    # Windows console the child would die encoding its own answer.
    env.update(REDIAL_ENV_FILE=str(tmp_path / "absent.env"), PYTHONPATH=str(root),
               PYTHONIOENCODING="utf-8")

    out = subprocess.run([sys.executable, str(probe)], cwd=str(root), env=env,
                         capture_output=True, text=True, encoding="utf-8")

    assert out.returncode == 0, out.stderr
    seen = dict(line.split("\t", 1) for line in out.stdout.splitlines() if "\t" in line)
    for value, boots in BOUNDARY_BOOTS:
        answer = seen.get(str(value), "")
        if boots:
            assert answer.startswith("BOOT"), \
                f"WAVE_BOUNDARY={value} is a legal time and must boot; it answered {answer!r}"
        else:
            assert answer.startswith("REFUSED"), \
                f"WAVE_BOUNDARY={value} is impossible and must refuse; it answered {answer!r}"
            assert str(value) in answer, \
                f"the refusal for {value!r} must name the value the operator typed: {answer!r}"


def test_a_campaign_closing_exactly_on_the_boundary_has_no_afternoon_band(client):
    """start == end is an empty band too, and only `>=` catches it.

    A campaign whose window ends exactly at WAVE_BOUNDARY clips to
    start == end == WAVE_BOUNDARY in the afternoon. With the guards written `>`
    instead of `>=` this sails through, and on a future date (`_floor_min`
    returns None, so the clock guard cannot fire either) the run that gets
    written stamps every one of its slots on the same single minute --
    the collapse `api/routes_core.py`'s `floor >= end_min` exists to prevent.

    Dated TOMORROW for exactly that reason: it removes the clock guard, leaving
    the empty-band check as the only thing that can close this window.
    """
    from engine.dispatcher import hhmm

    import api.day as day_module

    campaign_id = _arm()[0]
    tomorrow = now_ist().date() + timedelta(days=1)
    config = client.get(f"/api/campaigns/{campaign_id}/config").json()
    saved = client.put(f"/api/campaigns/{campaign_id}/config",
                       json={**config, "dial_window": {"start": "09:00",
                                                       "end": hhmm(WAVE_BOUNDARY)}})
    assert saved.status_code == 200, saved.text
    try:
        prepared = day_module._prepare_one(campaign_id, tomorrow, "auto_pm", False)
        with session() as conn:
            written = conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE campaign_id=? AND run_date=? "
                "AND kind='auto_pm'", (campaign_id, tomorrow.isoformat())).fetchone()["n"]
        _seed_run(campaign_id, tomorrow.isoformat(), "auto_pm", "planned", 3)
        with session() as conn:
            campaign = conn.execute("SELECT * FROM campaigns WHERE id=?",
                                    (campaign_id,)).fetchone()
            approved = day_module._approve_one(conn, campaign, tomorrow, "auto_pm", [])
    finally:
        client.put(f"/api/campaigns/{campaign_id}/config", json=config)

    assert prepared["status"] == "window_closed", prepared
    assert "no afternoon band" in prepared["detail"], prepared["detail"]
    assert written == 0, \
        "a zero-width band must not write a run whose every slot shares one minute"
    assert approved["status"] == "window_closed", approved
    assert approved.get("posted") is None, "a zero-width band must never reach Formi"


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


def test_a_wave_boundary_outside_the_dialling_hours_stops_the_api_at_boot(client, tmp_path):
    """`WAVE_BOUNDARY=00:00` used to boot cleanly and shut the morning down.

    `parse_hhmm` accepted it, nothing compared it against the dialling hours, and
    every campaign got a morning band with no minutes in it -- a silent, total
    morning shutdown on a box with DRY_RUN=0.
    """
    out = _boot(tmp_path, "WAVE_BOUNDARY=00:00")

    assert out.returncode != 0, (
        "WAVE_BOUNDARY=00:00 booted; every campaign now has an empty morning band "
        "and nothing anywhere says so")
    assert "WAVE_BOUNDARY" in out.stderr, out.stderr
    assert "09:00" in out.stderr and "20:00" in out.stderr, (
        f"the error must name the legal range, not just refuse: {out.stderr}")

    # `parse_hhmm` bounds only the TOTAL minutes, so this one is INSIDE the
    # range once read -- it is 14:10 -- and the range check alone lets it
    # through. A boundary set to a time nobody typed is the same failure.
    typo = _boot(tmp_path, "WAVE_BOUNDARY=13:70")

    assert typo.returncode != 0, (
        "WAVE_BOUNDARY=13:70 booted as 14:10; the operator typed one boundary and "
        "got another, with no error")
    assert "WAVE_BOUNDARY" in typo.stderr and "13:70" in typo.stderr, typo.stderr


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
