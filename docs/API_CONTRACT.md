# chola-redial — API contract

Base: `http://127.0.0.1:8000`. All bodies JSON. Errors: `{"error": "message"}` with a 4xx/5xx code.

**Safety:** the server boots with `DRY_RUN=1` by default. While set, the dispatcher
never POSTs to Formi — it records exactly what it *would* have sent and marks
items `simulated`. Every commit-style response carries `"dry_run": true` so the UI
can badge it. Only an explicit `DRY_RUN=0` in the environment enables live dialling.

**The approval gate:** nothing in this console dials on its own. Two passes a day
PREPARE a plan and stop; a call is placed only by `POST /api/day/approve` or
`POST /api/runs/{id}/approve`, both of which an operator has to invoke. If nobody
approves, no call goes out that day. Dialling hours are **09:00–20:00 IST**,
enforced server-side.

---

## Core objects

```jsonc
// Campaign
{ "id": 1, "agent_id": 125, "warehouse_id": 1650, "name": "0308Redial -PV Hindi",
  "enabled": true, "paused": false,
  "autopilot": false, "autopilot_note": "",    // see The daily plan below
  // Why it is paused, in words a screen can print: "paused by operator",
  // "paused in the Formi platform", "killed in the Formi platform".
  "stopped_reason": "",
  // The pause took autopilot away; a resume HERE puts it back. Nothing else
  // does — see the pause/resume rows below.
  "autopilot_latched": false,
  "platform_status": "active",                 // last status Formi reported
  // Retired by the operator: absent from every list here and impossible to
  // arm. No sync writes it — see Hidden below.
  "hidden": false }

// Config (versioned; PUT creates a new version, never mutates)
{ "version": 3, "created_at": "2026-08-28T09:12:00",
  "dial_window": { "start": "09:30", "end": "20:00" },   // clamped to 09:00-20:00 IST
  "frequency_table": [
    { "bucket": "F1", "label": "Warm-up",          "from_dte": 45, "to_dte": 32, "calls_per_week": 2, "calls_per_day": 0 },
    { "bucket": "F2", "label": "Early engagement", "from_dte": 31, "to_dte": 24, "calls_per_week": 2, "calls_per_day": 0 },
    { "bucket": "F3", "label": "Building urgency", "from_dte": 23, "to_dte": 16, "calls_per_week": 3, "calls_per_day": 0 },
    { "bucket": "F4", "label": "High frequency",   "from_dte": 15, "to_dte": 8,  "calls_per_week": 3, "calls_per_day": 0 },
    { "bucket": "F5", "label": "Critical window",  "from_dte": 7,  "to_dte": 1,  "calls_per_week": 0, "calls_per_day": 2 },
    { "bucket": "E0", "label": "Expiry window",    "from_dte": 0,  "to_dte": -1, "calls_per_week": 0, "calls_per_day": 2 },
    { "bucket": "F6", "label": "Grace period",     "from_dte": -2, "to_dte": -3, "calls_per_week": 0, "calls_per_day": 2 }
  ],
  "bucket_priority": ["M0","E0","F6","F5","F4","F3","F2","F1","D0"],
  // RED bands, applied AHEAD of bucket_priority. The client's two 2-calls/day
  // rows, in the order they named them: the three days PAST expiry first, then
  // the week running up to it, then everything else. This is what decides who
  // survives when a day is capped or approved with few hours left, so it
  // outranks the bucket order rather than living inside it.
  //
  // Their schedule is signed the other way round (negative = before RED), so
  // their "1 to 3" is dte -1..-3 here and their "-7 to 0" is dte 0..7.
  "red_priority": [[-1, -3], [0, 7]],
  "auto_dispositions": ["did_not_pick","hung_up","unreachable","rnr",
                        "beep_tone_number_busy_not_reachable_switched_off",
                        "voicemail","telephony_failed","dialer_nc",
                        "new","fresh","not_dialed",""],
  // Per-bucket disposition allow-list. A bucket that is absent, or maps to an
  // empty list, INHERITS `auto_dispositions` — so `{}` behaves exactly as before
  // and an operator only pays for the buckets they customise. This NARROWS only:
  // a slug listed here still has to pass the exclusion checks, so putting
  // `do_not_call` under F5 does not make F5 dial it.
  // Valid bucket keys: the frequency-table buckets plus "M0" and "D0".
  "bucket_dispositions": {
    "F5": ["did_not_pick", "hung_up", "voicemail", "unreachable"],
    "F1": ["did_not_pick"]
  },
  "mandatory_days": [1, 0],
  // On a mandatory day the client's rule inverts the exclusion ladder: "for all
  // cases excluding the renewed and DND cases, calls needs to be initiated on
  // RED−1 and RED date, irrespective of the disposition status". So RED−1 and
  // RED dial a `not_interested` or `human_review` lead, and `never_dial` lists
  // the only slugs that still veto — consent, already renewed, bad number.
  // `extra_exclusions` slugs are added to this set automatically.
  // `other_language` and unmapped dispositions also still veto: a call no agent
  // can hold, or one whose disposition we do not recognise, is not a last chance.
  "never_dial": ["do_not_call","dnc","dnd","renewed","already_paid_to_chola",
                 "wrong_number","number_not_working","invalid_number"],
  "calls_per_day_cap": 2,
  "same_day_gap_hours": 3.0,
  "shift_from_last_hours": 2.0,   // time rotation: yesterday 09:00 -> today 11:00
  "max_per_minute": 12,           // load stagger ceiling
  "max_per_run": 5000,            // 0 = unlimited
  "max_attempts": 0
}

// PlanItem
{ "id": 91, "run_id": 4, "lead_uuid": "…", "policy_no": "POL123", "contact_id": "551",
  "lead_name": "…", "disposition": "did_not_pick", "disposition_class": "dnp",
  "dte": 5, "bucket": "F5", "bucket_label": "Critical window", "priority": 2,
  "slot_no": 1, "scheduled_time": "2026-08-28T09:34:00",
  // planned | simulated | posted | failed | skipped
  // expired = its slot fell inside Formi's 5-minute floor by the time the day was
  // approved, so it was retired instead of posted. Not an error, and not a call.
  "status": "planned",
  "http_status": null, "response": null }

// Run
{ "id": 4, "campaign_id": 1, "run_date": "2026-08-28",
  "kind": "auto",                // auto (morning wave) | auto_pm (afternoon) | manual
  "status": "planned",           // planned | committed | paused
  "config_version": 3, "created_at": "…",
  "counts": { "evaluated": 9812, "planned": 1284, "slots": 1602,
              "posted": 0, "failed": 0 } }
```

