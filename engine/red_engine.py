"""RED-date redial engine — pure decision logic, no I/O.

Implements `docs/RED_DATE_REDIAL_LOGIC_DESIGN.md` and
`docs/RED_DATE_REDIAL_REQUIREMENTS.md` (platform-workers repo) as a set of
side-effect-free functions so the same rules can drive:

  * the console's bucket preview (counts only, no writes),
  * the dry-run plan CSV,
  * the commit path that POSTs schedules to Formi.

--------------------------------------------------------------------------
SIGN CONVENTION  (this is a deliberate correction to the design document)
--------------------------------------------------------------------------
The design doc's frequency table is written in "RED−45 … RED+3" prose, but its
pseudocode computes ``days_to_red = (red_date - today).days`` and then filters
``days_to_red < -45 or days_to_red > 3``. Those two disagree: when RED is 45
days in the future ``(red - today).days`` is **+45**, not −45. Taken literally
the doc's guard admits only leads whose RED has already passed, and
``mandatory_days = [-1, 0]`` fires the day *after* expiry rather than RED−1.

This module uses one unambiguous quantity throughout:

    dte = (red_date - today).days        # "days to expiry"

    dte = +45  ->  RED is 45 days away   (doc prose "RED−45")
    dte =  +1  ->  RED is tomorrow       (doc prose "RED−1")
    dte =   0  ->  RED is today          (doc prose "RED day 0")
    dte =  -3  ->  RED was 3 days ago    (doc prose "RED+3", grace period)

That matches `schedule_redials.py`'s existing `tte` field, so audit CSVs stay
comparable across the old and new engine.

--------------------------------------------------------------------------
DECISION PRECEDENCE  (requirements section 8, strict)
--------------------------------------------------------------------------
    1. EXCLUSION          -> never call, ever.
    2. MANDATORY DAY      -> dte in {1, 0}: force a call regardless of cadence.
    3. DISPOSITION CALLBACK -> a connected call asked for a specific date.
    4. FREQUENCY TABLE    -> the RED-relative exponential ramp.

Anything that falls through all four is skipped with a machine-readable reason.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "RedWindow",
    "DispositionRule",
    "RedConfig",
    "Decision",
    "DEFAULT_FREQUENCY_TABLE",
    "DEFAULT_DISPOSITION_RULES",
    "DEFAULT_CONFIG",
    "EXCLUDED",
    "HOLD",
    "REASSIGN",
    "CALLBACK",
    "DNP",
    "FRESH",
    "UNKNOWN",
    "ACTIONS",
    "parse_red",
    "parse_timestamp",
    "days_to_expiry",
    "find_window",
    "classify_disposition",
    "disposition_callback_date",
    "weekly_slots_for",
    "decide",
    "bucket_summary",
    "config_from_settings",
]


# ---------------------------------------------------------------------------
# Disposition classes
# ---------------------------------------------------------------------------
# A disposition maps to exactly one class. The class decides which branch of
# the precedence ladder the lead can even reach.

EXCLUDED = "excluded"    # permanent removal from the cycle (renewed/DND/terminal)
HOLD = "hold"            # paused pending a human/field outcome; no auto redial
REASSIGN = "reassign"    # needs a different agent (language); no auto redial
CALLBACK = "callback"    # connected, wants a specific follow-up date
DNP = "dnp"              # did not pick / no conversation -> follow RED frequency
FRESH = "fresh"          # never dialled, or a deprecated slug cleared to fresh
UNKNOWN = "unknown"      # slug we have no rule for -> reported, never dialled


# Decision actions surfaced in the plan CSV / bucket preview.
SCHEDULE = "SCHEDULE"
SKIP_EXCLUDED = "STAGE_TERMINAL"
SKIP_HOLD = "STAGE_HOLD"
SKIP_REASSIGN = "STAGE_REASSIGN"
SKIP_UNKNOWN = "STAGE_UNKNOWN"
SKIP_NO_RED = "NO_EXPIRY"
SKIP_OUTSIDE = "OUTSIDE_WINDOW"
SKIP_CADENCE = "CADENCE_WAIT"
SKIP_WEEKLY_BUDGET = "WEEKLY_BUDGET_MET"
SKIP_DAILY_CAP = "DAILY_CAP_MET"
SKIP_MAX_ATTEMPTS = "MAX_ATTEMPTS"
SKIP_CALLBACK_PENDING = "CALLBACK_PENDING"
SKIP_NOT_TODAYS_SLOT = "NOT_TODAYS_SLOT"
SKIP_ALREADY_SCHEDULED = "ALREADY_SCHEDULED_TODAY"
# The lead's disposition class is not in the auto-run's allow-list. It is not
# excluded and not waiting on cadence -- an operator may still dial it by hand
# from the manual screen. Distinct from every other skip so the UI can offer it.
SKIP_MANUAL_ONLY = "MANUAL_ONLY"
# The lead's bucket carries its own disposition allow-list and this disposition
# is not on it. Distinct from MANUAL_ONLY: that one is a property of the
# disposition everywhere, this one is a per-bucket choice the operator made --
# e.g. chase voicemail in the critical window but not 40 days out.
SKIP_BUCKET_DISPOSITION = "BUCKET_DISPOSITION_OFF"

ACTIONS = (
    SCHEDULE, SKIP_EXCLUDED, SKIP_HOLD, SKIP_REASSIGN, SKIP_UNKNOWN,
    SKIP_NO_RED, SKIP_OUTSIDE, SKIP_CADENCE, SKIP_WEEKLY_BUDGET,
    SKIP_DAILY_CAP, SKIP_MAX_ATTEMPTS, SKIP_CALLBACK_PENDING,
    SKIP_NOT_TODAYS_SLOT, SKIP_ALREADY_SCHEDULED, SKIP_MANUAL_ONLY,
    SKIP_BUCKET_DISPOSITION,
)

# Deliberately ASCII. Bucket labels reach Windows consoles (this runs under Task
# Scheduler) and the audit CSV, and a cp1252 stream cannot encode U+2212.
MANDATORY_LABEL = "Mandatory (RED-1 / RED)"
CALLBACK_LABEL = "Disposition callback"


# ---------------------------------------------------------------------------
# Frequency windows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RedWindow:
    """One row of the RED frequency table, expressed in `dte` days.

    `from_dte` is the FAR edge (larger number, further from expiry) and
    `to_dte` is the NEAR edge, so a window reads like a closed interval
    ``to_dte <= dte <= from_dte``. Exactly one of `calls_per_week` /
    `calls_per_day` is meaningful; `calls_per_day` wins when both are set,
    matching the design doc's "calls_per_day overrides calls_per_week".
    """

    bucket: str
    label: str
    from_dte: int
    to_dte: int
    calls_per_week: int = 0
    calls_per_day: int = 0

    def __post_init__(self) -> None:
        if self.from_dte < self.to_dte:
            raise ValueError(
                f"window {self.bucket}: from_dte ({self.from_dte}) must be >= "
                f"to_dte ({self.to_dte}); from_dte is the edge further from RED"
            )
        if self.calls_per_week < 0 or self.calls_per_day < 0:
            raise ValueError(f"window {self.bucket}: call counts cannot be negative")
        if not self.calls_per_week and not self.calls_per_day:
            raise ValueError(f"window {self.bucket}: set calls_per_week or calls_per_day")

    def contains(self, dte: int) -> bool:
        return self.to_dte <= dte <= self.from_dte

    @property
    def intensive(self) -> bool:
        """True for the 2-calls-per-day windows (F5/E0/F6)."""
        return self.calls_per_day > 0

    @property
    def span_days(self) -> int:
        return self.from_dte - self.to_dte + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket, "label": self.label,
            "from_dte": self.from_dte, "to_dte": self.to_dte,
            "calls_per_week": self.calls_per_week, "calls_per_day": self.calls_per_day,
        }


# The design doc's table 3, converted to the dte convention. Intensity roughly
# doubles per window (1 -> 2 -> 3 -> 5 -> 14 calls/week) as RED approaches.
DEFAULT_FREQUENCY_TABLE: tuple[RedWindow, ...] = (
    RedWindow("F1", "Warm-up",         from_dte=45, to_dte=32, calls_per_week=1),
    RedWindow("F2", "Early engagement", from_dte=31, to_dte=24, calls_per_week=2),
    RedWindow("F3", "Building urgency", from_dte=23, to_dte=16, calls_per_week=3),
    RedWindow("F4", "High frequency",   from_dte=15, to_dte=8,  calls_per_week=5),
    RedWindow("F5", "Critical window",  from_dte=7,  to_dte=1,  calls_per_day=2),
    # Expiry day and the day after, split out of F5/F6 because it is the moment
    # the policy actually lapses: same intensity, but its own bucket so it can
    # carry its own disposition allow-list and be counted on its own.
    RedWindow("E0", "Expiry window",    from_dte=0,  to_dte=-1, calls_per_day=2),
    RedWindow("F6", "Grace period",     from_dte=-2, to_dte=-3, calls_per_day=2),
)


# ---------------------------------------------------------------------------
# Disposition rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispositionRule:
    """How one disposition slug behaves after a call.

    For `CALLBACK` dispositions the next date is resolved in the priority order
    of requirements REQ-DISP-002:
        1. customer `callback_date`      (use_callback_date)
        2. `appointment_date` + N        (use_appointment_date / appointment_plus_days)
        3. `last_called_at` + offset     (offset_days)
        4. fall through to the RED frequency table (follow_red_frequency)
    """

    slug: str
    klass: str
    offset_days: Optional[int] = None
    use_callback_date: bool = False
    use_appointment_date: bool = False
    appointment_plus_days: int = 0
    # Lead_Appointment_Fixed wants the appointment day AND the day after.
    extra_offsets: tuple[int, ...] = ()
    follow_red_frequency: bool = False
    note: str = ""


def _rules(*rules: DispositionRule) -> dict[str, DispositionRule]:
    return {r.slug: r for r in rules}


# Slugs are the RAW Formi/warehouse values (lower snake case), matched
# case-insensitively. Where the warehouse carries two spellings for one
# business disposition both are listed.
DEFAULT_DISPOSITION_RULES: dict[str, DispositionRule] = _rules(
    # --- permanent exclusions (requirements 4.5) ---------------------------
    DispositionRule("already_paid_to_chola", EXCLUDED, note="Renewed"),
    # `renewed` is the highest-volume exclusion in production (~2.4k leads) and is
    # mandated by REQ-EXCL-002 "Leads with stage = 'renewed' SHALL be excluded".
    # Leaving it unmapped risked dialling customers who have already paid.
    DispositionRule("renewed", EXCLUDED, note="Renewed — business outcome achieved"),
    DispositionRule("do_not_call", EXCLUDED, note="DND"),
    DispositionRule("dnc", EXCLUDED, note="DND"),
    DispositionRule("dnd", EXCLUDED, note="DND"),
    DispositionRule("lost", EXCLUDED, note="Terminal"),
    DispositionRule("not_interested", EXCLUDED, note="Terminal"),
    DispositionRule("firm_decision_to_discontinue", EXCLUDED, note="Terminal — will not renew"),
    DispositionRule("wrong_number", EXCLUDED, note="Bad data"),
    DispositionRule("number_not_working", EXCLUDED, note="Bad data"),
    DispositionRule("invalid_number", EXCLUDED, note="Bad data"),
    DispositionRule("ai_qualified_lead", EXCLUDED, note="Handed to sales"),
    DispositionRule("lead_transferred_to_sales", EXCLUDED, note="Handed to sales"),

    # --- temporary holds (requirements REQ-EXCL-003) -----------------------
    DispositionRule("agent_number", HOLD, note="Awaiting agent outcome"),
    DispositionRule("chola_field_executive", HOLD, note="Awaiting field visit"),
    DispositionRule("requested_human_agent_connect", HOLD, note="Escalated to a human"),
    # ~2.7k leads sit in human_review. They are mid-QA, so auto-dialling them
    # would act on a disposition that is not final yet.
    DispositionRule("human_review", HOLD, note="Awaiting human review — disposition not final"),
    DispositionRule("alternate_contact_given", HOLD,
                    note="Number on file superseded — needs a data update before dialling"),

    # --- reassign, no auto redial ------------------------------------------
    DispositionRule("other_language", REASSIGN, note="Needs a language-capable agent"),

    # --- connected, callback required (requirements REQ-DISP-001) ----------
    DispositionRule("lead_appointment_fixed", CALLBACK, use_appointment_date=True,
                    appointment_plus_days=0, extra_offsets=(1,), offset_days=1,
                    note="Appointment day and the day after"),
    DispositionRule("lead_cmrl_interested", CALLBACK, offset_days=2),
    DispositionRule("lead_directed_to_branch", CALLBACK, offset_days=2,
                    use_appointment_date=True, appointment_plus_days=1,
                    note="Visit date + 1, else calling date + 2"),
    DispositionRule("directed_to_branch", CALLBACK, offset_days=2,
                    use_appointment_date=True, appointment_plus_days=1),
    DispositionRule("lead_premium_quotation", CALLBACK, offset_days=1, use_callback_date=True,
                    note="Share Premium & Quotation"),
    DispositionRule("lead_premium_quotation_required", CALLBACK, offset_days=1,
                    use_callback_date=True),
    DispositionRule("share_premium_quotation", CALLBACK, offset_days=1, use_callback_date=True,
                    note="Alias of Share Premium & Quotation"),
    DispositionRule("lead_link_sent_online", CALLBACK, offset_days=1),
    DispositionRule("payment_link_sent", CALLBACK, offset_days=1,
                    note="Same follow-up as Lead_Link sent online"),
    DispositionRule("positive_followup", CALLBACK, offset_days=5, use_callback_date=True),
    DispositionRule("lead_positive_followup", CALLBACK, offset_days=5, use_callback_date=True),
    DispositionRule("call_back", CALLBACK, use_callback_date=True, offset_days=1,
                    note="Exact customer-requested date; +1 day only as a fallback"),
    # Payment intent stated but not received. Not a renewal yet, so keep chasing —
    # but honour a stated date when the customer gave one.
    DispositionRule("committed_to_pay", CALLBACK, offset_days=2, use_callback_date=True,
                    note="Promise to pay — not yet renewed"),
    DispositionRule("agreed_to_pay_with_date", CALLBACK, offset_days=1, use_callback_date=True,
                    note="Customer named a payment date"),
    DispositionRule("promise_to_renew", CALLBACK, offset_days=2, use_callback_date=True),

    # --- did-not-pick family: follow the RED frequency table ---------------
    DispositionRule("hung_up", DNP, follow_red_frequency=True),
    DispositionRule("hung_up_no_contact", DNP, follow_red_frequency=True),
    DispositionRule("did_not_pick", DNP, follow_red_frequency=True),
    DispositionRule("unreachable", DNP, follow_red_frequency=True, note="RNR"),
    DispositionRule("rnr", DNP, follow_red_frequency=True, note="Alias of unreachable"),
    DispositionRule("beep_tone_number_busy_not_reachable_switched_off", DNP,
                    follow_red_frequency=True),
    DispositionRule("voicemail", DNP, follow_red_frequency=True),
    DispositionRule("voicemail_ivr", DNP, follow_red_frequency=True),
    DispositionRule("telephony_failed", DNP, follow_red_frequency=True),
    # Deprecated slugs (design doc 4.4): Dialer NC reclassifies as RNR.
    DispositionRule("dialer_nc", DNP, follow_red_frequency=True, note="Reclassified as RNR"),
    # The disposition engine explicitly asked for another attempt.
    DispositionRule("redial_required", DNP, follow_red_frequency=True,
                    note="Disposition engine requested a redial"),
    # Connected, but no committed date. Following the RED ramp is the conservative
    # choice: it keeps the weekly budget and window guards rather than inventing
    # a bespoke offset the requirements never specified.
    DispositionRule("potentially_interested", DNP, follow_red_frequency=True,
                    note="Connected, no committed date — follows the RED ramp"),
    DispositionRule("follow_up_required", DNP, follow_red_frequency=True,
                    note="Connected, no committed date — follows the RED ramp"),

    # --- fresh leads -------------------------------------------------------
    DispositionRule("new", FRESH, follow_red_frequency=True),
    DispositionRule("fresh", FRESH, follow_red_frequency=True),
    DispositionRule("", FRESH, follow_red_frequency=True, note="No disposition yet"),
    # Deprecated slug (design doc 4.4): treated as a fresh lead.
    DispositionRule("not_dialed", FRESH, follow_red_frequency=True, note="Treated as fresh"),
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RedConfig:
    """Everything a run needs, all overridable per saved strategy."""

    frequency_table: tuple[RedWindow, ...] = DEFAULT_FREQUENCY_TABLE
    disposition_rules: dict[str, DispositionRule] = field(
        default_factory=lambda: dict(DEFAULT_DISPOSITION_RULES))

    # dte values that force a call (RED−1 and RED itself).
    mandatory_days: tuple[int, ...] = (1, 0)

    # Hard per-calendar-day ceiling inside the intensive windows.
    calls_per_day_cap: int = 2
    # Minimum hours between two attempts on the same lead on the same day.
    same_day_gap_hours: float = 3.0

    # Minimum hours between attempts in the sparse (calls_per_week) windows.
    # None -> derived from the window as 168/calls_per_week * spread_tolerance.
    sparse_gap_hours: Optional[float] = None
    spread_tolerance: float = 0.85

    # Enforce the rolling 7-day call budget for sparse windows.
    enforce_weekly_budget: bool = True
    # Spread sparse-window leads deterministically across the week so a
    # 1-call/week cohort does not all land on the same day.
    spread_across_week: bool = True

    # 0 = unlimited.
    max_attempts: int = 0

    # Operator escape hatches (mirror the existing script's flags).
    skip_cadence: bool = False
    allow_second_daily_slot: bool = False
    treat_unknown_as_dnp: bool = False

    # --- auto-run allow-list ------------------------------------------------
    # Which disposition CLASSES the unattended morning run may schedule. The
    # default is deliberately narrow: only leads that never picked up (DNP) and
    # leads never dialled at all (FRESH). Connected dispositions -- positive
    # followup, link sent, appointment fixed, premium quotation, CMRL
    # interested, directed to branch -- are CALLBACK class and are reachable
    # only from the manual screen, because a human decides when to chase a warm
    # lead. Adding CALLBACK here restores the old auto-callback behaviour.
    auto_classes: tuple[str, ...] = (DNP, FRESH)

    # Dial order, most urgent first. E0/F6/F5 (RED-3 .. RED+7) are the critical
    # window and are planned before anything else, so if a run is capped or the
    # dialler falls behind it is the far-from-expiry leads that get dropped,
    # never the ones about to lapse. E0 (expiry day and the day after) outranks
    # the rest of that window: it is the last moment renewal is still routine.
    bucket_priority: tuple[str, ...] = ("M0", "E0", "F6", "F5", "F4", "F3", "F2", "F1", "D0")

    # --- per-bucket disposition allow-list ----------------------------------
    # bucket -> the disposition slugs that bucket may auto-dial. A bucket absent
    # from this map, or mapped to an empty set, inherits `auto_classes` -- so the
    # default {} preserves the global behaviour exactly and an operator only pays
    # for the buckets they actually customise.
    #
    # This narrows, it never widens: a slug listed here still has to survive the
    # exclusion checks and the class allow-list. Listing `do_not_call` under F5
    # does not make F5 dial it. That is deliberate -- exclusions are regulatory
    # (TRAI/NCPR), so no per-bucket setting may override them.
    bucket_dispositions: dict[str, frozenset[str]] = field(default_factory=dict)

    # --- who earns the SECOND call of the day -------------------------------
    # Slugs that qualify a lead in an intensive bucket (F5/E0/F6/M0) for its second
    # daily slot. Empty = every lead in those buckets gets both, which is the
    # historic behaviour. Set it to the no-contact slugs and a lead that has
    # already been reached is called once and left alone.
    #
    # This reads the disposition as it stands WHEN THE PLAN IS BUILT, which is
    # the outcome of the previous call, not of this morning's. To gate on
    # today's first call, sync the dispositions after the morning wave and plan
    # the afternoon as a separate run -- by then the slug is today's answer.
    second_call_dispositions: frozenset[str] = frozenset()

    def wants_second_call(self, stage: Any) -> bool:
        """Does `stage` qualify for the second daily slot? True when unset."""
        if not self.second_call_dispositions:
            return True
        return str(stage or "").strip().lower() in self.second_call_dispositions

    def window_for(self, dte: int) -> Optional[RedWindow]:
        return find_window(dte, self.frequency_table)

    def bucket_allows(self, bucket: Optional[str], stage: Any) -> bool:
        """Is `stage` on `bucket`'s own allow-list? True when unset (inherit)."""
        allowed = self.bucket_dispositions.get(bucket or "")
        if not allowed:
            return True
        return str(stage or "").strip().lower() in allowed

    def priority_of(self, bucket: str) -> int:
        """Lower sorts earlier. Unknown buckets sink to the bottom."""
        try:
            return self.bucket_priority.index(bucket)
        except ValueError:
            return len(self.bucket_priority)

    @property
    def dte_max(self) -> int:
        return max(w.from_dte for w in self.frequency_table)

    @property
    def dte_min(self) -> int:
        return min(w.to_dte for w in self.frequency_table)


