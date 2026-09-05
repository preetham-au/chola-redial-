// Offline fixtures. Shapes match docs/API_CONTRACT.md exactly — this file is
// the fallback when the Python backend is not up, not a second source of truth.

import type {
  Agent,
  AutopilotStatus,
  BucketsResponse,
  Campaign,
  Config,
  ConfigVersion,
  Health,
  ManualSample,
  PlanItem,
  Run,
  StageJob,
  SyncStatus,
  TestCallAttempt,
  TestCallResult,
  TestNumber,
} from './types';
import { agentsFrom, classOf, isIntensive, today } from './domain';

/** Deterministic PRNG so counts do not jitter between renders. */
function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

const D = today();

/** Mirrors the expanded seed: ~14 campaigns, several agents, PV/CV/TW, mixed
 *  languages and mixed enabled/paused state. Disabled ones are listed too —
 *  the server returns every row and the picker must not hide them. */
export const mockCampaigns: Campaign[] = [
  { id: 1, agent_id: 125, warehouse_id: 1650, name: '0308Redial -PV Hindi', enabled: true, paused: false },
  { id: 2, agent_id: 125, warehouse_id: 1651, name: '0308Redial -PV Tamil', enabled: true, paused: false },
  { id: 3, agent_id: 125, warehouse_id: 1652, name: '0308Redial -PV Telugu', enabled: true, paused: true },
  { id: 4, agent_id: 125, warehouse_id: 1653, name: '0308Redial -PV Kannada', enabled: false, paused: false },
  { id: 5, agent_id: 127, warehouse_id: 1599, name: '1008Redial -CV Hindi', enabled: true, paused: false },
  { id: 6, agent_id: 127, warehouse_id: 1600, name: '1008Redial -CV Tamil', enabled: true, paused: false },
  { id: 7, agent_id: 127, warehouse_id: 1601, name: '1008Redial -CV Marathi', enabled: true, paused: true },
  { id: 8, agent_id: 127, warehouse_id: 1602, name: '1008Redial -CV Telugu', enabled: false, paused: false },
  { id: 9, agent_id: 131, warehouse_id: 1710, name: '1508Redial -TW Hindi', enabled: true, paused: false },
  { id: 10, agent_id: 131, warehouse_id: 1711, name: '1508Redial -TW Tamil', enabled: true, paused: false },
  { id: 11, agent_id: 131, warehouse_id: 1712, name: '1508Redial -TW Kannada', enabled: true, paused: true },
  { id: 12, agent_id: 140, warehouse_id: 1780, name: '2008Redial -PV Malayalam', enabled: true, paused: false },
  { id: 13, agent_id: 140, warehouse_id: 1781, name: '2008Redial -CV Gujarati', enabled: true, paused: false },
  { id: 14, agent_id: 140, warehouse_id: 1782, name: '2008Redial -TW Bengali', enabled: false, paused: true },
];

/** Derived, never hand-written: the offline agent list and the offline campaign
 *  list can then never disagree about counts or paused state. */
export const mockAgents = (): Agent[] => agentsFrom(mockCampaigns);

/** Agent-level pause hits every campaign on the agent, so the fixture mutates
 *  in place — the UI re-reads the campaign list afterwards. */
export function mockAgentPause(agentId: number, paused: boolean) {
  mockCampaigns.forEach((c) => {
    if (c.agent_id === agentId) c.paused = paused;
  });
  return mockAgents().find((a) => a.agent_id === agentId)!;
}

export const mockTestNumbers: TestNumber[] = [
  { phone: '9379747274', label: 'TEST -Pipeline Check', campaign_id: 1, lead_uuid: '88c7a62a-d3f8-40a4-af7d-e0dd5e82566a', lead_name: 'TEST Rehearsal 1', found: true },
  // Allow-listed but unresolvable: a real state the UI has to explain rather
  // than treat as an error.
  { phone: '9845012345', label: 'QA handset — no seeded lead', campaign_id: null, lead_uuid: null, found: false },
];

