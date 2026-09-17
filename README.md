# chola-redial

A console for scheduling **renewal redial calls** for the Chola motor-insurance
AI voice agents. It decides who should be called today, when in the day each
call should go out, and hands that schedule to Formi — and it keeps a record of
what it sent and whether the call actually happened.

FastAPI + SQLite behind a React console. It runs fully offline on invented data,
with no credentials and no network, so you can learn it before you touch
anything live.

**New here? Read this file top to bottom.** `docs/API_CONTRACT.md` is the
endpoint-by-endpoint reference and wins any disagreement with this one.

---

## 1. What problem it solves

Every policy has a **RED** — the renewal expiry date. How often somebody should
be called depends on how far they are from their RED, and whether they picked up
last time. Doing that by hand across ~20 live campaigns and tens of thousands of
leads is where calls get missed.

This console does four things:

1. **Decides who to call today** from each lead's RED, its last disposition and
   how many times it has already been called.
2. **Places each call on a minute** of the dialling day, spread so Formi is never
   flooded and so people are not rung twice in quick succession.
3. **Waits for a human** to look at the plan and press Approve. Nothing reaches a
   real phone without that (the automatic recall below is the one exception, and
   it only chases people an approved plan already called).
4. **Proves what happened** — every call it sends is logged, then read back
   against the warehouse to check the phone really rang.

Two AI agents run the calls: **125 (Hindi)** and **127 (Tamil)**. Each has its
own campaigns and its own panel in the console; they never share a campaign.

---

## 2. Run it locally in five minutes

You need Python 3.11+ and Node 18+.

```bash
pip install -r requirements.txt
python -m engine.seed                          # invented campaigns + leads, no credentials
python -m uvicorn api.main:app --port 8000     # http://127.0.0.1:8000/api/health
```

In a second terminal:

```bash
cd web && npm install && npm run dev           # http://localhost:5173
```

Open `http://localhost:5173`. `DRY_RUN` defaults to `1`, so every "call" is
recorded as `simulated` and no network request is made — the code that would
talk to Formi is not even imported.

On Windows, `run.bat` does the backend half (it seeds only if `redial.db` is
missing).

### Real data instead of the seed

```bash
cp .env.example .env        # fill in METABASE_URL / METABASE_API_KEY / METABASE_DB_ID
python -m engine.sync       # pulls real campaigns + leads for agents 125/127
```

`engine.sync` reads the Metabase warehouse only. It never writes to Formi and
never places a call. `engine.seed` and `engine.sync` each clear the other's
campaigns, so the database is only ever all-real or all-invented — `/api/health`
reports which by looking at the campaign ids it actually holds.

```bash
python -m engine.sync --campaigns 250 --leads 20000  # the caps, defaults 250 and 5000
python -m engine.sync --keep-local                   # don't delete untouched campaigns
python -m engine.sync --all-leads                    # ignore the RED window, take everything
```

`--leads` is per campaign and the one that actually bites; ordering is
newest-first, so a cap below the real count silently drops the rest. Both caps
are printed as `CAP:` lines and a truncated sync says so out loud — read them.

**`--keep-local` is load-bearing on a bounded sync.** Without it, a sync that
only looked at some campaigns deletes the ones it did not touch, taking their
config and run history with them. The hourly production timer passes it.

Campaigns whose name carries a word like `test`, `dev`, `demo`, `staging`,
`killed` or `deprecated` are skipped, because "newest, has leads, has a RED"
otherwise describes a test campaign exactly — and approving one live dials the
real numbers inside it. Matching is on whole words split on non-alphanumerics,
so `Dev_Test_06-08-2026` is caught and `Contest_Aug` survives.
`--force-campaigns 1650,1618,…` overrides it for specific ids.

### Run the tests

```bash
python -m pytest tests -q              # 434 passed, 1 skipped — no network, no credentials
cd web && npx tsc --noEmit && npm run check && npm run build
```

`npm run check` is a self-check that drives the real frontend modules and
asserts on what they put on the wire. Both gates must be green before a deploy.

