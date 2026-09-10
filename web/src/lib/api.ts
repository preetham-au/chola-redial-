// Real API first. If the backend is unreachable we fall back to fixtures and
// raise a flag the UI shows on every screen — mock data must never be mistaken
// for the warehouse.

import type {
  Agent,
  ApproveResult,
  AutopilotStatus,
  BucketsResponse,
  Campaign,
  Config,
  ConfigVersion,
  DayView,
  DialLogPage,
  DialLogSummary,
  Health,
  ManualPreview,
  PagedItems,
  PlanItem,
  PrepareResult,
  Run,
  StageJob,
  StagePreview,
  SyncStatus,
  TestCallAttempt,
  TestCallResult,
  TestNumber,
} from './types';
import { agentsFrom, today } from './domain';
import {
  mockAgentPause,
  mockAgents,
  mockAutopilot,
  mockBuckets,
  mockCampaigns,
  mockConfig,
  mockConfigHistory,
  mockDay,
  mockDialLog,
  mockDialLogSummary,
  mockExpiredPreview,
  mockHealth,
  mockItems,
  mockManualPreview,
  mockRedPreview,
  mockRuns,
  mockStageJobs,
  mockStagePreview,
  mockSyncIdle as idleSync,
  mockTestCall,
  mockTestHistory,
  mockTestNumbers,
  mockTestTrigger,
} from './mock';

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

// Every path below is written absolute ('/api/...'), which only resolves when
// the app is served from the domain root. Behind the tunnel it is served under
// /redial/, so prefix with vite's base. BASE_URL is '/' in dev and '/redial/'
// in that build; the trailing slash is stripped so we never emit '//api'.
// Read defensively: `npm run check` bundles this to CJS, where `import.meta`
// is empty, and a hard read there crashes the self-check before it runs.
export const API_PREFIX = (import.meta.env?.BASE_URL ?? '/').replace(/\/$/, '');

let offline = false;
const listeners = new Set<(v: boolean) => void>();

export const isOffline = () => offline;

export function onOfflineChange(fn: (v: boolean) => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function setOffline(v: boolean) {
  if (offline === v) return;
  offline = v;
  listeners.forEach((f) => f(v));
}

/** Drop the sticky offline flag and try the real backend again. */
export function retryLive() {
  setOffline(false);
}

async function req<T>(path: string, init: RequestInit | undefined, fallback: () => T): Promise<T> {
  if (offline) return fallback();
  try {
    const res = await fetch(API_PREFIX + path, {
      ...init,
      headers: init?.body ? { 'Content-Type': 'application/json', ...init?.headers } : init?.headers,
    });
    if (!res.ok) {
      // A live server answering 4xx is a real answer — surface it.
      let msg = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body?.error) msg = body.error;
      } catch {
        /* non-JSON error body */
      }
      if (res.status >= 500) {
        setOffline(true);
        return fallback();
      }
      throw new ApiError(msg, res.status);
    }
    return (await res.json()) as T;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    setOffline(true); // fetch threw: backend not up
    return fallback();
  }
}

const q = (o: Record<string, string | number | undefined>) => {
  const p = new URLSearchParams();
  Object.entries(o).forEach(([k, v]) => {
    if (v !== undefined && v !== '') p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : '';
};

const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) });

/** Scoped campaign list. Both filters are re-applied client-side on purpose: a
 *  server that ignores `?agent_id` must not be able to leak another agent's
 *  campaigns into a scoped picker — that is how a script gets dialled at the
 *  wrong cohort — and one that ignores `include_hidden` must not be able to put
 *  a campaign the operator retired back in front of them. */
async function listCampaigns(agent_id?: number, includeHidden = false): Promise<Campaign[]> {
  const all = await req<Campaign[]>(
    `/api/campaigns${q({ agent_id, include_hidden: includeHidden ? 'true' : undefined })}`,
    undefined,
    () => mockCampaigns,
  );
  return all.filter(
    (c) => (agent_id === undefined || c.agent_id === agent_id) && (includeHidden || !c.hidden),
  );
}