export const mockAutopilot: AutopilotStatus = {
  passes: [{ kind: 'auto', at: '10:00' }, { kind: 'auto_pm', at: '15:00' }],
  urgent_buckets: ['M0', 'E0', 'F6', 'F5'],
  review_buckets: ['F4', 'F3', 'F2', 'F1', 'D0'],
  now: '09:00',
  fired_today: [],
};

export const mockHealth: Health = {
  ok: true,
  dry_run: true,
  db: 'redial.db',
  leads_source: 'seed',
  agents: [...new Set(mockCampaigns.map((c) => c.agent_id))],
  test_numbers: mockTestNumbers.map((t) => t.phone),
};

/* Offline there is no warehouse to pull from, so the button reports "idle" and
   never a success it did not have. */
export const mockSyncIdle: SyncStatus = {
  running: false,
  ok: null,
  error: '',
  campaigns: 0,
  leads: 0,
};

export const mockConfig: Config = {
  version: 3,
  created_at: `${D}T09:12:00`,
  dial_window: { start: '09:30', end: '19:00' },
  frequency_table: [
    { bucket: 'F1', label: 'Warm-up', from_dte: 45, to_dte: 32, calls_per_week: 2, calls_per_day: 0 },
    { bucket: 'F2', label: 'Early engagement', from_dte: 31, to_dte: 24, calls_per_week: 2, calls_per_day: 0 },
    { bucket: 'F3', label: 'Building urgency', from_dte: 23, to_dte: 16, calls_per_week: 3, calls_per_day: 0 },
    { bucket: 'F4', label: 'High frequency', from_dte: 15, to_dte: 8, calls_per_week: 3, calls_per_day: 0 },
    { bucket: 'F5', label: 'Critical window', from_dte: 7, to_dte: 1, calls_per_week: 0, calls_per_day: 2 },
    { bucket: 'E0', label: 'Expiry window', from_dte: 0, to_dte: -1, calls_per_week: 0, calls_per_day: 2 },
    { bucket: 'F6', label: 'Grace period', from_dte: -2, to_dte: -3, calls_per_week: 0, calls_per_day: 2 },
  ],
  bucket_priority: ['M0', 'E0', 'F6', 'F5', 'F4', 'F3', 'F2', 'F1', 'D0'],
  auto_dispositions: [
    'did_not_pick',
    'hung_up',
    'unreachable',
    'rnr',
    'beep_tone_number_busy_not_reachable_switched_off',
    'voicemail',
    'telephony_failed',
    'dialer_nc',
    'new',
    'fresh',
    'not_dialed',
    '',
  ],
  // F5 chases voicemail near expiry; F1 does not, 40 days out. Every other
  // bucket is absent from the map and inherits the list above.
  bucket_dispositions: {
    F1: ['did_not_pick', 'hung_up', 'unreachable', 'rnr', 'new', 'fresh', 'not_dialed', ''],
    F5: [
      'did_not_pick',
      'hung_up',
      'unreachable',
      'rnr',
      'beep_tone_number_busy_not_reachable_switched_off',
      'voicemail',
      'telephony_failed',
      'dialer_nc',
      'new',
      'fresh',
      'not_dialed',
      '',
    ],
  },
  mandatory_days: [1, 0],
  calls_per_day_cap: 2,
  same_day_gap_hours: 3.0,
  shift_from_last_hours: 2.0,
  max_per_minute: 12,
  max_per_run: 5000,
  max_attempts: 0,
};

export const mockConfigHistory: ConfigVersion[] = [
  { version: 3, created_at: `${D}T09:12:00` },
  { version: 2, created_at: '2026-08-21T18:40:00' },
  { version: 1, created_at: '2026-08-11T11:05:00' },
];

