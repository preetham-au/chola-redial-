"""Decision gate + dispatcher rules. Pure functions, no DB, no network."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from engine.dispatcher import (
    DispatchConfig, dispatch, manual_pairs, red_config_from_body, validate_dial_window,
)
from engine.red_engine import (
    DEFAULT_CONFIG, SCHEDULE, SKIP_CADENCE, SKIP_DAILY_CAP, SKIP_MANUAL_ONLY,
    SKIP_REACHED, Decision, decide,
)

# The config the live console actually ships, as opposed to the engine's bare
# fallback: the second-call rule is meaningless without a populated
# `second_call_dispositions`, and an empty one means "chase everybody".
from api.db import DEFAULT_CONFIG as API_DEFAULTS

TODAY = date(2026, 8, 28)
NOW = datetime(2026, 8, 28, 9, 30)


def lead(**over):
    base = {"lead_uuid": "u-1", "policy_no": "POL1", "stage": "did_not_pick",
            "red": (TODAY + timedelta(days=20)).isoformat(),
            "last_interaction_time": None, "total_interactions": 3,
            "calls_today": 0, "calls_last_7d": 0}
    return {**base, **over}


# ---------------------------------------------------------------------------
# The DNP-only gate
# ---------------------------------------------------------------------------

def test_connected_lead_is_manual_only():
    """A connected disposition is not auto-dialled — a human decides when."""
    decision = decide(lead(stage="positive_followup"), NOW, DEFAULT_CONFIG)
    assert decision.action == SKIP_MANUAL_ONLY
    assert decision.schedule is False
    assert decision.bucket == "D0"          # still counted, still offerable manually


def test_did_not_pick_lead_schedules():
    decision = decide(lead(stage="did_not_pick"), NOW, DEFAULT_CONFIG)
    assert decision.action == SCHEDULE and decision.schedule is True
    assert decision.bucket == "F3"          # dte 20


@pytest.mark.parametrize("dte, bucket", [(2, "F5"), (1, "M0"), (0, "M0"), (-1, "E0"), (-2, "F6")])
def test_expiry_day_and_the_day_after_land_in_their_own_bucket(dte, bucket):
    """E0 covers dte 0..-1. dte 1/0 still get the mandatory override first."""
    decision = decide(lead(red=(TODAY + timedelta(days=dte)).isoformat(),
                           last_interaction_time="2026-08-26 10:00:00"),
                      NOW, DEFAULT_CONFIG)
    assert decision.bucket == bucket


def test_e0_takes_its_own_disposition_allow_list():
    """The per-bucket allow-list reaches the new bucket like any other."""
    config = red_config_from_body({"bucket_dispositions": {"E0": ["voicemail"]}})
    blocked = decide(lead(stage="did_not_pick", red=(TODAY - timedelta(days=1)).isoformat()),
                     NOW, config)
    assert blocked.schedule is False and blocked.bucket == "E0"
    allowed = decide(lead(stage="voicemail", red=(TODAY - timedelta(days=1)).isoformat()),
                     NOW, config)
    assert allowed.schedule is True and allowed.bucket == "E0"


def test_mandatory_day_overrides_the_gate_for_a_connected_lead():
    """RED-1 forces a call even for a warm lead that is otherwise manual-only."""
    warm = lead(stage="positive_followup", red=(TODAY + timedelta(days=1)).isoformat())
    decision = decide(warm, NOW, DEFAULT_CONFIG)
    assert decision.schedule is True
    assert decision.bucket == "M0"
    assert decision.trigger == "mandatory"


# The six connected dispositions from the client's redial-logic table. Their
# "when" column (appointment+1, CMRL+2, branch+2/visit+1, premium+1, link+1,
# followup+5) sets a callback DATE, but the client does not want the AI dialling
# on it: "for this you dont make any calls, only do calls for this on t0 and t-1".
# So the date is computed and shown, and the only automated call these leads get
# is the RED-1 / RED mandatory pair.
CONNECTED_SIX = [
    "lead_appointment_fixed", "lead_cmrl_interested", "lead_directed_to_branch",
    "share_premium_quotation", "lead_link_sent_online", "lead_positive_followup",
]


@pytest.mark.parametrize("stage", CONNECTED_SIX)
def test_a_connected_disposition_is_never_auto_dialled_on_its_callback_date(stage):
    """Even standing exactly on the callback date, the AI does not dial."""
    # dte 9 keeps this clear of the mandatory days and of E0/F6, so the only
    # thing that could schedule it is the disposition callback itself.
    warm = lead(stage=stage, red=(TODAY + timedelta(days=9)).isoformat(),
                last_interaction_time=f"{TODAY.isoformat()} 10:00:00")
    decision = decide(warm, NOW, DEFAULT_CONFIG)
    assert decision.schedule is False
    assert decision.action == SKIP_MANUAL_ONLY
    assert decision.bucket == "D0", "still counted, still dialable by hand"


@pytest.mark.parametrize("stage", CONNECTED_SIX)
@pytest.mark.parametrize("dte", [1, 0])
def test_the_connected_six_are_called_on_t0_and_t_minus_1(stage, dte):
    """t-1 and t0 are the two days the client wants these dialled, and only those."""
    warm = lead(stage=stage, red=(TODAY + timedelta(days=dte)).isoformat(),
                last_interaction_time=f"{TODAY.isoformat()} 10:00:00")
    decision = decide(warm, NOW, DEFAULT_CONFIG)
    assert decision.schedule is True
    assert decision.bucket == "M0" and decision.trigger == "mandatory"


def test_auto_dispositions_can_re_enable_callbacks():
    config = red_config_from_body({"auto_dispositions": ["did_not_pick", "positive_followup"]})
    decision = decide(lead(stage="positive_followup", red=(TODAY).isoformat(),
                           last_interaction_time="2026-08-27 10:00:00"), NOW, config)
    assert decision.action != SKIP_MANUAL_ONLY


# ---------------------------------------------------------------------------
# The client's calling schedule, transcribed
# ---------------------------------------------------------------------------

# Their table, verbatim, in THEIR sign convention: negative = days before RED
# (fixed by their own line "calls needs to be initiated on RED - 1 and RED
# date"). Kept in client form so this reads against the source document rather
# than against our translation of it -- the negation is what is under test.
CLIENT_SCHEDULE = [
    (-45, -32, "2 Calls/Week"), (-31, -24, "2 Calls/Week"),
    (-23, -16, "3 Calls/Week"), (-15, -8, "3 Calls/week"),
    (-7, 0, "16 ( 2 calls/day )"), (1, 3, "6 ( 2 calls/day )"),
]


@pytest.mark.parametrize("c_from, c_to, calls", CLIENT_SCHEDULE)
def test_every_row_of_the_client_schedule_is_configured(c_from, c_to, calls):
    """Each client row, negated into dte, must be covered at the stated rate."""
    from engine.red_engine import find_window

    for c_day in range(c_from, c_to + 1):
        window = find_window(-c_day)        # their sign -> ours
        assert window is not None, f"client day {c_day} (dte {-c_day}) has no window"
        if "day" in calls:
            assert window.calls_per_day == 2, f"client day {c_day}: {window.bucket}"
        else:
            assert window.calls_per_week == int(calls.split()[0]), \
                f"client day {c_day}: {window.bucket} is {window.calls_per_week}/week"


def test_the_schedule_stops_three_days_past_red():
    """No window past RED+3 -- the client's table ends there, so we do too.

    This is the other half of the sign fix: read in OUR convention the second
    priority band would run to RED+7, and the ~5k live leads at RED+4..RED+7
    would be ranked top-priority with no window to dial them from.
    """
    from engine.red_engine import find_window

    assert find_window(-3) is not None and find_window(-3).bucket == "F6"
    for c_day in range(4, 9):
        assert find_window(-c_day) is None, f"RED+{c_day} should be out of schedule"


def test_the_api_default_config_matches_the_engine_table():
    """The shipped config and the engine fallback are the same schedule.

    They are two hand-written copies of one table. When F1/F4 were corrected to
    the client's 2 and 3 per week, only one copy was updated for a while and the
    other silently served the old rates to every campaign that never PUT a config.
    """
    from api.db import DEFAULT_CONFIG
    from engine.red_engine import DEFAULT_FREQUENCY_TABLE

    shipped = {row["bucket"]: row for row in DEFAULT_CONFIG["frequency_table"]}
    assert set(shipped) == {w.bucket for w in DEFAULT_FREQUENCY_TABLE}
    for window in DEFAULT_FREQUENCY_TABLE:
        row = shipped[window.bucket]
        assert (row["from_dte"], row["to_dte"]) == (window.from_dte, window.to_dte)
        assert row["calls_per_week"] == (window.calls_per_week or 0), window.bucket
        assert row["calls_per_day"] == (window.calls_per_day or 0), window.bucket


def test_the_red_bands_are_the_two_intensive_rows_of_that_table():
    """Bands 0/1 are the client's "1 to 3" and "-7 to 0", negated into dte."""
    from engine.dispatcher import DEFAULT_RED_PRIORITY, red_rank

    # Just-lapsed outranks the run-up: the client named "1 to 3" first.
    for c_day in (1, 2, 3):
        assert red_rank(-c_day, DEFAULT_RED_PRIORITY) == 0
    for c_day in range(-7, 1):
        assert red_rank(-c_day, DEFAULT_RED_PRIORITY) == 1
    # Everything outside RED-7..RED+3 falls to the catch-all.
    for dte in (8, 20, 45, -4, -8):
        assert red_rank(dte, DEFAULT_RED_PRIORITY) == len(DEFAULT_RED_PRIORITY)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _pair(bucket, uuid, **over):
    return (lead(lead_uuid=uuid, **over),
            Decision(action=SCHEDULE, reason="", schedule=True, bucket=bucket,
                     bucket_label=bucket, dte=0, disposition_class="dnp"))


