"""Turn engine decisions into timed dial slots.

`red_engine.decide` answers *whether* a lead should be called today. This module
answers *when*, and it is the only place that knows about the dial window, the
per-minute ceiling and the run cap. It performs no I/O of any kind — the caller
persists the result — so it is safe to run under DRY_RUN by construction.

Four rules, in the order they are applied:

  1. PRIORITY   — leads are ordered by `config.priority_of(bucket)`, so M0/E0/F6/F5
                  are placed first. When `max_per_run` bites it is the
                  far-from-expiry leads that get shed, never the ones about to
                  lapse. The shed count is returned, not swallowed.
  2. ONE SLOT   — one call per lead per pass. F5/E0/F6 and the mandatory days
                  allow two a day, but the second is the recall pass's, not this
                  one's: see the note where it used to be booked, below.
  3. EARLY      — a lead who gets TWO calls today takes its first one in the
                  first half of the window (`twice_deadline`), so the second one
                  still has hours to land in. Placed late, it is one call and not
                  two, whatever the plan believed it was giving them.
  4. ROTATION   — a lead dialled yesterday at 09:00 is not dialled at 09:00
                  today. Ported from schedule_redials.py: today's minute-of-day
                  is (last call's minute + shift_from_last_hours) wrapped into
                  the lead's own half of the window. Leads with no history are
                  spread uniformly across it.
  5. STAGGER    — at most `max_per_minute` calls share a minute; overflow moves
                  to the next free minute, still inside the lead's half.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from .red_engine import (
    CALLBACK, DNP, EXCLUDED, FRESH, MANDATORY_LABEL, SCHEDULE, Decision, RedConfig,
    classify_disposition, config_from_settings, days_to_expiry, parse_red,
    parse_timestamp,
)

__all__ = [
    "DispatchConfig", "Slot", "DispatchResult", "WINDOW_FLOOR", "WINDOW_CEIL",
    "DEFAULT_RED_PRIORITY", "parse_hhmm", "validate_dial_window", "red_rank",
    "dispatch_config_from_body", "red_config_from_body", "dispatch", "manual_pairs",
    "twice_today", "twice_deadline",
]

# Regulatory / operational clamp. Nothing may be dialled outside these hours,
# whatever a saved config says, so it is enforced here as well as at the API
# boundary — the API is not the only caller.
WINDOW_FLOOR = 9 * 60      # 09:00
WINDOW_CEIL = 20 * 60      # 20:00


def parse_hhmm(text: Any, label: str = "time") -> int:
    """'09:30' -> 570. Raises ValueError on anything else."""
    try:
        hh, _, mm = str(text).strip().partition(":")
        minute = int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be HH:MM, got {text!r}") from None
    if not 0 <= minute <= 24 * 60:
        raise ValueError(f"{label} must be HH:MM, got {text!r}")
    return minute


def hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def validate_dial_window(start: Any, end: Any) -> tuple[int, int]:
    """Return (start_min, end_min) or raise ValueError. 09:00-20:00, start < end."""
    start_min = parse_hhmm(start, "dial_window.start")
    end_min = parse_hhmm(end, "dial_window.end")
    if start_min >= end_min:
        raise ValueError(
            f"dial_window.start ({hhmm(start_min)}) must be before "
            f"dial_window.end ({hhmm(end_min)})")
    if start_min < WINDOW_FLOOR or end_min > WINDOW_CEIL:
        raise ValueError(
            f"dial_window {hhmm(start_min)}-{hhmm(end_min)} is outside the permitted "
            f"{hhmm(WINDOW_FLOOR)}-{hhmm(WINDOW_CEIL)} dialling hours")
    return start_min, end_min


# Days-to-expiry bands, best first, applied AHEAD of `bucket_priority`. Whatever
# is shed by `max_per_run` or will not fit in the hours left comes off the bottom
# of this, so a late approval keeps the calls that cannot be made up tomorrow and
# drops the ones that can.
#
# These are the client schedule's two 2-calls/day rows, in THEIR sign convention
# (negative = before RED, per "calls needs to be initiated on RED - 1 and RED
# date"): "1 to 3" is the three days AFTER expiry, "-7 to 0" is the week running
# up to it. Negated here into our dte (= red - today, positive before RED):
#
#     client  1 .. 3   ->  dte -1 .. -3   band 0   policy has just lapsed
#     client -7 .. 0   ->  dte  0 ..  7   band 1   the week before it lapses
#
# Together they cover RED-7 .. RED+3 exactly, which is the whole intensive part
# of the schedule and nothing else. Read in OUR convention instead, "0 to -7"
# would mean RED..RED+7 -- four days past where the client's schedule stops, and
# 5,069 live leads sit in that phantom range with no window to call them from.
#
# A band, not a bucket, because the bands cut ACROSS buckets: E0 is dte 0..-1 and
# straddles the pair, its RED day ranking under its day-after.
DEFAULT_RED_PRIORITY = ((-1, -3), (0, 7))


@dataclass(frozen=True)
class DispatchConfig:
    """The scheduling half of a saved config (the engine owns the other half)."""

    start_min: int = 9 * 60 + 30
    end_min: int = 19 * 60
    shift_from_last_hours: float = 2.0
    same_day_gap_hours: float = 3.0
    max_per_minute: int = 12       # 0 = unlimited
    max_per_run: int = 5000        # 0 = unlimited
    red_priority: tuple[tuple[int, int], ...] = DEFAULT_RED_PRIORITY

    @property
    def span(self) -> int:
        return self.end_min - self.start_min


def red_rank(dte: Any, bands: Sequence[tuple[int, int]] = DEFAULT_RED_PRIORITY) -> int:
    """Index of the first band containing `dte`; len(bands) for everything else.

    Either order of the pair is accepted, so a band can be written the way a
    person says it. Note the values are dte, NOT the client schedule's signing —
    their "1 to 3" is `(-1, -3)` here.
    """
    if dte is None:
        return len(bands)
    for index, (first, second) in enumerate(bands):
        if min(first, second) <= int(dte) <= max(first, second):
            return index
    return len(bands)


@dataclass
class Slot:
    lead: dict[str, Any]
    decision: Decision
    slot_no: int
    priority: int
    minute: int
    day: date

    @property
    def scheduled_time(self) -> str:
        """Naive `YYYY-MM-DDTHH:MM:SS` — Formi rejects an offset suffix."""
        return f"{self.day.isoformat()}T{self.minute // 60:02d}:{self.minute % 60:02d}:00"


@dataclass
class DispatchResult:
    slots: list[Slot]
    dropped: int = 0            # leads shed by max_per_run (lowest priority first)
    unplaceable: int = 0        # slots that found no free minute in the window

    @property
    def leads(self) -> int:
        return len({id(s.lead) for s in self.slots})


# ---------------------------------------------------------------------------
# Config bridging
# ---------------------------------------------------------------------------

def dispatch_config_from_body(body: dict[str, Any]) -> DispatchConfig:
    window = body.get("dial_window") or {}
    start_min, end_min = validate_dial_window(window.get("start", "09:00"),
                                              window.get("end", "20:00"))

    def num(key: str, default: float, minimum: float) -> float:
        raw = body.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number, got {raw!r}") from None
        if value < minimum:
            raise ValueError(f"{key} must be at least {minimum}")
        return value

    bands = body.get("red_priority") or DEFAULT_RED_PRIORITY
    try:
        red_priority = tuple((int(a), int(b)) for a, b in bands)
    except (TypeError, ValueError):
        raise ValueError("red_priority must be a list of [from_dte, to_dte] pairs") from None

    return DispatchConfig(
        start_min=start_min, end_min=end_min,
        shift_from_last_hours=num("shift_from_last_hours", 2.0, 0.0),
        same_day_gap_hours=num("same_day_gap_hours", 3.0, 0.0),
        max_per_minute=int(num("max_per_minute", 12, 0)),
        max_per_run=int(num("max_per_run", 5000, 0)),
        red_priority=red_priority,
    )


def red_config_from_body(body: dict[str, Any]) -> RedConfig:
    """Contract config JSON -> RedConfig.

    `auto_dispositions` is a list of slugs, while the engine gates on disposition
    CLASSES. Each listed slug is classified and its class allowed, so listing
    `positive_followup` really does re-enable auto callbacks and listing only the
    DNP family really does keep connected leads on the manual screen.
    """
    config = config_from_settings(body)

    if "auto_dispositions" in body:
        raw = body.get("auto_dispositions") or []
        if not isinstance(raw, (list, tuple)):
            raise ValueError("auto_dispositions must be a list of disposition slugs")
        classes: list[str] = []
        for slug in raw:
            klass, _ = classify_disposition(slug, config)
            if klass in (DNP, FRESH, CALLBACK) and klass not in classes:
                classes.append(klass)
        config = replace(config, auto_classes=tuple(classes))

    if body.get("bucket_priority"):
        order = tuple(str(b) for b in body["bucket_priority"])
        config = replace(config, bucket_priority=order)

    return config


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def twice_today(dte: Any, config: RedConfig) -> bool:
    """Could the engine give this lead a SECOND call today?

    Asked of the same config `evaluate` will be asked rather than hardcoded, so
    narrowing a window narrows this with it. The two branches of `evaluate` that
    let `calls_today` reach two: a mandatory day (RED-1, RED) and an intensive
    window — F5/E0/F6, dte 7..1, 0..-1, -2..-3, which is the operator's
    "red 0-7 and -1 to -3".
    """
    if dte is None:
        return False
    try:
        dte = int(dte)
    except (TypeError, ValueError):
        return False
    if dte in config.mandatory_days:
        return True
    window = config.window_for(dte)
    return bool(window and window.intensive)


def twice_deadline(dcfg: DispatchConfig, start_min: int) -> Optional[int]:
    """Latest minute a two-call lead may take its FIRST call. None = no room.

    Reported on 15 Sep 2026: "you schedule the calls over the day to people who
    can be dialled twice — if you schedule those at 8 how can we dial them".
    Exactly right. The second call is legal only `same_day_gap_hours` after the
    first and only inside the window, so a first call at 19:00 in a window that
    shuts at 19:00 is not a first call at all: it is the only call that lead will
    ever get, placed by a scheduler that believed it was giving them two.

    So the hours that are left are split in half around the gap:

        [start .......... deadline]   gap   [deadline+gap .......... end)
             first calls                          the chase's calls

    Equal halves on purpose. Any other split starves one of them, and they hold
    the same leads — a first-call half wide enough for everybody is no use if the
    chase behind it has ten minutes to place the same number again.

    None when what is left of the window cannot hold both. A campaign approved at
    16:00 against a 19:00 close and a 3h gap has room for one call each, and
    saying so is better than crushing every two-call lead onto one minute to
    honour a second call that cannot happen. `_recall_due` reads the same None
    and never comes due.
    """
    room = dcfg.end_min - start_min - int(round(dcfg.same_day_gap_hours * 60))
    return start_min + room // 2 if room >= 2 else None


def _lead_key(lead: dict[str, Any]) -> str:
    """Stable tiebreak so two runs over the same data produce the same plan."""
    for name in ("lead_uuid", "id", "warehouse_lead_id", "policy_no", "contact_id"):
        if lead.get(name) not in (None, ""):
            return str(lead[name])
    return ""


def dispatch(
    pairs: Sequence[tuple[dict[str, Any], Decision]],
    day: date,
    config: RedConfig,
    dcfg: DispatchConfig,
    floor_min: Optional[int] = None,
) -> DispatchResult:
    """Place every schedulable (lead, decision) pair on the clock for `day`.

    `floor_min` is the earliest minute-of-day a slot may take. It defaults to the
    dial-window start, which is right for a plan built for a FUTURE date. For a
    plan built for today the caller passes the current time instead: otherwise a
    plan generated at 15:00 emits its whole pass from 09:30, i.e. entirely
    in the past, and every one of those slots is undialable the moment it lands.
    """
    ordered = [(lead, dec) for lead, dec in pairs if dec.schedule]
    ordered.sort(key=lambda p: (red_rank(p[1].dte, dcfg.red_priority),
                                config.priority_of(p[1].bucket), _lead_key(p[0])))

    dropped = 0
    if dcfg.max_per_run and len(ordered) > dcfg.max_per_run:
        dropped = len(ordered) - dcfg.max_per_run
        ordered = ordered[:dcfg.max_per_run]

    start = dcfg.start_min if floor_min is None else max(dcfg.start_min, int(floor_min))
    shift_min = int(round(dcfg.shift_from_last_hours * 60))

    # --- rule 3: the two-call leads go early --------------------------------
    # Per lead, the last minute it may be placed on: `end_min` for almost
    # everybody, and for a lead the engine could call TWICE today the point past
    # which a second call could not follow it. Every step below works in the
    # lead's own ceiling rather than one shared span, because a ceiling applied
    # at placement alone is a ceiling rotation and the uniform spread have
    # already driven straight through.
    deadline = twice_deadline(dcfg, start)
    ceiling = [deadline if deadline and twice_today(dec.dte, config) else dcfg.end_min
               for _lead, dec in ordered]

    # --- rule 4: rotation ---------------------------------------------------
    desired: list[Optional[int]] = [None] * len(ordered)
    for index, (lead, _dec) in enumerate(ordered):
        last = parse_timestamp(lead.get("last_interaction_time") or lead.get("last_called_at"))
        if last is not None and shift_min > 0:
            base = last.hour * 60 + last.minute + shift_min
            # The modulo works in the lead's OWN reach: it has to shrink with the
            # floor or a rotated slot lands past the end of what is left, and it
            # has to shrink with the deadline or a two-call lead with any call
            # history at all wraps straight back into the evening.
            desired[index] = start + (base - start) % max(1, ceiling[index] - start)

    # One spread per ceiling, so each group fills its own half evenly. A single
    # spread across the whole window would deal two-call leads into the evening
    # before `_free_minute` ever saw them.
    #
    # Across `reach - 1`, not `reach`: the window is half-open (see
    # `_free_minute`), so the last minute anybody may be placed on is the one
    # before it shuts. Spreading onto the boundary handed the last lead a minute
    # the window had already closed on and `_free_minute` answered None -- one
    # lead silently unplaceable per plan, the one the spread picked to go last.
    for limit in sorted(set(ceiling)):
        blank = [i for i, m in enumerate(desired) if m is None and ceiling[i] == limit]
        reach = max(1, limit - start)
        for k, index in enumerate(blank):
            desired[index] = start if len(blank) < 2 else \
                start + int(round(k * (reach - 1) / (len(blank) - 1)))

    # --- rules 1 + 5: place the pass's call in priority order, staggered -----
    load: dict[int, int] = {}
    result = DispatchResult(slots=[])
    for index, (lead, dec) in enumerate(ordered):
        want = desired[index]
        limit = ceiling[index]
        minute = _free_minute(start if want is None else want, load, dcfg, start, limit)
        if minute is None and limit < dcfg.end_min:
            # The early half filled up. One call today beats none, so this lead
            # takes a late minute and loses its second call rather than its day.
            # That is the right way round to fail: the alternative is a lead the
            # plan counted and nobody rang.
            minute = _free_minute(limit, load, dcfg, start)
        if minute is None:
            result.unplaceable += 1
            continue
        load[minute] = load.get(minute, 0) + 1
        result.slots.append(Slot(lead=lead, decision=dec, slot_no=1,
                                 priority=config.priority_of(dec.bucket),
                                 minute=minute, day=day))

    # --- the second daily slot for F5/E0/F6 ------------------------------------
    # Not booked here, on purpose. This loop used to place slot 2 at slot 1 +
    # `same_day_gap_hours` at the moment the first plan was built, gated on
    # `wants_second_call(lead["stage"])` -- and at that moment `stage` is still
    # the outcome of YESTERDAY's call. The second call was committed before the
    # first had happened.
    #
    # It also silently made itself the only answer. A pre-booked slot 2 sits on
    # Formi's clock, so it counts in `queued_today`, and `evaluate` skips any
    # lead with a queued interaction (ALREADY_SCHEDULED_TODAY) rather than
    # double-book them. The recall pass therefore found nothing to do for
    # exactly the leads it existed to reconsider.
    #
    # So the second call is now the RECALL PASS's first call: prepare `auto_pm`
    # with resync=True and `evaluate` judges it on the real disposition and call
    # duration of the call that already went out today. The spacing that used to
    # be this loop's `first.minute + gap` is enforced there by the
    # same_day_gap_hours cadence check, and the count by the per-window daily
    # cap.

    result.dropped = dropped
    result.slots.sort(key=lambda s: (s.minute, s.priority, _lead_key(s.lead), s.slot_no))
    return result


def _free_minute(wanted: int, load: dict[int, int], dcfg: DispatchConfig,
                 floor_min: Optional[int] = None,
                 ceil_min: Optional[int] = None) -> Optional[int]:
    """A free minute in [floor, min(end, ceil)) at or nearest `wanted`, or None.

    Searched forward from `wanted` first -- a lead placed later than the spread
    asked still keeps its order relative to the leads after it -- and only then
    backward, when the whole rest of the window is full.

    `ceil_min` is the two-call deadline (see `twice_deadline`); without one the
    window's own end applies.

    The window is half-open: `end_min` is when it SHUTS, so no call is placed on
    it. That makes the capacity arithmetic true -- `end - start` minutes are
    dialable, which is what `_day_window` has always counted -- and it is why
    two adjacent windows can never both claim the minute where they meet, which
    is how a campaign capped at ten calls a minute once put twenty on 13:30 with
    neither run over its own ceiling.
    """
    floor = dcfg.start_min if floor_min is None else floor_min
    minute = max(wanted, floor)
    end = dcfg.end_min if ceil_min is None else min(dcfg.end_min, ceil_min)
    if not dcfg.max_per_minute:
        return minute if minute < end else None
    while minute < end:
        if load.get(minute, 0) < dcfg.max_per_minute:
            return minute
        minute += 1
    # Nothing ahead — so look BEHIND the wanted minute before giving the lead up.
    # The spread and the rotation each aim a lead at one minute, and a lead aimed
    # into a congested stretch used to be shed here with the rest of its window
    # standing empty in front of it: 65 leads over 14-15 Sep, among them 42 of
    # run 506's, counted in no column and reported on no screen. Ringing earlier
    # than the spread asked beats not ringing, and the search stays inside
    # [floor, wanted) so it can no more dial before the window opens, before the
    # clock, or past a two-call lead's deadline than the forward one could.
    for earlier in range(min(wanted, end) - 1, floor - 1, -1):
        if load.get(earlier, 0) < dcfg.max_per_minute:
            return earlier
    return None


# ---------------------------------------------------------------------------
# Manual selection
# ---------------------------------------------------------------------------

def manual_pairs(
    leads: Iterable[dict[str, Any]],
    now: datetime,
    config: RedConfig,
    dispositions: Sequence[str] = (),
    buckets: Sequence[str] = (),
) -> list[tuple[dict[str, Any], Decision]]:
    """Operator-chosen leads, forced to schedule today.

    Bypasses the `auto_dispositions` allow-list and the cadence guards — that is
    what the manual screen is for — but NEVER the exclusion list. `do_not_call`,
    `wrong_number` and friends are dropped here, server-side, whatever the UI
    asked for. That is TRAI/NCPR, not a preference.
    """
    wanted_disp = {str(d).strip().lower() for d in dispositions if str(d).strip()}
    wanted_buckets = {str(b).strip().upper() for b in buckets if str(b).strip()}
    today = now.date()
    out: list[tuple[dict[str, Any], Decision]] = []

    for lead in leads:
        stage = str(lead.get("stage") or lead.get("disposition") or "").strip().lower()
        if wanted_disp and stage not in wanted_disp:
            continue
        klass, _rule = classify_disposition(stage, config)
        if klass == EXCLUDED:
            continue                       # regulatory, not negotiable
        red = parse_red(lead.get("red") or lead.get("red_date"),
                        month_first=lead.get("campaign_month_first"))
        dte = days_to_expiry(red, today)
        if dte is None:
            continue

        if dte in config.mandatory_days:
            bucket, label = "M0", MANDATORY_LABEL
        else:
            window = config.window_for(dte)
            if window is not None:
                bucket, label = window.bucket, window.label
            elif klass == CALLBACK:
                bucket, label = "D0", f"Manual only ({stage})"
            else:
                continue                   # outside every window and not a callback
        if wanted_buckets and bucket not in wanted_buckets:
            continue

        out.append((lead, Decision(
            action=SCHEDULE, reason=f"{SCHEDULE} manual disposition={stage} bucket={bucket}",
            schedule=True, bucket=bucket, bucket_label=label, dte=dte,
            disposition_class=klass, slots=1, trigger="manual",
            next_call_dates=(today,), meta={"stage": stage},
        )))
    return out