const BUCKET_LABEL: Record<string, string> = {
  F1: 'Warm-up',
  F2: 'Early engagement',
  F3: 'Building urgency',
  F4: 'High frequency',
  F5: 'Critical window',
  E0: 'Expiry window',
  F6: 'Grace period',
  M0: 'Mandatory (RED-1 / RED)',
  D0: 'Disposition callback',
};

const BUCKET_DTE: Record<string, [number, number]> = {
  F1: [45, 32],
  F2: [31, 24],
  F3: [23, 16],
  F4: [15, 8],
  F5: [7, 1],
  E0: [0, -1],
  F6: [-2, -3],
  M0: [1, 0],
  D0: [40, 2],
};

/** disposition -> per-bucket weight. Fresh leads cluster far from expiry,
 *  DNP accumulates as the ramp intensifies, callbacks sit in D0. */
const DISP_MIX: Array<[string, number]> = [
  ['did_not_pick', 30],
  ['unreachable', 14],
  ['rnr', 9],
  ['hung_up', 7],
  ['beep_tone_number_busy_not_reachable_switched_off', 6],
  ['voicemail', 4],
  ['telephony_failed', 3],
  ['dialer_nc', 2],
  ['not_dialed', 8],
  ['new', 5],
  ['', 4],
  ['positive_followup', 5],
  ['payment_link_sent', 3],
  ['lead_appointment_fixed', 2],
  ['call_back', 3],
  ['committed_to_pay', 2],
  ['human_review', 1],
  ['other_language', 1],
  ['not_interested', 4],
  ['do_not_call', 2],
  ['wrong_number', 3],
  ['renewed', 5],
  ['already_paid_to_chola', 3],
  ['others', 1],
];

const BUCKET_TOTALS: Record<string, number> = {
  F1: 2380,
  F2: 1810,
  F3: 1560,
  F4: 1420,
  F5: 1180,
  E0: 260,
  F6: 402,
  M0: 336,
  D0: 724,
};

function buildMatrix() {
  const r = rng(20260828);
  const cells: Array<{ bucket: string; disposition: string; count: number }> = [];
  const bucketAgg: Record<string, { eligible: number; waiting: number; manual_only: number; total: number }> = {};
  const dispAgg: Record<string, { eligible: number; total: number }> = {};

  for (const b of Object.keys(BUCKET_TOTALS)) {
    bucketAgg[b] = { eligible: 0, waiting: 0, manual_only: 0, total: 0 };
    const target = BUCKET_TOTALS[b];
    const weights = DISP_MIX.map(([slug, w]) => {
      const cls = classOf(slug);
      let k = w;
      // callbacks land in D0, not on the runway
      if (cls === 'callback') k = b === 'D0' ? w * 9 : w * 0.15;
      if (b === 'D0' && cls !== 'callback') k = w * 0.05;
      // fresh leads are mostly far from expiry
      if (cls === 'fresh') k = w * (b === 'F1' || b === 'F2' ? 2.2 : 0.5);
      // renewals accumulate near expiry
      if (slug === 'renewed' || slug === 'already_paid_to_chola')
        k = w * (isIntensive(b) ? 2.4 : 0.7);
      return k * (0.75 + r() * 0.5);
    });
    const sum = weights.reduce((a, c) => a + c, 0);
    DISP_MIX.forEach(([slug], i) => {
      const count = Math.round((weights[i] / sum) * target);
      if (count === 0) return;
      cells.push({ bucket: b, disposition: slug, count });
      const cls = classOf(slug);
      const auto = mockConfig.auto_dispositions.includes(slug);
      bucketAgg[b].total += count;
      dispAgg[slug] = dispAgg[slug] ?? { eligible: 0, total: 0 };
      dispAgg[slug].total += count;
      if (auto) {
        // some of the auto-eligible pool is still inside its cadence gap
        const waitRate = isIntensive(b) ? 0.18 : 0.62;
        const waiting = Math.round(count * waitRate);
        bucketAgg[b].waiting += waiting;
        bucketAgg[b].eligible += count - waiting;
        dispAgg[slug].eligible += count - waiting;
      } else if (cls === 'callback' || cls === 'hold' || cls === 'reassign') {
        bucketAgg[b].manual_only += count;
      }
    });
  }
  return { cells, bucketAgg, dispAgg };
}

