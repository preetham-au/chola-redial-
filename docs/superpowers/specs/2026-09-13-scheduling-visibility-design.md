# Scheduling visibility: real waves, language split, dial progress, and proof

Repo: `D:\Users\WorkUser\Desktop\choal apps\redial` (NOT the session cwd).
Date: 2026-09-13.

## Context

Asked for on 13 Sep 2026:

> "see what is morning and afternoon — when I approve the plan, based on the
> time and logic that I gave, calls should be scheduled. Tamil should be
> separate, and Hindi. And I have no clue, is it properly scheduling or not.
> When I click on dial, add a progress bar also. And see, I might have placed
> the campaigns or started the calls by myself — it should also be noticed. And
> it should have older data: when was a call scheduled earlier, do we need to
> schedule today."

Followed by:

> "see if I select the campaign where a call might have been placed, you need to
> check for that also."

The answer to "is it properly scheduling or not" is **no**, and the evidence is
below. This spec closes five gaps, all of which are reporting and scoping
failures over data the system already holds.

## What is actually broken

Measured on the production VM (`oraclevm`, `/opt/apps/redial/redial.db`) for
12 Sep 2026:

| Finding | Evidence |
|---|---|
| **Half the day never dialled** | `auto`: 15 runs committed, **8 left `planned`** holding 491 slots. `auto_pm`: 8 committed, **15 left `planned`** holding 53 slots. No screen said so. |
| **Morning is not morning** | Posted-slot hours: `auto` spread 12:00–20:00, `auto_pm` spread 13:00–20:00. Approve re-plans from the current minute, so the wave name has no relationship to the clock. |
| **"Did it dial?" is unanswered** | All 3,551 `dial_log` rows for 12 Sep read `outcome='placed'`, `verified='pending'`. The warehouse read-back never completed for that day. |
| **Hindi and Tamil are one number** | Agents 125 and 127 each hold 10 armed campaigns with mirrored names (`CIFCO SEP` = 1818 on 125, 1819 on 127). `GET /api/day` and `POST /api/day/approve` have no agent filter. Today: 4,271 slots on 125 and 481 on 127, added together on one screen. |
| **One request, 2,967 calls** | 12 Sep afternoon posted 2,967 calls across 8 runs in as many HTTP requests — up to ~371 calls in a single blocking call. |

None of this requires new data. `runs`, `plan_items`, `decisions` and `dial_log`
already record every fact needed; the API does not expose it and the screen does
not ask.

## Design

Five pieces. Each ships independently and in the order given.

### 1 · Waves become real time bands

`DispatchConfig` (`engine/dispatcher.py:103`) is a frozen dataclass carrying
`start_min` / `end_min`, and `_write_run` (`api/routes_core.py:475`) already
threads it into `dispatch()`. A band is therefore a **narrowed window**, and the
dispatcher needs no change at all.

In `api/day.py`:

```python
WAVE_BOUNDARY = parse_hhmm((os.environ.get("WAVE_BOUNDARY") or "13:30").strip())
WAVE_BAND = {MORNING: (None, WAVE_BOUNDARY), AFTERNOON: (WAVE_BOUNDARY, None)}

def _band(kind: str, dcfg: DispatchConfig) -> DispatchConfig:
    """The campaign's own window clipped to the wave's half of the day."""
    lo, hi = WAVE_BAND[kind]
    return replace(dcfg,
                   start_min=max(dcfg.start_min, lo if lo is not None else 0),
                   end_min=min(dcfg.end_min, hi if hi is not None else 1440))
```

`_prepare_one` (`api/day.py:417`) and `_approve_one` (`api/day.py:513`) compute
`banded = _band(kind, dcfg)` once and use it for `_floor_min`, the
`window_closed` check and `_write_run`. **The existing check needs no new
logic** — it already reads:

```python
floor = _floor_min(now_ist(), day, dcfg)
if floor is not None and floor >= dcfg.end_min:
    return {**out, "status": "window_closed", ...}
```

Swap `dcfg` for `banded` and approving the morning wave at 14:02 returns
`window_closed` with `detail = "the 09:00-13:30 morning band has closed"`,
instead of silently dumping the morning's leads into the evening. A campaign
whose own window ends at 13:00 gets an empty afternoon band and falls down the
same path.

`_day_window` (`api/day.py:163`) takes a new `kind` argument and clips each
campaign's `dial_window` the same way before computing the envelope and
`capacity`, so the header on screen names the band the operator is approving —
not the whole day — and the capacity figure stops promising hours the wave
cannot use.