---

## Endpoints

### Campaigns & config
| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/campaigns` | list; `?include_hidden=true` opts hidden ones back in |
| `POST` | `/api/campaigns/{id}/pause` | sets `paused=true`, blocks approve/commit, **and takes today's un-dialled calls back off Formi's clock**. A pause that leaves calls booked is not a pause. Latches `autopilot` and clears it. |
| `POST` | `/api/campaigns/{id}/resume` | clears the pause and restores the latched `autopilot`. **The only way back** — see the platform-pause latch below. |
| `POST` | `/api/campaigns/{id}/hide` | sets `hidden=true`, clears `autopilot`. Returns the campaign plus `live_today` — see below. |
| `POST` | `/api/campaigns/{id}/unhide` | clears `hidden`. Never re-arms: `autopilot` stays off. |
| `GET` | `/api/campaigns/{id}/config` | current version |
| `PUT` | `/api/campaigns/{id}/config` | body = config; **422** if `dial_window` outside 09:00–20:00 or `start >= end`; returns new version |
| `GET` | `/api/campaigns/{id}/config/history` | `[{version, created_at}]` |
| `DELETE` | `/api/campaigns/{id}` | removes the campaign with its leads, config and runs |

**The platform-pause latch.** Pausing a campaign in the Formi platform pauses it
here too, on the EDGE into `paused` — the next sync or wave cancels its queued
calls and sets `stopped_reason: "paused in the Formi platform"`. Un-pausing it in
Formi does **not** start calls again here. That is deliberate and was asked for:
the campaign stays held until somebody hits `POST /api/campaigns/{id}/resume` on
this console. `autopilot_latched` is how the campaign remembers it was in the
daily plan, so a resume puts it back exactly as it was rather than arming a
campaign nobody armed.

**Hidden.** `hidden` is the operator's own decision to take a campaign out of
circulation for good. `GET /api/campaigns` leaves hidden campaigns out unless
asked, and that is the only list the console loads campaigns from, so hiding
removes one from the campaign switcher, the config screen, the dial log filter
and the picker at once. The day path (`ARMED` in `api/day.py`) requires
`hidden=0`, and `POST /api/campaigns/{id}/autopilot {"on": true}` returns **409**
for a hidden campaign — a stale client cannot put one back in the plan.

No sync writes `hidden` (`upsert_campaign` names its SET columns), unlike
`enabled` and `paused`, and it defaults to `0`, so a campaign created in Formi
appears here on its own. Hiding does **not** set `autopilot_latched`: that latch
is pause/resume's, and borrowing it would let a later resume silently re-arm a
hidden campaign. Un-hiding restores visibility only — arming is a separate act.

Two screens send these, and both ask for `?include_hidden=true` — nothing else
in the console does. The day's picker hides one row at a time, in the moment it
is noticed; **Settings → Campaign visibility** (`#visibility`) lists the scoped
agent's campaigns, hidden included, and hides or un-hides a batch of them one
request at a time. It is scoped like every other screen — the agent tabs reach
the other agent's list — but it is outside the "pick a campaign first" gate: it
is the way back from hiding, so it has to work when the campaign switcher is
empty. `GET /api/agents` keeps a row for an agent whose campaigns are all
hidden, so the scope can never strand one out of reach.