const M = buildMatrix();

export const mockBuckets: BucketsResponse = {
  date: D,
  total_leads: Object.values(M.bucketAgg).reduce((a, c) => a + c.total, 0),
  buckets: ['M0', 'E0', 'F6', 'F5', 'F4', 'F3', 'F2', 'F1', 'D0'].map((b) => ({
    bucket: b,
    label: BUCKET_LABEL[b],
    ...M.bucketAgg[b],
  })),
  dispositions: DISP_MIX.map(([slug]) => ({
    disposition: slug,
    class: classOf(slug),
    auto: mockConfig.auto_dispositions.includes(slug),
    eligible: M.dispAgg[slug]?.eligible ?? 0,
    total: M.dispAgg[slug]?.total ?? 0,
  })).filter((d) => d.total > 0),
  matrix: M.cells,
  skips: {
    CADENCE_WAIT: 3118,
    MANUAL_ONLY: 981,
    BUCKET_DISPOSITION_OFF: 410,
    STAGE_TERMINAL: 2402,
    NOT_TODAYS_SLOT: 1244,
    WEEKLY_BUDGET_MET: 486,
    CALLBACK_PENDING: 302,
    STAGE_HOLD: 141,
    DAILY_CAP_MET: 96,
    OUTSIDE_WINDOW: 88,
    STAGE_UNKNOWN: 61,
    STAGE_REASSIGN: 47,
    NO_EXPIRY: 33,
    ALREADY_SCHEDULED_TODAY: 27,
  },
};

const FIRST = ['Rakesh', 'Priya', 'Anil', 'Meena', 'Suresh', 'Kavitha', 'Vikram', 'Deepa', 'Arun', 'Sunita', 'Ramesh', 'Latha', 'Naveen', 'Divya', 'Manoj', 'Shalini'];
const LAST = ['Kumar', 'Sharma', 'Iyer', 'Reddy', 'Nair', 'Patel', 'Verma', 'Menon', 'Gupta', 'Rao', 'Joshi', 'Pillai'];

/** Plan items, spread across the dial window with F5/F6/M0 dialled first
 *  and capped at max_per_minute. Same distribution the timeline reads from. */
