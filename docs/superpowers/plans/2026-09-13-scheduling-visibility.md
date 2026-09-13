# Scheduling Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the day screen tell the truth about scheduling — which wave dials into which hours, which language is which, what is happening while it dials, whether a call was already placed, and whether anything was silently left undialled.

**Architecture:** Every change is reporting or scoping over rows the system already writes (`runs`, `plan_items`, `decisions`, `dial_log`). The one behavioural change is wave time bands, implemented by narrowing the frozen `DispatchConfig` the dispatcher already receives — no dispatcher change. The progress bar is purely a browser-side loop over the existing per-campaign approve.

**Tech Stack:** FastAPI + SQLite (`api/`), pure-Python engine (`engine/`), React + TypeScript + Vite (`web/`), pytest, no ORM.

**Spec:** `docs/superpowers/specs/2026-09-13-scheduling-visibility-design.md`

## Global Constraints

- Repo is `D:\Users\WorkUser\Desktop\choal apps\redial` — **NOT** the session cwd. Every bash command must `cd "D:/Users/WorkUser/Desktop/choal apps/redial"` first; the shell cwd resets between calls.
- **`DRY_RUN` is `0` on the production VM — dialling is live.** Nothing in this plan may be verified by pressing Approve on production.
- **No campaign filter, ever.** A newly created campaign must auto-arm and appear with zero configuration. Scoping is by `agent_id`, which every campaign already carries.
- **No hardcoded values** (CLAUDE.md): language labels come from the `AGENT_LANGUAGES` environment variable, never from frontend code.
- `ARMED` (`api/day.py:66`) is deliberately one string because the roster question is asked in three places. Keep it single-source — widen it through a helper, never by copying the literal.
- Environment variables follow the house pattern `(os.environ.get("X") or "default").strip()` (see `api/autopilot.py:43`).
- Omitting a new optional parameter must leave every endpoint behaving exactly as it does today. Every task adds a regression test asserting this.
- `python -m pytest tests -q` must pass. `tests/test_api.py::test_approve_under_dry_run_simulates_and_never_dials` is a known afternoon-only flake on clean `main` (task `task_32229e3f`) — not caused by this work.
- Frontend gate: `npx tsc --noEmit && npm run build` from `web/`.
- Commit after every task. Prefixes: `feat:`, `fix:`, `chore:`.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `api/day.py` | the daily gate: roster, bands, prepare, approve, day view | 1,2,3,6,7 |
| `api/routes_core.py` | `list_agents` gains a language label | 3 |
| `web/src/lib/types.ts` | `DayView` / `Agent` shapes | 1,3,6,7 |
| `web/src/lib/api.ts` | client calls gain `agent_id` | 3 |
| `web/src/screens/Today.tsx` | the day screen, panels, approve modal | 1,4,5,6,7 |
| `web/src/components/DayProgress.tsx` | **new** — the live dial progress bar | 5 |
| `web/src/styles/app.css` | progress bar styling | 5 |
| `tests/test_day.py` | **new** — bands, scoping, stranded, spread | 1,2,3,6,7 |
| `web/src/selfcheck.tsx` | render checks | 4,5 |
| `docs/API_CONTRACT.md` | the new fields and parameters | 3,7 |

---

### Task 1: Stranded warning — runs prepared but never dialled

Yesterday 8 campaigns holding 491 slots were left `planned` and nothing said so. This is the highest-value, smallest change, so it ships first.