Hiding does not reach out and cancel calls already accepted by Formi for today;
`live_today` in the response says how many are still to go, and `pause` is the
switch that takes them back. While such a campaign still has slots ahead of the
clock it stays in `GET /api/day`'s `stopped` list with
`why: "hidden — the calls it already put on Formi's clock today are still going
out"`, so a live campaign is never entirely off-screen.

### The daily plan (autopilot)

`campaigns.autopilot` means **"include this campaign in the daily plan"**. It is
not a dialler. Twice a day a pass re-reads campaign status, re-syncs those
campaigns' leads and PREPARES a plan for each — and stops there, leaving the run
`planned`. There is no path from a pass to Formi.

Pass times come from `AUTOPILOT_AM` (default `10:00`) and `AUTOPILOT_PM`
(default `15:00`), IST. Two waves because the client's rule is "second call only
if the first is not answered": the afternoon plan is built *after* a re-sync, so
it only reaches leads whose disposition still says nobody picked up. Each wave
needs its own approval.

The same tick settles the dial log every 10 minutes between 09:00 and 21:00 (see
Call log). Verification is read-only and cannot place a call.

A campaign leaves the daily plan when it is paused here, when it is paused or
killed in Formi, or when the warehouse holds no lead with a RED at or above
`dte_min` (−3) whose stage is not terminal. A warehouse it cannot reach never
counts as "finished", and a pass whose re-sync failed skips that campaign rather
than planning against stale leads.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/autopilot` | `{passes, dials: false, now, fired_today, campaigns[]}`. `dials` is always `false` and is said out loud so a screen can repeat it. `now` is the server's IST clock as `HH:MM` — pass times are IST and the browser is not, so "has 10:00 gone by?" is only answerable here. A pass whose `at` is `<= now` and is absent from `fired_today` was missed; it fires once a day and is never retried. |
| `POST` | `/api/campaigns/{id}/autopilot` | `{ "on": true \| false }` → the campaign; **409** if disabled. Switching it on never places a call. |
| `POST` | `/api/autopilot/run` | `{ "kind": "auto" \| "auto_pm", "date"? }` — prepare a wave now; safe to repeat, an already-approved wave answers `already_ran` |

Run kinds: `auto` (morning wave), `auto_pm` (afternoon wave), `manual`.

### The day

One page for the whole day across every campaign in the plan, and one approval.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/day?date=&kind=` | the whole day. Cheap by construction — two `GROUP BY`s over rows the console already wrote, so a screen may poll it all day without costing a warehouse query. Never re-runs the engine. |
| `POST` | `/api/day/prepare` | `{date?, kind?, resync?}` → builds `planned` runs for every campaign in the plan. **Dials nothing.** `resync: true` (what a pass sets) re-reads campaign status and re-pulls leads first. |
| `POST` | `/api/day/approve` | `{date?, kind?, buckets?[], campaign_ids?[]}` → **dials.** Empty `buckets` means every bucket; empty `campaign_ids` means every campaign with a plan waiting. |