function buildItems(runId: number, seed: number, count: number): PlanItem[] {
  const r = rng(seed);
  const order = mockConfig.bucket_priority;
  const share: Record<string, number> = { M0: 0.11, E0: 0.06, F6: 0.05, F5: 0.24, F4: 0.19, F3: 0.14, F2: 0.11, F1: 0.07, D0: 0.03 };
  const autoSlugs = mockConfig.auto_dispositions;

  const plan: Array<{ bucket: string; priority: number }> = [];
  order.forEach((b, pi) => {
    const k = Math.round(count * (share[b] ?? 0));
    for (let i = 0; i < k; i++) plan.push({ bucket: b, priority: pi });
  });

  const startMin = 9 * 60 + 30;
  const endMin = 19 * 60;
  const span = endMin - startMin;
  const perMin = mockConfig.max_per_minute;

  return plan.map((p, i) => {
    const slug = autoSlugs[Math.floor(r() * (autoSlugs.length - 1))];
    const [hi, lo] = BUCKET_DTE[p.bucket];
    const dte = hi === lo ? hi : lo + Math.floor(r() * (hi - lo + 1));
    const intensive = isIntensive(p.bucket);
    const slotNo = intensive && r() < 0.45 ? 2 : 1;
    // urgent buckets front-load; second slots land in the afternoon
    const base = intensive ? (slotNo === 2 ? 0.55 : 0.03) : 0.18;
    const frac = Math.min(0.995, base + r() * (intensive ? 0.4 : 0.78));
    const minute = startMin + Math.floor(frac * span);
    const jitter = Math.floor(r() * 60);
    const t = `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}:${String(jitter).padStart(2, '0')}`;
    const name = `${FIRST[Math.floor(r() * FIRST.length)]} ${LAST[Math.floor(r() * LAST.length)]}`;
    return {
      id: runId * 100000 + i + 1,
      run_id: runId,
      lead_uuid: `9f${(seed + i).toString(16).padStart(6, '0')}-4a1c-4e2b-9d33-${(i * 7919).toString(16).padStart(12, '0')}`.slice(0, 36),
      policy_no: `POL${(3100000 + ((i * 7717) % 900000)).toString()}`,
      contact_id: String(500 + ((i * 31) % 9500)),
      phone: `9${String(800000000 + ((seed + i * 7717) % 99999999)).slice(0, 9)}`,
      lead_name: name,
      disposition: slug,
      disposition_class: classOf(slug),
      dte,
      bucket: p.bucket,
      bucket_label: BUCKET_LABEL[p.bucket],
      priority: p.priority,
      slot_no: slotNo,
      scheduled_time: `${D}T${t}`,
      status: 'planned' as const,
      http_status: null,
      response: null,
    };
  }).sort((a, b) => a.scheduled_time.localeCompare(b.scheduled_time))
    .map((it, idx) => ({ ...it, id: runId * 100000 + idx + 1 }))
    // honour max_per_minute: nudge overflow to the next free minute
    .map((it, idx, arr) => {
      const sameMin = arr.slice(Math.max(0, idx - perMin * 2), idx).filter(
        (o) => o.scheduled_time.slice(0, 16) === it.scheduled_time.slice(0, 16),
      ).length;
      if (sameMin < perMin) return it;
      const mm = Number(it.scheduled_time.slice(14, 16)) + 1;
      const hh = Number(it.scheduled_time.slice(11, 13)) + (mm >= 60 ? 1 : 0);
      return {
        ...it,
        scheduled_time: `${D}T${String(Math.min(hh, 18)).padStart(2, '0')}:${String(mm % 60).padStart(2, '0')}:${it.scheduled_time.slice(17)}`,
      };
    });
}

export const mockRuns: Run[] = [
  {
    id: 41,
    campaign_id: 1,
    run_date: D,
    kind: 'auto',
    status: 'planned',
    config_version: 3,
    created_at: `${D}T08:41:00`,
    counts: { evaluated: 9812, planned: 1284, slots: 1602, posted: 0, failed: 0, dropped: 0 },
  },
  {
    id: 40,
    campaign_id: 1,
    run_date: '2026-08-27',
    kind: 'auto',
    status: 'committed',
    config_version: 3,
    created_at: '2026-08-27T08:39:00',
    counts: { evaluated: 9784, planned: 1301, slots: 1618, posted: 1611, failed: 7, dropped: 0 },
  },
  {
    id: 39,
    campaign_id: 1,
    run_date: '2026-08-27',
    kind: 'manual',
    status: 'committed',
    config_version: 3,
    created_at: '2026-08-27T14:02:00',
    counts: { evaluated: 981, planned: 214, slots: 214, posted: 214, failed: 0, dropped: 0 },
  },
  {
    id: 38,
    campaign_id: 1,
    run_date: '2026-08-26',
    kind: 'auto',
    status: 'committed',
    config_version: 2,
    created_at: '2026-08-26T08:40:00',
    counts: { evaluated: 9755, planned: 1268, slots: 1580, posted: 1580, failed: 0, dropped: 0 },
  },
  {
    id: 37,
    campaign_id: 1,
    run_date: '2026-08-25',
    kind: 'auto',
    status: 'failed',
    config_version: 2,
    created_at: '2026-08-25T08:40:00',
    counts: { evaluated: 9740, planned: 1259, slots: 1571, posted: 402, failed: 1169, dropped: 0 },
  },
  {
    id: 36,
    campaign_id: 1,
    run_date: '2026-08-24',
    kind: 'auto',
    status: 'committed',
    config_version: 2,
    created_at: '2026-08-24T08:41:00',
    counts: { evaluated: 9721, planned: 1240, slots: 1544, posted: 1544, failed: 0, dropped: 0 },
  },
];

