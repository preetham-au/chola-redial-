// Domain vocabulary lifted verbatim from engine/red_engine.py so the UI
// never invents a slug the server does not know.

import type { Agent, Campaign, Config, FrequencyRow } from './types';

/** The `/api/agents` shape, derived from a campaign list. Used for the offline
 *  fixture and as the fallback for a server that does not serve /api/agents
 *  yet — the rule for `paused` is the contract's: every *enabled* campaign
 *  paused, not every campaign. */
export function agentsFrom(campaigns: Campaign[]): Agent[] {
  const by = new Map<number, Campaign[]>();
  campaigns.forEach((c) => by.set(c.agent_id, [...(by.get(c.agent_id) ?? []), c]));
  return [...by.keys()]
    .sort((a, b) => a - b)
    .map((agent_id) => {
      const cs = by.get(agent_id)!;
      const live = cs.filter((c) => c.enabled);
      return {
        agent_id,
        name: `Agent ${agent_id}`,
        campaigns: cs.length,
        enabled: live.length,
        paused_campaigns: cs.filter((c) => c.paused).length,
        paused: live.length > 0 && live.every((c) => c.paused),
      };
    });
}

/** Sequential urgency ramp. Ordered along the runway: F1 is 45 days out
 *  (calm steel), F6 is past expiry (deep red). Never used for controls. */
export const BUCKET_COLOR: Record<string, string> = {
  F1: '#55779b',
  F2: '#6f7c86',
  F3: '#9c7c3c',
  F4: '#c07817',
  F5: '#dd5a26',
  E0: '#c9401f',
  F6: '#ab2b2b',
  M0: '#e8452f',
  D0: '#7c8a92',
};

export function bucketColor(b: string): string {
  return BUCKET_COLOR[b] ?? BUCKET_COLOR.D0;
}

/** F5/E0/F6/M0 get two calls a day and dial first. */
export function isIntensive(b: string): boolean {
  return b === 'F5' || b === 'E0' || b === 'F6' || b === 'M0';
}

export const BUCKET_ORDER = ['F1', 'F2', 'F3', 'F4', 'F5', 'E0', 'F6', 'M0', 'D0'];

export const BUCKET_NOTE: Record<string, string> = {
  M0: 'Mandatory (RED-1 / RED). Overrides cadence, weekly budget and pending callbacks.',
  D0: 'Disposition callback. Off the RED runway — driven by a promised date, not by days to expiry.',
};

/** Plain-English name for a bucket code so operators do not have to memorise
 *  F1..F6 / M0 / D0. Internal keys stay unchanged; only the label is friendly.
 *  Falls back to the code so an unknown bucket never renders blank. */
export const BUCKET_FRIENDLY: Record<string, string> = {
  M0: 'Renewal today',
  F6: 'Grace period',
  E0: 'Expiry window',
  F5: 'Critical week',
  F4: 'High-frequency',
  F3: 'Building urgency',
  F2: 'Early engagement',
  F1: 'Warm-up',
  D0: 'Connected — manual only',
};

export const BUCKET_RANGE: Record<string, string> = {
  M0: 'renewal day',
  F6: 'past due, 2–3 days',
  E0: 'expiry day and the day after',
  F5: '1–7 days out',
  F4: '8–15 days',
  F3: '16–23 days',
  F2: '24–31 days',
  F1: '32–45 days',
  D0: 'off the runway',
};

export function friendlyBucket(code: string): string {
  return BUCKET_FRIENDLY[code] ?? code;
}

export function bucketRange(code: string): string {
  return BUCKET_RANGE[code] ?? '';
}

/** Bucket keys the server accepts in `bucket_dispositions`: the frequency-table
 *  buckets plus M0 and D0. Anything else is a 422 on PUT. */
export function configurableBuckets(freq: FrequencyRow[]): string[] {
  const fromTable = freq.map((f) => f.bucket);
  const known = BUCKET_ORDER.filter((b) => fromTable.includes(b) || b === 'M0' || b === 'D0');
  return [...known, ...fromTable.filter((b) => !BUCKET_ORDER.includes(b))];
}

/** What a bucket actually dials. Absent or empty = inherit the global list.
 *  Intersected with the global list because a bucket narrows only — it can
 *  never re-enable something the campaign has switched off. */
export function effectiveDispositions(cfg: Config, bucket: string): string[] {
  const own = cfg.bucket_dispositions?.[bucket];
  if (!own || own.length === 0) return cfg.auto_dispositions;
  return cfg.auto_dispositions.filter((s) => own.includes(s));
}

