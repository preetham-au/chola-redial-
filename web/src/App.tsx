import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  FlaskConical,
  LayoutGrid,
  ListChecks,
  PhoneCall,
  PhoneOutgoing,
  Play,
  Radio,
  Settings2,
  Tags,
  WifiOff,
} from 'lucide-react';
import { API_PREFIX, retryLive } from './lib/api';
import { navigate, useAgent, useCampaign, useRoute, useStore } from './lib/store';
import { AgentChip, AgentScope, CampaignPauseButton } from './components/AgentBar';
import { CloseButton } from './components/ui';
import { DryRunToggle } from './components/DryRunToggle';
import { Dashboard } from './screens/Dashboard';
import { TestCall } from './screens/TestCall';
import { PlanReview } from './screens/PlanReview';
import { ConfigScreen } from './screens/ConfigScreen';
import { ManualRedial } from './screens/ManualRedial';
import { BulkStage } from './screens/BulkStage';
import type { Campaign } from './lib/types';

const NAV = [
  { group: 'Today', items: [
    { id: 'dashboard', label: 'Dashboard', icon: LayoutGrid },
    { id: 'plan', label: 'Plan review', icon: ListChecks },
  ]},
  { group: 'Dial deliberately', items: [
    { id: 'manual', label: 'Manual redial', icon: PhoneOutgoing },
    { id: 'testcall', label: 'Test call', icon: PhoneCall },
  ]},
  { group: 'Lead data', items: [
    { id: 'bulk', label: 'Bulk stage change', icon: Tags },
  ]},
  { group: 'Settings', items: [
    { id: 'config', label: 'Campaign config', icon: Settings2 },
  ]},
];