WIDE = DispatchConfig(start_min=9 * 60, end_min=19 * 60, shift_from_last_hours=2.0,
                      same_day_gap_hours=3.0, max_per_minute=12, max_per_run=0)


def test_priority_ordering_sheds_the_low_priority_leads_first():
    pairs = [_pair("F1", "f1"), _pair("F5", "f5"), _pair("F6", "f6")]
    result = dispatch(pairs, TODAY, DEFAULT_CONFIG, DispatchConfig(**{**WIDE.__dict__, "max_per_run": 2}))
    kept = {s.decision.bucket for s in result.slots}
    assert "F1" not in kept                      # F1 is furthest from expiry: shed first
    assert {"F5", "F6"} <= kept
    assert result.dropped == 1


def test_f5_is_placed_before_f1():
    pairs = [_pair("F1", "f1"), _pair("F5", "f5")]
    result = dispatch(pairs, TODAY, DEFAULT_CONFIG, WIDE)
    first = min(result.slots, key=lambda s: s.minute)
    assert first.decision.bucket == "F5"
    assert first.priority < max(s.priority for s in result.slots)


def test_f5_gets_one_slot_per_wave_not_two():
    """The second F5 call of the day is not booked while the first is unmade.

    `dispatch` used to emit slot 1 and slot 2 together, gap apart, deciding the
    afternoon from a disposition that was still the PREVIOUS day's. Worse, the
    pre-booked slot 2 landed on Formi's clock, so `queued_today` made the
    afternoon wave skip the very leads it existed to reconsider. The second call
    is now that wave's own slot 1, judged on the morning's real outcome.
    """
    result = dispatch([_pair("F5", "f5")], TODAY, DEFAULT_CONFIG, WIDE)
    assert [s.slot_no for s in result.slots] == [1]
    assert all(WIDE.start_min <= s.minute <= WIDE.end_min for s in result.slots)


