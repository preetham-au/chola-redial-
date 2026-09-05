// Shapes mirror docs/API_CONTRACT.md exactly. Do not reshape.

export interface Campaign {
  id: number;
  agent_id: number;
  warehouse_id: number;
  name: string;
  enabled: boolean;
  paused: boolean;
  /** Switched on here and only here: the server dials this campaign's urgent
   *  buckets twice a day until nothing is left to call. Optional so a backend
   *  that predates it still typechecks. */
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

export interface Health {
  ok: boolean;
  dry_run: boolean;
  db: string;
  leads_source: string;
  /** Optional: an older server build omits these. */
  agents?: number[];
  test_numbers?: string[];
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
