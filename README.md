# chola-redial — backend

RED-date redial scheduling console. FastAPI + SQLite, runs offline with zero
credentials. The API is specified by `docs/API_CONTRACT.md`; that document wins
any disagreement with this one.

## Start it

```bash
pip install -r requirements.txt
python -m engine.sync                       # REAL campaigns + leads for agents 125/127
uvicorn api.main:app --port 8000            # http://127.0.0.1:8000/api/health
```

`engine.sync` needs `.env` (`METABASE_URL` / `METABASE_API_KEY` / `METABASE_DB_ID`);
it reads the warehouse only, and never writes to Formi. Without credentials use
`python -m engine.seed` instead — the same schema filled with an invented
dataset, so the console is demoable offline. Each command clears the other's
campaigns, so the store is only ever all-real or all-synthetic.

```bash
python -m engine.sync --campaigns 90 --leads 20000   # widen the default caps
python -m engine.sync --keep-local                   # keep untouched campaigns
```

Pick the cap generously. Ordering is newest-first and test campaigns are no
longer filtered out of the sync, so they take the top slots -- 56 campaigns are
eligible and a cap of 40 silently drops eleven real ones. `chola-redial-sync.timer`
on the VM runs `--campaigns 90` for that reason.

Defaults: the 20 newest campaigns that have leads with a parseable RED, 5,000
leads each, plus whichever campaign currently holds a `test_numbers` lead. Both
caps are printed as `CAP:` lines — a truncated sync says so.

Campaigns whose name contains a word like `test`, `dev`, `demo` or `killed` are
skipped and listed on a `non-production:` line. "Newest, has leads, has a RED"
otherwise describes a test campaign exactly, and approving one under `DRY_RUN=0`
dials the real numbers inside it. Matching is on whole words, so `Contest_Aug`
survives. `--force-campaigns` overrides it for a specific id.

On Windows `run.bat` does all three (it seeds only if `redial.db` is missing).
The React dev server on `http://localhost:5173` is allowed by CORS.

```bash
pytest                                      # 163 passing, no network, no credentials
```

## How a day works

Nothing dials on its own. The clock only ever *prepares*:

1. **10:00** — the morning pass re-reads campaign status from Formi, re-syncs the
   leads of every campaign in the daily plan, and writes a `planned` run for each.
   It stops there.
2. **An operator opens the day screen** and approves it, choosing which buckets to
   call. That is the only thing that reaches Formi.
3. **15:00** — the afternoon pass does the same again, off a fresh re-sync, so the
   second call only goes to leads whose disposition still says nobody picked up.
   It needs its own approval.

If nobody approves, no call goes out that day. Approving late does not dial into
the night: approve re-plans from the current minute, so only what fits before
20:00 is scheduled and the rest returns in tomorrow's plan — best RED band first
(`red_priority`: the 1–3 days past RED, then RED day and the week before it).

Pausing a campaign **in the Formi platform** pauses it here and takes its queued
calls back off Formi's clock. Un-pausing it there does *not* restart calls here —
only `POST /api/campaigns/{id}/resume` on this console does.

## Did the call actually happen?

`dial_log` records every call this console sends, dry runs included, at the moment
it is sent. It keeps two facts apart:

* `outcome` — what Formi's API answered (`posted` / `failed` / `simulated`). A 2xx
  means the request was accepted, nothing more.
* `verified` — what the warehouse says later (`pending` / `queued` / `dialled` /
  `missing` / `simulated`). **`dialled` is the only proof a call happened.**

The autopilot tick reads the log back against the warehouse every 10 minutes
between 09:00 and 21:00 (an hour past the window, because a call booked at 19:59
is dialled after it shuts). That read is SELECT-only against the warehouse and
writes only to the local log, so it is unaffected by `DRY_RUN` and can never place
a call. Nothing open means no query and no log line, so an idle day costs nothing.

## Safety

