import { useState } from 'react';
import { CircleSlash, Loader2, Play, Users } from 'lucide-react';
import { api } from '../lib/api';
import { n } from '../lib/domain';
import { useAgent, useStore } from '../lib/store';
import { Fact, Modal } from './ui';
import type { Agent, Campaign } from '../lib/types';

/* Scope and pause controls.

   The agent is a scope, not a filter. Everything downstream — campaigns, plans,
   runs, config — belongs to exactly one agent, and approving a run against the
   wrong one is a real outbound-calling mistake. So the switcher sits above the
   campaign picker, the current scope is echoed in the topbar on every screen,
   and pausing reaches the whole agent from here.

   Campaign pause lives here too so the topbar and the config screen share one
   implementation and one wording. */

/** One place that knows what pausing a campaign means. Used by the topbar
 *  button and the config screen's state strip. */
export function useCampaignPause() {
  const { campaigns, setCampaigns, toast } = useStore();
  return async (campaign: Campaign) => {
    try {
      const next = campaign.paused ? await api.resume(campaign.id) : await api.pause(campaign.id);
      setCampaigns(campaigns.map((c) => (c.id === next.id ? next : c)));
      toast(
        'ok',
        next.paused
          ? 'Campaign paused. Approve and commit are blocked until you resume.'
          : 'Campaign resumed. Runs can be approved again.',
      );
    } catch (e) {
      toast('bad', (e as Error).message);
    }
  };
}

/** Compact topbar control — pause has to be findable from wherever a campaign
 *  is selected, not buried three screens deep on config. */
export function CampaignPauseButton({ campaign }: { campaign: Campaign }) {
  const togglePause = useCampaignPause();
  const [busy, setBusy] = useState(false);
  return (
    <button
      className={`btn btn-sm ${campaign.paused ? 'btn-primary' : 'btn-danger'}`}
      disabled={busy}
      title={
        campaign.paused
          ? 'Resume this campaign so runs can be approved'
          : 'Pause this campaign — approve and commit return 409 until resumed'
      }
      onClick={() => {
        setBusy(true);
        togglePause(campaign).finally(() => setBusy(false));
      }}
    >
      {busy ? <Loader2 className="spin" /> : campaign.paused ? <Play /> : <CircleSlash />}
      {campaign.paused ? 'Resume' : 'Pause'}
    </button>
  );
}

export function AgentSwitcher({
  agents,
  agentId,
  onPick,
}: {
  agents: Agent[];
  agentId: number | null;
  onPick: (id: number) => void;
}) {
  return (
    <div className="seg" role="tablist" aria-label="Agent scope">
      {agents.map((a) => (
        <button
          key={a.agent_id}
          role="tab"
          className={`seg-btn${a.agent_id === agentId ? ' is-active' : ''}${a.paused ? ' is-paused' : ''}`}
          aria-selected={a.agent_id === agentId}
          data-agent={a.agent_id}
          title={`${a.name} · ${a.campaigns} campaigns · ${a.paused_campaigns} paused`}
          onClick={() => onPick(a.agent_id)}
        >
          <span>{a.agent_id}</span>
          <small>
            {a.campaigns} camp
            {a.paused_campaigns > 0 ? ` · ${a.paused_campaigns}⏸` : ''}
          </small>
        </button>
      ))}
    </div>
  );
}

/** The topbar echo. Small, but it is the one thing on screen that says which
 *  agent an approve button is about to dial for. */
export function AgentChip({ agent }: { agent: Agent | null }) {
  if (!agent) return <span className="agent-chip">no agent</span>;
  return (
    <span className={`agent-chip${agent.paused ? ' is-paused' : ''}`} title={agent.name}>
      <Users size={11} /> agent {agent.agent_id}
      {agent.paused && ' · all paused'}
    </span>
  );
}