`status` is one of `no_campaigns` · `not_prepared` · `awaiting_approval` ·
`approved`, so the screen has one thing to switch on rather than four counters to
interpret.

```jsonc
// GET /api/day
{ "date": "2026-09-09", "kind": "auto", "wave": "morning",
  "now": "11:04", "dry_run": true,
  "window": { "start": "09:00", "end": "20:00" }, "window_open": true,
  "status": "awaiting_approval",
  "totals": { "campaigns": 4, "ready": 633, "posted": 0, "failed": 0, "dropped": 0 },
  // A ceiling, not a promise: minutes left in the window x max_per_minute.
  // Approve re-plans, so the real number is decided then — but an operator
  // opening this at 18:00 has to see the day no longer fits BEFORE approving.
  "capacity_before_close": 633,
  "red_bands": [ { "rank": 0, "dte_from": 3, "dte_to": 1,
                   "label": "renewal due in 1-3 days", "ready": 210 },
                 { "rank": 2, "dte_from": null, "dte_to": null,
                   "label": "outside the priority bands", "ready": 12 } ],
  // Best RED band first, then the bucket order inside it — the same order
  // approve dials in, so the screen cannot promise a sequence the dispatcher
  // will not honour.
  "buckets": [ { "bucket": "M0", "label": "Mandatory day", "ready": 196, "best_rank": 0 } ],
  "campaigns": [ { "id": 1650, "name": "…", "run_id": 912,
                   "run_status": "planned",   // or "not_prepared"
                   "ready": 312, "by_bucket": { "M0": 96 },
                   "posted": 0, "failed": 0, "dropped": 0 } ],
  // "Why is nothing happening for X" — armed campaigns now held, with the reason.
  "stopped": [ { "id": 1644, "name": "…", "why": "paused in the Formi platform" } ],
  "dial_log": { "dialled": 88, "queued": 12, "missing": 1 } }
```

**Approving late does not dial into the night.** `approve` RE-PLANS each campaign
from the current minute with the buckets the operator ticked, then commits it, so
only what genuinely fits before the window shuts is scheduled — best RED band
first. Whatever does not fit is not dialled today and returns in tomorrow's plan
(`not_dialled` in the response). Approving a wave twice does not dial twice: a run
that is no longer `planned` answers `already_committed`.

```jsonc
// POST /api/day/approve
{ "date": "2026-09-09", "kind": "auto", "wave": "morning", "dry_run": true,
  "buckets": ["M0","F5"],        // or "all"
  "approved": 4, "posted": 461, "failed": 0, "not_dialled": 172,
  "campaigns": [ { "campaign_id": 1650, "name": "…", "status": "approved",
                   "run_id": 913, "posted": 210, "failed": 0, "dropped": 0,
                   "expired": 0, "simulated": 210 },
                 // Did not dial, and says why. `detail` is present for
                 // window_closed / not_dialled / error; `run_id` for every
                 // outcome except not_prepared, where there is no run to act on.
                 { "campaign_id": 1651, "name": "…", "status": "window_closed",
                   "run_id": 914,
                   "detail": "the 10:00-19:00 window has closed (it is 19:24)" } ] }
```