`DRY_RUN` defaults to **1** and everything reads it at call time.

* Three endpoints can dial: `POST /api/day/approve`, `POST /api/runs/{id}/approve`
  and `POST /api/runs/{id}/resume`. All three need an operator. Under DRY_RUN they
  mark every item `simulated` and store the exact URL and body they would have
  posted; `requests` is imported *after* the guard, so a dry run has no code path
  to the network at all.
* The stage-update commits behave the same way: `applied: 0`, `dry_run: true`.
* `tests/test_api.py` patches `requests.post`/`Session.request` to raise and
  then drives approve and both commits, so a regression here fails the suite.

Live dialling needs an explicit `DRY_RUN=0` **and** a Formi credential in the
environment. `POST /api/config/dry-run` flips it without a restart at the cost of
typing `GO LIVE`, and deliberately does not write to `.env` — a restart returns to
whatever the file says.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `1` | `0` (or `false`) enables live dialling. Anything else is a dry run. |
| `LEADS_SOURCE` | — | **Not read.** Planning always uses the local `leads` table — whatever `engine.sync` or `engine.seed` last put there. A live per-plan warehouse re-query was documented here but never built; `engine.sync` is how leads get in. `/api/health` reports `seed` or `warehouse` by looking at the campaign ids it actually holds, so it cannot disagree with the data. |
| `REDIAL_DB` | `./redial.db` | SQLite path. |
| `FORMI_API_KEY` | — | Only read on a live (`DRY_RUN=0`) write. The name the rest of the Chola tooling uses; `FORMI_TOKEN` is honoured too and wins if both are set. |

## Layout

```
api/schema.sql       tables + the indices the contract's filters need
api/db.py            connection factory (WAL, Row factory), versioned config
api/main.py          app, CORS, {"error": ...} handlers, /api/health, sync, dry-run
api/routes_core.py   campaigns, config, plan, buckets, runs, approve, manual
api/routes_stage.py  bulk stage preview/commit, job history
api/autopilot.py     the clock: two passes a day that PREPARE, and never dial
api/day.py           the approval gate — one day, one screen, one Approve
api/dial_log.py      every call sent, and the warehouse read-back that proves it
engine/red_engine.py vendored decision logic (whether to call) — do not edit
engine/dispatcher.py RED bands, priority, two-slot F5/E0/F6, rotation, stagger
engine/seed.py       deterministic offline dataset + the one lead source
engine/sync.py       pulls the real warehouse campaigns/leads into redial.db
engine/stage_ops.py  mark_stage_by_policy / mark_stage_by_red, vendored
web/                 React + Vite console (npm run dev, npm run check)
```

### Dispatcher rules

1. **RED band, then priority** — `config.red_priority` (`[[3,1],[0,-7]]`) is applied
   first: a renewal due in the next three days outranks everything, then the RED
   day and the week after it, then the rest. Inside a band, `config.priority_of(bucket)`
   (`M0 E0 F6 F5 F4 F3 F2 F1 D0`). `max_per_run` sheds from the tail, so what gets
   dropped is the furthest from its renewal. The count lands in `runs.dropped`.
2. **Two slots** — F5/E0/F6 (`calls_per_day == 2`) get `slot_no` 1 and 2, slot 2 at
   least `same_day_gap_hours` later. If it will not fit before the window closes,
   slot 1 is emitted alone.
3. **Rotation** — today's minute is `(last call's minute-of-day + shift_from_last_hours)`
   wrapped into the window, ported from `schedule_redials.py`. Never-called leads
   are spread uniformly.
4. **Stagger** — at most `max_per_minute` calls per minute; overflow moves to the
   next free minute inside the window.

The dial window is clamped to **09:00–20:00 IST** in `engine/dispatcher.py`, not
just at the API edge, so every caller gets the same rule. `PUT .../config` returns
422 for a start before `09:00`, an end after `20:00`, or `start >= end`.
