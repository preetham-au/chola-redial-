# chola-redial — API contract

Base: `http://127.0.0.1:8000`. All bodies JSON. Errors: `{"error": "message"}` with a 4xx/5xx code.

**Safety:** the server boots with `DRY_RUN=1` by default. While set, the dispatcher
never POSTs to Formi — it records exactly what it *would* have sent and marks
items `simulated`. Every commit-style response carries `"dry_run": true` so the UI
can badge it. Only an explicit `DRY_RUN=0` in the environment enables live dialling.

---

## Core objects

```jsonc
// Campaign
{ "id": 1, "agent_id": 125, "warehouse_id": 1650, "name": "0308Redial -PV Hindi",
  "enabled": true, "paused": false,
  "autopilot": false, "autopilot_note": "" }   // see Autopilot below

// Config (versioned; PUT creates a new version, never mutates)
{ "version": 3, "created_at": "2026-08-28T09:12:00",
  "dial_window": { "start": "09:30", "end": "19:00" },   // clamped to 09:00-19:00
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
  "status": "planned",           // planned | simulated | posted | failed | skipped
  "http_status": null, "response": null }

// Run
{ "id": 4, "campaign_id": 1, "run_date": "2026-08-28", "kind": "auto",  // auto | manual
  "status": "planned",           // planned | approved | committed | failed
  "config_version": 3, "created_at": "…",
  "counts": { "evaluated": 9812, "planned": 1284, "slots": 1602,
              "posted": 0, "failed": 0 } }
```

---

## Endpoints

### Campaigns & config
| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/campaigns` | list |
| `POST` | `/api/campaigns/{id}/pause` | sets `paused=true`; blocks approve/commit |
| `POST` | `/api/campaigns/{id}/resume` | |
| `GET` | `/api/campaigns/{id}/config` | current version |
| `PUT` | `/api/campaigns/{id}/config` | body = config; **422** if `dial_window` outside 09:00–19:00 or `start >= end`; returns new version |
| `GET` | `/api/campaigns/{id}/config/history` | `[{version, created_at}]` |
| `DELETE` | `/api/campaigns/{id}` | removes the campaign with its leads, config and runs |

### Autopilot

Switched on per campaign; the server then runs that campaign until there is
nothing left to call. Two passes a day, each preceded by a re-sync of that
campaign's leads so the afternoon call only goes to leads that did not pick up.

* `M0 E0 F6 F5` (RED−7 … RED+3) are planned **and dialled** unattended.
* `F4 F3 F2 F1 D0` are planned as a `review` run and wait for `POST /api/runs/{id}/approve`.

The line between the two is `URGENT` / `REVIEW_BUCKETS` in `api/autopilot.py`.
Pass times come from `AUTOPILOT_AM` (default `10:00`) and `AUTOPILOT_PM`
(default `15:00`), IST.

It stops by itself when the campaign is paused, when it is killed in Formi, or
when the warehouse holds no lead with a RED at or above `dte_min` (−3) whose
stage is not terminal. A warehouse it cannot reach never counts as "finished",
and a pass whose re-sync failed is skipped rather than run against stale leads.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/autopilot` | `{passes, urgent_buckets, review_buckets, fired_today, campaigns[]}` |
| `POST` | `/api/campaigns/{id}/autopilot` | `{ "on": true \| false }` → the campaign; **409** if disabled |
| `POST` | `/api/autopilot/run` | `{ "kind": "auto" \| "auto_pm", "date"? }` — fire a pass now; safe to repeat, an already-committed pass answers `already_ran` |

Run kinds: `auto` (morning, dialled), `auto_pm` (afternoon, dialled), `review`
(morning, awaiting approval), `manual`.

### Planning & review
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/campaigns/{id}/plan` | body `{ "date": "YYYY-MM-DD" }` (default today). Runs the engine, writes a run + items with status `planned`. **Never dials.** Idempotent per (campaign, date, kind) — re-planning replaces the existing `planned` run; refuses if already `committed`. |
| `GET` | `/api/campaigns/{id}/buckets?date=` | the bucket × disposition matrix — see below |
| `GET` | `/api/runs?campaign_id=&limit=` | history |
| `GET` | `/api/runs/{id}` | run + counts |
| `GET` | `/api/runs/{id}/items?bucket=&disposition=&status=&page=&page_size=` | paged; default page_size 50 |
| `POST` | `/api/runs/{id}/approve` | **the only path that dials.** 409 if campaign paused or run not `planned`. Under DRY_RUN marks items `simulated`. |

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

### Bulk stage update
| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/stage/policies/preview` | multipart or `{policies: [...], target_stage}` → what would change |
| `POST` | `/api/stage/policies/commit` | wraps `mark_stage_by_policy` |
| `POST` | `/api/stage/expired/preview` | `{campaign_ids[], red_before, target_stage:"policy_expired"}` |
| `POST` | `/api/stage/expired/commit` | wraps `mark_stage_by_red`; keeps `already_paid_to_chola`/`renewed`/`policy_expired` untouched |
| `GET` | `/api/stage/jobs` | history |

Both preview endpoints return `{ "would_change": N, "unchanged": N, "by_stage": {...}, "sample": [...] }`.

Both commit endpoints return `{ "applied": N, "applied_formi": N, "applied_local": N, "job_id": N, "dry_run": bool }`.
Stage writes always go to Formi regardless of `LEADS_SOURCE`; `applied_local` counts only
seeded rows (`campaign_id != warehouse_id`), which have no lead id Formi would recognise.
A single `applied` that mixes the two reads as success when nothing reached Formi.

### Agents

Campaigns belong to an `agent_id` and the console is always scoped to one agent.
Two agents are in use: **15** and **127**. Mixing their campaigns in one view is
how you dial a Hindi script at a Tamil cohort, so the agent is a first-class
selector, not a filter chip.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/agents` | `[{agent_id, name, campaigns, enabled, paused_campaigns, paused}]` |
| `GET` | `/api/campaigns?agent_id=` | scoped list; omitting `agent_id` returns all |
| `POST` | `/api/agents/{agent_id}/pause` | pauses **every** campaign on that agent |
| `POST` | `/api/agents/{agent_id}/resume` | |

An agent is `paused: true` when every one of its enabled campaigns is paused.

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

### Misc
`GET /api/health` → `{ "ok": true, "dry_run": true, "db": "redial.db",
"leads_source": "seed", "agents": [15, 127], "test_numbers": ["9379747274"] }`