---

## 3. How a day works

**Nothing dials on its own except the recall.** The clock only *prepares*.

```
10:00  First pass PREPARES
       ├─ re-reads campaign status from Formi (paused? killed?)
       ├─ re-syncs the leads of every campaign in the plan
       └─ writes a `planned` run per campaign — and stops there.

  ↓    An operator opens "The day", reads what is ready, and presses Approve.
       This is the only thing that reaches Formi. Approving is per agent:
       Hindi and Tamil are separate panels with separate buttons.

+15m   The first call of that plan rings. See "The head start" below.

~3h    The RECALL chases, by itself
       ├─ only people the approved pass already called today
       ├─ only those whose result says nobody was reached
       └─ dials without being asked again. `AUTO_RECALL=0` switches it off.
```

If nobody approves, **no first call goes out that day.** A plan left unapproved
is not lost — it shows on the day screen and, after 14 days, as a stranded
warning.

### The head start

Approving does not dial instantly. `APPROVE_LEAD_MINUTES` (15) is the floor the
first slot of a fresh plan is placed on, so **approve at 11:00 and the first call
rings at 11:15.** That gap is the time you have to notice a wrong plan and stop
it. The day screen's capacity figure reads against the same floor, so what is on
screen is what Approve will really find.

Formi separately refuses any slot less than five minutes out
(`FORMI_LEAD_MINUTES`). That is a different question — "will Formi still take
this slot?" — and it governs retiring already-planned slots and the test call,
which has no time restriction at all.

The head start is a constant in `api/routes_core.py`, not a UI setting.

### Approving late does not dial into the night

Approve **re-plans** each campaign from the current minute, then commits it, so
only what genuinely fits before the window shuts is scheduled — best RED band
first. The rest is reported as `not_dialled` and returns in tomorrow's plan.
Approving twice does not dial twice.

### Which pass a plan is, is decided by the previous call, not the clock

`auto` (first pass) until a campaign has posted calls today, `auto_pm` (recall)
after that — per campaign. There is no morning/afternoon boundary and no
`WAVE_BOUNDARY` setting; both were removed on 14 Sep 2026, because "twice a day"
means "again after the first call", not "again after lunch".

---

## 4. Using the console

The sidebar is grouped by how often you need it.

### Every day

| Screen | What it is for |
|---|---|
| **The day** | The one screen that matters. Per agent: what is planned, what is ready, what capacity is left before the window shuts — and the Approve button. Also the Dial progress bar and its Stop. |
| **Call log** | Did the call actually happen? Two columns that are never merged — see below. |

**The day** is where a shift starts and ends. It shows one panel per agent
(Hindi / Tamil), each with its own Approve, because they are separate cohorts
read a separate script. A dial running for 125 does **not** block 127 — the two
walks run side by side. An unscoped walk (whole roster) and an agent's walk do
queue behind each other, which is what stops the automatic recall firing while
you are dialling by hand.

Pressing Dial opens a progress bar: campaign N of M, the campaign in flight, and
a Stop that finishes the current campaign and leaves the rest `planned` for
later. The walk lives in the API process, so closing the browser does not stop it
and reopening the screen rejoins it.

**Call log** keeps two facts apart, deliberately:

* **Sent** — what Formi's API answered when the schedule was posted
  (`posted` / `failed` / `simulated`). A 2xx means the request was *accepted*,
  nothing more.
* **Happened** — what the warehouse says afterwards (`pending` / `queued` /
  `dialled` / `missing` / `simulated`). **`dialled` is the only proof a call
  happened.**

The read-back runs every 10 minutes between 09:00 and 21:00 (an hour past the
window, because a call booked at 19:59 is dialled after it shuts). It is
SELECT-only against the warehouse and writes only to the local log, so it is
unaffected by `DRY_RUN` and can never place a call.

### One campaign

