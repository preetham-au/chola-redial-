// Shapes mirror docs/API_CONTRACT.md exactly. Do not reshape.

export interface Campaign {
  id: number;
  agent_id: number;
  warehouse_id: number;
  name: string;
  enabled: boolean;
  paused: boolean;
  /** "Include this campaign in the daily plan." It never dials: the server
   *  prepares a plan twice a day and it waits for the day to be approved.
   *  Optional so a backend that predates it still typechecks. */
  autopilot?: boolean;
  /** Why it last stopped, or the result of its last pass. */
  autopilot_note?: string;
}

/** The console is scoped to exactly one agent at a time. `paused` is true only
 *  when every *enabled* campaign on the agent is paused. */
export interface Agent {
  agent_id: number;
  name: string;
  campaigns: number;
  /** how many of them are enabled, not a flag */
  enabled: number;
  paused_campaigns: number;
  paused: boolean;
}

export interface FrequencyRow {
  bucket: string;
  label: string;
  from_dte: number;
  to_dte: number;
  calls_per_week: number;
  calls_per_day: number;
}

export interface DialWindow {
  start: string;
  end: string;
}

export interface Config {
  version: number;
  created_at: string;
  dial_window: DialWindow;
  frequency_table: FrequencyRow[];
  bucket_priority: string[];
  auto_dispositions: string[];
  /** bucket -> allow-list. Absent or empty list = inherit `auto_dispositions`.
   *  Optional: a server on an older build omits the key entirely. */
  bucket_dispositions?: Record<string, string[]>;
  /** Who earns the SECOND daily call in F5/F6/M0. Absent or empty = everyone in
   *  those buckets, which is the historic behaviour. */
  second_call_dispositions?: string[];
  mandatory_days: number[];
  /** Dispositions a mandatory day may NOT override. Absent on an older server. */
  never_dial?: string[];
  calls_per_day_cap: number;
  same_day_gap_hours: number;
  shift_from_last_hours: number;
  max_per_minute: number;
  max_per_run: number;
  max_attempts: number;
}

export interface ConfigVersion {
  version: number;
  created_at: string;
}

export type PlanItemStatus = 'planned' | 'simulated' | 'posted' | 'failed' | 'skipped';

export interface PlanItem {
  id: number;
  run_id: number;
  lead_uuid: string;
  policy_no: string | null;
  contact_id: string;
  phone: string | null;
  lead_name: string | null;
  disposition: string;
  disposition_class: string;
  dte: number;
  bucket: string;
  bucket_label: string;
  priority: number;
  slot_no: number;
  scheduled_time: string;
  status: PlanItemStatus;
  http_status: number | null;
  response: string | null;
}

export interface RunCounts {
  evaluated: number;
  planned: number;
  slots: number;
  posted: number;
  failed: number;
  dropped?: number;
}

export type RunStatus = 'planned' | 'approved' | 'committed' | 'paused' | 'failed';

export interface Run {
  id: number;
  campaign_id: number;
  run_date: string;
  kind: 'auto' | 'manual';
  status: RunStatus;
  config_version: number;
  created_at: string;
  counts: RunCounts;
}

export interface BucketRow {
  bucket: string;
  label: string;
  eligible: number;
  waiting: number;
  manual_only: number;
  total: number;
}

export interface DispositionRow {
  disposition: string;
  class: string;
  auto: boolean;
  eligible: number;
  total: number;
}

export interface MatrixCell {
  bucket: string;
  disposition: string;
  count: number;
}

export interface BucketsResponse {
  date: string;
  total_leads: number;
  buckets: BucketRow[];
  dispositions: DispositionRow[];
  matrix: MatrixCell[];
  skips: Record<string, number>;
}

/** `GET /api/autopilot` — when the passes fire, and which have fired today.
 *  A pass fires once per day; a pass whose time has gone by and is not in
 *  `fired_today` was missed (the warehouse was down) and can be re-fired. */