13:30 sits between autopilot's own two preparation times — `AUTOPILOT_AM`
10:00 and `AUTOPILOT_PM` 15:00 (`api/autopilot.py:42-44`) — so each wave is
still prepared inside the band it will dial into, and the unattended path needs
no retiming.

`WAVE_BOUNDARY` is one module constant, env-overridable, **not per-campaign**.
The screen must be able to name the band in words ("the morning band,
09:00–13:30"); per-campaign boundaries make that sentence unwritable. If
per-campaign bands are ever needed, they belong in the saved config beside
`dial_window`, which is a later change.

**This changes live dialling behaviour.** Approving a morning wave after 13:30
will now refuse rather than dial. That is the intent, but it must not be a dead
end: when every campaign in a panel returns `window_closed`, the screen names
the band that closed and offers the other wave in one click.

### 2 · One panel per agent, one Approve each

`agent_id: Optional[int]` is added to `GET /api/day`, `PrepareBody` and
`ApproveBody`. `ARMED` (`api/day.py:66`) gains `AND agent_id=?` when it is
supplied; omitted, every endpoint behaves exactly as it does today.

`GET /api/agents` (`api/routes_core.py:186`) currently returns
`name = f"Agent {agent_id}"` — no real name exists in the database — so the
language label comes from a setting, not from the frontend:

```
AGENT_LANGUAGES=125:Hindi,127:Tamil
```

parsed once into `{125: "Hindi", 127: "Tamil"}` and returned as a `language`
field, falling back to `None`. A frontend hardcode is forbidden by CLAUDE.md's
"no funny, dummy, or hard-coded values" rule, and would also break the moment a
third agent is added.

The screen reads `/api/agents`, and renders one panel per agent that has armed
campaigns — each with its own ready count, RED bands, buckets and Approve
button. An agent with no armed campaigns renders nothing.

**This is not a campaign filter.** A newly created campaign still auto-arms and
appears under its own agent with no configuration, which is the standing
constraint ("when new campaign is created that should automatically get added").
The split is by agent, which every campaign already carries.

### 3 · Progress bar — a browser-driven queue

`ApproveDay.submit` currently makes one call carrying every campaign id. It
becomes a sequential driver:

```
for (const c of selected) {
  setCurrent(c);
  const out = await api.approveDay(date, kind, buckets, [c.id], agentId);
  merge(out);
  if (stopRequested) break;
}
```

The bar is `done / total` with the campaign being dialled named beneath it, and
each finished row ticks ✔ / ✖ / ● as it lands. A **Stop** button finishes the
campaign in flight and halts; whatever was not reached stays `planned` and is
still approvable — closing the tab has the same effect.

This needs **zero backend change**, and it is also the queue asked for earlier
("do not overload the system all at once, make it queue properly so the main
won't get crashed"): one request posting 2,967 calls becomes 12 requests of
~250. The `DialResult` bar already shipped stays as the final summary, fed by
the merged result.

### 4 · Pre-dial check: was a call already placed?

The engine already skips leads with `queued_today > 0`
(`engine/red_engine.py:1017`, reason `ALREADY_SCHEDULED_TODAY queued_today=N`),
and `decisions` already stores a row for **every** lead evaluated, skipped ones
included (`api/routes_core.py:493`). The hole is timing, not detection: on
12 Sep plans sat six hours before approval, so anything booked in Formi *after*
the plan was built was invisible to it.

`GET /api/day` gains three fields per campaign, all from existing rows:

| field | source |
|---|---|
| `plan_built_at` | `runs.created_at` for the day's `planned` run |
| `last_dialled` | `MAX(run_date)` over that campaign's runs with `posted > 0` |
| `already_booked` | `COUNT(*) FROM decisions WHERE run_id=? AND reason LIKE 'ALREADY_SCHEDULED_TODAY%'` |

The approve dialog shows plan age and, per campaign, when it last dialled and
how many leads were already booked when the plan was built. If any plan is older
than `STALE_PLAN_MIN` (default 90 minutes, env-overridable) the dialog warns and
offers **Re-check now**, which needs **no new endpoint**: it is
`POST /api/day/prepare {date, kind, agent_id, resync: true}`, which already
re-reads Formi and rebuilds the plan with the fresh `queued_today` counts.

`last_dialled` is what answers "do we need to schedule today?" — a campaign that
last dialled on 11 Sep reads differently from one that dialled two hours ago,
and neither is currently on any screen.

### 5 · Proof that it scheduled correctly

Three additions to `GET /api/day`, plus one existing endpoint rendered:

- **`spread`** — hour → count over `plan_items.scheduled_time` for items with
  `status IN ('posted','simulated')` on the day's runs, alongside the band they
  were approved into. A morning wave whose calls sit at 19:00 becomes visible in
  one glance instead of requiring a database query.
- **Verified read-back** — `GET /api/dial-log/summary?date=` already returns
  `{sent, outcome{}, verified{}}` per campaign. Render `verified` as the
  dialled / queued / missing split, with a **Check now** button wired to the
  existing `POST /api/dial-log/verify`.
- **`stranded`** — runs still `status='planned'` with `run_date < today`, listed
  per campaign with their slot counts. Yesterday this would have read
  "8 campaigns · 491 calls never dialled", which nothing said at the time.

## Separate bug: the verified backlog

All 3,551 `dial_log` rows for 12 Sep remain `verified='pending'`.
`api/autopilot.py:186-191` calls `verify_async(now.date().isoformat())` —
**today's date only** — every 10 minutes between 09:00 and 21:00. Once the date
rolls over, a row left pending can never be picked up again.

That is a plausible root cause, **not a confirmed one**, and it does not explain
why the whole of 12 Sep stayed pending while the service was running that day.
This is investigated under `superpowers:systematic-debugging` before any code
changes — evidence first, fix second. It is listed here so it is not lost, and
is explicitly **not** bundled into the five pieces above.

## Error handling

- Every new `GET /api/day` field is derived from existing rows; a campaign with
  no run yields `null` / `0`, never an error. A slow query must not take the
  day screen down with it.
- `_band` producing an empty band is a normal outcome (`window_closed`), not an
  exception.
- An unparseable `WAVE_BOUNDARY` or `AGENT_LANGUAGES` fails loudly at import
  rather than silently defaulting — a boundary that quietly reverts to 13:30
  would look exactly like a working one.
- A failed campaign in the progress driver does not stop the queue; it is
  recorded in the row list and the loop continues. Only **Stop** halts it.
- `agent_id` naming an agent with no armed campaigns returns an empty day, not a
  404 — a paused-out agent is a real state.

## Testing

`tests/test_api.py` / `tests/test_autopilot.py`:

- morning approved after 13:30 → `window_closed`, and the detail names the band;
- a campaign with window 10:00–18:00 bands to 10:00–13:30 and 13:30–18:00;
- a campaign with window 09:00–13:00 has an empty afternoon band;
- no posted `scheduled_time` falls outside the band it was approved into;
- `GET /api/day?agent_id=125` never returns a campaign belonging to 127, and
  approve with `agent_id` dials only that agent's campaigns;
- omitting `agent_id` returns exactly what it returns today (regression guard);
- `already_booked` counts the `ALREADY_SCHEDULED_TODAY` decisions for the run;
- `stranded` lists yesterday's `planned` run and not today's;
- `spread` buckets posted items by hour and ignores `planned` ones.

Sabotage-verify each: widen `_band` back to the full window and the band tests
must go red; drop the `AND agent_id=?` and the scoping test must go red.

`web/src/selfcheck.tsx`: two panels render with their own Approve; the progress
bar advances and Stop halts it; a `window_closed` panel offers the other wave.

## Verification

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial" && python -m pytest tests -q
```

```bash
cd "D:/Users/WorkUser/Desktop/choal apps/redial/web" && npx tsc --noEmit && npm run build
```

`tests/test_api.py::test_approve_under_dry_run_simulates_and_never_dials` is a
known afternoon-only flake on clean `main` (task `task_32229e3f`); it is not
this change.

**`DRY_RUN` is 0 on the production VM — dialling is live.** The first check
after deploy is read-only: confirm the bands, panels and stranded warning render
on an already-approved day before pressing anything.

## Order of work

| # | piece | why here |
|---|---|---|
| 1 | stranded warning | ~491 real calls a day are being lost right now; smallest diff |
| 2 | time bands | everything else describes them |
| 3 | agent split | the Hindi / Tamil separation |
| 4 | progress bar | frontend only, no dialling risk |
| 5 | pre-dial check + proof | builds on 2 and 3 |
| — | verified backlog | investigate first, separately |

## Out of scope

- Per-campaign wave boundaries.
- Any change to bucket, RED-band or disposition logic — the engine's decisions
  are not in question here, only when they are dialled and what is reported.
- A campaign filter of any kind.
- Call audits.