A per-campaign `status` is one of `approved` · `not_prepared` ·
`already_committed` · `already_paused` · `nothing_to_dial` · `window_closed` ·
`not_dialled` · `error`.

**Every outcome that is not a clean dial is also written to that campaign's
`autopilot_note`** (`"2026-09-13 auto: NOT dialled — window_closed: …"`), and a
partial records the split (`"dialled 210, 12 refused by Formi"`). The response is
the only copy otherwise, and for `window_closed` and `error` there is no run row
either — so a campaign that failed to start would leave nothing behind once the
caller dropped the response. `already_committed` is the exception: that campaign
*did* dial, on an earlier approve, and keeps the note saying how that went.

`failed` and `not_dialled` are not the same kind of thing. `failed` is Formi
refusing a call — a fault, and what `POST /api/runs/{id}/retry` sends again.
`not_dialled` is a slot that no longer fitted before the window shut; it returns
in the next plan on its own, and retrying it would only expire it again.

### Call log

Two separate facts, never merged — merging them is why the console could not
answer "did the call actually happen?".

* **`outcome`** — what Formi's API answered when we POSTed: `posted` · `failed` ·
  `simulated`. A 2xx says the request was *accepted*, nothing more.
* **`verified`** — what the warehouse says later: `pending` (sent, not checked
  yet) · `queued` (Formi holds the slot, has not dialled it) · `dialled` (an
  interaction with a call stage exists — **the only proof a call happened**) ·
  `missing` (sent, nothing came back) · `simulated` (nothing was sent).

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/dial-log?date=&campaign_id=&run_id=&verified=&outcome=&limit=&offset=` | `{total, limit, offset, rows[]}` |
| `GET` | `/api/dial-log/summary?date=&campaign_id=` | `{date, campaigns:[{campaign_id, sent, outcome{}, verified{}, talk_time_sec}]}` — the rolled-up counts the day screen shows, so two screens cannot disagree |
| `POST` | `/api/dial-log/verify?date=&wait=` | read the warehouse back now. Read-only there, writes only to the local log, so it is unaffected by `DRY_RUN` and **can never place a call**. `wait=true` blocks for the result; otherwise it returns immediately and settles off-thread. |

A row is written for every call the console sends, dry runs included, at the
moment it is sent — never from an inference afterwards. Rows are pruned after
`DIAL_LOG_RETENTION_DAYS` (default 30).

### Planning & review
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/campaigns/{id}/plan` | body `{ "date": "YYYY-MM-DD" }` (default today). Runs the engine, writes a run + items with status `planned`. **Never dials.** Idempotent per (campaign, date, kind) — re-planning replaces the existing `planned` run; refuses if already `committed`. |
| `GET` | `/api/campaigns/{id}/buckets?date=` | the bucket × disposition matrix — see below |
| `GET` | `/api/runs?campaign_id=&limit=` | history |
| `GET` | `/api/runs/{id}` | run + counts |
| `GET` | `/api/runs/{id}/items?bucket=&disposition=&status=&page=&page_size=` | paged; default page_size 50 |
| `POST` | `/api/runs/{id}/approve` | **dials** — one campaign's run, the per-campaign twin of `POST /api/day/approve`. 409 if the campaign is paused or the run is not `planned`. Under DRY_RUN marks items `simulated`. |
| `POST` | `/api/runs/{id}/pause` | takes a `committed` run's un-dialled calls back off Formi's clock and marks it `paused`. Already-dialled calls stay in history. 409 if not `committed`. |
| `POST` | `/api/runs/{id}/resume` | puts a `paused` run's remainder back on Formi's clock, edits included. 409 if not `paused`, or if the campaign is paused. **Dials.** |
| `POST` | `/api/runs/{id}/retry` | sends the slots Formi refused a second time. 409 if the run is not `committed`, if the campaign is paused, or if nothing in the run is `failed`. **Dials.** |
| `DELETE` | `/api/runs/{id}` | discard a plan. 409 for anything not `planned` — a committed run is dial history and is kept. |