export const api = {
  health: () => req<Health>('/api/health', undefined, () => mockHealth),
  /* Switching dialling ON needs confirm='GO LIVE'; switching it off needs nothing.
     The server holds this in its environment, so it lasts until the next restart. */
  setDryRun: (enabled: boolean, confirm = '') =>
    req<{ dry_run: boolean }>(
      '/api/config/dry-run',
      json({ enabled, confirm }),
      () => ({ dry_run: enabled }),
    ),

  /* Returns as soon as the pull starts; poll syncStatus for the outcome. */
  startSync: () => req<SyncStatus>('/api/sync', json({}), () => idleSync),
  syncStatus: () => req<SyncStatus>('/api/sync', undefined, () => idleSync),

  campaigns: listCampaigns,

  /** Never hardcoded. A backend that has not shipped /api/agents yet answers
   *  404, and we derive the identical shape from the campaign list. */
  agents: async (): Promise<Agent[]> => {
    try {
      return await req<Agent[]>('/api/agents', undefined, mockAgents);
    } catch {
      return agentsFrom(await listCampaigns());
    }
  },

  pauseAgent: (agentId: number) =>
    req<Agent>(`/api/agents/${agentId}/pause`, { method: 'POST' }, () => mockAgentPause(agentId, true)),

  resumeAgent: (agentId: number) =>
    req<Agent>(`/api/agents/${agentId}/resume`, { method: 'POST' }, () => mockAgentPause(agentId, false)),

  testNumbers: () => req<TestNumber[]>('/api/test-call/numbers', undefined, () => mockTestNumbers),

  // `scheduled_time` omitted = the server picks the next minute inside the dial
  // window. When given it is validated server-side, never trusted.
  testPreview: (body: { phone: string; campaign_id?: number; scheduled_time?: string }) =>
    req<TestCallResult>('/api/test-call/preview', json(body), () =>
      mockTestCall(body.phone, body.campaign_id, 'preview'),
    ),

  testTrigger: (body: { phone: string; campaign_id?: number; scheduled_time?: string }) =>
    req<TestCallResult>('/api/test-call/trigger', json(body), () =>
      mockTestTrigger(body.phone, body.campaign_id),
    ),

  testHistory: () => req<TestCallAttempt[]>('/api/test-call/history', undefined, () => mockTestHistory),

  pause: (id: number) =>
    req<Campaign>(`/api/campaigns/${id}/pause`, { method: 'POST' }, () => ({
      ...(mockCampaigns.find((c) => c.id === id) ?? mockCampaigns[0]),
      paused: true,
    })),

  resume: (id: number) =>
    req<Campaign>(`/api/campaigns/${id}/resume`, { method: 'POST' }, () => ({
      ...(mockCampaigns.find((c) => c.id === id) ?? mockCampaigns[0]),
      paused: false,
    })),

  /** Take a campaign out of circulation: gone from every list here, and it can
   *  no longer be armed. `live_today` is what it already put on Formi's clock
   *  for the rest of today — hiding does not reach out and cancel those. */
  hide: (id: number) =>
    req<Campaign & { live_today: number }>(`/api/campaigns/${id}/hide`, { method: 'POST' }, () => {
      const c = mockCampaigns.find((x) => x.id === id) ?? mockCampaigns[0];
      Object.assign(c, { hidden: true, autopilot: false, autopilot_note: 'hidden by operator' });
      return { ...c, live_today: 0 };
    }),

  /** Put it back in the lists — disarmed. Arming is a separate, deliberate act. */
  unhide: (id: number) =>
    req<Campaign>(`/api/campaigns/${id}/unhide`, { method: 'POST' }, () => {
      const c = mockCampaigns.find((x) => x.id === id) ?? mockCampaigns[0];
      Object.assign(c, { hidden: false, autopilot_note: 'un-hidden by operator' });
      return { ...c };
    }),

  /* --- The day -----------------------------------------------------------
     One screen, one decision. `day` is cheap enough to poll: the server reads
     rows it already wrote and never re-runs the engine. */

  day: (date: string, kind = 'auto') =>
    req<DayView>(`/api/day${q({ date, kind })}`, undefined, () => mockDay(date, kind)),

  /** Build (or rebuild) the plan. Writes `planned` runs and dials nothing. */
  prepareDay: (date: string, kind = 'auto', resync = false) =>
    req<PrepareResult>('/api/day/prepare', json({ date, kind, resync }), () => {
      throw new ApiError('The server is unreachable — no plan was built.', 503);
    }),

  /** The only call in this client that reaches Formi. `buckets` empty = all of
   *  them; the rest are not dialled today and return in tomorrow's plan. */
  approveDay: (date: string, kind = 'auto', buckets: string[] = [], campaign_ids: number[] = []) =>
    req<ApproveResult>('/api/day/approve', json({ date, kind, buckets, campaign_ids }), () => {
      // Never invent an approval. This is the one answer that would tell an
      // operator customers were called when nothing was.
      throw new ApiError('The server is unreachable — nothing was approved or dialled.', 503);
    }),

  /* --- Dial log ----------------------------------------------------------- */

  dialLog: (f: { campaign_id?: number; date?: string; verified?: string; outcome?: string;
                 limit?: number; offset?: number }) =>
    req<DialLogPage>(`/api/dial-log${q(f)}`, undefined, () => mockDialLog(f.date ?? today())),

  dialLogSummary: (date: string, campaign_id?: number) =>
    req<DialLogSummary>(`/api/dial-log/summary${q({ date, campaign_id })}`, undefined,
      () => mockDialLogSummary(date)),

  /** Ask the warehouse what really happened. Read-only — it cannot place a call,
   *  whatever DRY_RUN says. */
  verifyDialLog: (date: string, wait = false) =>
    req<unknown>(`/api/dial-log/verify${q({ date, wait: wait ? 'true' : undefined })}`,
      { method: 'POST' }, () => {
        throw new ApiError('The server is unreachable — nothing was verified.', 503);
      }),

  /** The one switch. On = this campaign is in the daily plan. It never dials:
   *  the plan waits for the day to be approved. */
  setAutopilot: (id: number, on: boolean) =>
    req<Campaign>(`/api/campaigns/${id}/autopilot`, json({ on }), () => {
      const c = mockCampaigns.find((x) => x.id === id) ?? mockCampaigns[0];
      Object.assign(c, { autopilot: on, autopilot_note: on ? 'started by operator' : 'stopped by operator' });
      return { ...c };
    }),

  /** When the passes fire and which have fired today. A pass is fired once a
   *  day and never retried, so a missed one shows here and nowhere else. */
  autopilotStatus: () =>
    req<AutopilotStatus>('/api/autopilot', undefined, () => mockAutopilot),

  /** Re-fire a pass the server missed. The documented recovery path -- without
   *  it the only way back is curl. */
  runPass: (kind: string) =>
    req<unknown>('/api/autopilot/run', json({ kind }), () => {
      // Every other fallback here invents a plausible read. This one would be
      // inventing a dial that did not happen, on the one screen that exists to
      // tell an operator a pass was missed.
      throw new ApiError('The server is unreachable — nothing was re-run.', 503);
    }),

  config: (id: number) => req<Config>(`/api/campaigns/${id}/config`, undefined, () => mockConfig),

  saveConfig: (id: number, cfg: Config) =>
    req<Config>(`/api/campaigns/${id}/config`, { method: 'PUT', body: JSON.stringify(cfg) }, () => ({
      ...cfg,
      version: cfg.version + 1,
      created_at: new Date().toISOString().slice(0, 19),
    })),

  configHistory: (id: number) =>
    req<ConfigVersion[]>(`/api/campaigns/${id}/config/history`, undefined, () => mockConfigHistory),

  // `buckets` empty = every schedulable bucket. Pass e.g. ['M0','E0','F6','F5'] to put
  // only the urgent ones on the clock for the day.
  // `window` narrows the dial hours for this run only — it never edits the config.
  plan: (id: number, date: string, buckets: string[] = [], window?: { start: string; end: string }) =>
    req<Run>(`/api/campaigns/${id}/plan`, json({ date, buckets, ...window }),
      () => ({ ...mockRuns[0], run_date: date })),

  buckets: (id: number, date?: string) =>
    req<BucketsResponse>(`/api/campaigns/${id}/buckets${q({ date })}`, undefined, () => mockBuckets),

  runs: (campaign_id: number, limit = 50) =>
    req<Run[]>(`/api/runs${q({ campaign_id, limit })}`, undefined, () =>
      mockRuns.filter((r) => r.campaign_id === campaign_id || campaign_id === 1),
    ),

  run: (id: number) =>
    req<Run>(`/api/runs/${id}`, undefined, () => mockRuns.find((r) => r.id === id) ?? mockRuns[0]),

  items: (
    runId: number,
    f: { bucket?: string; disposition?: string; status?: string; page?: number; page_size?: number },
  ) =>
    req<PagedItems>(`/api/runs/${runId}/items${q(f)}`, undefined, () => {
      const page = f.page ?? 1;
      const size = f.page_size ?? 50;
      const all = mockItems(runId).filter(
        (i) =>
          (!f.bucket || i.bucket === f.bucket) &&
          (!f.disposition || i.disposition === f.disposition) &&
          (!f.status || i.status === f.status),
      );
      return { items: all.slice((page - 1) * size, page * size), page, page_size: size, total: all.length };
    }),

  /** Unfiltered items for the dial-window timeline. */
  allItems: (runId: number) =>
    req<PagedItems>(`/api/runs/${runId}/items${q({ page: 1, page_size: 5000 })}`, undefined, () => {
      const all = mockItems(runId);
      return { items: all, page: 1, page_size: 5000, total: all.length };
    }),

  deleteRun: (runId: number) =>
    req<{ deleted: number }>(`/api/runs/${runId}`, { method: 'DELETE' }, () => ({ deleted: runId })),

  updateItemTime: (runId: number, itemId: number, scheduled_time: string) =>
    req<PlanItem>(`/api/runs/${runId}/items/${itemId}`,
      { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scheduled_time }) },
      () => ({ ...(mockItems(runId).find((i) => i.id === itemId)!), scheduled_time })),

  approve: (runId: number) =>
    req<Run & { dry_run: boolean }>(`/api/runs/${runId}/approve`, { method: 'POST' }, () => {
      const r = mockRuns.find((x) => x.id === runId) ?? mockRuns[0];
      const done: Run & { dry_run: boolean } = {
        ...r,
        status: 'committed',
        counts: { ...r.counts, posted: r.counts.slots },
        dry_run: true,
      };
      Object.assign(r, { status: 'committed', counts: done.counts });
      mockItems(runId).forEach((i) => {
        i.status = 'simulated';
      });
      return done;
    }),

  /** Take the rest of a committed run back off Formi's clock so it can be edited. */
  pauseRun: (runId: number) =>
    req<Run & { cancelled: number; cancel_failed: number; left_running: number }>(
      `/api/runs/${runId}/pause`, { method: 'POST' }, () => {
        const r = mockRuns.find((x) => x.id === runId) ?? mockRuns[0];
        const live = mockItems(runId).filter((i) => i.status !== 'planned');
        live.forEach((i) => { i.status = 'planned'; });
        Object.assign(r, { status: 'paused' });
        return { ...r, status: 'paused' as const, cancelled: live.length, cancel_failed: 0, left_running: 0 };
      }),

  resumeRun: (runId: number) =>
    req<Run & { dry_run: boolean }>(`/api/runs/${runId}/resume`, { method: 'POST' }, () => {
      const r = mockRuns.find((x) => x.id === runId) ?? mockRuns[0];
      mockItems(runId).forEach((i) => { if (i.status === 'planned') i.status = 'simulated'; });
      Object.assign(r, { status: 'committed' });
      return { ...r, status: 'committed' as const, dry_run: true };
    }),

  manualPreview: (body: { campaign_id: number; dispositions: string[]; buckets: string[]; date?: string }) =>
    req<ManualPreview>('/api/manual/preview', json(body), () =>
      mockManualPreview(body.dispositions, body.buckets),
    ),

  manualSchedule: (body: { campaign_id: number; dispositions: string[]; buckets: string[]; date?: string }) =>
    req<Run & { dry_run: boolean }>('/api/manual/schedule', json(body), () => {
      const p = mockManualPreview(body.dispositions, body.buckets);
      const run: Run & { dry_run: boolean } = {
        id: 42,
        campaign_id: body.campaign_id,
        run_date: body.date ?? mockRuns[0].run_date,
        kind: 'manual',
        status: 'planned',
        config_version: 3,
        created_at: new Date().toISOString().slice(0, 19),
        counts: { evaluated: p.count, planned: p.count, slots: p.slots, posted: 0, failed: 0 },
        dry_run: true,
      };
      if (!mockRuns.some((r) => r.id === 42)) mockRuns.unshift(run);
      return run;
    }),

  policiesPreview: (body: { policies: string[]; target_stage: string; campaign_ids: number[] }) =>
    req<StagePreview>('/api/stage/policies/preview', json(body), () =>
      mockStagePreview(body.policies, body.target_stage),
    ),

  policiesCommit: (body: { policies: string[]; target_stage: string; campaign_ids: number[] }) =>
    req<StagePreview & { dry_run: boolean; applied: number }>(
      '/api/stage/policies/commit',
      json(body),
      () => {
        const p = mockStagePreview(body.policies, body.target_stage);
        return { ...p, applied: p.would_change, dry_run: true };
      },
    ),

  // A renewal date corrected here is written locally and re-applied after every
  // sync. Formi exposes no endpoint that writes one, so it changes what this
  // console schedules from, not what the agent reads out on the call.
  redPreview: (body: { policies: string[]; red: string; campaign_ids: number[] }) =>
    req<StagePreview & { red: string }>('/api/stage/red/preview', json(body), () =>
      mockRedPreview(body.policies, body.red),
    ),

  redCommit: (body: { policies: string[]; red: string; campaign_ids: number[] }) =>
    req<StagePreview & { red: string; applied: number; dry_run: boolean }>(
      '/api/stage/red/commit',
      json(body),
      () => {
        const p = mockRedPreview(body.policies, body.red);
        return { ...p, applied: p.would_change, dry_run: true };
      },
    ),

  expiredPreview: (body: { campaign_ids: number[]; red_before: string; target_stage: string }) =>
    req<StagePreview>('/api/stage/expired/preview', json(body), () => mockExpiredPreview(body.red_before)),

  expiredCommit: (body: { campaign_ids: number[]; red_before: string; target_stage: string }) =>
    req<StagePreview & { dry_run: boolean; applied: number }>(
      '/api/stage/expired/commit',
      json(body),
      () => {
        const p = mockExpiredPreview(body.red_before);
        return { ...p, applied: p.would_change, dry_run: true };
      },
    ),

  stageJobs: () => req<StageJob[]>('/api/stage/jobs', undefined, () => mockStageJobs),
};

export type { PlanItem };