| Screen | What it is for |
|---|---|
| **Dashboard** | One campaign's leads by RED bucket and disposition, and its recent runs. |
| **Plan review** | The individual slots of one run — every lead, its bucket, its minute. Approve, pause, resume or delete a single run here. |
| **Campaign config** | That campaign's dialling rules: window, frequency table, per-bucket dispositions, gap between the two calls, `short_call_seconds`, caps. |

### Dial deliberately

| Screen | What it is for |
|---|---|
| **Manual redial** | Build and dial a one-off list outside the daily plan — pick buckets and dispositions yourself. |
| **Test call** | Dial one allow-listed number to hear the agent. No time restriction: it schedules whenever you ask. |

### Lead data / Settings

| Screen | What it is for |
|---|---|
| **Bulk stage change** | Paste a policy list and set a stage, a renewal date, or sweep by RED. Preview first, then commit; every job is kept in a history. |
| **Campaign visibility** | Hide campaigns you never dial so they stop cluttering every other screen. Hiding also takes a campaign out of the daily plan. |

### RED buckets

What the codes on every screen mean:

| Code | Name | Days to renewal (`dte`) | Cadence |
|---|---|---|---|
| `M0` | Renewal today | RED−1 and RED | mandatory — overrides cadence, the weekly budget and pending callbacks |
| `F1` | Warm-up | 45–32 | 2 / week |
| `F2` | Early engagement | 31–24 | 2 / week |
| `F3` | Building urgency | 23–16 | 3 / week |
| `F4` | High-frequency | 15–8 | 3 / week |
| `F5` | Critical week | 7–1 | **2 / day** |
| `E0` | Expiry window | RED (0) and the day after (−1) | **2 / day** |
| `F6` | Grace period | −2 to −3, past due | **2 / day** |
| `D0` | Connected — manual only | off the runway | a promised callback date, not `dte` |

The three `2 / day` buckets are the ones the recall chases. The screens write
negative `dte` as `RED+2` rather than `−2`, because a negative number under a
"days to renewal" heading reads as a mistake.

`M0` and `D0` are not rows in the frequency table — `M0` is the mandatory
override (`mandatory_days: [1, 0]`) and `D0` is driven by a disposition callback.
Editing the table on the Campaign config screen changes the other seven.

### Pausing

Pausing a campaign **in the Formi platform** pauses it here and takes its queued
calls back off Formi's clock. Un-pausing it there does *not* restart calls here —
only `POST /api/campaigns/{id}/resume` on this console does. That asymmetry is on
purpose: stopping should be easy from anywhere, starting should need a decision.

---

## 5. Safety

`DRY_RUN` defaults to **1** and every dialling path reads it at call time.

* Under `DRY_RUN=1` every item is marked `simulated` with the exact URL and body
  it *would* have posted. `requests` is imported **after** the guard, so a dry
  run has no code path to the network at all.
* `tests/test_api.py` patches `requests.post` to raise and then drives approve
  and both bulk commits, so a regression here fails the suite.
* Live dialling needs `DRY_RUN=0` **and** a Formi credential in the environment.
* `POST /api/config/dry-run` flips it without a restart, at the cost of typing
  the words `GO LIVE`. Turning it back off is free — a switch that is hard to
  flip back to safe is a switch nobody flips in a hurry.
* The flip is **written through to `.env`, so it survives a restart.** It used to
  be in-memory only, until the operator went live at 05:49 on 12 Sep 2026 and a
  deploy seven minutes later put the console back to simulating without anyone
  asking. A dry run believed to be live places no calls at all, which is the
  worse of the two failures. `persisted: false` in the response is the honest
  answer where there is no `.env` to write to.

**The production box runs `DRY_RUN=0`. Every approve there places real calls to
real customers.**

---

## 6. Configuration

### Environment (`.env`, loaded by `api.main` at import)