DEFAULT_CONFIG = RedConfig()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Months in which Chola renewals actually cluster. Used ONLY to break a tie that
# the >12 rule cannot: in the live data the dashed form's month component is
# always one of {7, 8, 9}. Kept as (8, 9) to match the heuristic the existing
# report SQL (build_red_cohort_daily.py) already uses, so the reports and the
# scheduler never disagree about a lead's RED.
RENEWAL_MONTH_HINT = (8, 9)

_MONTH_NAMES = ("%d-%b-%Y", "%d-%B-%Y")
# Year-less named month — the shape Chola's 29-Aug upload uses ('04-Sep').
_MONTH_NAMES_NO_YEAR = ("%d-%b", "%d-%B")


def _nearest_year(month: int, day: int, today: Optional[date] = None) -> Optional[date]:
    """Resolve a year-less day/month to the occurrence nearest `today`.

    Mirrors `metabase_source._day_month_sql`. Picking the current year outright
    would read a '05-Jan' seen in December as eleven months past instead of a
    fortnight away, and a RED in the past is a lead the engine never dials.
    """
    today = today or date.today()
    best: Optional[date] = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue                      # 29 Feb in a non-leap year
        if best is None or abs(candidate - today) < abs(best - today):
            best = candidate
    return best


def _resolve_three_part(a: int, b: int, year: int, month_first_default: bool,
                        month_first: Optional[bool] = None) -> Optional[date]:
    """Resolve two ambiguous components into a real date, or None.

    Evidence order, identical to `metabase_source._three_part_sql`:
      1. a component > 12 must be the day
      2. both > 12 is impossible
      3. `month_first`, when known: that campaign's OWN proven convention
      4. a component in RENEWAL_MONTH_HINT is likely the month
      5. otherwise use `month_first_default`
    """
    if not (1 <= a <= 31 and 1 <= b <= 31):
        return None
    if a > 12 and b > 12:
        return None

    if a > 12:
        month, day = b, a
    elif b > 12:
        month, day = a, b
    elif month_first is True:
        month, day = a, b
    elif month_first is False:
        month, day = b, a
    elif b in RENEWAL_MONTH_HINT:
        month, day = b, a
    elif a in RENEWAL_MONTH_HINT:
        month, day = a, b
    elif month_first_default:
        month, day = a, b
    else:
        month, day = b, a

    if not 1 <= month <= 12:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_red(value: Any, month_first: Optional[bool] = None,
              today: Optional[date] = None) -> Optional[date]:
    """Parse Chola's free-text RED into a date, or None when unusable.

    Mirrors `metabase_source.red_parse_expression` exactly, verified against
    every distinct real value in the warehouse. Handles:

        2026-09-15 00:00:00   ISO timestamp (machine-written, always ISO)
        2026-09-15            yyyy-mm-dd
        2026-15-09            yyyy-dd-mm  <- order resolved, not assumed
        29-08-2026            dd-mm-yyyy  (day-first: proven by the live data)
        8/9/2026              genuinely MIXED between campaigns, so `month_first`
                              carries that campaign's proven convention
        1-Sep-2026            named month
        02 Sep / 4-Sep        named month, NO year -> nearest occurrence to today
        15.09.2026            '.' separator
        15/9/26               2-digit year -> 20xx

    `month_first` is the campaign's own convention, learned in SQL from its
    unambiguous rows and passed back as `campaign_month_first`. Supplying it is
    what makes `8/9/2026` decidable; without it the renewal-month heuristic and
    a day-first default apply.

    Never raises. Anything self-contradictory (both components > 12, month 13,
    31 February) returns None so the lead is skipped and counted rather than
    dialled on a wrong date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "nan", "-"}:
        return None

    # An ISO timestamp is machine-written, so its order is not in doubt.
    head = text.replace("T", " ").split(" ")[0].split("+")[0].strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", head) and " " in text.replace("T", " "):
        try:
            return datetime.strptime(head, "%Y-%m-%d").date()
        except ValueError:
            return None

    # Named month, e.g. 1-Sep-2026. Read off the whole string with spaces and
    # dots folded to '-', so '02 Sep' and '1.Sep.2026' land here too — `head` is
    # cut at the first space and would otherwise be just the day.
    named = re.sub(r"[\s.]+", "-", text)
    for fmt in _MONTH_NAMES:
        try:
            return datetime.strptime(named, fmt).date()
        except (ValueError, TypeError):
            continue
    # Year-less named month, e.g. '04-Sep'. The year is INFERRED from today,
    # never assumed to be the current one.
    for fmt in _MONTH_NAMES_NO_YEAR:
        try:
            # A year is appended (2024, a leap year, so '29-Feb' still parses)
            # because bare day/month parsing is deprecated from Python 3.15.
            stub = datetime.strptime(f"{named}-2024", fmt + "-%Y")
        except (ValueError, TypeError):
            continue
        return _nearest_year(stub.month, stub.day, today)

    # Three numeric parts. The separator decides the DEFAULT order only; the
    # >12 and renewal-month rules take precedence.
    normalised = head.replace(".", "-")
    for sep, pattern in (("-", r"^(\d{1,4})-(\d{1,2})-(\d{1,4})$"),
                         ("/", r"^(\d{1,4})/(\d{1,2})/(\d{1,4})$")):
        source = normalised if sep == "-" else head
        match = re.match(pattern, source)
        if not match:
            continue
        first, middle, last = match.group(1), match.group(2), match.group(3)

        if len(first) == 4:
            # yyyy-A-B: could be ISO (yyyy-mm-dd) or yyyy-dd-mm.
            try:
                year, a, b = int(first), int(middle), int(last)
            except ValueError:
                return None
            return _resolve_three_part(a, b, year, month_first_default=True,
                                       month_first=month_first)

        if len(last) in (2, 4):
            try:
                a, b, year = int(first), int(middle), int(last)
            except ValueError:
                return None
            if len(str(last)) == 2:
                year += 2000
            # Dashed data is day-first (proven); slashed data differs between
            # campaigns, which is what `month_first` resolves.
            return _resolve_three_part(a, b, year, month_first_default=False,
                                       month_first=month_first)

    return None


_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a Formi/warehouse timestamp into a naive datetime, else None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").split("+")[0].replace("Z", "").strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def days_to_expiry(red: Optional[date], today: date) -> Optional[int]:
    """`dte` — positive means RED is still in the future."""
    return None if red is None else (red - today).days


def find_window(dte: int, table: Sequence[RedWindow] = DEFAULT_FREQUENCY_TABLE) -> Optional[RedWindow]:
    """First window containing `dte`, scanning from furthest-from-RED inwards."""
    for window in sorted(table, key=lambda w: -w.from_dte):
        if window.contains(dte):
            return window
    return None


def classify_disposition(stage: Any, config: RedConfig = DEFAULT_CONFIG) -> tuple[str, Optional[DispositionRule]]:
    """Return (class, rule) for a raw disposition slug."""
    slug = str(stage or "").strip().lower()
    rule = config.disposition_rules.get(slug)
    if rule is not None:
        return rule.klass, rule
    if not slug:
        return FRESH, config.disposition_rules.get("")
    return (DNP, None) if config.treat_unknown_as_dnp else (UNKNOWN, None)


# ---------------------------------------------------------------------------
# Disposition callback dates
# ---------------------------------------------------------------------------

def disposition_callback_date(
    rule: DispositionRule,
    today: date,
    last_called_at: Optional[datetime] = None,
    callback_date: Any = None,
    appointment_date: Any = None,
) -> list[date]:
    """Resolve a CALLBACK disposition into the date(s) it wants.

    Returns a sorted, de-duplicated list. Dates in the past are pulled forward
    to `today` (requirements REQ-DISP-004); the caller decides whether today is
    actually one of them.
    """
    if rule.klass != CALLBACK:
        return []

    wanted: list[date] = []

    # Priority 1 — the customer named a date.
    if rule.use_callback_date:
        parsed = _as_date(callback_date)
        if parsed:
            wanted.append(parsed)

    # Priority 2 — an appointment or branch-visit date.
    if not wanted and rule.use_appointment_date:
        appointment = _as_date(appointment_date)
        if appointment:
            base = appointment + timedelta(days=rule.appointment_plus_days)
            wanted.append(base)
            wanted.extend(base + timedelta(days=extra) for extra in rule.extra_offsets)

    # Priority 3 — a fixed offset from the last call.
    if not wanted and rule.offset_days is not None:
        anchor = (last_called_at.date() if last_called_at else today)
        wanted.append(anchor + timedelta(days=rule.offset_days))

    # Never ask for a date in the past.
    return sorted({d if d >= today else today for d in wanted})


def _as_date(value: Any) -> Optional[date]:
    """Coerce a date-ish or datetime-ish value to a date, else None."""
    if value in (None, ""):
        return None
    parsed = parse_red(value)
    if parsed is not None:
        return parsed
    stamp = parse_timestamp(value)
    return stamp.date() if stamp is not None else None


# ---------------------------------------------------------------------------
# Weekly spreading
# ---------------------------------------------------------------------------

def weekly_slots_for(key: str, calls_per_week: int) -> frozenset[int]:
    """Deterministic weekday set (Mon=0 … Sun=6) for a sparse-window lead.

    Spreads a cohort evenly across the week without any stored state: the same
    lead always gets the same days, but different leads get different days, so
    a 1-call-per-week bucket does not stampede on Mondays.

    5 calls/week collapses to Mon–Fri, matching REQ-FREQ-003.
    """
    if calls_per_week <= 0:
        return frozenset()
    if calls_per_week >= 7:
        return frozenset(range(7))
    if calls_per_week == 5:
        return frozenset(range(5))  # weekdays

    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    offset = digest[0] % 7
    step = 7 / calls_per_week
    return frozenset(int((offset + round(i * step)) % 7) for i in range(calls_per_week))


def _sparse_gap_hours(window: RedWindow, config: RedConfig) -> float:
    """Minimum hours between calls in a calls-per-week window."""
    if config.sparse_gap_hours is not None:
        return float(config.sparse_gap_hours)
    if window.calls_per_week <= 0:
        return 0.0
    return 168.0 / window.calls_per_week * config.spread_tolerance


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """The outcome for one lead on one day."""

    action: str
    reason: str
    schedule: bool = False
    bucket: str = ""
    bucket_label: str = ""
    dte: Optional[int] = None
    disposition_class: str = ""
    slots: int = 1
    trigger: str = ""            # cron | mandatory | disposition
    next_call_dates: tuple[date, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "reason": self.reason, "schedule": self.schedule,
            "bucket": self.bucket, "bucket_label": self.bucket_label, "dte": self.dte,
            "disposition_class": self.disposition_class, "slots": self.slots,
            "trigger": self.trigger,
            "next_call_dates": [d.isoformat() for d in self.next_call_dates],
            **self.meta,
        }


def _lead_get(lead: dict, *names: str, default: Any = None) -> Any:
    """First present, non-empty value among `names`."""
    for name in names:
        if name in lead and lead[name] not in (None, ""):
            return lead[name]
    return default


def decide(lead: dict[str, Any], now: datetime, config: RedConfig = DEFAULT_CONFIG) -> Decision:
    """Apply the full precedence ladder to one lead.

    `lead` is a plain dict so the same function serves Formi payloads and
    Metabase rows. Recognised keys (aliases in parentheses):

        stage (disposition, lead_stage_computed)
        red (red_date, expiry_date)
        last_interaction_time (last_called_at)
        total_interactions (attempts)
        calls_today
        calls_last_7d (calls_in_week)
        callback_date, appointment_date (visit_date)
        lead_uuid (id, policy_no)  -- only used to spread the week
    """
    today = now.date()
    stage_raw = _lead_get(lead, "stage", "disposition", "lead_stage_computed", default="")
    stage = str(stage_raw or "").strip().lower()
    klass, rule = classify_disposition(stage, config)

    red = parse_red(_lead_get(lead, "red", "red_date", "expiry_date"),
                    month_first=_lead_get(lead, "campaign_month_first"))
    dte = days_to_expiry(red, today)
    last_called = parse_timestamp(_lead_get(lead, "last_interaction_time", "last_called_at"))
    attempts = int(_lead_get(lead, "total_interactions", "attempts", default=0) or 0)
    calls_today = int(_lead_get(lead, "calls_today", default=0) or 0)
    calls_last_7d = int(_lead_get(lead, "calls_last_7d", "calls_in_week", default=0) or 0)
    queued_today = int(_lead_get(lead, "queued_today", default=0) or 0)
    spread_key = str(_lead_get(lead, "lead_uuid", "id", "policy_no", "contact_id", default=stage))

    hours_since = None
    if last_called is not None:
        hours_since = (now - last_called).total_seconds() / 3600.0

    meta: dict[str, Any] = {
        "stage": stage,
        "red": red.isoformat() if red else None,
        "attempts": attempts,
        "calls_today": calls_today,
        "calls_last_7d": calls_last_7d,
        "queued_today": queued_today,
        "hours_since_last": None if hours_since is None else round(hours_since, 1),
        "last_interaction_time": last_called.isoformat(sep=" ") if last_called else None,
    }

    def out(action: str, reason: str, **kw: Any) -> Decision:
        return Decision(action=action, reason=reason, dte=dte,
                        disposition_class=klass, meta=meta, **kw)

    # --- Priority 1: exclusions -------------------------------------------
    if klass == EXCLUDED:
        note = rule.note if rule else ""
        return out(SKIP_EXCLUDED, f"{SKIP_EXCLUDED} ({stage}){f' — {note}' if note else ''}")
    if klass == HOLD:
        return out(SKIP_HOLD, f"{SKIP_HOLD} ({stage}) — {rule.note if rule else 'paused'}")
    if klass == REASSIGN:
        return out(SKIP_REASSIGN, f"{SKIP_REASSIGN} ({stage}) — route to a language-capable agent")
    if klass == UNKNOWN:
        return out(SKIP_UNKNOWN, f"{SKIP_UNKNOWN} ({stage})")

    # RED is required for every remaining branch.
    if dte is None:
        return out(SKIP_NO_RED, f"{SKIP_NO_RED} — no usable RED date")

    # Never double-book. `queued_today` counts interactions that exist for today
    # but have not been dialled yet (empty call_stage). This is what stops the
    # bucket preview from reporting leads as actionable when the dialler is
    # already holding a slot for them — campaign backlogs can be large enough
    # that ignoring it overstates the eligible count several-fold.
    if queued_today and not config.allow_second_daily_slot:
        return out(SKIP_ALREADY_SCHEDULED,
                   f"{SKIP_ALREADY_SCHEDULED} queued_today={queued_today}")

    # --- Priority 2: mandatory days (RED−1, RED) --------------------------
    # Force a call even when cadence, weekly budget, the attempt cap or a pending
    # callback would otherwise say wait. REQ-MAND-002 allows only the exclusion
    # checks above to veto a mandatory day, so the max-attempts cap is applied
    # *after* this branch rather than before it.
    if dte in config.mandatory_days:
        # M0 honours a per-bucket allow-list if the operator set one. Left off by
        # default: with `bucket_dispositions` empty this never fires, so RED-1 and
        # RED keep forcing the call. Narrowing M0 is possible but it is the one
        # setting that can silence the last chance to save a policy.
        if not config.bucket_allows("M0", stage):
            return out(SKIP_BUCKET_DISPOSITION,
                       f"{SKIP_BUCKET_DISPOSITION} bucket=M0 disposition={stage} "
                       f"— not on the mandatory-day allow-list",
                       bucket="M0", bucket_label=MANDATORY_LABEL)
        window = config.window_for(dte)
        if calls_today and not config.allow_second_daily_slot:
            cap = window.calls_per_day if window and window.intensive else config.calls_per_day_cap
            if calls_today >= max(1, cap):
                return out(SKIP_DAILY_CAP,
                           f"{SKIP_DAILY_CAP} mandatory day dte={dte} calls_today={calls_today}")
        return out(SCHEDULE, f"{SCHEDULE} mandatory_day dte={dte}", schedule=True,
                   bucket="M0", bucket_label=MANDATORY_LABEL,
                   trigger="mandatory", slots=1, next_call_dates=(today,))

    if config.max_attempts and attempts >= config.max_attempts:
        return out(SKIP_MAX_ATTEMPTS,
                   f"{SKIP_MAX_ATTEMPTS} (attempts={attempts} limit={config.max_attempts})")

    # --- Priority 3: disposition callback ---------------------------------
    # Gate first: when CALLBACK is not in the auto-run allow-list, a connected
    # lead leaves the ladder here. It keeps bucket D0 so the UI can still count
    # and offer it on the manual screen -- this is "not now, not automatically",
    # not "never". Placed after the mandatory-day branch on purpose: RED-1 and
    # RED still force a call even for a warm lead about to lapse.
    if klass == CALLBACK and CALLBACK not in config.auto_classes:
        return out(SKIP_MANUAL_ONLY,
                   f"{SKIP_MANUAL_ONLY} disposition={stage} — connected lead, dial from the manual screen",
                   bucket="D0", bucket_label=f"Manual only ({stage})")

    if klass == CALLBACK and rule is not None:
        if not config.bucket_allows("D0", stage):
            return out(SKIP_BUCKET_DISPOSITION,
                       f"{SKIP_BUCKET_DISPOSITION} bucket=D0 disposition={stage} "
                       f"— not on the callback allow-list",
                       bucket="D0", bucket_label=f"Disposition callback ({stage})")
        wanted = disposition_callback_date(
            rule, today, last_called,
            _lead_get(lead, "callback_date"),
            _lead_get(lead, "appointment_date", "visit_date"),
        )
        if today in wanted:
            return out(SCHEDULE,
                       f"{SCHEDULE} disposition={stage} callback_on={today.isoformat()}",
                       schedule=True, bucket="D0",
                       bucket_label=f"Disposition callback ({stage})",
                       trigger="disposition", slots=1, next_call_dates=tuple(wanted))
        nxt = wanted[0].isoformat() if wanted else "unresolved"
        return out(SKIP_CALLBACK_PENDING,
                   f"{SKIP_CALLBACK_PENDING} disposition={stage} next={nxt}",
                   bucket="D0", bucket_label=f"Disposition callback ({stage})",
                   next_call_dates=tuple(wanted))

    # --- Priority 4: RED frequency table ----------------------------------
    window = config.window_for(dte)
    if window is None:
        edge = "not yet in window" if dte > config.dte_max else "grace period expired"
        return out(SKIP_OUTSIDE, f"{SKIP_OUTSIDE} (dte={dte} — {edge})")

    base = {"bucket": window.bucket, "bucket_label": window.label, "trigger": "cron"}

    # Per-bucket allow-list. Checked before cadence so the reason reads as the
    # operator's own choice ("F1 does not chase voicemail") rather than a cadence
    # wait that would never resolve.
    if not config.bucket_allows(window.bucket, stage):
        return out(SKIP_BUCKET_DISPOSITION,
                   f"{SKIP_BUCKET_DISPOSITION} bucket={window.bucket} disposition={stage} "
                   f"— not on this bucket's allow-list", **base)

    meta["window"] = window.bucket
    meta["calls_per_week"] = window.calls_per_week
    meta["calls_per_day"] = window.calls_per_day

    if window.intensive:
        # F5 / E0 / F6: one call per cron pass. The 2nd call of the day is reactive —
        # the DNP handler fires it, so we never pre-schedule it here.
        cap = min(window.calls_per_day, config.calls_per_day_cap) or 1
        if calls_today >= cap and not config.allow_second_daily_slot:
            return out(SKIP_DAILY_CAP,
                       f"{SKIP_DAILY_CAP} bucket={window.bucket} calls_today={calls_today} cap={cap}",
                       **base)
        if (hours_since is not None and hours_since < config.same_day_gap_hours
                and not config.skip_cadence):
            return out(SKIP_CADENCE,
                       f"{SKIP_CADENCE} bucket={window.bucket} last={hours_since:.1f}h "
                       f"(need >= {config.same_day_gap_hours}h)", **base)
        return out(SCHEDULE,
                   f"{SCHEDULE} bucket={window.bucket} dte={dte} "
                   f"{'never_called' if last_called is None else f'last={hours_since:.1f}h'}",
                   schedule=True, slots=1, next_call_dates=(today,),
                   **base)

    # Sparse windows F1–F4: a rolling weekly budget plus a derived minimum gap.
    if config.enforce_weekly_budget and calls_last_7d >= window.calls_per_week and not config.skip_cadence:
        return out(SKIP_WEEKLY_BUDGET,
                   f"{SKIP_WEEKLY_BUDGET} bucket={window.bucket} "
                   f"calls_last_7d={calls_last_7d} budget={window.calls_per_week}", **base)

    gap = _sparse_gap_hours(window, config)
    if hours_since is not None and hours_since < gap and not config.skip_cadence:
        return out(SKIP_CADENCE,
                   f"{SKIP_CADENCE} bucket={window.bucket} last={hours_since:.1f}h "
                   f"(need >= {gap:.0f}h)", **base)

    if (config.spread_across_week and not config.skip_cadence
            and window.calls_per_week < 7 and last_called is not None):
        slots = weekly_slots_for(spread_key, window.calls_per_week)
        if today.weekday() not in slots:
            return out(SKIP_NOT_TODAYS_SLOT,
                       f"{SKIP_NOT_TODAYS_SLOT} bucket={window.bucket} "
                       f"weekday={today.weekday()} slots={sorted(slots)}", **base)

    return out(SCHEDULE,
               f"{SCHEDULE} bucket={window.bucket} dte={dte} "
               f"{'never_called' if last_called is None else f'last={hours_since:.1f}h'}",
               schedule=True, slots=1, next_call_dates=(today,), **base)


# ---------------------------------------------------------------------------
# Aggregation for the console preview
# ---------------------------------------------------------------------------

def bucket_summary(
    leads: Iterable[dict[str, Any]],
    now: datetime,
    config: RedConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Bucket counts for the UI: no writes, no network, one pass over leads.

    Returns per-bucket eligible/holding counts in frequency-table order, plus a
    skip-reason histogram so an operator can see *why* leads were dropped.
    """
    order = [w.bucket for w in sorted(config.frequency_table, key=lambda w: -w.from_dte)]
    labels = {w.bucket: w.label for w in config.frequency_table}
    labels["M0"] = MANDATORY_LABEL
    labels["D0"] = CALLBACK_LABEL

    buckets: dict[str, dict[str, Any]] = {}
    for bucket in ["M0", "D0", *order]:
        buckets[bucket] = {"bucket": bucket, "label": labels.get(bucket, bucket),
                           "eligible": 0, "waiting": 0, "total": 0}

    skips: dict[str, int] = {}
    eligible = total = 0
    slots = 0
    for lead in leads:
        total += 1
        decision = decide(lead, now, config)
        if decision.bucket:
            entry = buckets.setdefault(
                decision.bucket,
                {"bucket": decision.bucket, "label": decision.bucket_label or decision.bucket,
                 "eligible": 0, "waiting": 0, "total": 0})
            entry["total"] += 1
            entry["eligible" if decision.schedule else "waiting"] += 1
        if decision.schedule:
            eligible += 1
            slots += decision.slots
        else:
            skips[decision.action] = skips.get(decision.action, 0) + 1

    return {
        "total_leads": total,
        "eligible": eligible,
        "scheduled_slots": slots,
        "buckets": [buckets[b] for b in ["M0", "D0", *order] if b in buckets],
        "skips": dict(sorted(skips.items(), key=lambda kv: -kv[1])),
        "evaluated_at": now.isoformat(timespec="seconds"),
        "frequency_table": [w.as_dict() for w in config.frequency_table],
    }