export interface AutopilotStatus {
  passes: { kind: string; at: string }[];
  /** Always false. A pass PREPARES a plan; the day screen dials it. */
  dials: boolean;
  /** Server clock, IST HH:MM. The pass times are IST too; the browser is not. */
  now: string;
  fired_today: string[];
}

/* --- The day ---------------------------------------------------------------
   `GET /api/day` is the whole console on one page: what is ready, in what
   order, and whether it has been dialled. Nothing goes out until the day is
   approved, so `status` is the only thing an operator has to read. */

export type DayStatus = 'no_campaigns' | 'not_prepared' | 'awaiting_approval' | 'approved';

/** A RED priority band, best first. `dte` is (RED date − today): positive means
 *  the renewal is still ahead, negative means the policy is past its RED date.
 *  The catch-all band that comes last has null bounds. */
export interface DayBand {
  rank: number;
  dte_from: number | null;
  dte_to: number | null;
  label: string;
  ready: number;
}

/** `best_rank` is the best RED band any lead in the bucket sits in — the bucket
 *  list is sorted by it, which is the order approve will dial in. */
export interface DayBucket {
  bucket: string;
  label: string;
  ready: number;
  best_rank: number;
}

export interface DayCampaign extends Campaign {
  run_id: number | null;
  run_status: RunStatus | 'not_prepared';
  ready: number;
  by_bucket: Record<string, number>;
  posted: number;
  failed: number;
  dropped: number;
}

export interface DayView {
  date: string;
  kind: string;
  wave: string;
  /** Server clock, IST HH:MM. */
  now: string;
  dry_run: boolean;
  /** The ENVELOPE across the armed campaigns — no call goes out before its start
   *  or after its end. Each campaign carries its own window, so this is not
   *  necessarily any one campaign's hours. */
  window: DialWindow;
  /** The armed campaigns do not share a window, so naming one close time would
   *  be wrong for most of them. */
  window_varies: boolean;
  /** At least one armed campaign can still dial today. */
  window_open: boolean;
  status: DayStatus;
  totals: { campaigns: number; ready: number; posted: number; failed: number; dropped: number };
  /** A ceiling, not a promise: how many calls the hours left can still hold,
   *  capped per campaign against its own window and then summed. Approve
   *  re-plans, so the real number is decided then. */
  capacity_before_close: number;
  buckets: DayBucket[];
  red_bands: DayBand[];
  campaigns: DayCampaign[];
  /** Armed campaigns that are paused or disabled, so "why is nothing happening
   *  for X" has an answer on the screen rather than in a log. */
  stopped: (Campaign & { why: string })[];
  /** verify state -> count, straight off the dial log. */
  dial_log: Record<string, number>;
}

export interface PrepareResult {
  date: string;
  kind: string;
  wave: string;
  ready: number;
  prepared: number;
  campaigns: Array<{
    campaign_id: number;
    name?: string;
    status: string;
    detail?: string;
    ready?: number;
    run_id?: number;
  }>;
}

export interface ApproveResult {
  date: string;
  kind: string;
  wave: string;
  dry_run: boolean;
  buckets: string[] | 'all';
  approved: number;
  posted: number;
  failed: number;
  /** Slots that no longer fit before the window shuts. Not lost — they come back
   *  in tomorrow's plan. */
  not_dialled: number;
  campaigns: Array<{
    campaign_id: number;
    name: string;
    status: string;
    detail?: string;
    posted?: number;
    failed?: number;
  }>;
}

/* --- Dial log --------------------------------------------------------------
   Two separate facts. `outcome` is what Formi answered when we posted;
   `verified` is what the warehouse says actually happened, read back later.
   A 2xx says the request was accepted — only `verified: 'dialled'` says a call
   was made. */

export type DialOutcome = 'posted' | 'failed' | 'simulated' | 'skipped';
export type DialVerified = 'pending' | 'queued' | 'dialled' | 'missing' | 'simulated';