def test_f1_gets_one_slot_only():
    result = dispatch([_pair("F1", "f1")], TODAY, DEFAULT_CONFIG, WIDE)
    assert len(result.slots) == 1


def test_time_rotation_shifts_the_hour():
    """Dialled yesterday at 09:00, shift 2h -> 11:00 today. Not 09:00."""
    yesterday = _pair("F1", "rot", last_interaction_time="2026-08-27 09:00:00")
    result = dispatch([yesterday], TODAY, DEFAULT_CONFIG, WIDE)
    assert result.slots[0].minute == 11 * 60
    assert result.slots[0].scheduled_time == "2026-08-28T11:00:00"


def test_rotation_wraps_back_into_the_window():
    late = _pair("F1", "wrap", last_interaction_time="2026-08-27 18:30:00")
    result = dispatch([late], TODAY, DEFAULT_CONFIG, WIDE)
    minute = result.slots[0].minute
    assert WIDE.start_min <= minute <= WIDE.end_min
    assert minute == 10 * 60 + 30               # 20:30, wrapped modulo the 10h span


def test_leads_with_no_history_are_spread_across_the_window():
    pairs = [_pair("F1", f"n{i}") for i in range(11)]
    result = dispatch(pairs, TODAY, DEFAULT_CONFIG, WIDE)
    minutes = sorted(s.minute for s in result.slots)
    assert minutes[0] == WIDE.start_min and minutes[-1] == WIDE.end_min
    assert len(set(minutes)) == 11


def test_stagger_never_exceeds_max_per_minute():
    # 60 leads that all *want* the same minute.
    pairs = [_pair("F5", f"s{i}", last_interaction_time="2026-08-27 09:00:00")
             for i in range(60)]
    dcfg = DispatchConfig(**{**WIDE.__dict__, "max_per_minute": 4})
    result = dispatch(pairs, TODAY, DEFAULT_CONFIG, dcfg)
    load = Counter(s.minute for s in result.slots)
    assert max(load.values()) <= 4
    assert len(result.slots) == 60              # one slot each, all placed


def test_no_slot_lands_outside_the_dial_window():
    pairs = [_pair("F5", f"w{i}", last_interaction_time="2026-08-27 17:45:00")
             for i in range(40)]
    result = dispatch(pairs, TODAY, DEFAULT_CONFIG, WIDE)
    assert all(WIDE.start_min <= s.minute <= WIDE.end_min for s in result.slots)