`retry` is an approve over a smaller set, not a second dial path: it puts the
`failed` items back to `planned` and calls the same commit every other dial goes
through. Three consequences worth knowing before you call it:

- **Only the refused slots go.** Anything already on Formi's clock is untouched —
  a retry that re-posted those would dial the customer twice.
- **A refused slot whose time has passed does not dial late.** It is retired as
  `expired`, exactly as an unapproved plan's stale slots are, and the lead comes
  back in the next plan. So `retried` can exceed the number actually posted.
- **`runs.failed` is corrected, not added to.** The old tally is taken off before
  the re-post, so a run that retries clean reads `failed: 0` rather than carrying
  a rejection it has already cleared.

```jsonc
// 200 — the run, plus what this call did
{ "id": 41, "status": "committed", "counts": { "posted": 308, "failed": 0 },
  "retried": 12,        // how many refused slots were sent again
  "expired": 0,         // of those, how many had run out of clock
  "simulated": 12,      // non-zero only under DRY_RUN
  "dry_run": true }
```

`GET /api/campaigns/{id}/buckets` returns both dimensions plus the crosstab:

```jsonc
{ "date": "2026-08-28", "total_leads": 9812,
  "buckets": [ { "bucket": "F5", "label": "Critical window",
                 "eligible": 220, "waiting": 40, "manual_only": 0, "total": 260 } ],
  "dispositions": [ { "disposition": "did_not_pick", "class": "dnp",
                      "auto": true, "eligible": 700, "total": 900 },
                    { "disposition": "positive_followup", "class": "callback",
                      "auto": false, "eligible": 0, "total": 412 } ],
  "matrix": [ { "bucket": "F5", "disposition": "did_not_pick", "count": 180 } ],
  "skips": { "CADENCE_WAIT": 3100, "MANUAL_ONLY": 980, "STAGE_TERMINAL": 2400,
             "BUCKET_DISPOSITION_OFF": 410 } }
```

`BUCKET_DISPOSITION_OFF` is new: the lead's bucket carries its own allow-list and
this disposition is not on it. It is distinct from `MANUAL_ONLY` — that one is a
property of the disposition everywhere, this one is a per-bucket choice the
operator made, and the UI should say so ("F1 does not chase voicemail") and link
to the bucket's row on the config screen.

`PUT /api/campaigns/{id}/config` returns **422** for an unknown bucket key in
`bucket_dispositions`.

### Manual redial
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/manual/preview` | `{campaign_id, dispositions[], buckets[], date?}` → `{count, slots, sample[]}` |
| `POST` | `/api/manual/schedule` | same body → creates a `kind:"manual"` run in `planned`. Approve it the same way. |

Manual mode bypasses the `auto_dispositions` allow-list — that is its purpose —
but **never** bypasses exclusions. `do_not_call`, `dnc`, `wrong_number`,
`number_not_working` and other `excluded`-class leads are rejected server-side
even if the UI asks for them. This is regulatory (TRAI/NCPR), not a preference.

### Bulk lead edits
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/stage/policies/preview` | multipart or `{policies: [...], target_stage, campaign_ids?}` → what would change |
| `POST` | `/api/stage/policies/commit` | wraps `mark_stage_by_policy` |
| `POST` | `/api/stage/red/preview` | `{policies: [...], red, campaign_ids?}` → whose renewal date would move |
| `POST` | `/api/stage/red/commit` | writes the corrected date locally; see below |
| `POST` | `/api/stage/expired/preview` | `{campaign_ids[], red_before, target_stage:"policy_expired"}` |
| `POST` | `/api/stage/expired/commit` | wraps `mark_stage_by_red`; keeps `already_paid_to_chola`/`renewed`/`policy_expired` untouched |
| `GET` | `/api/stage/jobs` | history; `kind` is `policies` \| `red` \| `expired` |

