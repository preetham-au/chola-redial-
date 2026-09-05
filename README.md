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
pytest                                      # 47 tests, no network, no credentials
```

## Safety

`DRY_RUN` defaults to **1** and everything reads it at call time.

* `POST /api/runs/{id}/approve` is the only endpoint that could dial. Under
  DRY_RUN it marks every item `simulated` and stores the exact URL and body it
  would have posted; `requests` is imported *after* the guard, so a dry run has
  no code path to the network at all.
* The stage-update commits behave the same way: `applied: 0`, `dry_run: true`.
* `tests/test_api.py` patches `requests.post`/`Session.request` to raise and
  then drives approve and both commits, so a regression here fails the suite.

Live dialling needs an explicit `DRY_RUN=0` **and** a Formi credential in the
environment. There is no other switch.

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
api/main.py          app, CORS, {"error": ...} handlers, /api/health
api/routes_core.py   campaigns, config, plan, buckets, runs, approve, manual
api/routes_stage.py  bulk stage preview/commit, job history
engine/red_engine.py vendored decision logic (whether to call) — do not edit
engine/dispatcher.py priority, two-slot F5/E0/F6, time rotation, load stagger
engine/seed.py       deterministic offline dataset + the one lead source
engine/sync.py       pulls the real warehouse campaigns/leads into redial.db
engine/stage_ops.py  mark_stage_by_policy / mark_stage_by_red, vendored
```

### Dispatcher rules

1. **Priority** — sorted by `config.priority_of(bucket)` (`M0 E0 F6 F5 F4 F3 F2 F1 D0`).
   `max_per_run` sheds from the tail, so the leads that get dropped are the ones
   furthest from expiry. The count lands in `runs.dropped`.
2. **Two slots** — F5/E0/F6 (`calls_per_day == 2`) get `slot_no` 1 and 2, slot 2 at
   least `same_day_gap_hours` later. If it will not fit before the window closes,
   slot 1 is emitted alone.
3. **Rotation** — today's minute is `(last call's minute-of-day + shift_from_last_hours)`
   wrapped into the window, ported from `schedule_redials.py`. Never-called leads
   are spread uniformly.
4. **Stagger** — at most `max_per_minute` calls per minute; overflow moves to the
   next free minute inside the window.

The dial window is clamped to **09:00–19:00** in `engine/dispatcher.py`, not just
at the API edge, so every caller gets the same rule. `PUT .../config` returns 422
for `08:00`, `20:00`, or `start >= end`.