const itemCache = new Map<number, PlanItem[]>();

export function mockItems(runId: number): PlanItem[] {
  if (!itemCache.has(runId)) {
    const run = mockRuns.find((r) => r.id === runId) ?? mockRuns[0];
    const items = buildItems(runId, runId * 1013, Math.min(run.counts.slots, 1602));
    if (run.status === 'committed') {
      items.forEach((it, i) => {
        it.status = i % 190 === 7 && run.counts.failed > 0 ? 'failed' : 'posted';
        it.http_status = it.status === 'failed' ? 502 : 200;
        it.response = it.status === 'failed' ? 'upstream timeout' : 'ok';
      });
    }
    itemCache.set(runId, items);
  }
  return itemCache.get(runId)!;
}

export const mockStageJobs: StageJob[] = [
  { id: 12, kind: 'expired', mode: 'commit', target_stage: 'policy_expired', would_change: 812, changed: 812, created_at: '2026-08-26T19:20:00', dry_run: false },
  { id: 11, kind: 'expired', mode: 'preview', target_stage: 'policy_expired', would_change: 812, changed: 0, created_at: '2026-08-26T19:18:00', dry_run: true },
  { id: 10, kind: 'policies', mode: 'commit', target_stage: 'renewed', would_change: 46, changed: 46, created_at: '2026-08-25T16:04:00', dry_run: false },
  { id: 9, kind: 'policies', mode: 'commit', target_stage: 'do_not_call', would_change: 8, changed: 8, created_at: '2026-08-22T10:11:00', dry_run: false },
];

export function mockStagePreview(policies: string[], target: string) {
  const r = rng(policies.length * 977 + target.length);
  const found = Math.max(0, policies.length - Math.floor(policies.length * 0.06));
  const unchanged = Math.floor(found * 0.22);
  const by_stage: Record<string, number> = {};
  const pool = ['did_not_pick', 'unreachable', 'positive_followup', 'not_dialed', 'payment_link_sent'];
  let left = found - unchanged;
  pool.forEach((s, i) => {
    const take = i === pool.length - 1 ? left : Math.floor(left * (0.4 - i * 0.07));
    if (take > 0) by_stage[s] = take;
    left -= take;
  });
  return {
    would_change: found - unchanged,
    unchanged,
    by_stage,
    sample: policies.slice(0, 12).map((p) => ({
      policy_no: p,
      lead_name: `${FIRST[Math.floor(r() * FIRST.length)]} ${LAST[Math.floor(r() * LAST.length)]}`,
      stage: pool[Math.floor(r() * pool.length)],
    })),
  };
}

export function mockExpiredPreview(redBefore: string) {
  const r = rng(redBefore.split('-').join('').length * 31 + 7);
  const would = 640 + Math.floor(r() * 400);
  return {
    would_change: would,
    unchanged: 1174,
    by_stage: {
      did_not_pick: Math.round(would * 0.44),
      unreachable: Math.round(would * 0.21),
      not_dialed: Math.round(would * 0.14),
      positive_followup: Math.round(would * 0.12),
      hung_up: Math.round(would * 0.09),
    },
    sample: Array.from({ length: 10 }, (_, i) => ({
      policy_no: `POL${3400000 + i * 811}`,
      lead_name: `${FIRST[(i * 3) % FIRST.length]} ${LAST[(i * 5) % LAST.length]}`,
      stage: ['did_not_pick', 'unreachable', 'not_dialed'][i % 3],
      red: `2026-0${(i % 8) + 1}-${String((i * 3) % 28 + 1).padStart(2, '0')}`,
    })),
  };
}