Copy `.env.example` to `.env`. Real environment variables win over the file.

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `1` | `0` or `false` enables live dialling. Anything else is a dry run. |
| `REDIAL_DB` | `./redial.db` | SQLite path. |
| `METABASE_URL` / `METABASE_API_KEY` / `METABASE_DB_ID` | — | The lead warehouse. Read-only; needed by `engine.sync` and by the call-log read-back. |
| `FORMI_API_KEY` | — | Only read on a live write. `FORMI_TOKEN` is honoured too and wins if both are set. |
| `CHOLA_OUTLET_ID` | `1497` | The Formi outlet the calls belong to. |
| `AGENT_LANGUAGES` | — | `125:Hindi,127:Tamil`. Labels the panel an operator approves. A malformed entry stops the API **at boot**, naming it — the wrong language on a dialling console is a script read to the wrong cohort. |
| `AUTO_RECALL` | `1` | `0` switches the automatic recall off, including its manual endpoint. A kill switch with a bypass beside it is not a kill switch. |
| `AUTOPILOT_AM` / `AUTOPILOT_PM` | `10:00` / `15:00` | When the passes prepare. They never dial. |
| `DIAL_LOG_DAYS` | `90` | How long call-log rows are kept. |
| `FORMI_POST_RATE_PER_SEC` | `10` | How fast schedules are posted to Formi. |
| `METABASE_ROW_CAP` | `2000` | Raise if the warehouse server-side limit is raised. |
| `LEADS_SOURCE` | — | **Not read for planning.** Planning always uses the local `leads` table — whatever `engine.sync` or `engine.seed` last put there. `/api/health` reports `seed` or `warehouse` by inspecting the campaign ids it actually holds, so it cannot disagree with the data. |

### Per-campaign config (the "Campaign config" screen)

Every campaign carries its own copy, versioned on each save. The production
defaults:

| Setting | Value | Meaning |
|---|---|---|
| Dial window | `09:00–20:00` | Half-open: `end` is the minute the window **shuts**; no call is placed on it. Clamped in the engine, not just at the API edge — `PUT` returns 422 outside it. |
| `same_day_gap_hours` | `3.0` | Minimum gap between a lead's two calls of the day. Also halves the window a two-call lead may be placed in, so both calls fit. |
| `shift_from_last_hours` | `2.0` | Time rotation — yesterday 09:00 becomes today 11:00, so nobody is always rung first. |
| `max_per_minute` | `12` | Stagger ceiling. Overflow moves to the next free minute. |
| `max_per_run` | `5000` | Sheds from the tail — the leads furthest from their renewal. Lands in `runs.dropped`. |
| `calls_per_day_cap` | `2` | Nobody is called more than twice a day. |
| `short_call_seconds` | `15` | A call with **no** disposition that ran shorter than this counts as "nobody was reached", so the recall chases it. `0` chases every such call. Set this per campaign. |
| `red_priority` | `[[-1,-3],[0,7]]` | Which RED band is scheduled first when the day cannot fit everyone. |
| `never_dial` | list | Dispositions that are never called again, whatever the RED says. |

---

## 7. How the schedule is actually built

`engine/dispatcher.py`, in order:

1. **RED band, then bucket priority.** `red_priority` is applied first: the 1–3
   days *past* RED outrank everything, then the RED day and the week before it,
   then the rest. Inside a band, bucket order is `M0 E0 F6 F5 F4 F3 F2 F1 D0`.
   `max_per_run` sheds from the tail.
2. **Two slots** for the `calls_per_day == 2` buckets (F5 / E0 / F6): `slot_no`
   1 and 2, slot 2 at least `same_day_gap_hours` later. If the second will not
   fit before the window closes, slot 1 is emitted alone.
3. **Rotation.** Today's minute is last call's minute-of-day plus
   `shift_from_last_hours`, wrapped into the window. Never-called leads are
   spread uniformly across it.
4. **Stagger.** At most `max_per_minute` calls per minute. Overflow searches
   **forward** first, so ordering follows the spread — and only if nothing is
   left ahead does it look **behind** the wanted minute. That backward search was
   added on 16 Sep 2026: without it, a lead aimed into a congested stretch was
   dropped with hours of its own window standing empty in front of it (65 leads
   over 14–15 Sep went that way, counted in no column and reported on no screen).