/** Buckets that genuinely drop something, with what they drop. Drives both the
 *  config warnings and the dashboard's BUCKET_DISPOSITION_OFF sentence. */
export function narrowedBuckets(cfg: Config): Array<{ bucket: string; dropped: string[] }> {
  const map = cfg.bucket_dispositions ?? {};
  return Object.keys(map)
    .map((bucket) => ({
      bucket,
      dropped: cfg.auto_dispositions.filter((s) => !effectiveDispositions(cfg, bucket).includes(s)),
    }))
    .filter((x) => x.dropped.length > 0)
    .sort((a, b) => BUCKET_ORDER.indexOf(a.bucket) - BUCKET_ORDER.indexOf(b.bucket));
}

export type DispClass = 'dnp' | 'fresh' | 'callback' | 'hold' | 'reassign' | 'excluded' | 'unknown';

export const CLASS_META: Record<
  DispClass,
  { label: string; blurb: string; autoEligible: boolean; manualEligible: boolean }
> = {
  dnp: {
    label: 'Did not pick',
    blurb: 'Never connected. These follow the RED frequency ramp.',
    autoEligible: true,
    manualEligible: true,
  },
  fresh: {
    label: 'Fresh',
    blurb: 'Never dialled. These follow the RED frequency ramp.',
    autoEligible: true,
    manualEligible: true,
  },
  callback: {
    label: 'Connected — callback',
    blurb: 'Connected and gave a date. Dialled deliberately from Manual redial.',
    autoEligible: true,
    manualEligible: true,
  },
  hold: {
    label: 'On hold',
    blurb: 'Paused pending a human or field outcome. Dial only if you know the outcome.',
    autoEligible: false,
    manualEligible: true,
  },
  reassign: {
    label: 'Reassign',
    blurb: 'Needs a different agent before it can be dialled.',
    autoEligible: false,
    manualEligible: true,
  },
  excluded: {
    label: 'Excluded',
    blurb: 'Rejected by the server whatever the UI asks for. Regulatory (TRAI/NCPR) or already renewed.',
    autoEligible: false,
    manualEligible: false,
  },
  unknown: {
    label: 'Unrecognised',
    blurb: 'No rule matches this slug. Add a rule before dialling it.',
    autoEligible: false,
    manualEligible: true,
  },
};

/** Every slug the engine maps, with its class and the reason it sits there. */
export const DISPOSITIONS: Array<{ slug: string; cls: DispClass; note?: string }> = [
  // fresh
  { slug: '', cls: 'fresh', note: 'No disposition yet' },
  { slug: 'new', cls: 'fresh' },
  { slug: 'fresh', cls: 'fresh' },
  { slug: 'not_dialed', cls: 'fresh', note: 'Treated as fresh' },
  // dnp
  { slug: 'did_not_pick', cls: 'dnp' },
  { slug: 'hung_up', cls: 'dnp' },
  { slug: 'hung_up_no_contact', cls: 'dnp' },
  { slug: 'unreachable', cls: 'dnp', note: 'RNR' },
  { slug: 'rnr', cls: 'dnp', note: 'Alias of unreachable' },
  { slug: 'beep_tone_number_busy_not_reachable_switched_off', cls: 'dnp' },
  { slug: 'voicemail', cls: 'dnp' },
  { slug: 'voicemail_ivr', cls: 'dnp' },
  { slug: 'telephony_failed', cls: 'dnp' },
  { slug: 'dialer_nc', cls: 'dnp', note: 'Reclassified as RNR' },
  { slug: 'redial_required', cls: 'dnp' },
  { slug: 'potentially_interested', cls: 'dnp', note: 'Connected, no committed date' },
  { slug: 'follow_up_required', cls: 'dnp', note: 'Connected, no committed date' },
  // callback
  { slug: 'lead_appointment_fixed', cls: 'callback' },
  { slug: 'lead_cmrl_interested', cls: 'callback' },
  { slug: 'lead_directed_to_branch', cls: 'callback' },
  { slug: 'directed_to_branch', cls: 'callback' },
  { slug: 'lead_premium_quotation', cls: 'callback' },
  { slug: 'lead_premium_quotation_required', cls: 'callback' },
  { slug: 'share_premium_quotation', cls: 'callback' },
  { slug: 'lead_link_sent_online', cls: 'callback' },
  { slug: 'payment_link_sent', cls: 'callback' },
  { slug: 'positive_followup', cls: 'callback' },
  { slug: 'lead_positive_followup', cls: 'callback' },
  { slug: 'call_back', cls: 'callback' },
  { slug: 'committed_to_pay', cls: 'callback' },
  { slug: 'agreed_to_pay_with_date', cls: 'callback' },
  { slug: 'promise_to_renew', cls: 'callback' },
  // hold
  { slug: 'agent_number', cls: 'hold', note: 'Awaiting agent outcome' },
  { slug: 'chola_field_executive', cls: 'hold', note: 'Awaiting field visit' },
  { slug: 'requested_human_agent_connect', cls: 'hold', note: 'Escalated to a human' },
  { slug: 'human_review', cls: 'hold', note: 'Awaiting human review' },
  { slug: 'alternate_contact_given', cls: 'hold', note: 'Number on file superseded' },
  // reassign
  { slug: 'other_language', cls: 'reassign', note: 'Needs a language-capable agent' },
  // excluded
  { slug: 'already_paid_to_chola', cls: 'excluded', note: 'Renewed' },
  { slug: 'renewed', cls: 'excluded', note: 'Renewed — business outcome achieved' },
  { slug: 'do_not_call', cls: 'excluded', note: 'DND' },
  { slug: 'dnc', cls: 'excluded', note: 'DND' },
  { slug: 'dnd', cls: 'excluded', note: 'DND' },
  { slug: 'lost', cls: 'excluded', note: 'Terminal' },
  { slug: 'not_interested', cls: 'excluded', note: 'Terminal' },
  { slug: 'firm_decision_to_discontinue', cls: 'excluded', note: 'Terminal — will not renew' },
  { slug: 'wrong_number', cls: 'excluded', note: 'Bad data' },
  { slug: 'number_not_working', cls: 'excluded', note: 'Bad data' },
  { slug: 'invalid_number', cls: 'excluded', note: 'Bad data' },
  { slug: 'ai_qualified_lead', cls: 'excluded', note: 'Handed to sales' },
  { slug: 'lead_transferred_to_sales', cls: 'excluded', note: 'Handed to sales' },
];

