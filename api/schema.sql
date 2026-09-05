-- chola-redial local store. SQLite, one file (redial.db).
-- Nothing here is a Formi mirror: `leads` exists only so the app is explorable
-- offline (LEADS_SOURCE=seed). With LEADS_SOURCE=metabase it stays empty.

-- `autopilot` is the console's own switch and is NEVER written by a sync: the
-- operator turns it on here and only this app, or a killed campaign, turns it
-- off. It ends by itself when every lead is past the grace floor or in a
-- terminal stage (api/autopilot.py).
CREATE TABLE IF NOT EXISTS campaigns (
  id            INTEGER PRIMARY KEY,
  agent_id      INTEGER NOT NULL,
  warehouse_id  INTEGER NOT NULL,
  name          TEXT    NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  paused        INTEGER NOT NULL DEFAULT 0,
  autopilot     INTEGER NOT NULL DEFAULT 0,
  autopilot_note TEXT   NOT NULL DEFAULT ''   -- why it last stopped, or last pass
);

-- Versioned and append-only: a PUT inserts, it never updates. The current
-- config is simply MAX(version) for the campaign.
CREATE TABLE IF NOT EXISTS config (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  version     INTEGER NOT NULL,
  created_at  TEXT    NOT NULL,
  body        TEXT    NOT NULL,          -- the whole config object as JSON
  UNIQUE (campaign_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id    INTEGER NOT NULL REFERENCES campaigns(id),
  run_date       TEXT    NOT NULL,       -- YYYY-MM-DD
  kind           TEXT    NOT NULL,       -- auto | manual
  status         TEXT    NOT NULL,       -- planned | committed | paused
  config_version INTEGER NOT NULL,
  created_at     TEXT    NOT NULL,
  dry_run        INTEGER NOT NULL DEFAULT 1,
  evaluated      INTEGER NOT NULL DEFAULT 0,
  planned        INTEGER NOT NULL DEFAULT 0,
  slots          INTEGER NOT NULL DEFAULT 0,
  posted         INTEGER NOT NULL DEFAULT 0,
  failed         INTEGER NOT NULL DEFAULT 0,
  dropped        INTEGER NOT NULL DEFAULT 0,  -- shed by max_per_run (lowest priority first)
  note           TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_runs_campaign ON runs(campaign_id, run_date, kind, status);

CREATE TABLE IF NOT EXISTS plan_items (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  lead_uuid         TEXT,
  policy_no         TEXT,
  contact_id        TEXT,
  phone             TEXT,                -- dialable number, copied off the lead at plan time
  lead_name         TEXT,
  disposition       TEXT,
  disposition_class TEXT,
  dte               INTEGER,
  bucket            TEXT,
  bucket_label      TEXT,
  priority          INTEGER,
  slot_no           INTEGER NOT NULL DEFAULT 1,
  scheduled_time    TEXT,                -- YYYY-MM-DDTHH:MM:SS, naive IST
  status            TEXT NOT NULL,       -- planned | simulated | posted | failed | skipped | expired
  http_status       INTEGER,
  response          TEXT
);
-- The contract filters items by run_id + bucket / disposition / status.
CREATE INDEX IF NOT EXISTS ix_items_run         ON plan_items(run_id, id);
CREATE INDEX IF NOT EXISTS ix_items_bucket      ON plan_items(run_id, bucket);
CREATE INDEX IF NOT EXISTS ix_items_disposition ON plan_items(run_id, disposition);
CREATE INDEX IF NOT EXISTS ix_items_status      ON plan_items(run_id, status);

-- Append-only audit of every lead the engine looked at, skips included.
CREATE TABLE IF NOT EXISTS decisions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  lead_uuid         TEXT,
  policy_no         TEXT,
  disposition       TEXT,
  disposition_class TEXT,
  dte               INTEGER,
  bucket            TEXT,
  action            TEXT NOT NULL,       -- SCHEDULE | CADENCE_WAIT | MANUAL_ONLY | ...
  reason            TEXT NOT NULL,
  scheduled         INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_run    ON decisions(run_id, action);
CREATE INDEX IF NOT EXISTS ix_decisions_bucket ON decisions(run_id, bucket);

CREATE TABLE IF NOT EXISTS stage_jobs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,            -- policies | expired
  mode         TEXT NOT NULL,            -- preview | commit
  target_stage TEXT NOT NULL,
  params       TEXT NOT NULL,            -- JSON echo of the request
  would_change INTEGER NOT NULL DEFAULT 0,
  unchanged    INTEGER NOT NULL DEFAULT 0,
  committed    INTEGER NOT NULL DEFAULT 0,
  dry_run      INTEGER NOT NULL DEFAULT 1,
  by_stage     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL
);

-- Seed dataset (LEADS_SOURCE=seed only).
CREATE TABLE IF NOT EXISTS leads (
  id                    INTEGER PRIMARY KEY,
  campaign_id           INTEGER NOT NULL REFERENCES campaigns(id),
  lead_uuid             TEXT NOT NULL,
  policy_no             TEXT,
  contact_id            TEXT,
  lead_name             TEXT,
  phone                 TEXT,            -- 10-digit Indian mobile
  stage                 TEXT NOT NULL DEFAULT '',
  red                   TEXT,            -- deliberately free text, like Formi
  last_interaction_time TEXT,
  total_interactions    INTEGER NOT NULL DEFAULT 0,
  calls_today           INTEGER NOT NULL DEFAULT 0,
  calls_last_7d         INTEGER NOT NULL DEFAULT 0,
  -- Interactions already sitting on today's clock with no call_stage yet: a call
  -- somebody scheduled in Formi directly. The engine skips these so this console
  -- never double-books a lead the main system is about to dial.
  queued_today          INTEGER NOT NULL DEFAULT 0,
  callback_date         TEXT,
  appointment_date      TEXT
);
CREATE INDEX IF NOT EXISTS ix_leads_campaign ON leads(campaign_id);
CREATE INDEX IF NOT EXISTS ix_leads_policy   ON leads(policy_no);
CREATE INDEX IF NOT EXISTS ix_leads_phone    ON leads(phone);

-- Every test-call attempt, allow-listed by config.test_numbers. Separate from
-- plan_items on purpose: a rehearsal belongs to no run, so hanging it off one
-- would need a synthetic run row and would pollute the run history the operator
-- approves from.
CREATE TABLE IF NOT EXISTS test_calls (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at     TEXT    NOT NULL,
  phone          TEXT    NOT NULL,
  campaign_id    INTEGER,
  agent_id       INTEGER,
  lead_uuid      TEXT,
  lead_name      TEXT,
  disposition    TEXT,
  scheduled_time TEXT,
  status         TEXT    NOT NULL,       -- simulated | posted | failed | not_found
  dry_run        INTEGER NOT NULL DEFAULT 1,
  would_post     TEXT,                   -- JSON: exactly what was (or would be) sent
  http_status    INTEGER,
  response       TEXT
);
