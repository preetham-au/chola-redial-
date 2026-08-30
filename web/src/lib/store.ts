import { create } from 'zustand';
import { useEffect, useState } from 'react';
import { api, isOffline, onOfflineChange } from './api';
import { agentsFrom, today } from './domain';
import type { Agent, Campaign, Health } from './types';

export type Toast = { id: number; kind: 'ok' | 'bad' | 'info'; text: string };

const AGENT_KEY = 'redial.agent';

const remember = (id: number) => {
  try {
    localStorage.setItem(AGENT_KEY, String(id));
  } catch {
    /* private mode */
  }
};

const remembered = (): number | null => {
  try {
    const v = Number(localStorage.getItem(AGENT_KEY));
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
};

/** The agent rows for every agent except the current one come from the server;
 *  the current one is recomputed from the campaigns actually loaded, so a pause
 *  shows up in the switcher without a second round trip. */
function mergeAgents(agents: Agent[], scoped: Campaign[]): Agent[] {
  const [fresh] = agentsFrom(scoped);
  if (!fresh) return agents;
  return agents.map((a) =>
    a.agent_id === fresh.agent_id ? { ...a, ...fresh, name: a.name || fresh.name } : a,
  );
}

interface State {
  agents: Agent[];
  /** Every screen is scoped to this agent. Persisted across reloads. */
  agentId: number | null;
  campaigns: Campaign[];
  campaignId: number | null;
  date: string;
  health: Health | null;
  offline: boolean;
  toasts: Toast[];
  bootstrap: () => Promise<void>;
  setAgent: (id: number) => Promise<void>;
  setCampaign: (id: number) => void;
  setDate: (d: string) => void;
  setCampaigns: (c: Campaign[]) => void;
  toast: (kind: Toast['kind'], text: string) => void;
  dismiss: (id: number) => void;
}

let toastSeq = 0;

export const useStore = create<State>((set, get) => ({
  agents: [],
  agentId: null,
  campaigns: [],
  campaignId: null,
  date: today(),
  health: null,
  offline: isOffline(),
  toasts: [],

  bootstrap: async () => {
    const [health, agents] = await Promise.all([api.health(), api.agents()]);
    const want = get().agentId ?? remembered();
    const agentId =
      agents.find((a) => a.agent_id === want)?.agent_id ?? agents[0]?.agent_id ?? null;
    set({ health, agents, offline: isOffline() });
    if (agentId !== null) await get().setAgent(agentId);
  },

  setAgent: async (id) => {
    remember(id);
    const campaigns = await api.campaigns(id);
    const keep = campaigns.some((c) => c.id === get().campaignId) ? get().campaignId : null;
    set({
      agentId: id,
      campaigns,
      // Never carry a campaign across the scope boundary.
      campaignId: keep ?? campaigns[0]?.id ?? null,
      agents: mergeAgents(get().agents, campaigns),
      offline: isOffline(),
    });
  },

  setCampaign: (id) => set({ campaignId: id }),
  setDate: (d) => set({ date: d }),
  setCampaigns: (campaigns) =>
    set({ campaigns, agents: mergeAgents(get().agents, campaigns) }),

  toast: (kind, text) => {
    const id = ++toastSeq;
    set((s) => ({ toasts: [...s.toasts, { id, kind, text }] }));
    setTimeout(() => get().dismiss(id), 6000);
  },

  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

onOfflineChange((v) => useStore.setState({ offline: v }));

export const useCampaign = () =>
  useStore((s) => s.campaigns.find((c) => c.id === s.campaignId) ?? null);

export const useAgent = () =>
  useStore((s) => s.agents.find((a) => a.agent_id === s.agentId) ?? null);

/** Hash routing. Seven screens do not need a router dependency. */
export function useRoute(): [string, (r: string) => void] {
  // tolerate a pasted "#/plan" as well as "#plan"
  const read = () => window.location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return [route, (r: string) => { window.location.hash = r; }];
}

export function navigate(r: string) {
  window.location.hash = r;
}

interface Async<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Minimal fetch-on-deps hook. No cache layer — the console is small and
 *  the operator wants a fresh read every time they change campaign or date. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): Async<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    fn()
      .then((d) => live && setData(d))
      .catch((e: Error) => live && setError(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}