# ---------------------------------------------------------------------------
# Dial window
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("start,end", [("08:00", "19:00"), ("09:00", "20:30"),
                                       ("19:00", "09:00"), ("12:00", "12:00")])
def test_dial_window_rejects_out_of_range(start, end):
    with pytest.raises(ValueError):
        validate_dial_window(start, end)


def test_dial_window_accepts_the_edges():
    """09:00-20:00 IST, the hours the client dials in."""
    assert validate_dial_window("09:00", "20:00") == (540, 1200)


# ---------------------------------------------------------------------------
# Manual selection
# ---------------------------------------------------------------------------

def test_manual_never_returns_an_excluded_lead():
    leads = [lead(lead_uuid="a", stage="do_not_call"), lead(lead_uuid="b", stage="wrong_number"),
             lead(lead_uuid="c", stage="positive_followup")]
    pairs = manual_pairs(leads, NOW, DEFAULT_CONFIG,
                         dispositions=["do_not_call", "wrong_number", "positive_followup"])
    assert [p[0]["lead_uuid"] for p in pairs] == ["c"]


@pytest.mark.parametrize("text,want", [
    ("10th October", date(2026, 10, 10)), ("1st October", date(2026, 10, 1)),
    ("21st Oct", date(2026, 10, 21)), ("3rd Oct 2026", date(2026, 10, 3)),
    ("2nd September", date(2026, 9, 2)),
    ("Friday, October 10, 2026", date(2026, 10, 10)),
    ("Fri, 10 October 2026", date(2026, 10, 10)),
    ("Monday, 15-Sep-2026", date(2026, 9, 15)),
    ("October 10, 2026", date(2026, 10, 10)), ("Oct 10", date(2026, 10, 10)),
    ("10-October-2026", date(2026, 10, 10)), ("tenth October", date(2026, 10, 10)),
    # Nothing above may be reached by loosening what the parser already rejects.
    ("garbage", None), ("someday october", None), ("31-02-2026", None),
])
def test_red_written_by_hand_still_lands_on_the_day_it_says(text, want):
    """A RED a person typed is a RED, not a NULL.

    Every shape here yielded None before the weekday, the ordinal suffix and the
    commas were stripped, and a lead with no RED falls outside every band and is
    never dialled — the same silent drop 'eleventh september' caused on 5 Sep
    2026, which took campaigns 1740/1744/1746 out of the console entirely.

    Verified in lockstep against `metabase_source.red_parse_expression` on the
    live warehouse: 40 shapes, 40 identical answers. The SQL cannot run here, so
    this half is what CI keeps honest.
    """
    from engine.red_engine import parse_red
    assert parse_red(text, today=date(2026, 9, 9)) == want


def test_a_named_month_never_reaches_to_date():
    """`Mon` consumes exactly three characters, so TO_DATE aborts the query.

    'October-10-2026' leaves 'ober-10-2026' behind and Postgres raises
    `invalid value "ob" for "DD"` — not a NULL for that one lead, but a failed
    sync for every campaign in the batch. Verified against the real warehouse.
    """
    from engine.metabase_source import red_parse_expression
    assert "TO_DATE" not in red_parse_expression()


# ---------------------------------------------------------------------------
# Seed fixture
# ---------------------------------------------------------------------------

def test_every_seeded_red_parses_back_to_the_day_it_was_written():
    """The seed's RED strings must be unambiguous to `parse_red`.

    A bare `yyyy-mm-dd` is not: parse_red also accepts yyyy-dd-mm and its
    renewal-month tie-break read `2026-10-08` as 10 August, silently moving a
    lead ~40 days out of the bucket the seed intended.
    """
    import random
    from datetime import date, timedelta

    from engine.red_engine import parse_red
    from engine.seed import _fmt_red

    rnd = random.Random(1)
    start = date(2026, 1, 1)
    for offset in range(400):
        day = start + timedelta(days=offset)
        for _ in range(8):
            text = _fmt_red(rnd, day)
            assert parse_red(text) == day, f"{text!r} parsed as {parse_red(text)}, wanted {day}"


# ---------------------------------------------------------------------------
# engine.sync — the pure mapping bits, no warehouse needed
# ---------------------------------------------------------------------------

def test_warehouse_status_maps_onto_enabled_and_paused():
    """`public.campaigns.status` has three states; the console has two flags."""
    from engine.sync import campaign_status_flags

    assert campaign_status_flags("active") == (1, 0)
    assert campaign_status_flags("paused") == (1, 1)
    assert campaign_status_flags("killed") == (0, 0)
    # Unknown/absent must never read as "stopped": show it running, under its
    # real name, rather than silently hiding a live campaign.
    assert campaign_status_flags(None) == (1, 0)
    assert campaign_status_flags(" ACTIVE ") == (1, 0)


