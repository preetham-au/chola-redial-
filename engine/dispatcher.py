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
  2. TWO SLOTS  — F5/E0/F6 (`calls_per_day == 2`) get slot_no 1 and 2, with slot 2
                  at least `same_day_gap_hours` later. If that does not fit
                  inside the window we emit slot 1 only; a call outside the
                  window is worse than a call not made.
  3. ROTATION   — a lead dialled yesterday at 09:00 is not dialled at 09:00
                  today. Ported from schedule_redials.py: today's minute-of-day
                  is (last call's minute + shift_from_last_hours) wrapped into
                  the window. Leads with no history are spread uniformly.
  4. STAGGER    — at most `max_per_minute` calls share a minute; overflow moves
                  to the next free minute, still inside the window.
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
    "parse_hhmm", "validate_dial_window", "dispatch_config_from_body",
    "red_config_from_body", "dispatch", "manual_pairs",
]

# Regulatory / operational clamp. Nothing may be dialled outside these hours,
# whatever a saved config says, so it is enforced here as well as at the API
# boundary — the API is not the only caller.
WINDOW_FLOOR = 9 * 60      # 09:00
WINDOW_CEIL = 19 * 60      # 19:00


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
    """Return (start_min, end_min) or raise ValueError. 09:00-19:00, start < end."""
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


@dataclass(frozen=True)
class DispatchConfig:
    """The scheduling half of a saved config (the engine owns the other half)."""

    start_min: int = 9 * 60 + 30
    end_min: int = 19 * 60
    shift_from_last_hours: float = 2.0
    same_day_gap_hours: float = 3.0
    max_per_minute: int = 12       # 0 = unlimited
    max_per_run: int = 5000        # 0 = unlimited

    @property
    def span(self) -> int:
        return self.end_min - self.start_min


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
    start_min, end_min = validate_dial_window(window.get("start", "09:30"),
                                              window.get("end", "19:00"))

    def num(key: str, default: float, minimum: float) -> float:
        raw = body.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number, got {raw!r}") from None
        if value < minimum:
            raise ValueError(f"{key} must be at least {minimum}")
        return value

    return DispatchConfig(
        start_min=start_min, end_min=end_min,
        shift_from_last_hours=num("shift_from_last_hours", 2.0, 0.0),
        same_day_gap_hours=num("same_day_gap_hours", 3.0, 0.0),
        max_per_minute=int(num("max_per_minute", 12, 0)),
        max_per_run=int(num("max_per_run", 5000, 0)),
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

def _lead_key(lead: dict[str, Any]) -> str:
    """Stable tiebreak so two runs over the same data produce the same plan."""
    for name in ("lead_uuid", "id", "warehouse_lead_id", "policy_no", "contact_id"):
        if lead.get(name) not in (None, ""):
            return str(lead[name])
    return ""


def _slots_for(bucket: str, config: RedConfig) -> int:
    """How many calls this bucket wants today (1, or 2 for the intensive windows)."""
    for window in config.frequency_table:
        if window.bucket == bucket:
            if window.intensive:
                return max(1, min(window.calls_per_day, config.calls_per_day_cap))
            return 1
    return 1


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
    plan generated at 15:00 emits its whole first wave from 09:30, i.e. entirely
    in the past, and every one of those slots is undialable the moment it lands.
    """
    ordered = [(lead, dec) for lead, dec in pairs if dec.schedule]
    ordered.sort(key=lambda p: (config.priority_of(p[1].bucket), _lead_key(p[0])))

    dropped = 0
    if dcfg.max_per_run and len(ordered) > dcfg.max_per_run:
        dropped = len(ordered) - dcfg.max_per_run
        ordered = ordered[:dcfg.max_per_run]

    start = dcfg.start_min if floor_min is None else max(dcfg.start_min, int(floor_min))
    # Span drives the rotation modulo below; it has to shrink with the floor or a
    # rotated slot can be pushed back past the end of the remaining window.
    span = max(1, dcfg.end_min - start)
    shift_min = int(round(dcfg.shift_from_last_hours * 60))

    # --- rule 3: rotation ---------------------------------------------------
    desired: list[Optional[int]] = [None] * len(ordered)
    for index, (lead, _dec) in enumerate(ordered):
        last = parse_timestamp(lead.get("last_interaction_time") or lead.get("last_called_at"))
        if last is not None and shift_min > 0:
            base = last.hour * 60 + last.minute + shift_min
            desired[index] = start + (base - start) % span

    blank = [i for i, m in enumerate(desired) if m is None]
    for k, index in enumerate(blank):
        desired[index] = start if len(blank) < 2 else start + int(round(k * span / (len(blank) - 1)))

    # --- rules 1 + 4: place slot 1 in priority order, staggered -------------
    load: dict[int, int] = {}
    result = DispatchResult(slots=[])
    firsts: list[tuple[int, Slot]] = []
    for index, (lead, dec) in enumerate(ordered):
        minute = _free_minute(desired[index] or start, load, dcfg, start)
        if minute is None:
            result.unplaceable += 1
            continue
        load[minute] = load.get(minute, 0) + 1
        slot = Slot(lead=lead, decision=dec, slot_no=1,
                    priority=config.priority_of(dec.bucket), minute=minute, day=day)
        result.slots.append(slot)
        firsts.append((index, slot))

    # --- rule 2: the second daily slot for F5/E0/F6 ----------------------------
    gap = int(round(dcfg.same_day_gap_hours * 60))
    for _index, first in firsts:
        if _slots_for(first.decision.bucket, config) < 2:
            continue
        # An intensive bucket says "up to two a day"; the disposition says whether
        # THIS lead has earned the second one. A lead already reached does not
        # need chasing again the same afternoon.
        if not config.wants_second_call(first.lead.get("stage")):
            continue
        wanted = first.minute + gap
        if wanted > dcfg.end_min:
            continue                      # will not fit today: slot 1 only
        minute = _free_minute(wanted, load, dcfg, start)
        if minute is None:
            continue
        load[minute] = load.get(minute, 0) + 1
        result.slots.append(Slot(lead=first.lead, decision=first.decision, slot_no=2,
                                 priority=first.priority, minute=minute, day=day))

    result.dropped = dropped
    result.slots.sort(key=lambda s: (s.minute, s.priority, _lead_key(s.lead), s.slot_no))
    return result


def _free_minute(wanted: int, load: dict[int, int], dcfg: DispatchConfig,
                 floor_min: Optional[int] = None) -> Optional[int]:
    """First minute >= `wanted` under the per-minute ceiling, or None."""
    minute = max(wanted, dcfg.start_min if floor_min is None else floor_min)
    if not dcfg.max_per_minute:
        return minute if minute <= dcfg.end_min else None
    while minute <= dcfg.end_min:
        if load.get(minute, 0) < dcfg.max_per_minute:
            return minute
        minute += 1
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