`campaign_ids` narrows a policy sweep to the campaigns named. Omitted or empty
means every campaign the policy appears in — the ported default. A policy that
exists but not in the chosen campaigns is returned under `not_found`.

Every preview returns `{ "would_change": N, "unchanged": N, "by_stage": {...}, "sample": [...],
"not_found": [...] }`. For a RED preview `by_stage` is keyed by the date the leads carry
today, and `target_stage` is the new date.

Every commit returns
`{ "applied": N, "applied_formi": N, "applied_local": N, "rejected_formi": N, "job_id": N, "dry_run": bool }`.

**`/api/stage/red/*` is local.** Formi exposes no endpoint that writes a renewal
expiry date, so the correction changes the date THIS console schedules from and
not what the agent reads out on the call — the commit answers
`"applied_formi": 0, "formi_notified": false` and `DRY_RUN` does not gate it,
because nothing is sent. The value is kept in `lead_red_overrides` and
re-applied by `engine.sync` after each refresh (a sync replaces every lead of a
campaign). An override is dropped once the warehouse reports a date matching
neither the correction nor the value it replaced: the source has moved on and
wins.
Stage writes always go to Formi regardless of `LEADS_SOURCE`; `applied_local` counts only
seeded rows (`campaign_id != warehouse_id`), which have no lead id Formi would recognise.
A single `applied` that mixes the two reads as success when nothing reached Formi.

`applied_formi` and `rejected_formi` come from Formi's response **body**, not its status
code. `/bulk-update-stage` answers a partially applied batch with HTTP 200 and the real
numbers in `payload.successful_updates` / `failed_updates` — a lead belonging to another
agent or outlet is skipped into `errors`, not rejected. Counting the 200 would report 200
applied when 12 were.

### Agents

Campaigns belong to an `agent_id` and the console is always scoped to one agent.
Two agents are in use: **125** and **127**. Mixing their campaigns in one view is
how you dial a Hindi script at a Tamil cohort, so the agent is a first-class
selector, not a filter chip.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/agents` | `[{agent_id, name, campaigns, enabled, paused_campaigns, paused}]` |
| `GET` | `/api/campaigns?agent_id=` | scoped list; omitting `agent_id` returns all. Hidden campaigns are excluded from both. |
| `POST` | `/api/agents/{agent_id}/pause` | pauses **every** campaign on that agent |
| `POST` | `/api/agents/{agent_id}/resume` | |

An agent is `paused: true` when every one of its enabled campaigns is paused.
`campaigns`, `enabled` and `paused_campaigns` count only the campaigns the
console shows, so a tab cannot claim 40 while six of them are hidden. An agent
whose campaigns are *all* hidden still gets a row — losing it would drop the
agent out of the switcher entirely.

### Test call

A rehearsal against one known number so an operator can confirm the pipeline is
alive before approving a real run.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/test-call/numbers` | the allow-list, `[{phone, label, campaign_id, lead_uuid, found}]` |
| `POST` | `/api/test-call/preview` | `{phone, campaign_id?}` → resolves the lead and returns the exact payload |
| `POST` | `/api/test-call/trigger` | `{phone, campaign_id?, scheduled_time?}` → schedules that one lead |
| `GET` | `/api/test-call/history` | past attempts with status and response |

```jsonc
// preview / trigger response
{ "found": true, "dry_run": true,
  "lead": { "lead_uuid": "…", "phone": "9379747274", "lead_name": "…",
            "campaign_id": 15, "agent_id": 15, "stage": "did_not_pick" },
  "would_post": { "url": "/v2/campaign/leads/15/{uuid}/schedule",
                  "body": { "scheduled_time": "2026-08-29T14:05:00" } },
  "status": "simulated",        // simulated | posted | failed | not_found
  "http_status": null, "response": null }
```