# ---------------------------------------------------------------------------
# Config from saved-strategy JSON
# ---------------------------------------------------------------------------

def config_from_settings(settings: dict[str, Any] | None) -> RedConfig:
    """Build a `RedConfig` from a console strategy's `settings` JSON.

    Unknown keys are ignored and anything malformed raises ValueError so the
    API can reject a bad strategy instead of silently mis-dialling.
    """
    settings = dict(settings or {})
    config = DEFAULT_CONFIG

    raw_table = settings.get("frequency_table")
    if raw_table:
        if not isinstance(raw_table, list) or not raw_table:
            raise ValueError("frequency_table must be a non-empty list of windows")
        windows: list[RedWindow] = []
        for index, row in enumerate(raw_table, start=1):
            if not isinstance(row, dict):
                raise ValueError("each frequency_table entry must be an object")
            try:
                windows.append(RedWindow(
                    bucket=str(row.get("bucket") or f"F{index}"),
                    label=str(row.get("label") or f"Window {index}"),
                    from_dte=int(row["from_dte"]), to_dte=int(row["to_dte"]),
                    calls_per_week=int(row.get("calls_per_week") or 0),
                    calls_per_day=int(row.get("calls_per_day") or 0),
                ))
            except KeyError as exc:
                raise ValueError(f"frequency_table window {index} is missing {exc}") from exc
            except (TypeError, ValueError) as exc:
                raise ValueError(f"frequency_table window {index}: {exc}") from exc
        _assert_no_overlap(windows)
        config = replace(config, frequency_table=tuple(windows))

    if "mandatory_days" in settings:
        raw_days = settings.get("mandatory_days")
        if isinstance(raw_days, str):
            raw_days = [p for p in raw_days.replace(" ", "").split(",") if p]
        try:
            days = tuple(int(d) for d in (raw_days or ()))
        except (TypeError, ValueError) as exc:
            raise ValueError("mandatory_days must be whole numbers") from exc
        config = replace(config, mandatory_days=days)

    raw_bucket_disp = settings.get("bucket_dispositions")
    if raw_bucket_disp:
        if not isinstance(raw_bucket_disp, dict):
            raise ValueError("bucket_dispositions must be an object of bucket -> slug list")
        known = {w.bucket for w in config.frequency_table} | {"M0", "D0"}
        parsed: dict[str, frozenset[str]] = {}
        for bucket, slugs in raw_bucket_disp.items():
            bucket = str(bucket).strip().upper()
            if bucket not in known:
                raise ValueError(f"bucket_dispositions: unknown bucket {bucket!r}")
            if slugs is None:
                continue
            if not isinstance(slugs, (list, tuple, set, frozenset)):
                raise ValueError(f"bucket_dispositions[{bucket}] must be a list of slugs")
            cleaned = {str(s).strip().lower() for s in slugs}
            cleaned.discard("")
            # An empty list means "inherit the global allow-list", not "dial
            # nothing" -- clearing a bucket in the UI should restore the default
            # rather than silently switching that bucket off.
            if cleaned:
                parsed[bucket] = frozenset(cleaned)
        config = replace(config, bucket_dispositions=parsed)

    raw_second = settings.get("second_call_dispositions")
    if raw_second is not None:
        if not isinstance(raw_second, (list, tuple, set, frozenset)):
            raise ValueError("second_call_dispositions must be a list of slugs")
        cleaned = {str(s).strip().lower() for s in raw_second}
        cleaned.discard("")
        config = replace(config, second_call_dispositions=frozenset(cleaned))

    numeric: dict[str, Any] = {}
    for key, caster, minimum in (
        ("calls_per_day_cap", int, 1),
        ("same_day_gap_hours", float, 0.0),
        ("spread_tolerance", float, 0.1),
        ("max_attempts", int, 0),
    ):
        if settings.get(key) not in (None, ""):
            try:
                value = caster(settings[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a number") from exc
            if value < minimum:
                raise ValueError(f"{key} must be at least {minimum}")
            numeric[key] = value
    if settings.get("sparse_gap_hours") not in (None, ""):
        try:
            numeric["sparse_gap_hours"] = float(settings["sparse_gap_hours"])
        except (TypeError, ValueError) as exc:
            raise ValueError("sparse_gap_hours must be a number") from exc
        if numeric["sparse_gap_hours"] < 0:
            raise ValueError("sparse_gap_hours cannot be negative")

    for key in ("enforce_weekly_budget", "spread_across_week", "skip_cadence",
                "allow_second_daily_slot", "treat_unknown_as_dnp"):
        if key in settings:
            numeric[key] = bool(settings[key])

    if numeric:
        config = replace(config, **numeric)

    extra_excluded = settings.get("extra_exclusions") or []
    if extra_excluded:
        rules = dict(config.disposition_rules)
        for slug in extra_excluded:
            slug = str(slug).strip().lower()
            if slug:
                rules[slug] = DispositionRule(slug, EXCLUDED, note="Excluded by strategy")
        config = replace(config, disposition_rules=rules)

    return config


def _assert_no_overlap(windows: Sequence[RedWindow]) -> None:
    """Windows must not overlap, otherwise bucketing is ambiguous."""
    ordered = sorted(windows, key=lambda w: -w.from_dte)
    for earlier, later in zip(ordered, ordered[1:]):
        if later.from_dte >= earlier.to_dte:
            raise ValueError(
                f"windows {earlier.bucket} ({earlier.from_dte}..{earlier.to_dte}) and "
                f"{later.bucket} ({later.from_dte}..{later.to_dte}) overlap"
            )