export interface DialLogRow {
  id: number;
  created_at: string;
  campaign_id: number;
  agent_id: number | null;
  run_id: number | null;
  item_id: number | null;
  source: string;
  lead_uuid: string | null;
  policy_no: string | null;
  lead_name: string | null;
  phone: string | null;
  bucket: string | null;
  disposition: string | null;
  dte: number | null;
  scheduled_time: string | null;
  dry_run: boolean;
  url: string | null;
  attempts: number;
  http_status: number | null;
  response: string | null;
  outcome: string;
  verified: string;
  checked_at?: string | null;
  interaction_id?: number | null;
  call_stage?: string | null;
  call_disposition?: string | null;
  duration_sec?: number | null;
}

export interface DialLogPage {
  total: number;
  limit: number;
  offset: number;
  rows: DialLogRow[];
}

export interface DialLogSummary {
  date: string;
  campaigns: Array<{
    campaign_id: number;
    sent: number;
    outcome: Record<string, number>;
    verified: Record<string, number>;
    talk_time_sec: number;
  }>;
}

export interface Health {
  ok: boolean;
  dry_run: boolean;
  db: string;
  leads_source: string;
  /** Optional: an older server build omits these. */
  agents?: number[];
  test_numbers?: string[];
}

/** GET/POST /api/sync. `ok` is null while running and after a restart. */
export interface SyncStatus {
  running: boolean;
  ok: boolean | null;
  error: string;
  campaigns: number;
  leads: number;
}

/* --- Test call -------------------------------------------------------------
   A rehearsal against one allow-listed number. `found` says the phone resolved
   to a real lead; the allow-list itself is server-side (`config.test_numbers`)
   and `trigger` returns 422 for anything not on it. */

export interface TestNumber {
  phone: string;
  label: string;
  campaign_id: number | null;
  lead_uuid: string | null;
  found: boolean;
  lead_name?: string | null;
}

export interface TestLead {
  lead_uuid: string;
  phone: string;
  lead_name: string | null;
  campaign_id: number;
  agent_id: number;
  stage: string;
  policy_no?: string | null;
  campaign_name?: string | null;
  disposition_class?: string;
}

/** `preview` is what the preview endpoint returns — it is not an attempt. */
export type TestCallStatus = 'preview' | 'simulated' | 'posted' | 'failed' | 'not_found';

export interface TestCallResult {
  found: boolean;
  /** true = the trigger made NO network call. Proves resolution + payload only. */
  dry_run: boolean;
  lead: TestLead | null;
  would_post: { url: string; body: Record<string, unknown> } | null;
  status: TestCallStatus;
  http_status: number | null;
  response: string | null;
}

export interface TestCallAttempt {
  id: number;
  phone: string;
  created_at: string;
  status: TestCallStatus;
  http_status: number | null;
  response: string | null;
  dry_run: boolean;
  campaign_id?: number | null;
  agent_id?: number | null;
  lead_uuid?: string | null;
  lead_name?: string | null;
  scheduled_time?: string | null;
}

export interface PagedItems {
  items: PlanItem[];
  page: number;
  page_size: number;
  total: number;
}

/** The server's manual preview sample is a lead, not a persisted plan item —
 *  it has no id or run_id yet. Kept loose on purpose. */
export interface ManualSample {
  lead_uuid: string;
  policy_no: string | null;
  lead_name: string | null;
  disposition: string;
  disposition_class?: string;
  dte: number;
  bucket: string;
  slot_no?: number;
  scheduled_time: string;
}

export interface ManualPreview {
  count: number;
  slots: number;
  dropped?: number;
  /** Of these leads, how many Formi already holds a call for today. The manual
   *  screen overrides the no-double-book guard on purpose, so this is the only
   *  warning the operator gets. Absent on an older server. */
  already_scheduled?: number;
  sample: ManualSample[];
}

export interface StagePreview {
  would_change: number;
  unchanged: number;
  by_stage: Record<string, number>;
  sample: Array<{ policy_no: string; lead_name?: string | null; stage: string; red?: string | null }>;
}

export interface StageJob {
  id: number;
  kind: 'policies' | 'expired';
  mode: 'preview' | 'commit';
  target_stage: string;
  would_change: number;
  changed: number;
  created_at: string;
  dry_run: boolean;
}