export const CLASS_OF: Record<string, DispClass> = Object.fromEntries(
  DISPOSITIONS.map((d) => [d.slug, d.cls]),
);

export const NOTE_OF: Record<string, string> = Object.fromEntries(
  DISPOSITIONS.filter((d) => d.note).map((d) => [d.slug, d.note as string]),
);

export const CLASS_ORDER: DispClass[] = [
  'dnp',
  'fresh',
  'callback',
  'hold',
  'reassign',
  'unknown',
  'excluded',
];

export function classOf(slug: string): DispClass {
  return CLASS_OF[slug] ?? 'unknown';
}

/** Human label for a slug: "did_not_pick" -> "Did not pick". */
export function dispLabel(slug: string): string {
  if (slug === '') return '(no disposition)';
  const s = slug.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** What putting a lead ON this stage does to it — the bulk-update question,
 *  which is not the same as the auto-run question CLASS_META answers. */
export function stageEffect(slug: string): string {
  if (slug === 'policy_expired') return 'The lead drops out of the RED window and is never evaluated again.';
  switch (classOf(slug)) {
    case 'excluded':
      return 'The lead is never dialled again, on any run.';
    case 'callback':
      return 'The lead leaves the auto-run and is dialled from Manual redial instead.';
    case 'hold':
      return 'The lead is held out of every run until someone changes it back.';
    case 'reassign':
      return 'The lead is held for an agent who speaks the right language.';
    case 'dnp':
    case 'fresh':
      return 'The lead follows the RED frequency ramp and can be auto-dialled again.';
    default:
      return 'No engine rule matches this stage, so the lead will be skipped as unrecognised.';
  }
}

/** Why a lead is not being called. The real question the dashboard answers. */
export const SKIP_META: Record<string, { label: string; why: string; fixable: boolean }> = {
  CADENCE_WAIT: {
    label: 'Cadence wait',
    why: 'Called recently — the minimum gap for its bucket has not elapsed.',
    fixable: true,
  },
  MANUAL_ONLY: {
    label: 'Manual only',
    why: 'Connected and gave a date. Dial these from Manual redial.',
    fixable: true,
  },
  // Deliberately not MANUAL_ONLY's wording: that one is a property of the
  // disposition everywhere, this is a per-bucket choice made on the config
  // screen, so it never clears itself. The dashboard names the buckets.
  BUCKET_DISPOSITION_OFF: {
    label: 'Bucket does not chase it',
    why: 'This bucket is configured to skip this disposition. It will not resolve on its own.',
    fixable: false,
  },
  STAGE_TERMINAL: {
    label: 'Terminal stage',
    why: 'Renewed, DND, wrong number or not interested. Never dialled.',
    fixable: false,
  },
  STAGE_HOLD: {
    label: 'On hold',
    why: 'Waiting on a human or field outcome.',
    fixable: false,
  },
  STAGE_REASSIGN: {
    label: 'Needs reassignment',
    why: 'Needs a language-capable agent.',
    fixable: false,
  },
  STAGE_UNKNOWN: {
    label: 'Unrecognised stage',
    why: 'The disposition slug has no rule in the engine.',
    fixable: false,
  },
  NO_EXPIRY: { label: 'No expiry date', why: 'No usable RED on the lead.', fixable: false },
  OUTSIDE_WINDOW: {
    label: 'Outside RED window',
    why: 'Not yet inside 45 days, or past the 3-day grace.',
    fixable: false,
  },
  WEEKLY_BUDGET_MET: {
    label: 'Weekly budget met',
    why: 'Rolling 7-day call budget for its bucket is spent.',
    fixable: true,
  },
  DAILY_CAP_MET: { label: 'Daily cap met', why: 'Already at the per-day cap.', fixable: true },
  MAX_ATTEMPTS: { label: 'Max attempts', why: 'Attempt ceiling reached.', fixable: true },
  CALLBACK_PENDING: {
    label: 'Callback pending',
    why: 'Promised callback date is still in the future.',
    fixable: false,
  },
  NOT_TODAYS_SLOT: {
    label: 'Not today’s slot',
    why: 'Its weekly slots fall on other weekdays.',
    fixable: true,
  },
  ALREADY_SCHEDULED_TODAY: {
    label: 'Already queued',
    why: 'An undialled interaction is already queued for today.',
    fixable: false,
  },
};

export function skipMeta(code: string) {
  return SKIP_META[code] ?? { label: code, why: 'No description for this code.', fixable: false };
}

export const RUN_STATUS_TONE: Record<string, string> = {
  planned: 'badge-accent',
  approved: 'badge-warn',
  committed: 'badge-ok',
  paused: 'badge-warn',
  failed: 'badge-bad',
};

export const ITEM_STATUS_TONE: Record<string, string> = {
  planned: '',
  simulated: 'badge-accent',
  posted: 'badge-ok',
  failed: 'badge-bad',
  skipped: 'badge-warn',
};

export const DIAL_WINDOW_FLOOR = '09:00';
export const DIAL_WINDOW_CEIL = '19:00';

export function minutesOf(hhmm: string): number {
  const [h, m] = hhmm.split(':').map(Number);
  return (h || 0) * 60 + (m || 0);
}

/** Mirrors the server's 422: inside 09:00–19:00 and start < end. */
export function dialWindowError(start: string, end: string): string | null {
  if (!/^\d{2}:\d{2}$/.test(start) || !/^\d{2}:\d{2}$/.test(end)) return 'Use HH:MM.';
  const s = minutesOf(start);
  const e = minutesOf(end);
  if (s < minutesOf(DIAL_WINDOW_FLOOR)) return 'Cannot start before 09:00.';
  if (e > minutesOf(DIAL_WINDOW_CEIL)) return 'Cannot end after 19:00.';
  if (s >= e) return 'Start must be before end.';
  return null;
}

export const fmt = new Intl.NumberFormat('en-IN');

export function n(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : fmt.format(v);
}

export function timeOf(iso: string): string {
  return iso.slice(11, 16);
}

export function dateOf(iso: string): string {
  return iso.slice(0, 10);
}

export function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`;
}

export function dteLabel(dte: number): string {
  if (dte > 0) return `${dte}d left`;
  if (dte === 0) return 'expires today';
  return `${Math.abs(dte)}d past`;
}

/** Where a daily autopilot pass stands right now.
 *
 *  A pass fires once a day and is never retried: if the warehouse was down at
 *  10:00 those calls simply did not go out. `missed` is the only place the
 *  console says so. `now` must be the SERVER's IST clock — the pass times are
 *  IST and the operator's laptop may not be. An empty `now` (server too old to
 *  send one) reads as "waiting", which claims nothing. */
export function passState(at: string, fired: string[], kind: string, now: string):
  'ran' | 'missed' | 'waiting' {
  if (fired.includes(kind)) return 'ran';
  return now >= at && now !== '' ? 'missed' : 'waiting';
}