export function AgentPauseConfirm({
  agent,
  mode,
  running,
  busy,
  onClose,
  onConfirm,
}: {
  agent: Agent;
  mode: 'pause' | 'resume';
  /** enabled campaigns on this agent that are currently dialable */
  running: number;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const pausing = mode === 'pause';
  return (
    <Modal
      title={pausing ? `Pause every campaign on agent ${agent.agent_id}` : `Resume agent ${agent.agent_id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className={pausing ? 'btn btn-danger' : 'btn btn-primary'}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? <Loader2 className="spin" /> : pausing ? <CircleSlash /> : <Play />}
            {pausing ? `Pause all ${n(agent.campaigns)}` : `Resume all ${n(agent.campaigns)}`}
          </button>
        </>
      }
    >
      <div className={pausing ? 'infobox' : 'warnbox'}>
        {pausing ? <CircleSlash /> : <Play />}
        <span>
          {pausing ? (
            <>
              This hits <b>every campaign on agent {agent.agent_id}</b>, not just the one selected.
              Approve and commit return <span className="mono">409</span> on all of them until you
              resume. Planning still works, and nothing already dialled is undone.
            </>
          ) : (
            <>
              This makes <b>every campaign on agent {agent.agent_id}</b> approvable again, including
              ones somebody paused individually for their own reason. Resume the single campaign you
              need instead if you are not sure.
            </>
          )}
        </span>
      </div>

      <div className="confirm-facts">
        <Fact k="Agent" v={agent.agent_id} />
        <Fact k="Campaigns affected" v={n(agent.campaigns)} />
        <Fact k="Currently paused" v={n(agent.paused_campaigns)} />
        <Fact
          k="Currently dialable"
          v={n(running)}
          tone={pausing && running > 0 ? 'var(--warn)' : undefined}
        />
      </div>
    </Modal>
  );
}

/** Rail block: switch scope, see the agent's state, pause the whole agent. */
export function AgentScope() {
  const { agents, agentId, campaigns, setAgent, toast } = useStore();
  const agent = useAgent();
  const [mode, setMode] = useState<'pause' | 'resume' | null>(null);
  const [busy, setBusy] = useState(false);

  const running = campaigns.filter((c) => c.enabled && !c.paused).length;

  const pick = (id: number) => {
    if (id === agentId) return;
    setAgent(id).then(() => toast('info', `Scope switched to agent ${id}.`));
  };

  const apply = async () => {
    if (!agent || !mode) return;
    setBusy(true);
    try {
      if (mode === 'pause') await api.pauseAgent(agent.agent_id);
      else await api.resumeAgent(agent.agent_id);
      // Re-read the scope rather than trusting the response shape: this touched
      // every campaign on the agent.
      await setAgent(agent.agent_id);
      toast(
        'ok',
        mode === 'pause'
          ? `Agent ${agent.agent_id} paused. All ${agent.campaigns} campaigns block approve until resumed.`
          : `Agent ${agent.agent_id} resumed. All ${agent.campaigns} campaigns can be approved again.`,
      );
      setMode(null);
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (agents.length === 0) return null;

  return (
    <div className="agent-scope">
      <div className="eyebrow">Agent scope</div>
      <AgentSwitcher agents={agents} agentId={agentId} onPick={pick} />
      {agent && (
        <>
          <div className="agent-meta">
            <span className="mono">{agent.campaigns}</span> camp ·{' '}
            <span className="mono">{agent.enabled}</span> enabled ·{' '}
            <span className={`mono${agent.paused_campaigns ? ' is-warn' : ''}`}>
              {agent.paused_campaigns}
            </span>{' '}
            paused
          </div>
          <button
            className={`btn btn-sm ${agent.paused ? 'btn-primary' : 'btn-danger'}`}
            onClick={() => setMode(agent.paused ? 'resume' : 'pause')}
          >
            {agent.paused ? <Play /> : <CircleSlash />}
            <span>{agent.paused ? 'Resume all' : 'Pause all'}</span>
          </button>
        </>
      )}

      {mode && agent && (
        <AgentPauseConfirm
          agent={agent}
          mode={mode}
          running={running}
          busy={busy}
          onClose={() => setMode(null)}
          onConfirm={apply}
        />
      )}
    </div>
  );
}