/* --- Test call -------------------------------------------------------------
   The fixture mirrors DRY_RUN=1: `dry_run: true`, status `simulated`, and no
   http_status — because no network call was made. A number that is on the
   allow-list but resolves to nothing comes back `not_found`, which is a real
   answer, not an error. */

export function mockTestCall(
  phone: string,
  campaignId?: number,
  status: TestCallResult['status'] = 'simulated',
): TestCallResult {
  const known = mockTestNumbers.find((t) => t.phone === phone);
  if (!known || !known.found || !known.lead_uuid) {
    return { found: false, dry_run: true, lead: null, would_post: null, status: 'not_found', http_status: null, response: null };
  }
  const campaign =
    mockCampaigns.find((c) => c.id === (campaignId ?? known.campaign_id)) ?? mockCampaigns[0];
  return {
    found: true,
    dry_run: true,
    lead: {
      lead_uuid: known.lead_uuid,
      phone,
      lead_name: known.lead_name ?? null,
      policy_no: '3377/008151/00',
      campaign_id: campaign.id,
      campaign_name: campaign.name,
      agent_id: campaign.agent_id,
      stage: 'did_not_pick',
      disposition_class: 'dnp',
    },
    would_post: {
      url: `/v2/campaign/leads/${campaign.agent_id}/${known.lead_uuid}/schedule`,
      body: { scheduled_time: `${D}T14:05:00` },
    },
    status,
    http_status: null,
    response: null,
  };
}

const TEST_UUID = '88c7a62a-d3f8-40a4-af7d-e0dd5e82566a';

export const mockTestHistory: TestCallAttempt[] = [
  { id: 3, phone: '9379747274', created_at: `${D}T09:02:11`, status: 'simulated', http_status: null, response: null, dry_run: true, campaign_id: 1, agent_id: 125, lead_uuid: TEST_UUID, lead_name: 'TEST Rehearsal 1' },
  { id: 2, phone: '9379747274', created_at: '2026-08-27T17:44:03', status: 'posted', http_status: 200, response: '{"ok":true,"scheduled":1}', dry_run: false, campaign_id: 1, agent_id: 125, lead_uuid: TEST_UUID, lead_name: 'TEST Rehearsal 1' },
  { id: 1, phone: '9379747274', created_at: '2026-08-27T17:41:58', status: 'failed', http_status: 502, response: 'upstream timeout', dry_run: false, campaign_id: 1, agent_id: 125, lead_uuid: TEST_UUID, lead_name: 'TEST Rehearsal 1' },
];

export function mockTestTrigger(phone: string, campaignId?: number): TestCallResult {
  const res = mockTestCall(phone, campaignId);
  mockTestHistory.unshift({
    id: (mockTestHistory[0]?.id ?? 0) + 1,
    phone,
    created_at: new Date().toISOString().slice(0, 19),
    status: res.status,
    http_status: res.http_status,
    response: res.response,
    dry_run: res.dry_run,
    campaign_id: res.lead?.campaign_id ?? null,
    lead_uuid: res.lead?.lead_uuid ?? null,
  });
  return res;
}

export function mockManualPreview(dispositions: string[], buckets: string[]) {
  const cells = mockBuckets.matrix.filter(
    (c) => dispositions.includes(c.disposition) && buckets.includes(c.bucket),
  );
  const count = cells.reduce((a, c) => a + c.count, 0);
  const intensive = buckets.filter(isIntensive).length > 0;
  const sample: ManualSample[] = mockItems(41)
    .filter((i) => buckets.includes(i.bucket))
    .slice(0, 8)
    .map((i, idx) => ({
      lead_uuid: i.lead_uuid,
      policy_no: i.policy_no,
      lead_name: i.lead_name,
      disposition: dispositions[idx % dispositions.length] ?? i.disposition,
      disposition_class: i.disposition_class,
      dte: i.dte,
      bucket: i.bucket,
      slot_no: i.slot_no,
      scheduled_time: i.scheduled_time,
    }));
  return { count, slots: Math.round(count * (intensive ? 1.25 : 1)), sample };
}