`scheduled_time` is naive IST, and it must be at least **five minutes** ahead: Formi's
`/schedule` answers 400 "Scheduled time must be at least 5 minutes from now". The console
holds that floor itself — planned slots inside it are retired as `expired` at approve time
rather than posted, an omitted `scheduled_time` defaults to the first minute past the
floor, and an operator-picked one inside it is a 422 before anything reaches the network.

`preview` returns the same shape with `"status": "preview"` — it resolves and
builds, it never dispatches and never writes to the history, so claiming
`simulated` there would badge a no-op as an attempt. Only `trigger` yields
`simulated` / `posted` / `failed`. Both yield `{"found": false, "status":
"not_found"}` (HTTP 200) when the allow-listed number has no lead.

`campaign_id` is optional and scopes resolution: the number is seeded on one
lead per agent, so omitting it resolves the lowest campaign id. Supplying it
also selects that campaign's own `config.test_numbers` as the allow-list.
A lead whose disposition is `excluded`-class is refused with **409**.

**Guardrail — the allow-list is the whole point.** `config.test_numbers` holds the
numbers that may be dialled this way (default `["9379747274"]`). `trigger` returns
**422** for any number not on it. Without this, "test call" is a button that dials
an arbitrary customer, which is exactly what it must never become.

`trigger` still respects `DRY_RUN`: under `DRY_RUN=1` it resolves the lead, builds
the payload, records the attempt and returns `status: "simulated"` **without any
network call**. It is the same dispatch path as `approve`, so a successful
simulated test proves lead resolution and payload shape — not connectivity.
`trigger` ignores campaign pause (a paused campaign is exactly when you want to
rehearse) but never ignores exclusions or the allow-list.

### Sync

The systemd timer (`chola-redial-sync.timer`) pulls campaigns and leads hourly at
`:15`. That is the floor, not the ceiling — a campaign created at 20:20 is
invisible until 21:15 — so the console can ask for the pull itself.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/sync` | start a pull; returns immediately. Asking twice returns the run already in flight rather than starting a second one. Reads only — **nothing here dials**, so it is safe under any `DRY_RUN`. |
| `GET` | `/api/sync` | `{running, ok, error, campaigns, leads}`. `ok: null` means still running. |

State is in process memory, not a table: one process owns the console, the answer
only matters for the minutes the pull takes, and a restart that loses it is
correct — the restart killed the thread too.

### The dry-run switch

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/config/dry-run` | `{"enabled": true}` → back to dry run, free. `{"enabled": false, "confirm": "GO LIVE"}` → **live dialling**; **400** without that exact word. Returns `{dry_run, persisted}`. |

Every dialling helper re-reads `DRY_RUN` as its first statement, so the switch
reaches all of them without a restart — and it is written through to the `DRY_RUN`
line of the deployment's `.env` (`REDIAL_ENV_FILE`, else `<root>/.env`), so it
survives one too. That write replaces the one line and leaves the rest of the file
byte-for-byte; where there is no `.env` to write to, the flip still takes effect in
the process and `persisted` comes back `false` — the UI's cue to stop promising it
lasts. Each flip is printed to the journal, persisted or not: this is the one
control that decides whether real customers get called, and the journal is where
that question gets answered afterwards.

### Misc
`GET /api/health` → `{ "ok": true, "dry_run": true, "db": "redial.db",
"leads_source": "warehouse", "agents": [125, 127], "test_numbers": ["9379747274"] }`

`leads_source` is read from the campaign table, not from `LEADS_SOURCE`: that is
a hand-set string nobody edits after a sync, and the banner an operator checks
before a live dial read "seed" over 22 real campaigns. Seed ids are 1–16 and
warehouse ids 1400+, so the data answers this without being asked.
