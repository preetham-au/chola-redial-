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
  /** Taken out of circulation by the operator: absent from every list in the
   *  console and impossible to arm. Only the campaign picker asks for these,
   *  so it can offer them back. Optional so an older backend still typechecks. */
  hidden?: boolean;
}

/** The console is scoped to exactly one agent at a time. `paused` is true only
 *  when every *enabled* campaign on the agent is paused. */
export interface Agent {
  agent_id: number;
  name: string;
  /** From AGENT_LANGUAGES on the server. Null when the deployment has not
   *  labelled this agent. Never hardcode it in the UI: "125 is Hindi" is a fact
   *  about one deployment, not about this console. */
  language: string | null;
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

/** `expired` is a slot whose time passed before it could be dialled. `_commit`
 *  retires it rather than asking Formi for a call at a time that has gone, and
 *  the lead comes back in the next plan — so it is not a failure. */
export type PlanItemStatus =
  'planned' | 'simulated' | 'posted' | 'failed' | 'skipped' | 'expired';

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
  /** `auto` is the morning pass, `auto_pm` the afternoon one (`make_plan` in
   *  `api/routes_core.py` writes both); `manual` is an operator-built run. */
  kind: 'auto' | 'auto_pm' | 'manual';
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

/** `nothing_to_dial` is a plan that was built and came back EMPTY — the
 *  afternoon wave after a morning that booked every lead reaching it. `approved`
 *  was the server's answer here until 14 Sep 2026, and it was wrong twice over:
 *  nobody approved anything, and the screen that believed it offered neither
 *  Build nor Approve, so leads a later re-sync pulled in could not be planned at
 *  all. It is what `_approve_one` already calls this state for one campaign. */
/** `part_prepared` is the same defect one state over: some campaigns acted on and
 *  at least one with no run at all — a campaign whose prepare failed, or one
 *  armed after the wave was approved. `{committed, not_prepared}` is a SET that
 *  matched no arm of the server's ladder and fell through to `approved`, whose
 *  hero offers only the call log; the picker's Save is greyed out for a campaign
 *  that is already armed, so those leads had no route onto the clock at all. */
export type DayStatus =
  | 'no_campaigns' | 'not_prepared' | 'awaiting_approval' | 'nothing_to_dial'
  | 'part_prepared' | 'approved';

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
  /** When this plan was built. The older it is, the less `already_booked` can
   *  be trusted — leads booked in Formi since are not in it.
   *
   *  `runs.created_at`, which the server writes as naive IST with NO offset on
   *  it (api/db.py's `now_iso`). Never hand it to a bare `new Date(...)`: that
   *  reads it as the browser's local time. `planAge` in screens/Today.tsx is
   *  the one place that parses it. */
  plan_built_at: string | null;
  /** The most recent date this campaign actually posted calls. */
  last_dialled: string | null;
  /** Leads Formi had already queued when the plan was built, which the engine
   *  skipped.
   *
   *  OPTIONAL, because a server one deploy behind does not send it and the type
   *  saying otherwise is what hid that: summing a missing field gave `NaN`,
   *  `NaN > 0` is false, and the whole "already on Formi's clock" warning
   *  disappeared without a word. Read it through `alreadyBooked` in
   *  screens/Today.tsx, which separates "none" from "the server did not say". */
  already_booked?: number;
}

export interface StrandedRun {
  campaign_id: number;
  name: string;
  run_date: string;
  kind: string;
  slots: number;
}

export interface DaySpread {
  /** The hours this wave is allowed to dial into. */
  band: DialWindow;
  /** Hour of day ("9".."19") -> calls actually put on the clock. */
  hours: Record<string, number>;
}

export interface DayView {
  date: string;
  kind: string;
  wave: string;
  /** What this answer is narrowed to. Null when the day is not scoped to one
   *  agent, in which case every list here spans every armed campaign. Absent
   *  from a server that predates per-agent scoping — which cannot narrow, so an
   *  absent field and a null one say the same thing and must be read the same
   *  way. `scopeMismatch` is the only reader; see the note there. */
  agent_id?: number | null;
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
  /** Proof: where the calls actually landed, against the band approved. */
  spread: DaySpread;
  buckets: DayBucket[];
  red_bands: DayBand[];
  campaigns: DayCampaign[];
  /** Armed campaigns that are paused or disabled, so "why is nothing happening
   *  for X" has an answer on the screen rather than in a log. */
  stopped: (Campaign & { why: string })[];
  /** verify state -> count, straight off the dial log. */
  dial_log: Record<string, number>;
  /** Plans from earlier days that were never approved. Those leads were never
   *  called — nothing else in this console reports them. */
  stranded: StrandedRun[];
  /** How many distinct PEOPLE those runs hold. An unapproved plan is rebuilt for
   *  the same leads the next morning, so summing the rows' `slots` multiplies one
   *  backlog by the days it sat — 544 leads over a fortnight read as "7,616 calls
   *  never dialled". Each row's `slots` is still true of that row; this is the
   *  only number that is true of the backlog. */
  stranded_leads: number;
}

export interface PrepareResult {
  date: string;
  kind: string;
  wave: string;
  ready: number;
  prepared: number;
  campaigns: Array<{
    campaign_id: number;
    /** Absent on every failure row. `_prepare_one` builds its answer off `out`,
     *  which holds `campaign_id` alone, and assigns `out["name"]` only AFTER the
     *  resync block — so a `resync_failed` row has never carried one. Name a
     *  campaign through the day view's own `campaigns`, not through this. */
    name?: string;
    /** Every value `api/day.py`'s `_prepare_one` can return, all seven of them:
     *  `prepared` | `not_in_daily_plan` | `finished` | `window_closed` |
     *  `already_ran` | `resync_failed` | `error`.
     *
     *  TWO of them are failures, not one. `resync_failed` is the warehouse read
     *  failing before planning; `error` is `_write_run` raising during it. Either
     *  way nothing was written and the campaign is in NO plan at all — the silent
     *  failure the re-check exists to report. The other five are ordinary
     *  answers: nothing was planned and nothing is wrong. */
    status: string;
    detail?: string;
    ready?: number;
    run_id?: number;
  }>;
  /** Campaign ids this pass found paused in Formi and stopped here — the one
   *  fact only a `resync` prepare can learn, and the reason "Re-check now"
   *  exists. Absent unless the pass re-read Formi (`api/day.py`'s `prepare_day`
   *  always sends it; a backend too old not to). */
  stopped_in_formi?: number[];
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
    /** Present whenever a run exists to act on — which is every outcome except
     *  `not_prepared`, where there is nothing to retry. */
    run_id?: number;
    /** Slots the dial path refused for having drifted outside this wave's band —
     *  leads that were NOT called. They are inside `not_dialled` already, but
     *  only as part of a number that also holds slots retired for being in the
     *  past, and the two want different things done about them.
     *
     *  Optional because it is newer than this field list: a bundle that ships
     *  ahead of the API sees nothing here. Absent is not zero — read it through
     *  `outOfBand`, which says which of the two it is. */
    out_of_band?: number;
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
  sample: Array<{
    /** Present on a live server; the sample lists key on it because one policy
     *  is several leads and policy_no alone is not unique. */
    lead_id?: number;
    policy_no: string;
    lead_name?: string | null;
    stage: string;
    red?: string | null;
    new_red?: string | null;
  }>;
}

export interface StageJob {
  id: number;
  kind: 'policies' | 'expired' | 'red';
  mode: 'preview' | 'commit';
  target_stage: string;
  would_change: number;
  committed: number;
  created_at: string;
  dry_run: boolean;
}