def test_synced_red_keeps_the_warehouses_own_reading():
    """An ambiguous RED must not be re-opened to a coin flip on the way in.

    The warehouse resolved `4/8/2026` with the convention it proved campaign
    1618 uses. Storing the raw text would let `parse_red` read it as 8 April and
    move the lead a whole bucket, so the resolved date is what is written -- in
    the ISO *timestamp* shape that takes parse_red's unambiguous fast path.
    """
    from datetime import date

    from engine.red_engine import parse_red
    from engine.sync import _red

    resolved = _red({"red_raw": "4/8/2026", "red": "2026-08-04T00:00:00+05:30"})
    assert parse_red(resolved) == date(2026, 8, 4)
    # Unparseable RED keeps its raw text, so the NO_EXPIRY skip stays visible.
    assert _red({"red_raw": "25-Aug", "red": "25-Aug"}) == "25-Aug"
    assert _red({"red_raw": "", "red": None}) == ""


def test_red_written_in_words_parses():
    """'eleventh september' is a RED. The 05-Sep-2026 upload wrote 4,514 of them.

    Every one parsed to NULL before this, which zeroed `leads_with_red` on
    campaigns 1740/1744/1746 and so dropped them out of the console's list
    without a word -- 2,351 real dials on 1744 alone, invisible.
    """
    from datetime import date

    from engine.red_engine import parse_red

    today = date(2026, 9, 6)
    assert parse_red("eleventh september", today=today) == date(2026, 9, 11)
    assert parse_red("Tenth September", today=today) == date(2026, 9, 10)
    # The compound days, in each spelling the uploads have produced.
    for text in ("twenty-first september", "twenty first september", "twentyfirst september"):
        assert parse_red(text, today=today) == date(2026, 9, 21)
    assert parse_red("thirty-first october", today=today) == date(2026, 10, 31)
    # Year inferred, not assumed: read in September, 'second january' is next year.
    assert parse_red("second january", today=today) == date(2027, 1, 2)
    # Impossible and unrecognised stay None rather than becoming a wrong date.
    assert parse_red("thirty-first february", today=today) is None
    assert parse_red("eleventh smarch", today=today) is None
    assert parse_red("not known", today=today) is None


# ---------------------------------------------------------------------------
# The Formi credential
# ---------------------------------------------------------------------------
# The app documented FORMI_TOKEN; every other Chola tool writes FORMI_API_KEY
# into the same .env. A working credential file therefore produced "not set" on
# every live dial and every bulk stage commit. Both names must resolve.

def test_formi_token_accepts_either_env_name(monkeypatch):
    from api.db import formi_token

    monkeypatch.delenv("FORMI_TOKEN", raising=False)
    monkeypatch.delenv("FORMI_API_KEY", raising=False)
    assert formi_token() is None

    monkeypatch.setenv("FORMI_API_KEY", "key-from-the-shared-env")
    assert formi_token() == "key-from-the-shared-env"

    # Explicit beats inherited, so exporting FORMI_TOKEN still wins.
    monkeypatch.setenv("FORMI_TOKEN", "explicit")
    assert formi_token() == "explicit"


def test_formi_token_ignores_a_blank_value(monkeypatch):
    """An empty export is how a half-filled .env fails; it must not count."""
    from api.db import formi_token

    monkeypatch.setenv("FORMI_TOKEN", "   ")
    monkeypatch.setenv("FORMI_API_KEY", "real")
    assert formi_token() == "real"


# ---------------------------------------------------------------------------
# Which campaigns the sync is allowed to offer
# ---------------------------------------------------------------------------
# These names are no longer filtered out of the sync -- every campaign with leads
# and a RED is offered, test ones included. The predicate now only decides which
# ones `sync` prints a WARNING about, so what these cases pin is the wording of
# that warning, not what reaches the console.

@pytest.mark.parametrize("name", [
    "test", "test 1", "test 26", "link test", "send_payment_link-test",
    "Test campagin", "24_July_Dev_Campaign", "Dev_Test_06-08-2026",
    "audit_redial (killed)", "paymnet link (link plumbing)",
])
def test_non_production_campaigns_are_flagged(name):
    from engine.sync import is_production_campaign
    assert not is_production_campaign(name)


@pytest.mark.parametrize("name", [
    "0308Redial -CV", "0608-PV_updated", "10-08-Redial-missed", "RED-22-07",
    "Redial_missed_2608",
    # Substring matching would kill this one: it contains "test" inside
    # "Contest". Splitting into words is the whole reason the filter is safe to
    # apply to names nobody has reviewed.
    "Contest_Aug",
])
def test_real_campaigns_are_not_flagged(name):
    from engine.sync import is_production_campaign
    assert is_production_campaign(name)