export function App() {
  const [route, go] = useRoute();
  const { bootstrap, campaigns, campaignId, setCampaign, date, setDate, health, offline, toasts, dismiss } =
    useStore();
  const campaign = useCampaign();
  const agent = useAgent();

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  const live = health ? !health.dry_run : false;
  const head = route.split('/')[0];

  return (
    <div className={`shell${live ? ' is-live' : ''}`}>
      <nav className="rail">
        <div className="rail-brand">
          <div className="rail-brand-mark">
            <strong>Redial</strong>
            <span>console</span>
          </div>
        </div>

        {/* Scope before navigation: every screen below belongs to one agent. */}
        <AgentScope />

        <div className="rail-nav">
          {NAV.map((g) => (
            <div key={g.group}>
              <div className="nav-group eyebrow">{g.group}</div>
              {g.items.map((it) => {
                const Icon = it.icon;
                return (
                  <button
                    key={it.id}
                    className={`nav-item${head === it.id ? ' is-active' : ''}`}
                    onClick={() => go(it.id)}
                    aria-current={head === it.id ? 'page' : undefined}
                  >
                    <Icon />
                    <span>{it.label}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </div>

        <div className="rail-foot">
          <DryRunToggle />
          {health && (
            <div className="eyebrow" style={{ paddingLeft: 2 }}>
              db {health.db} · {health.leads_source}
            </div>
          )}
        </div>
      </nav>

      <main className="main">
        <header className={`topbar${campaign?.paused ? ' is-paused' : ''}`}>
          {/* The scope, restated where the approve buttons are. */}
          <AgentChip agent={agent} />

          <div className="topbar-sep" />

          <label className="eyebrow" htmlFor="campaign">Campaign</label>
          <CampaignPicker
            campaigns={campaigns}
            campaignId={campaignId}
            onPick={setCampaign}
          />

          {campaign?.paused && (
            <span className="badge badge-warn">
              <CircleSlash size={11} /> Paused
            </span>
          )}
          {campaign && !campaign.enabled && (
            <span className="badge">
              <CircleSlash size={11} /> Disabled
            </span>
          )}
          {campaign && <CampaignPauseButton campaign={campaign} />}

          <div className="topbar-sep" />

          <label className="eyebrow" htmlFor="date">Date</label>
          <input
            id="date"
            className="input"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />

          <div className="topbar-spacer" />

          <div className={`dry-badge${live ? ' is-live' : ''}`}>
            {live ? <Radio /> : <FlaskConical />}
            <span>{live ? 'LIVE' : 'DRY RUN'}</span>
          </div>
        </header>

        {offline && (
          <div className="mock-strip">
            <WifiOff />
            <span>
              {/* The real prefix, not a hardcoded guess: behind the tunnel this
                  is /redial/api, and naming 127.0.0.1:8000 sent people to debug
                  a port the console has never called. */}
              Backend unreachable at {API_PREFIX || ''}/api — showing mock data. Nothing here is real.
            </span>
            <button
              onClick={() => {
                retryLive();
                bootstrap();
              }}
            >
              Try live again
            </button>
          </div>
        )}

        <div className="scroller">
          {campaignId === null && head !== 'testcall' ? (
            <div className="page">
              <div className="empty">
                <Play />
                <h3>
                  {agent && campaigns.length === 0
                    ? `Agent ${agent.agent_id} has no campaigns`
                    : 'Loading campaigns'}
                </h3>
                {agent && campaigns.length === 0 && (
                  <p>Switch the agent scope in the rail. Nothing here can be planned or dialled.</p>
                )}
              </div>
            </div>
          ) : (
            <Screen route={route} />
          )}
        </div>
      </main>

      <div className="toast-stack">
        {toasts.map((t) => (
          <div key={t.id} className={`toast is-${t.kind}`}>
            {t.kind === 'bad' ? <AlertTriangle /> : <CheckCircle2 />}
            <span>{t.text}</span>
            <CloseButton onClick={() => dismiss(t.id)} />
          </div>
        ))}
      </div>
    </div>
  );
}

/** A native select does not truncate and is typeahead-searchable for free, so
 *  it stays. It no longer groups by agent: the list is already scoped to one
 *  agent, and a picker that can reach a second agent's campaigns is exactly the
 *  mistake the scope switcher exists to prevent. Disabled campaigns are shown,
 *  never hidden — you cannot resume what the picker will not list. */
export function CampaignPicker({
  campaigns,
  campaignId,
  onPick,
}: {
  campaigns: Campaign[];
  campaignId: number | null;
  onPick: (id: number) => void;
}) {
  const [filter, setFilter] = useState('');
  const q = filter.trim().toLowerCase();

  // The selected campaign always stays in the list, or the select goes blank.
  const shown = campaigns
    .filter(
      (c) =>
        c.id === campaignId ||
        !q ||
        `${c.name} ${c.agent_id} ${c.warehouse_id}`.toLowerCase().includes(q),
    )
    .sort((a, b) => a.name.localeCompare(b.name));

  if (campaigns.length === 0) {
    return <span className="badge badge-warn">agent has no campaigns</span>;
  }

  return (
    <>
      {campaigns.length > 8 && (
        <input
          className="input"
          style={{ width: 128 }}
          value={filter}
          placeholder="filter…"
          aria-label="Filter campaigns"
          onChange={(e) => setFilter(e.target.value)}
        />
      )}
      <select
        id="campaign"
        className="select"
        value={campaignId ?? ''}
        onChange={(e) => onPick(Number(e.target.value))}
      >
        {shown.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name} · wh {c.warehouse_id}
            {c.paused ? ' · paused' : ''}
            {c.enabled ? '' : ' · disabled'}
          </option>
        ))}
      </select>
      {q && (
        <span className="eyebrow">
          {shown.length} / {campaigns.length}
        </span>
      )}
    </>
  );
}

function Screen({ route }: { route: string }) {
  const [head, arg] = route.split('/');
  switch (head) {
    case 'plan':
      return <PlanReview runId={arg ? Number(arg) : null} />;
    // Legacy routes fold into their new homes: runs → dashboard drawer,
    // /runs/{id} → plan review; expired → bulk stage change (mode toggle).
    case 'runs':
      return arg ? <PlanReview runId={Number(arg)} /> : <Dashboard />;
    case 'expired':
      return <BulkStage />;
    case 'config':
      return <ConfigScreen focusBucket={arg ?? null} />;
    case 'manual':
      return <ManualRedial />;
    case 'testcall':
      return <TestCall />;
    case 'bulk':
      return <BulkStage />;
    case 'dashboard':
      return <Dashboard />;
    default:
      navigate('dashboard');
      return null;
  }
}