A campaign leaves the daily plan when it is paused here, paused or killed in
Formi, or when the warehouse holds no lead with a RED at or above `dte_min` (−3)
whose stage is not terminal. A warehouse it cannot reach never counts as
"finished", and a campaign whose re-sync failed is skipped rather than planned
against stale leads.

---

## 8. Production

Everything lives on the `oraclevm` box (`130.210.44.241`, user `ubuntu`).

| | |
|---|---|
| Code | `/opt/apps/redial` (branch `main`) |
| Service | `chola-redial.service` → uvicorn on `127.0.0.1:8082` |
| Console URL | nginx serves it at `/redial/`, fronted by an ngrok tunnel (basic-auth; the credential is with the operators, not in this repo) |
| Lead sync | `chola-redial-sync.timer`, hourly at `:15` — hourly and not nightly because the engine gates on "calls today" and "hours since last call" |
| Database | `/opt/apps/redial/redial.db` (SQLite, WAL) |
| Logs | `journalctl -u chola-redial -f` |

### Deploy

```bash
cd /opt/apps/redial && git pull --ff-only && cd web && npm ci && npm run build && cd .. && sudo systemctl restart chola-redial
```

**`npm ci && npm run build` is not optional when anything under `web/` changed.**
`web/dist` is gitignored, so the frontend is built on the box; skip it and the
console silently serves the previous version.

Run both gates locally before pushing:

```bash
python -m pytest tests -q && cd web && npx tsc --noEmit && npm run check && npm run build
```

### Checks after a deploy

```bash
systemctl is-active chola-redial && curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/api/health
```

`GET /api/health` answers `{ok, dry_run, db, leads_source, agents, test_numbers}`.
Check `dry_run` and `leads_source` before approving anything.

---

## 9. Where the code is

```
api/schema.sql       tables + the indices the contract's filters need
api/db.py            connection factory (WAL, Row factory), versioned config, DRY_RUN
api/main.py          app, CORS, error handlers, /api/health, sync, dry-run switch
api/routes_core.py   campaigns, config, plan, buckets, runs, approve, manual, test call
api/routes_stage.py  bulk stage preview/commit, job history
api/autopilot.py     the clock — passes that PREPARE, the recall that dials, verification
api/day.py           the approval gate: one day, one screen, one Approve per agent
api/dial_log.py      every call sent, and the warehouse read-back that proves it

engine/red_engine.py    vendored decision logic (whether to call) — do not edit
engine/dispatcher.py    RED bands, priority, two slots, rotation, stagger
engine/sync.py          pulls real warehouse campaigns/leads into redial.db
engine/seed.py          deterministic offline dataset
engine/metabase_source.py  the warehouse queries
engine/stage_ops.py     mark_stage_by_policy / mark_stage_by_red, vendored

web/src/screens/     one file per screen in the sidebar
web/src/lib/api.ts   every call the console makes
web/src/selfcheck.tsx  the `npm run check` suite

tests/               pytest — 434 passed, 1 skipped, no network
docs/API_CONTRACT.md the endpoint reference; it wins over this file
```

---

## 10. If something looks wrong

| Symptom | Where to look |
|---|---|
| "I approved and nothing rang" | The first call is 15 minutes out by design. After that, Call log → is it `posted` but not `dialled`? |
| "Approve said `window_closed`" | It is past 19:44 — a fresh plan cannot place anything inside the head start before a 20:00 close. |
| "The other agent's panel is stuck" | It should not be — walks are per agent since 16 Sep 2026. Check `GET /api/day/dial?agent_id=…` names the agent you expect. |
| "Fewer calls went out than the plan said" | `not_dialled` in the approve response, and `left_behind` for campaigns stopped after their plan was built. |
| "A campaign vanished from the plan" | Paused or killed in Formi, hidden here, or the warehouse has no lead above `dte_min`. |
| "The console shows an old version" | `npm ci && npm run build` was skipped on the box. |
| Anything about a specific endpoint | `docs/API_CONTRACT.md` |