**Files:**
- Modify: `api/day.py` (add `_stranded`, call it in `get_day`)
- Modify: `web/src/lib/types.ts` (`DayView.stranded`)
- Modify: `web/src/screens/Today.tsx` (render the warning)
- Test: `tests/test_day.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `_stranded(conn: sqlite3.Connection, day: date) -> list[dict]`, each `{"campaign_id": int, "name": str, "run_date": str, "kind": str, "slots": int}`. `get_day` returns them under the key `stranded`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_day.py`:

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q
```

Expected: FAIL — `KeyError: 'stranded'`.

- [ ] **Step 3: Add `_stranded` to `api/day.py`**

Insert directly after `_dialled_today` (which ends at `api/day.py:240`):

```python
def _stranded(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    """Runs prepared on an EARLIER day and never dialled.

    A run stays `planned` until somebody approves it. On 12 Sep 2026 eight
    campaigns holding 491 slots sat like that until the day ended, and no screen
    in this console said so -- the day view only ever looked at the date it was
    asked about. Those leads were not dropped, re-queued or reported; they simply
    did not get called.

    Bounded to the last 14 days: older than that the leads have been re-planned
    several times over and the row is history, not a thing to act on.
    """
    since = (day - timedelta(days=14)).isoformat()
    rows = conn.execute(
        f"SELECT r.campaign_id, c.name, r.run_date, r.kind, r.slots "
        f"FROM runs r JOIN campaigns c ON c.id=r.campaign_id "
        f"WHERE r.status='planned' AND r.run_date < ? AND r.run_date >= ? "
        f"AND r.slots > 0 AND c.{ARMED} "
        f"ORDER BY r.run_date DESC, r.campaign_id", (day.isoformat(), since)).fetchall()
    return [{"campaign_id": r["campaign_id"], "name": r["name"], "run_date": r["run_date"],
             "kind": r["kind"], "slots": r["slots"]} for r in rows]
```

Add `timedelta` to the datetime import at `api/day.py:28`:

```python
from datetime import date, timedelta
```

- [ ] **Step 4: Call it from `get_day` and return it**

In `get_day`, inside the `with session() as conn:` block, directly after
`log = _dialled_today(conn, day)` (`api/day.py:282`):

```python
        stranded_runs = _stranded(conn, day)
```

Then in the returned dict, directly after the `"dial_log": log,` line (`api/day.py:371`):

```python
        # Plans from earlier days that nobody ever approved. Not history: those
        # leads were never called and nothing else in this console says so.
        "stranded": stranded_runs,
```

- [ ] **Step 5: Run the tests — they must pass**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Sabotage-verify**

Temporarily change `r.run_date < ?` to `r.run_date <= ?` in `_stranded` and re-run.
Expected: `test_stranded_ignores_today_and_committed_runs` FAILS.
Revert the change and confirm both pass again.

- [ ] **Step 7: Add the type**

In `web/src/lib/types.ts`, inside `interface DayView` (ends at line 237), after the `dial_log` field:

```typescript
  /** Plans from earlier days that were never approved. Those leads were never
   *  called — nothing else in this console reports them. */
  stranded: StrandedRun[];
```

And above `interface DayView` (line 207):

```typescript
export interface StrandedRun {
  campaign_id: number;
  name: string;
  run_date: string;
  kind: string;
  slots: number;
}
```

- [ ] **Step 8: Render the warning**

In `web/src/screens/Today.tsx`, add this component directly above `function Stopped` (line 848):

```tsx
/** Plans nobody ever approved. Left alone, this is silent: the run stays
 *  `planned` for ever and those leads are simply never called. */
function Stranded({ day }: { day: DayView }) {
  if (day.stranded.length === 0) return null;
  const calls = day.stranded.reduce((s, r) => s + r.slots, 0);
  const campaigns = new Set(day.stranded.map((r) => r.campaign_id)).size;
  return (
    <div className="warnbox">
      <AlertTriangle />
      <span>
        <b>
          {n(calls)} {calls === 1 ? 'call was' : 'calls were'} planned on {campaigns}{' '}
          {campaigns === 1 ? 'campaign' : 'campaigns'} and never dialled.
        </b>{' '}
        {day.stranded
          .slice(0, 4)
          .map((r) => `${r.name} · ${r.run_date} ${r.kind === 'auto' ? 'morning' : 'afternoon'} (${n(r.slots)})`)
          .join(', ')}
        {day.stranded.length > 4 && ` and ${day.stranded.length - 4} more`}. Those leads return
        in a later plan; they were not called on the day they were planned for.
      </span>
    </div>
  );
}
```

Render it in `Today()`, directly after the `{day.error && …}` block closes (line 153) and before `<Headline`:

```tsx
      {d && <Stranded day={d} />}
```

- [ ] **Step 9: Typecheck and build**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

Expected: no errors.

- [ ] **Step 10: Commit**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add api/day.py tests/test_day.py web/src/lib/types.ts web/src/screens/Today.tsx && git commit -m "feat(day): warn about plans prepared on earlier days and never dialled"
```

---

### Task 2: Waves become real time bands

Morning and afternoon are labels with no relationship to the clock: on 12 Sep the "morning" wave put calls out between 12:00 and 20:00. A band is a narrowed `DispatchConfig`, so the dispatcher is untouched.

**Files:**
- Modify: `api/day.py` (`WAVE_BOUNDARY`, `_band`, `_day_window`, `_prepare_one`, `_approve_one`)
- Test: `tests/test_day.py`

**Interfaces:**
- Consumes: `_stranded` from Task 1 (only as a neighbour in the same file).
- Produces: `_band(kind: str, dcfg: DispatchConfig) -> DispatchConfig`, and `_day_window(configs, ready, floor, today, kind)` — note the **new fifth positional parameter `kind`**, which Tasks 6 and 7 must keep passing.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_day.py`:

```python
from dataclasses import replace

from api.day import WAVE_BOUNDARY, _band
from engine.dispatcher import DispatchConfig


def test_band_clips_morning_to_the_first_half_of_the_day():
    dcfg = DispatchConfig(start_min=9 * 60, end_min=20 * 60)
    band = _band("auto", dcfg)
    assert band.start_min == 9 * 60, "morning keeps the campaign's own opening"
    assert band.end_min == WAVE_BOUNDARY, "morning must stop at the boundary"


def test_band_clips_afternoon_to_the_second_half_of_the_day():
    dcfg = DispatchConfig(start_min=9 * 60, end_min=20 * 60)
    band = _band("auto_pm", dcfg)
    assert band.start_min == WAVE_BOUNDARY, "afternoon must not start before the boundary"
    assert band.end_min == 20 * 60, "afternoon keeps the campaign's own close"


def test_band_never_widens_a_narrow_campaign_window():
    """A campaign that shuts at 13:00 has no afternoon at all."""
    dcfg = DispatchConfig(start_min=10 * 60, end_min=13 * 60)
    morning = _band("auto", dcfg)
    assert (morning.start_min, morning.end_min) == (10 * 60, 13 * 60), \
        "the band must never open earlier or close later than the campaign itself"
    afternoon = _band("auto_pm", dcfg)
    assert afternoon.start_min >= afternoon.end_min, \
        "an empty band is how 'this wave cannot run here' is expressed"


def test_band_leaves_other_config_untouched():
    dcfg = DispatchConfig(start_min=9 * 60, end_min=20 * 60, max_per_minute=7, max_per_run=99)
    band = _band("auto", dcfg)
    assert band.max_per_minute == 7 and band.max_per_run == 99
    assert band.red_priority == dcfg.red_priority
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q -k band
```

Expected: FAIL — `ImportError: cannot import name 'WAVE_BOUNDARY' from 'api.day'`.

- [ ] **Step 3: Add the boundary and `_band`**

In `api/day.py`, add to the imports at the top (after line 26, `import logging`):

```python
import os
from dataclasses import replace
```

and extend the engine import at line 34:

```python
from engine.dispatcher import (
    DEFAULT_RED_PRIORITY, DispatchConfig, hhmm, parse_hhmm, red_rank,
)
```

Then insert this block directly below `WAVE_LABEL` (`api/day.py:60`):

```python
# Where the morning stops and the afternoon starts. One boundary for the whole
# console, not one per campaign: the screen has to be able to SAY which band it
# is approving ("the morning band, 09:00-13:30"), and a per-campaign boundary
# makes that sentence unwritable.
#
# Until 13 Sep 2026 the two waves were labels with no clock behind them. Approve
# re-plans from the current minute, so the morning wave approved at noon dialled
# into the evening -- on 12 Sep the `auto` wave's calls landed between 12:00 and
# 20:00 and the `auto_pm` wave's between 13:00 and 20:00, which is the same day
# twice. The band is what makes the name true.
#
# 13:30 sits between autopilot's own two preparation times (AUTOPILOT_AM 10:00,
# AUTOPILOT_PM 15:00, see autopilot.py) so each wave is still prepared inside the
# band it dials into.
WAVE_BOUNDARY = parse_hhmm((os.environ.get("WAVE_BOUNDARY") or "13:30").strip())
WAVE_BAND = {MORNING: (None, WAVE_BOUNDARY), AFTERNOON: (WAVE_BOUNDARY, None)}


def _band(kind: str, dcfg: DispatchConfig) -> DispatchConfig:
    """The campaign's own dial window, clipped to this wave's half of the day.

    Narrowing the config is the whole implementation: `dispatch` already receives
    a DispatchConfig and honours start_min/end_min, so nothing in the dispatcher
    or in `_write_run` needs to know a band exists.

    CLIPPING, never widening. A campaign that shuts at 13:00 gets an afternoon
    band whose start is at or past its end -- an empty band, which the existing
    `floor >= end_min` guard already reports as `window_closed`.
    """
    lo, hi = WAVE_BAND[kind]
    return replace(dcfg,
                   start_min=max(dcfg.start_min, lo if lo is not None else 0),
                   end_min=min(dcfg.end_min, hi if hi is not None else 24 * 60))
```

- [ ] **Step 4: Run the band tests — they must pass**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q -k band
```

Expected: `4 passed`.

- [ ] **Step 5: Commit the helper**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add api/day.py tests/test_day.py && git commit -m "feat(day): add wave time bands, clipping each campaign's window to its half of the day"
```

- [ ] **Step 6: Write the failing test for using the band**

Append to `tests/test_day.py`:

```python
def test_prepare_reports_the_band_that_closed_not_the_whole_window(monkeypatch):
    """Preparing the morning wave after the boundary must name the BAND.

    Before bands, this said "the 09:00-20:00 window has closed" only after 20:00,
    and happily planned a 'morning' wave into the evening at any hour before it.
    """
    import api.day as day_module

    class _Evening:
        """now_ist() pinned past the boundary, on today's date."""
        @staticmethod
        def __call__():
            real = day_module.now_ist()
            return real.replace(hour=15, minute=0, second=0, microsecond=0)

    monkeypatch.setattr(day_module, "now_ist", _Evening())
    out = day_module.prepare_day(date.today(), "auto")
    closed = [c for c in out["campaigns"] if c["status"] == "window_closed"]

    assert closed, "the morning band is shut at 15:00 — every campaign must say so"
    assert "13:30" in closed[0]["detail"], \
        f"the detail must name the band, got {closed[0]['detail']!r}"
    assert "20:00" not in closed[0]["detail"], \
        "naming the full window hides the fact that the morning band is what shut"
```

- [ ] **Step 7: Run it and watch it fail**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q -k band_that_closed
```

Expected: FAIL — either no campaign is `window_closed`, or the detail reads `09:00-20:00`.

- [ ] **Step 8: Use the band in `_prepare_one`**

Replace `api/day.py:445-454` (the `try:` block up to and including the `_write_run` call) with:

```python
        try:
            cfg, red, dcfg, _now, leads, pairs = _evaluate(conn, campaign, day)
            # The wave's half of the day, not the campaign's whole window: a
            # 'morning' plan that dials at 19:00 is not a morning plan.
            dcfg = _band(kind, dcfg)
            floor = _floor_min(now_ist(), day, dcfg)
            if floor is not None and floor >= dcfg.end_min:
                return {**out, "status": "window_closed",
                        "detail": f"the {hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)} "
                                  f"{WAVE_LABEL[kind]} band has closed"}
            run_id = _write_run(conn, campaign, day, kind, cfg["version"], pairs, red, dcfg,
                                evaluated=len(leads), note=f"{WAVE_LABEL[kind]} plan, awaiting "
                                                           f"approval", floor_min=floor)
```

Note the empty-band case: when `_band` returns `start_min >= end_min` on a past
date, `_floor_min` returns `None` and the guard does not fire. Add the explicit
check immediately above the `floor` line:

```python
            if dcfg.start_min >= dcfg.end_min:
                return {**out, "status": "window_closed",
                        "detail": f"this campaign's window has no "
                                  f"{WAVE_LABEL[kind]} band"}
```

- [ ] **Step 9: Use the band in `_approve_one`**

Replace `api/day.py:552-558` (from `try:` through the `window_closed` return) with:

```python
    try:
        cfg, red, dcfg, now, leads, pairs = _evaluate(conn, campaign, day)
        dcfg = _band(kind, dcfg)
        if dcfg.start_min >= dcfg.end_min:
            return failing("window_closed", run_id=run["id"],
                           detail=f"this campaign's window has no {WAVE_LABEL[kind]} band")
        floor = _floor_min(now, day, dcfg)
        if floor is not None and floor >= dcfg.end_min:
            return failing("window_closed", run_id=run["id"],
                           detail=f"the {hhmm(dcfg.start_min)}-{hhmm(dcfg.end_min)} "
                                  f"{WAVE_LABEL[kind]} band has closed "
                                  f"(it is {now_ist().strftime('%H:%M')})")
```

- [ ] **Step 10: Clip the reported window to the band too**

The header must name the band the operator is approving, and `capacity` must not
promise hours the wave cannot use. Change the signature at `api/day.py:163`:

```python
def _day_window(configs: dict[int, dict[str, Any]], ready: dict[int, int],
                floor: int, today: bool, kind: str) -> dict[str, Any]:
```

Inside it, replace the two `parse_hhmm` lines (`api/day.py:191-192`) with:

```python
        # Clipped to the wave's band, so the screen names the hours this approval
        # can actually reach rather than the campaign's whole day.
        lo, hi = WAVE_BAND[kind]
        start = max(parse_hhmm(window.get("start", DEFAULT_WINDOW["start"])),
                    lo if lo is not None else 0)
        end = min(parse_hhmm(window.get("end", DEFAULT_WINDOW["end"])),
                  hi if hi is not None else 24 * 60)
```

The `room` calculation below already reads `max(0, end - max(start, floor))`, so
an empty band contributes zero capacity with no further change.

Update the call site at `api/day.py:342`:

```python
    span = _day_window(configs, {c["id"]: c["ready"] for c in listed},
                       first_free.hour * 60 + first_free.minute, today, kind)
```

- [ ] **Step 11: Run the whole suite**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests -q
```

Expected: all pass (except the known afternoon flake noted in Global Constraints).

- [ ] **Step 12: Sabotage-verify**

Temporarily change `_band` to `return dcfg` and re-run `python -m pytest tests/test_day.py -q`.
Expected: the three clipping tests and `test_prepare_reports_the_band_that_closed_not_the_whole_window` FAIL.
Revert and confirm they pass.

- [ ] **Step 13: Commit**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add api/day.py tests/test_day.py && git commit -m "feat(day): dial each wave into its own band instead of the whole window"
```

---

### Task 3: Scope the day by agent

Agents 125 and 127 hold mirrored campaigns in two languages and the day screen adds their numbers together. Scoping is by agent, which every campaign already carries — **not** a campaign filter.

**Files:**
- Modify: `api/day.py` (`_armed`, `get_day`, `PrepareBody`, `ApproveBody`, `prepare_day`, `approve_day`)
- Modify: `api/routes_core.py:186-200` (`list_agents` gains `language`)
- Modify: `web/src/lib/api.ts:241-253`
- Modify: `web/src/lib/types.ts` (`Agent.language`)
- Modify: `docs/API_CONTRACT.md`
- Test: `tests/test_day.py`

**Interfaces:**
- Consumes: `_band` (Task 2) — unchanged by this task.
- Produces: `_armed(agent_id: Optional[int]) -> tuple[str, list]` returning an SQL fragment and its parameters; `GET /api/day?agent_id=`, `PrepareBody.agent_id`, `ApproveBody.agent_id`, all `Optional[int] = None`; `GET /api/agents` gains `language: str | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_day.py`:

```python
def _two_armed_agents() -> tuple[int, int]:
    """Two agent ids that each own at least one armed campaign."""
    with session() as conn:
        rows = conn.execute(
            "SELECT DISTINCT agent_id FROM campaigns WHERE autopilot=1 AND enabled=1 "
            "AND paused=0 AND hidden=0 ORDER BY agent_id").fetchall()
    if len(rows) < 2:
        pytest.skip("fixture DB has fewer than two armed agents")
    return int(rows[0]["agent_id"]), int(rows[1]["agent_id"])


def test_day_scoped_to_one_agent_excludes_the_other(client):
    first, second = _two_armed_agents()
    body = client.get(f"/api/day?agent_id={first}").json()

    assert body["campaigns"], "the scoped agent must still have its campaigns"
    assert all(c["agent_id"] == first for c in body["campaigns"]), \
        "a scoped day must never show another agent's campaigns"
    assert all(c["agent_id"] != second for c in body["campaigns"])


def test_day_without_agent_id_is_unchanged(client):
    """Omitting the parameter must behave exactly as it did before scoping."""
    first, second = _two_armed_agents()
    whole = client.get("/api/day").json()
    ids = {c["campaign_id"] for c in whole["campaigns"]}

    for agent in (first, second):
        part = client.get(f"/api/day?agent_id={agent}").json()
        assert {c["campaign_id"] for c in part["campaigns"]} <= ids

    assert whole["totals"]["campaigns"] == len(whole["campaigns"])


def test_day_for_an_agent_with_nothing_armed_is_empty_not_an_error(client):
    res = client.get("/api/day?agent_id=999999")
    assert res.status_code == 200, "a quiet agent is a real state, not a 404"
    assert res.json()["campaigns"] == []
    assert res.json()["status"] == "no_campaigns"


def test_agents_carry_a_language_label(client, monkeypatch):
    monkeypatch.setenv("AGENT_LANGUAGES", "125:Hindi,127:Tamil")
    import importlib

    import api.routes_core as core
    importlib.reload(core)

    labels = {a["agent_id"]: a.get("language") for a in core.list_agents()}
    assert labels.get(125) == "Hindi" or 125 not in labels
    assert labels.get(127) == "Tamil" or 127 not in labels
```

Note: these tests read `c["agent_id"]` off each day campaign. That field is
already there — `_campaign_json` emits it at `api/routes_core.py:146` and
`DayCampaign extends Campaign` in `web/src/lib/types.ts:197` — so no new field is
needed for the assertion to work.

- [ ] **Step 2: Run it and watch it fail**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q -k "agent or language"
```

Expected: FAIL — the scoped call returns every campaign.

- [ ] **Step 3: Add the `_armed` helper**

`ARMED` stays the single source of the roster question. Add directly below it
(`api/day.py:66`):

```python
def _armed(agent_id: Optional[int] = None) -> tuple[str, list[Any]]:
    """The roster clause, optionally narrowed to one agent.

    Agent scoping is NOT a campaign filter: a campaign carries its agent already,
    so a newly created one still auto-arms and appears under its own agent with
    nothing to configure. What it buys is two languages that stop being one
    number -- agents 125 and 127 hold mirrored campaigns and the day screen used
    to sum them, so 4,271 Hindi slots and 481 Tamil ones were shown as 4,752.
    """
    if agent_id is None:
        return ARMED, []
    return f"{ARMED} AND agent_id=?", [agent_id]
```

- [ ] **Step 4: Thread it through `get_day`**

Change the signature at `api/day.py:243-244`:

```python
@router.get("/api/day")
def get_day(date: Optional[str] = Query(None), kind: str = Query(MORNING),
            agent_id: Optional[int] = Query(None)) -> dict[str, Any]:
```

Replace the campaigns query at `api/day.py:258`:

```python
        where, params = _armed(agent_id)
        campaigns = conn.execute(
            f"SELECT * FROM campaigns WHERE {where} ORDER BY id", params).fetchall()
```

The `stopped` query directly below (line 265) must be scoped too, or a scoped
panel would show the other language's stopped campaigns. Replace it with:

```python
        stopped_where = "(autopilot=1 OR autopilot_latched=1) AND (paused=1 OR enabled=0)"
        stopped_params: list[Any] = []
        if agent_id is not None:
            stopped_where += " AND agent_id=?"
            stopped_params = [agent_id]
        stopped = conn.execute(
            f"SELECT * FROM campaigns WHERE {stopped_where} ORDER BY id",
            stopped_params).fetchall()
```

And add `agent_id` to the response dict, directly after the `"kind": kind,` entry
(`api/day.py:346`):

```python
        "agent_id": agent_id,
```

- [ ] **Step 5: Scope `_stranded` to the same agent**

Change the signature added in Task 1:

```python
def _stranded(conn: sqlite3.Connection, day: date,
              agent_id: Optional[int] = None) -> list[dict[str, Any]]:
```

and inside it replace the `c.{ARMED}` fragment and query with:

```python
    where, params = _armed(agent_id)
    since = (day - timedelta(days=14)).isoformat()
    rows = conn.execute(
        f"SELECT r.campaign_id, c.name, r.run_date, r.kind, r.slots "
        f"FROM runs r JOIN campaigns c ON c.id=r.campaign_id "
        f"WHERE r.status='planned' AND r.run_date < ? AND r.run_date >= ? "
        f"AND r.slots > 0 AND c.id IN (SELECT id FROM campaigns WHERE {where}) "
        f"ORDER BY r.run_date DESC, r.campaign_id",
        (day.isoformat(), since, *params)).fetchall()
```

Update its call site in `get_day` to `_stranded(conn, day, agent_id)`.

- [ ] **Step 6: Scope prepare and approve**

Add to `PrepareBody` (`api/day.py:69-75`), after `resync`:

```python
    # Narrows the pass to one agent — one language. None = every armed campaign,
    # which is what the scheduled passes use.
    agent_id: Optional[int] = None
```

Add the identical field to `ApproveBody` (after `campaign_ids`, `api/day.py:85`):

```python
    agent_id: Optional[int] = None
```

Change `prepare_day`'s signature (`api/day.py:379-380`):

```python
def prepare_day(day: Optional[date] = None, kind: str = MORNING,
                resync: bool = False, agent_id: Optional[int] = None) -> dict[str, Any]:
```

and its roster query (`api/day.py:407-408`):

```python
    with session() as conn:
        where, params = _armed(agent_id)
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM campaigns WHERE {where} ORDER BY id", params)]
```

Update `post_prepare` (`api/day.py:471`):

```python
    return prepare_day(_parse_day(body.date), body.kind, body.resync, body.agent_id)
```

And `approve_day`'s roster query (`api/day.py:498`):

```python
        where, params = _armed(body.agent_id)
        campaigns = conn.execute(
            f"SELECT * FROM campaigns WHERE {where} ORDER BY id", params).fetchall()
```

- [ ] **Step 7: Give agents a language label**

In `api/routes_core.py`, directly above `@router.get("/api/agents")` (line 185):

```python
def _agent_languages() -> dict[int, str]:
    """agent id -> the language it speaks, from AGENT_LANGUAGES.

    There is no agents table and no name in the data -- `list_agents` has always
    answered "Agent 125" -- so the label has to come from configuration. It is
    NOT a frontend constant: hardcoding "125 is Hindi" in the UI puts a fact
    about this deployment in the build, and breaks the day a third agent lands.

    Format: `AGENT_LANGUAGES=125:Hindi,127:Tamil`. A malformed entry raises here,
    at import, rather than silently labelling an agent wrong.
    """
    raw = (os.environ.get("AGENT_LANGUAGES") or "").strip()
    out: dict[int, str] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        agent, _, label = part.partition(":")
        if not label.strip():
            raise ValueError(f"AGENT_LANGUAGES entry {part!r} is not `id:Language`")
        out[int(agent)] = label.strip()
    return out
```

Confirm `import os` is present at the top of `api/routes_core.py`; add it if not.

Then in `list_agents`, bind the map once and add the field:

```python
@router.get("/api/agents")
def list_agents() -> list[dict[str, Any]]:
    languages = _agent_languages()
    with session() as conn:
        return [{"agent_id": r["agent_id"], "name": f"Agent {r['agent_id']}",
                 "language": languages.get(r["agent_id"]),
                 "campaigns": r["campaigns"], "enabled": r["enabled"],
```

(the rest of the dict and the query below it are unchanged)

- [ ] **Step 8: Run the tests**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 9: Sabotage-verify**

Temporarily make `_armed` ignore its argument (`return ARMED, []` unconditionally).
Expected: `test_day_scoped_to_one_agent_excludes_the_other` FAILS.
Revert and confirm it passes.

- [ ] **Step 10: Thread `agent_id` through the API client**

In `web/src/lib/api.ts`, replace the three day methods (lines 241-253):

```typescript
  day: (date: string, kind = 'auto', agent_id?: number) =>
    req<DayView>(`/api/day${q({ date, kind, agent_id })}`, undefined,
      () => mockDay(date, kind)),

  prepareDay: (date: string, kind = 'auto', resync = false, agent_id?: number) =>
    req<PrepareResult>('/api/day/prepare', json({ date, kind, resync, agent_id }), () => {
```

(the mock body of `prepareDay` is unchanged)

```typescript
  approveDay: (
    date: string,
    kind = 'auto',
    buckets: string[] = [],
    campaign_ids: number[] = [],
    agent_id?: number,
  ) =>
    req<ApproveResult>('/api/day/approve',
      json({ date, kind, buckets, campaign_ids, agent_id }), () => {
```

(the mock body of `approveDay` is unchanged)

`q()` (`web/src/lib/api.ts:129`) already skips `undefined` and `''`, so an
unscoped call sends no `agent_id` parameter at all rather than the string
`"undefined"`. Passing `agent_id` straight through is safe.

- [ ] **Step 11: Add the types**

In `web/src/lib/types.ts`, add to `interface Agent` (line 24-32), after `name`:

```typescript
  /** From AGENT_LANGUAGES on the server. Null when the deployment has not
   *  labelled this agent. */
  language: string | null;
```

and to `interface DayView`, after `kind`:

```typescript
  /** Null when the day is not scoped to one agent. */
  agent_id: number | null;
```

- [ ] **Step 12: Typecheck and build**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

Expected: no errors. Fix any call site the new `DayView.agent_id` / `Agent.language`
fields break (mock data in `web/src/lib/mock.ts` will need both).

- [ ] **Step 13: Document it**

In `docs/API_CONTRACT.md`, add to the `GET /api/day` section: the optional
`agent_id` query parameter ("narrows the day to one agent; omitted = every armed
campaign"), the `agent_id` response field, and the `stranded` array from Task 1.
Add `agent_id` to the `POST /api/day/prepare` and `POST /api/day/approve` request
bodies, and `language` to `GET /api/agents`.

- [ ] **Step 14: Commit**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add api/day.py api/routes_core.py tests/test_day.py web/src/lib/api.ts web/src/lib/types.ts web/src/lib/mock.ts docs/API_CONTRACT.md && git commit -m "feat(day): scope the day, prepare and approve by agent; label agents by language"
```

---

### Task 4: One panel per agent on the day screen

**Files:**
- Modify: `web/src/screens/Today.tsx` (`Today` splits into a shell plus a per-agent panel)
- Modify: `web/src/selfcheck.tsx`

**Interfaces:**
- Consumes: `api.day(date, kind, agentId)` and `Agent.language` (Task 3).
- Produces: `DayPanel({ agent, date, kind })` — a self-contained panel owning its own `useAsync`, bucket selection and Approve modal. Task 5 modifies its approve path; Tasks 6 and 7 add to it.

- [ ] **Step 1: Extract the body of `Today` into `DayPanel`**

In `web/src/screens/Today.tsx`, everything `Today()` currently renders below the
page header — `Stranded`, `Headline`, `RedBands`, `Buckets`, `Campaigns`,
`Stopped` and the `ApproveDay` modal — plus the state that feeds them (`day`,
`picked`, `all`, `chosen`, `approving`, `busy`, `prepare`) moves verbatim into:

```tsx
/** One agent's half of the day. Each panel owns its own plan, its own bucket
 *  ticks and its own Approve — two languages that used to be added together
 *  into one number are now two decisions. */
function DayPanel({
  agent,
  date,
  kind,
  onPick,
}: {
  agent: Agent | null;
  date: string;
  kind: string;
  onPick: () => void;
}) {
  const toast = useStore((s) => s.toast);
  const agentId = agent?.agent_id;
  const day = useAsync(() => api.day(date, kind, agentId), [date, kind, agentId]);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [approving, setApproving] = useState(false);
  const [busy, setBusy] = useState('');

  const d = day.data;
  const all = useMemo(() => (d?.buckets ?? []).map((b) => b.bucket), [d]);
  useEffect(() => {
    setPicked((p) => (p === null ? null : p.filter((b) => all.includes(b))));
  }, [all]);
  const chosen = picked ?? all;

  const prepare = async () => {
    setBusy('prepare');
    try {
      const res = await api.prepareDay(date, kind, false, agentId);
      toast('ok', `Plan built: ${n(res.ready)} leads ready across ${res.prepared} campaigns. Nothing has been dialled.`);
      day.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="grid" style={{ gap: 18 }}>
      {agent && (
        <div className="row" style={{ gap: 8, alignItems: 'baseline' }}>
          <h2 style={{ margin: 0 }}>{agent.language ?? agent.name}</h2>
          <span className="eyebrow">
            {agent.language ? `${agent.name} · ` : ''}
            {n(d?.totals.ready ?? 0)} ready
          </span>
        </div>
      )}

      {day.error && (
        <div className="warnbox">
          <AlertTriangle />
          <span>{day.error}</span>
        </div>
      )}

      {d && <Stranded day={d} />}

      <Headline
        day={d}
        busy={busy}
        onPrepare={prepare}
        onApprove={() => setApproving(true)}
        onPick={onPick}
      />

      {d && d.status !== 'no_campaigns' && (
        <>
          <RedBands day={d} />
          <Buckets day={d} chosen={chosen} onChange={setPicked} />
          <Campaigns day={d} />
          <Stopped day={d} />
        </>
      )}

      {approving && d && (
        <ApproveDay
          day={d}
          buckets={wireBuckets(chosen, all)}
          shown={chosen}
          onClose={() => setApproving(false)}
          onDone={() => day.reload()}
        />
      )}
    </div>
  );
}
```

This is a **pure extraction** — `RedBands`, `Buckets`, `Campaigns`, `Stopped`,
`Headline` and `ApproveDay` are not modified and not moved; only the code that
*calls* them relocates from `Today` into `DayPanel`. Two rules for doing it
safely:

1. Open `web/src/screens/Today.tsx:73-203` (the current `Today`) side by side
   with the block above. Every `<Component … />` call, with **every prop it
   currently passes**, moves across unchanged. The block above is the target
   shape, not a licence to drop a prop — if the current code passes something it
   omits, the current code is right.
2. The only three deliberate differences are: `api.day` / `api.prepareDay` now
   take `agentId`, `useAsync`'s dependency array gains `agentId`, and the panel
   renders an `<h2>` naming the agent's language.

Verify the extraction changed no behaviour by diffing what moved:

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git diff -U2 web/src/screens/Today.tsx
```

Every removed line should reappear as an added line, modulo indentation and the
three differences above.

- [ ] **Step 2: Make `Today` the shell that renders one panel per agent**

```tsx
export function Today() {
  const date = useStore((s) => s.date);
  const setDate = useStore((s) => s.setDate);
  const [kind, setKind] = useState('auto');
  const [picking, setPicking] = useState(false);
  const agents = useAsync(() => api.agents(), []);

  // One panel per agent that has campaigns. Falling back to a single unscoped
  // panel keeps this screen working on a backend without /api/agents, and on a
  // deployment that only ever had one agent.
  const panels = agents.data?.length ? agents.data : [null];

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            {kind === 'auto' ? 'Morning band' : 'Afternoon band'}
          </span>
          <h1>The day</h1>
        </div>
        <div className="row" style={{ marginLeft: 'auto', gap: 8 }}>
          <input
            className="input"
            type="date"
            value={date}
            aria-label="Day"
            onChange={(e) => setDate(e.target.value)}
          />
          <div className="seg" role="group" aria-label="Wave">
            {WAVES.map((w) => (
              <button
                key={w.kind}
                className={`seg-btn${kind === w.kind ? ' is-active' : ''}`}
                onClick={() => setKind(w.kind)}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button className="btn btn-ghost" onClick={() => setPicking(true)}>
            <ListChecks /> Campaigns
          </button>
        </div>
      </div>

      {panels.map((a) => (
        <DayPanel
          key={a?.agent_id ?? 'all'}
          agent={a}
          date={date}
          kind={kind}
          onPick={() => setPicking(true)}
        />
      ))}

      {picking && <PickCampaigns onClose={() => setPicking(false)} />}
    </div>
  );
}
```

Keep whatever props `PickCampaigns` currently receives from `Today`
(`web/src/screens/Today.tsx:178`) — this sketch shows only the shape.

The per-panel Refresh button lives in `Headline`; the shell-level one is dropped
because a single button cannot reload two independent panels honestly.

Add `Agent` to the type import on line 32.

- [ ] **Step 3: Typecheck and build**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

Expected: no errors.

- [ ] **Step 4: Add selfcheck cases**

In `web/src/selfcheck.tsx`, beside the existing `ApproveDay` case (line ~337), add
a case rendering `Today` against two mock agents (one with `language: 'Hindi'`,
one `'Tamil'`) and assert both headings appear and that each panel's ready count
is its own, not the sum.

- [ ] **Step 5: Run the selfcheck**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npm run check
```

Expected: pass. (`check` bundles `src/selfcheck.tsx` with esbuild and runs it
under node — `web/package.json:10`.)

- [ ] **Step 6: Commit**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add web/src/screens/Today.tsx web/src/selfcheck.tsx && git commit -m "feat(web): one day panel per agent, each with its own plan and Approve"
```

---

### Task 5: Progress bar — approve one campaign at a time

One approve currently posts every campaign in a single request: on 12 Sep the afternoon wave sent 2,967 calls that way. Driving it campaign-by-campaign from the browser gives the progress bar **and** the queue, with no backend change.

**Files:**
- Create: `web/src/components/DayProgress.tsx`
- Modify: `web/src/screens/Today.tsx` (`ApproveDay.submit`)
- Modify: `web/src/styles/app.css`
- Modify: `web/src/selfcheck.tsx`

**Interfaces:**
- Consumes: `api.approveDay(date, kind, buckets, campaign_ids, agent_id)` (Task 3), `ApproveResult` and `DialResult` (already shipped).
- Produces: `DayProgress({ done, total, current, rows })` where `rows: Array<{ campaign_id: number; name: string; state: 'done' | 'failed' | 'running' }>`.

- [ ] **Step 1: Create the progress component**

`web/src/components/DayProgress.tsx`:

```tsx
import { AlertTriangle, Check, Loader2 } from 'lucide-react';
import { n } from '../lib/domain';

export interface ProgressRow {
  campaign_id: number;
  name: string;
  state: 'done' | 'failed' | 'running';
}

/** What is happening RIGHT NOW, while the day is being dialled.
 *
 *  This is not the result bar. The result bar says what went out once the whole
 *  day is finished; this one moves campaign by campaign while it is still going,
 *  because a single approve used to post thousands of calls in one blocking
 *  request with nothing on screen but a spinner. */
export function DayProgress({
  done,
  total,
  current,
  rows,
}: {
  done: number;
  total: number;
  current: string | null;
  rows: ProgressRow[];
}) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  return (
    <>
      <div className="dialbar">
        <div className="dialbar-seg is-scheduled" style={{ width: `${pct}%` }} />
      </div>
      <div className="dialbar-keys">
        <span className="dialbar-key">
          <b>
            {n(done)} of {n(total)} campaigns
          </b>
        </span>
        {current && <span className="dialbar-key trunc">Dialling {current}…</span>}
      </div>
      <div className="grid" style={{ gap: 0, marginTop: 4 }}>
        {rows.map((r) => (
          <div className="dialrow" key={r.campaign_id}>
            {r.state === 'running' ? (
              <Loader2 className="spin" size={14} style={{ flex: '0 0 auto' }} />
            ) : r.state === 'failed' ? (
              <AlertTriangle size={14} style={{ color: 'var(--bad)', flex: '0 0 auto' }} />
            ) : (
              <Check size={14} style={{ color: 'var(--ok)', flex: '0 0 auto' }} />
            )}
            <b className="trunc">{r.name}</b>
          </div>
        ))}
      </div>
    </>
  );
}
```

- [ ] **Step 2: Drive approve campaign by campaign**

In `web/src/screens/Today.tsx`, replace `submit` (lines 900-909) with:

```tsx
  const [progress, setProgress] = useState<ProgressRow[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const stop = useRef(false);

  /** One campaign per request, in order, waiting for each.
   *
   *  Sending every campaign in one approve posted 2,967 calls in a single
   *  request on 12 Sep 2026 — one timeout away from losing the whole day, with
   *  nothing on screen while it ran. Twelve requests of ~250 is the same work,
   *  queued, and it is what makes the progress bar possible at all. */
  const submit = async () => {
    setBusy(true);
    stop.current = false;
    const targets = day.campaigns.filter((c) => c.run_status === 'planned');
    const merged: ApproveResult[] = [];
    try {
      for (const c of targets) {
        if (stop.current) break;
        setCurrent(c.name);
        setProgress((p) => [...p, { campaign_id: c.campaign_id, name: c.name, state: 'running' }]);
        try {
          const out = await api.approveDay(
            day.date, day.kind, buckets, [c.campaign_id], day.agent_id ?? undefined,
          );
          merged.push(out);
          const ok = out.campaigns.every((r) => r.status === 'approved' && !r.failed);
          setProgress((p) =>
            p.map((r) =>
              r.campaign_id === c.campaign_id ? { ...r, state: ok ? 'done' : 'failed' } : r,
            ),
          );
        } catch (e) {
          // One campaign failing must not end the day for the rest.
          toast('bad', `${c.name}: ${(e as Error).message}`);
          setProgress((p) =>
            p.map((r) => (r.campaign_id === c.campaign_id ? { ...r, state: 'failed' } : r)),
          );
        }
      }
      setRes(mergeResults(merged, day));
    } finally {
      setCurrent(null);
      setBusy(false);
    }
  };
```

Add `useRef` to the React import at the top of the file, and import
`DayProgress`, plus `type ProgressRow`, from `'../components/DayProgress'`.

- [ ] **Step 3: Add the merge helper**

Directly above `export function ApproveDay` (line 868):

```tsx
/** Fold the per-campaign approves back into the one result the bar expects.
 *
 *  Every request covered a different campaign, so the totals are pure addition
 *  and no campaign can appear twice. An empty list — every campaign stopped
 *  before it started — still has to produce a valid result, or the modal has
 *  nothing to show. */
export function mergeResults(parts: ApproveResult[], day: DayView): ApproveResult {
  const base: ApproveResult = {
    date: day.date,
    kind: day.kind,
    wave: day.wave,
    dry_run: day.dry_run,
    buckets: 'all',
    approved: 0,
    posted: 0,
    failed: 0,
    not_dialled: 0,
    campaigns: [],
  };
  return parts.reduce(
    (acc, p) => ({
      ...acc,
      buckets: p.buckets,
      approved: acc.approved + p.approved,
      posted: acc.posted + p.posted,
      failed: acc.failed + p.failed,
      not_dialled: acc.not_dialled + p.not_dialled,
      campaigns: [...acc.campaigns, ...p.campaigns],
    }),
    base,
  );
}
```

- [ ] **Step 4: Show the bar and a Stop button while it runs**

In `ApproveDay`'s returned JSX, directly above the `{live ? … : …}` box (line 946):

```tsx
      {busy && (
        <DayProgress
          done={progress.filter((r) => r.state !== 'running').length}
          total={day.campaigns.filter((c) => c.run_status === 'planned').length}
          current={current}
          rows={progress}
        />
      )}
```

and replace the modal footer's Cancel button (line 934) with:

```tsx
          <button className="btn btn-ghost" onClick={() => (busy ? (stop.current = true) : onClose())}>
            {busy ? 'Stop after this campaign' : 'Cancel'}
          </button>
```

Stopping leaves the campaigns it never reached `planned` — they stay on the
screen and stay approvable. Closing the tab does exactly the same thing.

- [ ] **Step 5: Style the running segment**

In `web/src/styles/app.css`, beside the existing `.dialbar-seg` rules, add:

```css
/* The progress bar reuses the result bar's track. While it is still moving the
   filled part is animated so a long campaign does not read as a hang. */
.dialbar-seg.is-scheduled { transition: width .3s ease; }
```

- [ ] **Step 6: Typecheck and build**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

Expected: no errors.

- [ ] **Step 7: Add selfcheck cases**

In `web/src/selfcheck.tsx`, add three cases: `DayProgress` at 2 of 5 renders a
40% bar and names the current campaign; a row with `state: 'failed'` renders the
warning icon; `mergeResults([], mockDay(...))` returns zero totals and an empty
campaign list without throwing.

- [ ] **Step 8: Commit**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add web/src/components/DayProgress.tsx web/src/screens/Today.tsx web/src/styles/app.css web/src/selfcheck.tsx && git commit -m "feat(web): dial one campaign at a time with a live progress bar and a stop button"
```

---

### Task 6: Pre-dial check — was a call already placed?

The engine already skips leads with `queued_today > 0`, but plans sat six hours before approval on 12 Sep, so anything booked in Formi afterwards was invisible to it.

**Files:**
- Modify: `api/day.py` (`_plan_facts`, `get_day`)
- Modify: `web/src/lib/types.ts` (`DayCampaign`)
- Modify: `web/src/screens/Today.tsx` (`ApproveDay` facts + re-check)
- Test: `tests/test_day.py`

**Interfaces:**
- Consumes: `_armed` (Task 3), `DayPanel` (Task 4), `api.prepareDay(date, kind, resync, agent_id)` (Task 3).
- Produces: three new fields on each entry of `DayView.campaigns` — `plan_built_at: string | null`, `last_dialled: string | null`, `already_booked: number`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_day.py`:

```python
def test_already_booked_counts_leads_formi_had_already_queued(client):
    """The engine skips them at plan time; the screen has to say how many."""
    campaign_id = _armed_campaign_id()
    today = date.today().isoformat()
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
    row = next(c for c in body["campaigns"] if c["campaign_id"] == campaign_id)

    assert row["already_booked"] == 3, "only the ALREADY_SCHEDULED_TODAY rows count"
    assert row["plan_built_at"], "the age of the plan is what makes the count meaningful"


def test_last_dialled_is_the_most_recent_day_that_posted(client):
    campaign_id = _armed_campaign_id()
    older = (date.today() - timedelta(days=4)).isoformat()
    newer = (date.today() - timedelta(days=2)).isoformat()
    with session() as conn:
        for run_date, posted in ((older, 5), (newer, 9), 
                                 ((date.today() - timedelta(days=1)).isoformat(), 0)):
            conn.execute(
                "INSERT INTO runs (campaign_id, run_date, kind, status, config_version, "
                "created_at, dry_run, evaluated, planned, slots, posted, failed, dropped, note) "
                "VALUES (?,?,'auto','committed',1,?,1,0,0,0,?,0,0,'seeded')",
                (campaign_id, run_date, f"{run_date}T09:00:00", posted))
        conn.commit()

    body = client.get("/api/day").json()
    row = next(c for c in body["campaigns"] if c["campaign_id"] == campaign_id)

    assert row["last_dialled"] == newer, \
        "a run that posted nothing did not dial, however recent it is"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q -k "already_booked or last_dialled"
```

Expected: FAIL — `KeyError: 'already_booked'`.

- [ ] **Step 3: Add `_plan_facts` to `api/day.py`**

Insert directly below `_stranded`:

```python
def _plan_facts(conn: sqlite3.Connection, runs: dict[int, sqlite3.Row],
                campaign_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per campaign: when the plan was built, when it last dialled, what was booked.

    All three answer the same operator question -- "has a call already been
    placed for these leads, by me or by anybody?" The engine does check: it skips
    every lead with `queued_today > 0` (red_engine.SKIP_ALREADY_SCHEDULED). But
    it checks at PLAN time, and on 12 Sep 2026 the plans were built in the
    morning and approved six hours later, so anything booked in Formi in between
    was invisible to the approval.

    `already_booked` is therefore both a count and a clock: N leads were already
    on Formi's clock when this plan was built, and the older the plan the less
    that number can be trusted. `last_dialled` is the other half -- a campaign
    that dialled two hours ago reads very differently from one last called on
    Tuesday.
    """
    facts = {cid: {"plan_built_at": None, "last_dialled": None, "already_booked": 0}
             for cid in campaign_ids}
    if not campaign_ids:
        return facts

    for campaign_id, run in runs.items():
        if campaign_id in facts:
            facts[campaign_id]["plan_built_at"] = run["created_at"]

    marks = ",".join("?" * len(campaign_ids))
    for row in conn.execute(
            f"SELECT campaign_id, MAX(run_date) AS last FROM runs "
            f"WHERE campaign_id IN ({marks}) AND posted > 0 GROUP BY campaign_id",
            campaign_ids):
        facts[row["campaign_id"]]["last_dialled"] = row["last"]

    run_ids = [r["id"] for r in runs.values()]
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        owner = {r["id"]: cid for cid, r in runs.items()}
        for row in conn.execute(
                f"SELECT run_id, COUNT(*) AS n FROM decisions "
                f"WHERE run_id IN ({marks}) AND reason LIKE 'ALREADY_SCHEDULED_TODAY%' "
                f"GROUP BY run_id", run_ids):
            facts[owner[row["run_id"]]]["already_booked"] = row["n"]
    return facts
```

- [ ] **Step 4: Return the facts from `get_day`**

Inside the `with session() as conn:` block, directly after `stranded_runs = …`:

```python
        facts = _plan_facts(conn, runs, [c["id"] for c in campaigns])
```

and in the `listed.append({…})` block (`api/day.py:315-323`), add after `"dropped": …`:

```python
            **facts[campaign["id"]],
```

- [ ] **Step 5: Run the tests**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 6: Sabotage-verify**

Temporarily drop `AND posted > 0` from the `last_dialled` query.
Expected: `test_last_dialled_is_the_most_recent_day_that_posted` FAILS.
Revert and confirm it passes.

- [ ] **Step 7: Add the types**

In `web/src/lib/types.ts`, add to `interface DayCampaign`:

```typescript
  /** When this plan was built. The older it is, the less `already_booked` can
   *  be trusted — leads booked in Formi since are not in it. */
  plan_built_at: string | null;
  /** The most recent date this campaign actually posted calls. */
  last_dialled: string | null;
  /** Leads Formi had already queued when the plan was built, which the engine
   *  skipped. */
  already_booked: number;
```

- [ ] **Step 8: Warn in the approve dialog and offer a re-check**

In `web/src/screens/Today.tsx`, inside `ApproveDay`, above the `return`:

```tsx
  const STALE_MIN = 90;
  const builtAt = day.campaigns
    .map((c) => c.plan_built_at)
    .filter((t): t is string => !!t)
    .sort()[0];
  const ageMin = builtAt ? Math.round((Date.now() - new Date(builtAt).getTime()) / 60000) : 0;
  const booked = day.campaigns.reduce((s, c) => s + c.already_booked, 0);
  const [rechecking, setRechecking] = useState(false);

  /** Re-read Formi and rebuild the plan, so leads booked since it was built drop
   *  out of it. This is the existing prepare pass, not a new one. */
  const recheck = async () => {
    setRechecking(true);
    try {
      const out = await api.prepareDay(day.date, day.kind, true, day.agent_id ?? undefined);
      toast('ok', `Re-checked: ${n(out.ready)} still ready across ${out.prepared} campaigns.`);
      onDone();
      onClose();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setRechecking(false);
    }
  };
```

and render, directly below the `confirm-facts` block (line 974):

```tsx
      {booked > 0 && (
        <p className="hero-sub">
          {n(booked)} {booked === 1 ? 'lead was' : 'leads were'} already on Formi’s clock when
          this plan was built and {booked === 1 ? 'was' : 'were'} left out of it.
        </p>
      )}

      {ageMin >= STALE_MIN && (
        <div className="warnbox">
          <AlertTriangle />
          <span>
            <b>This plan is {Math.floor(ageMin / 60)}h {ageMin % 60}m old.</b> Any call placed in
            Formi since it was built — by you or by anyone — is not accounted for in it.
            <button
              className="btn btn-ghost btn-sm"
              style={{ marginLeft: 8 }}
              disabled={rechecking || busy}
              onClick={recheck}
            >
              {rechecking ? <Loader2 className="spin" /> : <RefreshCw />} Re-check now
            </button>
          </span>
        </div>
      )}
```

Add `Fact` rows for the plan's age and last dial into the existing
`confirm-facts` block:

```tsx
        <Fact k="Plan built" v={builtAt ? `${Math.floor(ageMin / 60)}h ${ageMin % 60}m ago` : '—'} />
        <Fact
          k="Last dialled"
          v={day.campaigns.map((c) => c.last_dialled).filter(Boolean).sort().reverse()[0] ?? 'never'}
        />
```

Ensure `RefreshCw` and `Loader2` are in the lucide import at the top of the file.

- [ ] **Step 9: Typecheck and build**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

Expected: no errors. Update `web/src/lib/mock.ts` so its `DayCampaign` entries
carry the three new fields.

- [ ] **Step 10: Commit**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add api/day.py tests/test_day.py web/src/lib/types.ts web/src/lib/mock.ts web/src/screens/Today.tsx && git commit -m "feat(day): show plan age, last dial and already-booked leads before approving"
```

---

### Task 7: Proof — the clock spread and the verified read-back

"I have no clue is it properly scheduling or not." Two facts answer it: which hours the calls actually landed in, and what the warehouse says happened.

**Files:**
- Modify: `api/day.py` (`_spread`, `get_day`)
- Modify: `web/src/lib/types.ts` (`DayView.spread`)
- Modify: `web/src/screens/Today.tsx` (render spread + verified)
- Modify: `docs/API_CONTRACT.md`
- Test: `tests/test_day.py`

**Interfaces:**
- Consumes: `_armed` (Task 3), `_band` / `WAVE_BAND` (Task 2), `DayPanel` (Task 4), `api.verifyDialLog(date)` (already in `web/src/lib/api.ts:271`).
- Produces: `DayView.spread: { band: { start: string; end: string }; hours: Record<string, number> }`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_day.py`:

```python
def test_spread_reports_the_hours_posted_calls_actually_landed_in(client):
    """On 12 Sep the 'morning' wave's calls landed between 12:00 and 20:00."""
    campaign_id = _armed_campaign_id()
    today = date.today().isoformat()
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

    spread = client.get("/api/day").json()["spread"]

    assert spread["hours"]["10"] == 2, "both 10:xx calls belong to the 10:00 hour"
    assert spread["hours"]["19"] == 1
    assert "11" not in spread["hours"], "a planned slot has not been scheduled anywhere yet"
    assert spread["band"] == {"start": "09:00", "end": "13:30"}, \
        "the band is what the spread has to be judged against"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests/test_day.py -q -k spread
```

Expected: FAIL — `KeyError: 'spread'`.

- [ ] **Step 3: Add `_spread` to `api/day.py`**

Insert directly below `_plan_facts`:

```python
def _spread(conn: sqlite3.Connection, runs: dict[int, sqlite3.Row],
            kind: str) -> dict[str, Any]:
    """Which hours the day's calls actually went onto, against the band approved.

    This is the honest answer to "is it scheduling properly". On 12 Sep 2026 the
    `auto` wave's posted slots were spread across 12:00-20:00 and the `auto_pm`
    wave's across 13:00-20:00 -- the same evening twice, under two names -- and
    nothing in this console showed it. A count per hour beside the band the
    operator approved makes a wave that dialled outside its half of the day
    visible at a glance instead of needing a database query.

    Only `posted` and `simulated` items count: a `planned` slot has not been
    scheduled anywhere yet, and an `expired` one never will be.
    """
    lo, hi = WAVE_BAND[kind]
    band = {"start": hhmm(lo if lo is not None else parse_hhmm(DEFAULT_WINDOW["start"])),
            "end": hhmm(hi if hi is not None else parse_hhmm(DEFAULT_WINDOW["end"]))}
    run_ids = [r["id"] for r in runs.values()]
    if not run_ids:
        return {"band": band, "hours": {}}

    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT substr(scheduled_time, 12, 2) AS hour, COUNT(*) AS n FROM plan_items "
        f"WHERE run_id IN ({marks}) AND status IN ('posted','simulated') "
        f"GROUP BY hour ORDER BY hour", run_ids).fetchall()
    return {"band": band, "hours": {str(int(r["hour"])): r["n"] for r in rows}}
```

- [ ] **Step 4: Return it from `get_day`**

Inside the `with session() as conn:` block, after `facts = …`:

```python
        spread = _spread(conn, runs, kind)
```

and in the response dict, after `"stranded": stranded_runs,`:

```python
        # Which hours the calls actually landed in, against the band approved.
        "spread": spread,
```

- [ ] **Step 5: Run the tests**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests -q
```

Expected: all pass.

- [ ] **Step 6: Sabotage-verify**

Temporarily widen the status filter to `IN ('posted','simulated','planned')`.
Expected: `test_spread_reports_the_hours_posted_calls_actually_landed_in` FAILS on
the `"11" not in spread["hours"]` assertion. Revert and confirm it passes.

- [ ] **Step 7: Add the type**

In `web/src/lib/types.ts`, above `interface DayView`:

```typescript
export interface DaySpread {
  /** The hours this wave is allowed to dial into. */
  band: DialWindow;
  /** Hour of day ("9".."19") -> calls actually put on the clock. */
  hours: Record<string, number>;
}
```

and inside `DayView`, after `capacity_before_close`:

```typescript
  /** Proof: where the calls actually landed, against the band approved. */
  spread: DaySpread;
```

- [ ] **Step 8: Render the proof card**

In `web/src/screens/Today.tsx`, add above `function Stopped`:

```tsx
/** The two facts that answer "did it schedule properly": which hours the calls
 *  landed in, and what the warehouse says actually happened. */
function Proof({ day, onReload }: { day: DayView; onReload: () => void }) {
  const toast = useStore((s) => s.toast);
  const [busy, setBusy] = useState(false);
  const hours = Object.entries(day.spread.hours).sort(([a], [b]) => +a - +b);
  const total = hours.reduce((s, [, v]) => s + v, 0);
  if (total === 0) return null;

  const peak = Math.max(...hours.map(([, v]) => v));
  // An hour counts as outside only if NO minute of it falls in the band. With a
  // 13:30 boundary the 13:00 hour is half in, so flagging it red would be a lie.
  const min = (t: string) => +t.slice(0, 2) * 60 + +t.slice(3, 5);
  const [lo, hi] = [min(day.spread.band.start), min(day.spread.band.end)];
  const outside = hours.filter(([h]) => +h * 60 + 59 < lo || +h * 60 >= hi);

  const check = async () => {
    setBusy(true);
    try {
      await api.verifyDialLog(day.date, true);
      toast('ok', 'Read the warehouse back.');
      onReload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title="Where the calls landed"
      eyebrow={`${n(total)} on the clock · band ${day.spread.band.start}–${day.spread.band.end}`}
    >
      <div className="row" style={{ gap: 4, alignItems: 'flex-end', height: 64 }}>
        {hours.map(([h, v]) => (
          <div key={h} style={{ flex: 1, textAlign: 'center' }} title={`${h}:00 — ${n(v)} calls`}>
            <div
              style={{
                height: `${(v / peak) * 48}px`,
                background: outside.some(([o]) => o === h) ? 'var(--bad)' : 'var(--ok)',
                borderRadius: 2,
              }}
            />
            <span className="eyebrow">{h}</span>
          </div>
        ))}
      </div>

      {outside.length > 0 && (
        <p className="hero-sub" style={{ color: 'var(--bad)' }}>
          {n(outside.reduce((s, [, v]) => s + v, 0))} calls landed outside the{' '}
          {day.spread.band.start}–{day.spread.band.end} band.
        </p>
      )}

      <div className="dialbar-keys">
        {Object.entries(day.dial_log).map(([state, count]) => (
          <span key={state} className="dialbar-key">
            <b>{n(count)}</b> {state}
          </span>
        ))}
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={check}>
          {busy ? <Loader2 className="spin" /> : <RefreshCw />} Check now
        </button>
      </div>
      <p className="hero-sub" style={{ marginBottom: 0 }}>
        A call reads <span className="mono">dialled</span> only once the warehouse shows a real
        interaction for it. <span className="mono">pending</span> means it was accepted by Formi
        and not yet read back.
      </p>
    </Card>
  );
}
```

Render it in `DayPanel`, inside the `{d && d.status !== 'no_campaigns' && (…)}`
block, directly after `<Campaigns day={d} />`:

```tsx
          <Proof day={d} onReload={() => day.reload()} />
```

- [ ] **Step 9: Typecheck and build**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

Expected: no errors. Add `spread` to the mock day in `web/src/lib/mock.ts`.

- [ ] **Step 10: Document it**

In `docs/API_CONTRACT.md`, add `spread`, `plan_built_at`, `last_dialled` and
`already_booked` to the `GET /api/day` response documentation.

- [ ] **Step 11: Commit**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && git add api/day.py tests/test_day.py web/src/lib/types.ts web/src/lib/mock.ts web/src/screens/Today.tsx docs/API_CONTRACT.md && git commit -m "feat(day): show which hours the calls landed in and what the warehouse read back"
```

---

## Final verification

- [ ] **Full suite**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests -q
```

- [ ] **Frontend gate**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

- [ ] **Local smoke, DRY_RUN=1**

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && DRY_RUN=1 AGENT_LANGUAGES=125:Hindi,127:Tamil .venv/bin/python -m uvicorn api.main:app --port 8082
```

Confirm by hand: two panels appear with their own ready counts; approving the
morning wave after 13:30 refuses and names the band; the progress bar advances
one campaign at a time and Stop halts it; the spread card shows the hours.

- [ ] **Deploy**

On `oraclevm` (`ssh -i ~/.ssh/oraclevm.key ubuntu@130.210.44.241`):
`cd /opt/apps/redial && git pull --ff-only && (cd web && npm run build) && sudo systemctl restart chola-redial`.

Set `AGENT_LANGUAGES=125:Hindi,127:Tamil` in the service environment **before**
restarting, or both panels read "Agent 125" / "Agent 127".

**`DRY_RUN` is 0 there.** The first check after deploying is read-only: open an
already-approved day and confirm the panels, bands, stranded warning and spread
card render. Press nothing.

---

## Deferred — investigate separately

All 3,551 `dial_log` rows for 12 Sep read `verified='pending'`.
`api/autopilot.py:186-191` only ever calls `verify_async(now.date().isoformat())`,
so a row left pending when the date rolls can never be picked up again — a
plausible root cause that does **not** explain why the whole of 12 Sep stayed
pending while the service was running that day.

This needs `superpowers:systematic-debugging` — evidence before any fix — and is
deliberately not a task in this plan. Task 7 makes the backlog visible, which is
what turns it from invisible into reportable.