def test_a_missing_name_is_not_treated_as_a_test_campaign():
    """An unnamed campaign is a data gap, not permission to drop real leads."""
    from engine.sync import is_production_campaign
    assert is_production_campaign(None)
    assert is_production_campaign("")


# ---------------------------------------------------------------------------
# What counts as an applied stage write
# ---------------------------------------------------------------------------
# Formi's /bulk-update-stage answers a partially applied batch with HTTP 200 and
# the real numbers in the body: a lead belonging to another agent or outlet is
# skipped into `errors`. Counting the 200 as "all 200 applied" is the same
# silent success the seed-source bug produced, one layer down.

class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_a_partially_applied_batch_is_not_counted_as_fully_applied():
    from engine.stage_ops import batch_counts
    body = {"success": True, "payload": {"total_requested": 200,
                                         "successful_updates": 12,
                                         "failed_updates": 188}}
    assert batch_counts(_Resp(200, body), 200) == (12, 188)


def test_a_fully_applied_batch_counts_every_lead():
    from engine.stage_ops import batch_counts
    body = {"success": True, "payload": {"successful_updates": 200, "failed_updates": 0}}
    assert batch_counts(_Resp(200, body), 200) == (200, 0)


@pytest.mark.parametrize("response", [
    _Resp(400, {"success": False, "message": "Invalid stage"}),
    _Resp(404, {"success": False, "message": "Agent not found"}),
    _Resp(200, None),                              # 200, unreadable body
    _Resp(200, {"success": True}),                 # 200, no payload at all
    _Resp(200, {"payload": {"failed_updates": 3}}),  # 200, no count we can use
])
def test_anything_we_cannot_read_as_applied_is_failed(response):
    """A 200 we cannot parse is not evidence of a write. Never assume success."""
    from engine.stage_ops import batch_counts
    assert batch_counts(response, 200) == (0, 200)


# ---------------------------------------------------------------------------
# Formi's five-minute notice
# ---------------------------------------------------------------------------
# /schedule answers 400 "Scheduled time must be at least 5 minutes from now".
# Planning or posting inside that window is a guaranteed rejection, so the
# console's floor has to be Formi's floor, not "later than now".

@pytest.mark.parametrize("now, expected", [
    ("2026-09-05T10:00:00", "2026-09-05T10:05:00"),   # exact minute, no rounding
    ("2026-09-05T10:00:01", "2026-09-05T10:06:00"),   # any second rounds *up*
    ("2026-09-05T10:26:40", "2026-09-05T10:32:00"),
    ("2026-09-05T23:58:00", "2026-09-06T00:03:00"),   # crosses midnight
])
def test_the_earliest_dialable_minute_clears_formis_floor(now, expected):
    from datetime import datetime, timedelta
    from api.routes_core import FORMI_LEAD_MINUTES, _earliest_dialable

    start = datetime.fromisoformat(now)
    first = _earliest_dialable(start)
    assert first.isoformat() == expected
    # The property that matters: Formi rejects `scheduled < now + 5min`, and it
    # re-checks against its own clock when the POST lands, so never round down.
    assert first >= start + timedelta(minutes=FORMI_LEAD_MINUTES)
    assert first.second == 0


def test_a_rejected_stage_write_stops_and_carries_formis_reason():
    """`policy_expired` may not be in the agent's funnel config at all.

    Every chunk after the first would be rejected identically, so grinding
    through them to report "0 applied" hides the one sentence that explains it.
    """
    from engine.stage_ops import _why
    reason = "Invalid stage. Valid stages are: renewed, did_not_pick"
    assert _why(_Resp(400, {"success": False, "message": reason})) == reason
    assert _why(_Resp(404, None)) == "HTTP 404"


# ---------------------------------------------------------------------------
# RED−1 and RED override the disposition
# ---------------------------------------------------------------------------
# The client's rule, verbatim: "For all cases excluding the renewed and DND
# cases - calls needs to be initiated on RED - 1 and RED date - irrespective of
# the disposition status." The exclusion ladder used to run first, so ~400
# not_interested/lost leads and 2.7k in human_review silently lost the two days
# that matter most.

