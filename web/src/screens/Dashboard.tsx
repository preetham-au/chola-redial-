import { useMemo, useState } from 'react';
import { ArrowRight, ChevronDown, ChevronRight, Loader2, PlayCircle, RefreshCw } from 'lucide-react';
import { api } from '../lib/api';
import {
  bucketColor,
  bucketRange,
  dateOf,
  dispLabel,
  friendlyBucket,
  n,
  narrowedBuckets,
  RUN_STATUS_TONE,
  skipMeta,
  timeOf,
} from '../lib/domain';
import { navigate, useAsync, useCampaign, useStore } from '../lib/store';
import { Crosstab } from '../components/Crosstab';
import { PlanDialog } from '../components/PlanDialog';
import { Runway } from '../components/Runway';
import { Card, Empty } from '../components/ui';
import type { BucketRow, Config, Run } from '../lib/types';

export function Dashboard() {
  const campaignId = useStore((s) => s.campaignId)!;
  const date = useStore((s) => s.date);
  const campaign = useCampaign();

  const buckets = useAsync(() => api.buckets(campaignId, date), [campaignId, date]);
  const runs = useAsync(() => api.runs(campaignId, 20), [campaignId]);
  const config = useAsync(() => api.config(campaignId), [campaignId]);

  const todays = useMemo(
    () => runs.data?.find((r) => r.run_date === date && r.kind === 'auto') ?? null,
    [runs.data, date],
  );

  const [planning, setPlanning] = useState(false);
  const plan = () => setPlanning(true);

  const b = buckets.data;

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">{campaign?.name}</span>
          <h1>Today’s dial picture</h1>
        </div>
        <div className="row" style={{ marginLeft: 'auto' }}>
          <button
            className="btn btn-ghost"
            onClick={() => { buckets.reload(); runs.reload(); }}
            aria-label="Refresh"
          >
            <RefreshCw /> Refresh
          </button>
        </div>
      </div>

      <Autopilot />

      <Hero
        run={todays}
        loading={runs.loading || buckets.loading}
        date={date}
        onPlan={plan}
      />

      {b ? <BucketSummary buckets={b.buckets} /> : <div className="card"><div className="card-body"><Loading/></div></div>}

      <Card
        title="Why some leads aren’t being called"
        eyebrow={b ? `${n(sum(b.skips))} total` : ''}
      >
        {b ? <SkipsPlain skips={b.skips} config={config.data} /> : <Loading />}
      </Card>

      <Details summary="See breakdown" eyebrow="runway · crosstab · bucket detail">
        {b ? (
          <div className="grid" style={{ gap: 14 }}>
            <Card title="RED runway" eyebrow={`${n(b.total_leads)} leads in window`}>
              <Runway buckets={b.buckets} onSelect={(x) => x && navigate(`plan`)} />
            </Card>
            <div className="split-3-2">
              <Card title="Where leads sit and why" eyebrow={`as of ${b.date}`} flush>
                <Crosstab data={b} />
              </Card>
              <Card title="Bucket detail">
                <div className="table-wrap">
                  <table className="t">
                    <thead>
                      <tr>
                        <th>Bucket</th>
                        <th className="n">Eligible</th>
                        <th className="n">Waiting</th>
                        <th className="n">Manual</th>
                        <th className="n">Total</th>
                      </tr>
                    </thead>
                    <tbody>
                      {b.buckets.map((row) => (
                        <tr key={row.bucket}>
                          <td>
                            <span className="row" style={{ gap: 7 }}>
                              <span className="chip-dot" style={{ background: bucketColor(row.bucket) }} />
                              <b style={{ color: bucketColor(row.bucket) }}>{friendlyBucket(row.bucket)}</b>
                              <span className="cell-dim" style={{ fontSize: 11 }}>{bucketRange(row.bucket)}</span>
                            </span>
                          </td>
                          <td className="n" style={{ fontWeight: 600 }}>{n(row.eligible)}</td>
                          <td className="n cell-dim">{n(row.waiting)}</td>
                          <td className="n cell-dim">{n(row.manual_only)}</td>
                          <td className="n">{n(row.total)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </div>
          </div>
        ) : (
          <Loading />
        )}
      </Details>

      <Details summary="Recent runs" eyebrow="history · newest first">
        <RecentRuns runs={runs.data ?? []} loading={runs.loading} />
      </Details>

      {planning && (
        <PlanDialog
          campaignId={campaignId}
          date={date}
          replacing={todays?.status === 'planned' ? todays.id : null}
          onClose={() => setPlanning(false)}
          onDone={() => {
            runs.reload();
            buckets.reload();
          }}
        />
      )}
    </div>
  );
}

/** The one switch that makes this campaign run itself.
 *
 *  On: the server re-syncs the leads and dials RED−7…RED+3 twice a day, and
 *  leaves the calmer buckets as a plan to approve. It stops on its own when
 *  every policy is past the grace window, and the moment the campaign is paused
 *  or removed in Formi. Everything below this card still works by hand. */
function Autopilot() {
  const campaign = useCampaign();
  const { campaigns, setCampaigns, toast } = useStore();
  const [busy, setBusy] = useState(false);
  if (!campaign) return null;

  const on = campaign.autopilot === true;
  const toggle = async () => {
    setBusy(true);
    try {
      const next = await api.setAutopilot(campaign.id, !on);
      setCampaigns(campaigns.map((c) => (c.id === next.id ? next : c)));
      toast(
        on ? 'info' : 'ok',
        on
          ? `Autopilot off for ${next.name}. Nothing runs unless you plan it.`
          : `Autopilot on for ${next.name}. Urgent buckets dial themselves until the policies run out.`,
      );
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title="Autopilot"
      eyebrow={on ? 'running · urgent buckets dial themselves' : 'off · everything is manual'}
    >
      <div className="row" style={{ gap: 14, alignItems: 'flex-start' }}>
        <p className="hero-sub" style={{ margin: 0, flex: 1 }}>
          {on
            ? 'Morning and afternoon, this campaign re-syncs and dials RED−7 to RED+3 by itself. The calmer buckets are planned and wait for you. It stops when every policy is past the grace window, or when you pause it.'
            : 'Turn this on once and this campaign keeps calling on its own until every policy expires. You still approve the calmer buckets.'}
          {campaign.autopilot_note ? (
            <>
              {' '}
              <span className="cell-dim">Last: {campaign.autopilot_note}</span>
            </>
          ) : null}
        </p>
        <button
          className={`btn ${on ? 'btn-ghost' : 'btn-primary'}`}
          onClick={toggle}
          disabled={busy || !campaign.enabled}
        >
          {busy ? <Loader2 /> : <PlayCircle />} {on ? 'Stop autopilot' : 'Start autopilot'}
        </button>
      </div>
    </Card>
  );
}

/** The hero card. One warm CTA — plan today, or open the planned run. */
function Hero({
  run,
  loading,
  date,
  onPlan,
}: {
  run: Run | null;
  loading: boolean;
  date: string;
  onPlan: () => void;
}) {
  const day = new Date(date).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });

  if (loading && !run) {
    return (
      <section className="hero"><div className="hero-body"><Loading /></div></section>
    );
  }

  if (!run) {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">Today · {day}</span>
          <h2 className="hero-h">No plan for today yet.</h2>
          <p className="hero-sub">Planning writes a run — it does not dial. You review before anything goes out.</p>
        </div>
        <button className="btn btn-primary btn-hero" onClick={onPlan}>
          <PlayCircle /> Plan {dateOf(date)}
        </button>
      </section>
    );
  }

  const leads = n(run.counts.planned);
  const slots = n(run.counts.slots);

  return (
    <section className="hero">
      <div className="hero-body">
        <span className="eyebrow">
          Today · {day} · planned {timeOf(run.created_at)} · config v{run.config_version}
        </span>
        <h2 className="hero-h">
          {leads} <span className="hero-h-dim">leads to dial</span> · {slots}{' '}
          <span className="hero-h-dim">scheduled slots</span>
        </h2>
        <p className="hero-sub">
          Run #{run.id}{' '}
          <span className={`badge ${RUN_STATUS_TONE[run.status] ?? ''}`}>{run.status}</span>{' '}
          {run.counts.posted > 0 && <>· {n(run.counts.posted)} posted </>}
          {run.counts.failed > 0 && <>· <span style={{ color: 'var(--bad)' }}>{n(run.counts.failed)} failed</span></>}
        </p>
      </div>
      <button
        className="btn btn-primary btn-hero"
        onClick={() => navigate(`plan/${run.id}`)}
      >
        Review plan <ArrowRight />
      </button>
    </section>
  );
}

/** One horizontal bar coloured per bucket — the whole story in one glance. */
function BucketSummary({ buckets }: { buckets: BucketRow[] }) {
  const rows = buckets.filter((b) => b.total > 0);
  const total = rows.reduce((a, c) => a + c.total, 0);
  if (total === 0) {
    return (
      <div className="card"><div className="card-body">
        <Empty title="No leads in the window today" note="Nothing to dial. Check the RED dates on the warehouse — or the campaign might be new." />
      </div></div>
    );
  }
  return (
    <div className="bucket-summary">
      <div className="bucket-bar" role="img" aria-label="Lead distribution by bucket">
        {rows.map((r) => (
          <span
            key={r.bucket}
            className="bucket-bar-seg"
            style={{ flex: r.total, background: bucketColor(r.bucket) }}
            title={`${friendlyBucket(r.bucket)} · ${n(r.total)} leads`}
          />
        ))}
      </div>
      <ul className="bucket-legend">
        {rows.map((r) => (
          <li key={r.bucket}>
            <span className="chip-dot" style={{ background: bucketColor(r.bucket) }} />
            <b>{friendlyBucket(r.bucket)}</b>
            <span className="cell-dim">{bucketRange(r.bucket)}</span>
            <span className="mono">{n(r.total)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Plain-English skip list. Sentence per reason, not a data table. */
function SkipsPlain({ skips, config }: { skips: Record<string, number>; config: Config | null }) {
  const entries = Object.entries(skips).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    return (
      <p style={{ margin: 0, color: 'var(--text-dim)' }}>
        No leads have been skipped today — these are all first-dial candidates.
      </p>
    );
  }
  return (
    <ul className="skip-list">
      {entries.map(([code, v]) => {
        const m = skipMeta(code);
        return (
          <li key={code}>
            <b className="mono">{n(v)}</b>{' '}
            {code === 'BUCKET_DISPOSITION_OFF' ? (
              <BucketOffWhy config={config} />
            ) : (
              <>{m.label.toLowerCase()} — <span className="cell-dim">{m.why}</span></>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Names the operator's own choice rather than the code — unlike MANUAL_ONLY,
 *  this is a per-bucket setting, so it links straight to that row on the
 *  config screen. The raw bucket key stays as the link text so the operator
 *  can match it against the config matrix headings. */
export function BucketOffWhy({ config }: { config: Config | null }) {
  const narrowed = config ? narrowedBuckets(config) : [];
  if (!narrowed.length) {
    return (
      <>
        A bucket is configured to skip some dispositions.{' '}
        <button className="link-inline" onClick={() => navigate('config')}>
          Open the per-bucket matrix
        </button>
        .
      </>
    );
  }
  return (
    <>
      Your own per-bucket setting, not a property of the disposition — it will never clear itself.{' '}
      {narrowed.map((x, i) => (
        <span key={x.bucket}>
          {i > 0 && '; '}
          <button
            className="link-inline"
            onClick={() => navigate(`config/${x.bucket}`)}
            title={`Jump to the ${friendlyBucket(x.bucket)} row on the config screen`}
          >
            {x.bucket}
          </button>{' '}
          is configured not to chase{' '}
          {x.dropped.slice(0, 3).map((s) => dispLabel(s).toLowerCase()).join(', ')}
          {x.dropped.length > 3 ? ` and ${x.dropped.length - 3} more` : ''}
        </span>
      ))}
      .
    </>
  );
}

/** Native <details>. No third state, no libraries. */
function Details({
  summary,
  eyebrow,
  children,
}: {
  summary: string;
  eyebrow?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details className="disclosure" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>{summary}</span>
        {eyebrow && <span className="eyebrow" style={{ marginLeft: 'auto' }}>{eyebrow}</span>}
      </summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}

function RecentRuns({ runs, loading }: { runs: Run[]; loading: boolean }) {
  if (loading) return <Loading />;
  if (!runs.length) return <Empty title="No runs yet" note="Plan a day above to create the first run." />;
  return (
    <div className="table-wrap">
      <table className="t">
        <thead>
          <tr>
            <th className="n">Run</th>
            <th>Date</th>
            <th>Kind</th>
            <th>Status</th>
            <th className="n">Slots</th>
            <th className="n">Posted</th>
            <th className="n">Failed</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr
              key={r.id}
              style={{ cursor: 'pointer' }}
              onClick={() => navigate(`plan/${r.id}`)}
              tabIndex={0}
              onKeyDown={(e) => e.key === 'Enter' && navigate(`plan/${r.id}`)}
            >
              <td className="n" style={{ fontWeight: 600 }}>#{r.id}</td>
              <td className="mono">{r.run_date}</td>
              <td><span className="badge">{r.kind}</span></td>
              <td><span className={`badge ${RUN_STATUS_TONE[r.status] ?? ''}`}>{r.status}</span></td>
              <td className="n">{n(r.counts.slots)}</td>
              <td className="n" style={{ color: r.counts.posted ? 'var(--ok)' : undefined }}>{n(r.counts.posted)}</td>
              <td className="n" style={{ color: r.counts.failed ? 'var(--bad)' : undefined }}>
                {r.counts.failed ? n(r.counts.failed) : '·'}
              </td>
              <td><ChevronRight size={14} color="var(--faint)" /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Loading() {
  return (
    <div className="empty">
      <Loader2 className="spin" />
      <h3>Reading the warehouse</h3>
    </div>
  );
}

function sum(o: Record<string, number>) {
  return Object.values(o).reduce((a, c) => a + c, 0);
}