@pytest.mark.parametrize("dte", [1, 0])
@pytest.mark.parametrize("stage", [
    "not_interested", "lost", "firm_decision_to_discontinue",   # were EXCLUDED
    "ai_qualified_lead", "lead_transferred_to_sales",
    "human_review", "agent_number", "chola_field_executive",     # were HOLD
    "requested_human_agent_connect", "alternate_contact_given",
])
def test_a_mandatory_day_overrides_an_exclusion_or_a_hold(stage, dte):
    from engine.red_engine import MANDATORY_LABEL
    decision = decide(lead(stage=stage, red=(TODAY + timedelta(days=dte)).isoformat()),
                      NOW, DEFAULT_CONFIG)
    assert decision.action == SCHEDULE, f"{stage} at dte={dte}: {decision.reason}"
    assert decision.bucket == "M0" and decision.bucket_label == MANDATORY_LABEL


@pytest.mark.parametrize("dte", [1, 0])
@pytest.mark.parametrize("stage", [
    "do_not_call", "dnc", "dnd",                    # consent — regulatory
    "renewed", "already_paid_to_chola",             # already renewed
    "wrong_number", "number_not_working", "invalid_number",   # not this customer
])
def test_consent_renewal_and_bad_numbers_survive_a_mandatory_day(stage, dte):
    """The two exceptions the client named, plus numbers that reach a stranger."""
    decision = decide(lead(stage=stage, red=(TODAY + timedelta(days=dte)).isoformat()),
                      NOW, DEFAULT_CONFIG)
    assert decision.action != SCHEDULE, f"{stage} at dte={dte} would be dialled"


@pytest.mark.parametrize("stage", ["not_interested", "human_review"])
def test_the_override_lasts_exactly_two_days(stage):
    """RED−2 and RED+1 are ordinary days: the exclusion holds again."""
    for dte in (2, -1):
        decision = decide(lead(stage=stage, red=(TODAY + timedelta(days=dte)).isoformat()),
                          NOW, DEFAULT_CONFIG)
        assert decision.action != SCHEDULE, f"{stage} dialled at dte={dte}"


def test_an_operator_added_exclusion_is_not_undone_by_a_mandatory_day():
    """`extra_exclusions` means "stop calling these" — including on RED−1."""
    config = red_config_from_body({"extra_exclusions": ["positive_followup"]})
    decision = decide(lead(stage="positive_followup", red=(TODAY + timedelta(days=1)).isoformat()),
                      NOW, config)
    assert decision.action != SCHEDULE, decision.reason


# ---------------------------------------------------------------------------
# The second call of the day
#
# The operator's rule: "monitor for red 0-7 and -1 to -3 -- if they have not
# picked or hung up or call duration is less than 15s, call them again the same
# day", and then, precisely: ">= logic should only be used when disposition is
# not there, if not based on disposition only".
#
# So there are two arms and they never overlap. A disposition is an answer and is
# taken at its word. Duration is consulted ONLY where there is no slug to read --
# 13,149 dials of the 62,423 in the seven days to 12 Sep 2026 for outlet 1497.
# The numbers below are that week's, because the rule is only worth what the data
# says it separates. See `wants_second_call`.
# ---------------------------------------------------------------------------

SHIPPED = red_config_from_body(API_DEFAULTS)
AFTERNOON = datetime(2026, 8, 28, 14, 0)


@pytest.mark.parametrize("stage, duration, again, why", [
    # --- arm 1: a disposition is present, so it alone decides ---------------
    ("did_not_pick", None, True, "28,186 dnp dials, on the re-dial list"),
    ("telephony_failed", None, True, "3,015 dials, on the list"),
    ("voicemail_ivr", 30, True, "a recording answering for 30s is still no contact"),
    # The duration is deliberately ignored on these two. `hung_up` is on the
    # list, so a 40-second hang-up is chased exactly like a 6-second one: to stop
    # chasing it the operator takes `hung_up` off the list, which is the knob.
    ("hung_up", 6, True, "4,582 of 8,912 hung_up dials ran under 15s"),
    ("hung_up", 40, True, "and the other 4,330 ran longer -- the slug still decides"),
    ("hung_up", 600, True, "no length of call overrides a slug on the list"),
    # Not on the list, so no duration saves them either. Symmetry is the point:
    # the slug is the whole answer in both directions.
    ("wrong_number", 3, False, "a wrong number stays wrong however brief"),
    ("contacted", 2, False, "a terminal outcome is not chased on a technicality"),
    ("redial_required", 600, True, "they asked to be rung back; ring them back"),
    ("follow_up_required", 4, False, "somebody was reached and agreed a follow-up"),
    # --- arm 2: no disposition at all, so duration is the only evidence -----
    ("", None, True, "8,545 dnp rows carry no disposition; null = never connected"),
    ("", 4, True, "4,515 of 4,604 completed/(none) dials ran under 15s"),
    ("", 120, False, "1,911 complete/(none) dials were real conversations"),
    ("", 15, False, "the threshold itself counts as reached"),
])
def test_who_has_earned_a_second_call_today(stage, duration, again, why):
    assert SHIPPED.wants_second_call(stage, duration) is again, why


def test_the_duration_fallback_never_overrules_a_disposition():
    """The arms do not overlap, whatever `short_call_seconds` is set to.

    Pinned separately from the table because it is the whole shape of the rule:
    changing the threshold must move only the undispositioned calls.
    """
    for seconds in (0, 15, 900):
        cfg = red_config_from_body({**API_DEFAULTS, "short_call_seconds": seconds})
        assert cfg.wants_second_call("hung_up", 600) is True
        assert cfg.wants_second_call("wrong_number", 1) is False


def test_an_unreadable_duration_is_treated_as_no_call_rather_than_a_long_one():
    """One extra dial costs less than dropping a lead days from expiry."""
    assert SHIPPED.wants_second_call("", "not-a-number") is True


def test_the_duration_fallback_can_be_switched_off():
    """0 means an undispositioned call is always chased, however long it ran."""
    off = red_config_from_body({**API_DEFAULTS, "short_call_seconds": 0})
    assert off.wants_second_call("", 600) is True


def _afternoon(**over):
    """A lead in F5 that was already dialled once this morning."""
    return lead(**{"red": (TODAY + timedelta(days=3)).isoformat(), "calls_today": 1,
                   "last_interaction_time": "2026-08-28 09:30:00", **over})


def test_the_first_call_of_the_day_is_never_judged_by_a_previous_outcome():
    """`calls_today == 0`, so there is no call today for the gate to read.

    Without this the morning wave would inherit yesterday's 40-second hang-up as
    a reason not to dial at all, which is the opposite of the rule -- that lead
    is exactly who the critical window exists for.
    """
    decision = decide(lead(red=(TODAY + timedelta(days=3)).isoformat(),
                           stage="hung_up", calls_today=0,
                           last_call_duration_sec=40), NOW, SHIPPED)
    assert decision.action == SCHEDULE and decision.bucket == "F5"


def test_a_short_morning_call_earns_the_afternoon_one():
    decision = decide(_afternoon(stage="hung_up", last_call_duration_sec=6),
                      AFTERNOON, SHIPPED)
    assert decision.action == SCHEDULE and decision.bucket == "F5"


def test_a_long_one_on_the_same_disposition_earns_it_too():
    """`hung_up` is on the list, so its duration is not consulted at all.

    The pair above and here is the operator's "if not, based on disposition only"
    in one line: same slug, 6s and 40s, same answer.
    """
    decision = decide(_afternoon(stage="hung_up", last_call_duration_sec=40),
                      AFTERNOON, SHIPPED)
    assert decision.action == SCHEDULE and decision.bucket == "F5"


def test_a_morning_conversation_with_no_disposition_does_not():
    """The fallback arm: no slug to read, so 120 seconds is the only evidence."""
    decision = decide(_afternoon(stage="", last_call_duration_sec=120),
                      AFTERNOON, SHIPPED)
    assert decision.action == SKIP_REACHED
    assert decision.schedule is False
    assert "120" in decision.reason                 # the operator can see why


def test_a_disposition_off_the_list_is_refused_whatever_the_duration():
    """`follow_up_required` reaches this gate and is turned away by it.

    Of the 52 known slugs only three get this far and are refused by their slug
    alone: this one, `potentially_interested`, and -- until the operator asked
    for it on 12 Sep 2026 -- `redial_required`. All three mean somebody was
    actually reached, which is why a 4-second duration does not rescue them.
    """
    decision = decide(_afternoon(stage="follow_up_required", last_call_duration_sec=2),
                      AFTERNOON, SHIPPED)
    assert decision.action == SKIP_REACHED


def test_a_morning_that_nobody_answered_earns_it_with_no_duration_at_all():
    decision = decide(_afternoon(stage="did_not_pick", last_call_duration_sec=None),
                      AFTERNOON, SHIPPED)
    assert decision.action == SCHEDULE


def test_the_afternoon_call_still_has_to_wait_out_the_gap():
    """The re-dial is spaced, not immediate: `same_day_gap_hours` still rules.

    10:15 against a 09:30 call is 45 minutes. Whatever the disposition says, a
    customer is not rung twice inside the hour.
    """
    decision = decide(_afternoon(stage="did_not_pick"),
                      datetime(2026, 8, 28, 10, 15), SHIPPED)
    assert decision.action == SKIP_CADENCE


def test_a_third_call_is_refused_by_the_daily_cap():
    """The gate says "worth calling"; the cap says "allowed to". Both apply."""
    decision = decide(_afternoon(stage="did_not_pick", calls_today=2),
                      AFTERNOON, SHIPPED)
    assert decision.action == SKIP_DAILY_CAP
