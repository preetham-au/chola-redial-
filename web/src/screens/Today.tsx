/** The day. One screen, one decision.
 *
 *  Nothing in this console dials on its own. A pass prepares a plan each morning
 *  and afternoon and leaves it waiting; this screen is where an operator reads
 *  what is ready and approves it. If nobody approves, no call goes out.
 *
 *  The order is the client's, not the bucket order: the three days just PAST
 *  expiry first (their "1 to 3"), then the week running up to it (their "-7 to
 *  0"). That is what decides who survives when the day is approved with few
 *  hours left, so it is the first thing on the page — above the buckets, which
 *  follow it.
 */
import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CircleSlash,
  ClipboardList,
  FlaskConical,
  Info,
  Loader2,
  PhoneCall,
  Radio,
  RefreshCw,
} from 'lucide-react';
import { api } from '../lib/api';
import { bandRange, bucketColor, friendlyBucket, n } from '../lib/domain';
import { navigate, useAsync, useStore } from '../lib/store';
import { Card, Empty, Fact, Modal, TypeToConfirm } from '../components/ui';
import type { DayBucket, DayView } from '../lib/types';

const WAVES = [
  { kind: 'auto', label: 'Morning' },
  { kind: 'auto_pm', label: 'Afternoon' },
];

/** What goes on the wire. The backend reads an empty list as "every bucket", so
 *  a partial tick MUST be sent verbatim — sending [] after unticking one bucket
 *  would dial the ones the operator just excluded. */
export const wireBuckets = (chosen: string[], all: string[]) =>
  chosen.length === all.length ? [] : chosen;

export function Today() {
  const date = useStore((s) => s.date);
  const setDate = useStore((s) => s.setDate);
  const toast = useStore((s) => s.toast);
  const [kind, setKind] = useState('auto');
  const day = useAsync(() => api.day(date, kind), [date, kind]);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [approving, setApproving] = useState(false);
  const [busy, setBusy] = useState('');

  const d = day.data;

  // Ticked buckets default to every bucket in the plan and follow it when the
  // plan changes. Null means "not touched yet" so a re-plan cannot silently
  // resurrect a bucket the operator just unticked.
  const all = useMemo(() => (d?.buckets ?? []).map((b) => b.bucket), [d]);
  useEffect(() => {
    setPicked((p) => (p === null ? null : p.filter((b) => all.includes(b))));
  }, [all]);
  const chosen = picked ?? all;

  const prepare = async () => {
    setBusy('prepare');
    try {
      const res = await api.prepareDay(date, kind);
      toast('ok', `Plan built: ${n(res.ready)} leads ready across ${res.prepared} campaigns. Nothing has been dialled.`);
      day.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            Server clock {d?.now ?? '—'} IST · window {d?.window.start ?? '09:00'}–
            {d?.window.end ?? '20:00'}
          </span>
          <h1>The day</h1>
        </div>
        <div className="row" style={{ marginLeft: 'auto', gap: 8 }}>
          <input
            className="input"
            type="date"
            value={date}
            aria-label="Day"
            onChange={(e) => setDate(e.target.value)}
          />
          <div className="seg" role="group" aria-label="Wave">
            {WAVES.map((w) => (
              <button
                key={w.kind}
                className={`seg-btn${kind === w.kind ? ' is-active' : ''}`}
                onClick={() => setKind(w.kind)}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button className="btn btn-ghost" onClick={() => day.reload()} aria-label="Refresh">
            <RefreshCw /> Refresh
          </button>
        </div>
      </div>

      {day.error && (
        <div className="warnbox">
          <AlertTriangle />
          <span>{day.error}</span>
        </div>
      )}

      <Headline
        day={d}
        busy={busy}
        onPrepare={prepare}
        onApprove={() => setApproving(true)}
      />

      {d && d.status !== 'no_campaigns' && (
        <>
          <RedBands day={d} />
          <Buckets day={d} chosen={chosen} onChange={setPicked} />
          <Campaigns day={d} />
        </>
      )}

      {d && d.stopped.length > 0 && <Stopped day={d} />}

      {approving && d && (
        <ApproveDay
          day={d}
          buckets={wireBuckets(chosen, all)}
          shown={chosen}
          onClose={() => setApproving(false)}
          onDone={() => day.reload()}
        />
      )}
    </div>
  );
}

/** The one sentence that answers "what is happening today?", and the one button
 *  that changes it. Everything below this is detail. */
export function Headline({
  day,
  busy,
  onPrepare,
  onApprove,
}: {
  day: DayView | null;
  busy: string;
  onPrepare: () => void;
  onApprove: () => void;
}) {
  if (!day) {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">Loading</span>
          <h2 className="hero-h">Reading today’s plan…</h2>
        </div>
      </section>
    );
  }

  const live = !day.dry_run;
  const short = day.capacity_before_close < day.totals.ready;

  if (day.status === 'no_campaigns') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.wave}</span>
          <h2 className="hero-h">No campaign is in the daily plan.</h2>
          <p className="hero-sub">
            Put a campaign in from its dashboard. Doing so never places a call — it decides whose
            leads appear here tomorrow morning.
          </p>
        </div>
        <button className="btn btn-primary btn-hero" onClick={() => navigate('dashboard')}>
          Choose campaigns
        </button>
      </section>
    );
  }

  if (day.status === 'not_prepared') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.wave} · {day.totals.campaigns} campaigns</span>
          <h2 className="hero-h">No plan built yet for this wave.</h2>
          <p className="hero-sub">
            Building a plan writes it down and dials nothing. You approve it afterwards.
          </p>
        </div>
        <button className="btn btn-primary btn-hero" disabled={busy !== ''} onClick={onPrepare}>
          {busy === 'prepare' ? <Loader2 className="spin" /> : <ClipboardList />} Build the plan
        </button>
      </section>
    );
  }

  if (day.status === 'approved') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.wave}</span>
          <h2 className="hero-h">
            {n(day.totals.posted)} <span className="hero-h-dim">calls on the clock</span>
            {day.totals.failed > 0 && (
              <> · <span style={{ color: 'var(--bad)' }}>{n(day.totals.failed)} failed</span></>
            )}
          </h2>
          <p className="hero-sub">
            This wave has been approved. {day.totals.dropped > 0 && (
              <>{n(day.totals.dropped)} did not fit before {day.window.end} and return in tomorrow’s
              plan. </>
            )}
            The call log says whether each one was actually dialled.
          </p>
        </div>
        <button className="btn btn-primary btn-hero" onClick={() => navigate('calllog')}>
          <PhoneCall /> Open the call log
        </button>
      </section>
    );
  }

  return (
    <section className="hero">
      <div className="hero-body">
        <span className="eyebrow">
          {day.date} · {day.wave} · {day.totals.campaigns} campaigns
        </span>
        <h2 className="hero-h">
          {n(day.totals.ready)} <span className="hero-h-dim">calls ready. Nothing is dialled yet.</span>
        </h2>
        <p className="hero-sub">
          {!day.window_open ? (
            <>
              The {day.window.start}–{day.window.end} window has closed for today. Approving now
              would place no call; this plan returns tomorrow morning.
            </>
          ) : short ? (
            <>
              Only about {n(day.capacity_before_close)} of them still fit before {day.window.end}.
              Approving takes them in RED order — the rest are not dialled today and come back in
              tomorrow’s plan.
            </>
          ) : (
            <>
              Approving puts them on Formi’s clock inside {day.window.start}–{day.window.end}, best
              RED band first.
            </>
          )}
        </p>
      </div>
      <button
        className={`btn btn-hero ${live ? 'btn-live' : 'btn-primary'}`}
        disabled={!day.window_open || day.totals.ready === 0}
        onClick={onApprove}
      >
        {live ? <Radio /> : <FlaskConical />} {live ? 'Approve and dial' : 'Approve (simulated)'}
      </button>
    </section>
  );
}

/** RED priority order — what actually decides who gets called first. */
function RedBands({ day }: { day: DayView }) {
  const total = day.red_bands.reduce((s, b) => s + b.ready, 0) || 1;
  return (
    <Card
      title="Who gets called first"
      eyebrow="RED order · applied before the bucket order"
    >
      <div className="table-wrap">
        <table className="t">
          <thead>
            <tr>
              <th style={{ width: 34 }}>#</th>
              <th>Band</th>
              <th>When it renews</th>
              <th className="n">Ready</th>
              <th style={{ width: '32%' }} />
            </tr>
          </thead>
          <tbody>
            {day.red_bands.map((b) => (
              <tr key={b.rank}>
                <td className="cell-dim">{b.rank + 1}</td>
                <td><b>{b.label}</b></td>
                <td className="mono cell-dim">{bandRange(b.dte_from, b.dte_to)}</td>
                <td className="n" style={{ fontWeight: 600 }}>{n(b.ready)}</td>
                <td>
                  <div className="bucket-bar">
                    <span
                      className="bucket-bar-seg"
                      style={{
                        width: `${(b.ready / total) * 100}%`,
                        background: b.dte_from === null ? 'var(--muted)' : 'var(--accent)',
                      }}
                    />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Which buckets to call today. Ticked = dialled; unticked is not lost, it comes
 *  back in tomorrow's plan. */
function Buckets({
  day,
  chosen,
  onChange,
}: {
  day: DayView;
  chosen: string[];
  onChange: (v: string[]) => void;
}) {
  const all = day.buckets.map((b) => b.bucket);
  const ready = day.buckets
    .filter((b) => chosen.includes(b.bucket))
    .reduce((s, b) => s + b.ready, 0);

  const toggle = (b: DayBucket) =>
    onChange(chosen.includes(b.bucket) ? chosen.filter((x) => x !== b.bucket) : [...chosen, b.bucket]);

  if (day.buckets.length === 0) {
    return (
      <Card title="Which buckets to call">
        <Empty title="No bucket has anything ready" note="Nothing in this wave’s plan to pick from." />
      </Card>
    );
  }

  return (
    <Card
      title="Which buckets to call"
      eyebrow={`${n(ready)} of ${n(day.totals.ready)} selected`}
      actions={
        <>
          <button className="btn btn-sm btn-ghost" onClick={() => onChange(all)}>All</button>
          <button className="btn btn-sm btn-ghost" onClick={() => onChange([])}>None</button>
        </>
      }
    >
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        {day.buckets.map((b) => {
          const on = chosen.includes(b.bucket);
          return (
            <button
              key={b.bucket}
              type="button"
              className={`tog${on ? ' is-on' : ''}`}
              aria-pressed={on}
              onClick={() => toggle(b)}
            >
              <span className="tog-box" />
              <span className="tog-label">
                <span className="chip-dot" style={{ background: bucketColor(b.bucket) }} />
                <b style={{ color: bucketColor(b.bucket) }}>{friendlyBucket(b.bucket)}</b>{' '}
                <span className="cell-dim">{n(b.ready)}</span>
              </span>
            </button>
          );
        })}
      </div>
      <p className="hero-sub" style={{ marginBottom: 0 }}>
        Buckets are listed in the order they will be dialled — best RED band first. Unticking one
        does not lose those leads; they return in tomorrow’s plan.
      </p>
    </Card>
  );
}

function Campaigns({ day }: { day: DayView }) {
  return (
    <Card title="Campaigns in today’s plan" eyebrow={`${day.totals.campaigns}`} flush>
      <div className="table-wrap">
        <table className="t">
          <thead>
            <tr>
              <th>Campaign</th>
              <th className="n">Ready</th>
              <th>Plan</th>
              <th className="n">On the clock</th>
              <th className="n">Failed</th>
            </tr>
          </thead>
          <tbody>
            {day.campaigns.map((c) => (
              <tr key={c.id}>
                <td>
                  <b>{c.name}</b> <span className="cell-dim">· agent {c.agent_id}</span>
                </td>
                <td className="n" style={{ fontWeight: 600 }}>{n(c.ready)}</td>
                <td>
                  <span className={`badge ${c.run_status === 'committed' ? 'badge-ok' : ''}`}>
                    {c.run_status === 'not_prepared' ? 'not built' : c.run_status}
                  </span>
                </td>
                <td className="n">{n(c.posted)}</td>
                <td className="n" style={c.failed ? { color: 'var(--bad)' } : undefined}>
                  {n(c.failed)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** "Why is nothing happening for X." Campaigns that were armed and are now held
 *  — usually because Formi paused them, which this console honours. */
function Stopped({ day }: { day: DayView }) {
  return (
    <Card title="Held back" eyebrow={`${day.stopped.length} not in today’s plan`}>
      <div className="grid" style={{ gap: 6 }}>
        {day.stopped.map((c) => (
          <div key={c.id} className="row" style={{ gap: 8 }}>
            <CircleSlash size={13} style={{ color: 'var(--warn)' }} />
            <b>{c.name}</b>
            <span className="cell-dim">{c.why}</span>
          </div>
        ))}
      </div>
      <p className="hero-sub" style={{ marginBottom: 0 }}>
        A campaign paused in Formi stops here too, and resuming it there does not restart calls —
        resume it in this console when you want it back in the plan.
      </p>
    </Card>
  );
}

/** Approving is the only thing in this console that reaches Formi. */
export function ApproveDay({
  day,
  buckets,
  shown,
  onClose,
  onDone,
}: {
  day: DayView;
  /** What goes on the wire: empty means every bucket. */
  buckets: string[];
  /** What the operator actually ticked, for the sentence they read. */
  shown: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useStore((s) => s.toast);
  const live = !day.dry_run;
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);

  const ready = day.buckets
    .filter((b) => shown.includes(b.bucket))
    .reduce((s, b) => s + b.ready, 0);
  const fits = Math.min(ready, day.capacity_before_close);
  const ok = !live || typed.trim().toUpperCase() === 'DIAL';

  const submit = async () => {
    setBusy(true);
    try {
      const res = await api.approveDay(day.date, day.kind, buckets);
      toast(
        res.failed > 0 ? 'bad' : 'ok',
        res.dry_run
          ? `Simulated ${n(res.posted)} calls across ${res.approved} campaigns. Nothing was sent to Formi.`
          : `${n(res.posted)} calls are on Formi's clock across ${res.approved} campaigns.` +
            (res.not_dialled ? ` ${n(res.not_dialled)} did not fit today.` : ''),
      );
      onDone();
      onClose();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={live ? 'Approve and dial the day' : 'Approve the day (simulated)'}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className={live ? 'btn btn-live' : 'btn btn-primary'}
            disabled={!ok || busy || shown.length === 0}
            onClick={submit}
          >
            {busy ? <Loader2 className="spin" /> : live ? <Radio /> : <FlaskConical />}
            {live ? `Dial up to ${n(fits)} calls` : `Simulate up to ${n(fits)} calls`}
          </button>
        </>
      }
    >
      {live ? (
        <div className="warnbox">
          <Radio />
          <span>
            <b>DRY_RUN is off.</b> This puts real calls on Formi’s clock for every campaign in the
            plan. There is no undo.
          </span>
        </div>
      ) : (
        <div className="infobox">
          <FlaskConical />
          <span>
            The server is in dry run. Every call is recorded in the call log as{' '}
            <span className="mono">simulated</span> and nothing reaches Formi.
          </span>
        </div>
      )}

      <div className="confirm-facts">
        <Fact k="Day" v={`${day.date} · ${day.wave}`} />
        <Fact k="Campaigns" v={day.totals.campaigns} />
        <Fact k="Buckets" v={buckets.length === 0 ? 'all of them' : shown.join(', ')} />
        <Fact k="Selected" v={n(ready)} />
        <Fact k="Fits before close" v={n(fits)} tone={fits < ready ? 'var(--warn)' : undefined} />
        <Fact k="Window" v={`${day.window.start}–${day.window.end} IST`} />
      </div>

      {shown.length === 0 && (
        <div className="warnbox">
          <AlertTriangle />
          <span>No bucket is ticked, so there is nothing to dial. Tick at least one.</span>
        </div>
      )}

      <div className="infobox">
        <Info />
        <span>
          Approving re-plans from {day.now} first, so only what genuinely fits before{' '}
          {day.window.end} is scheduled — best RED band first. Whatever does not fit is{' '}
          <b>not dialled today</b> and returns in tomorrow’s plan.
        </span>
      </div>

      {live && (
        <TypeToConfirm
          word="DIAL"
          value={typed}
          onChange={setTyped}
          hint="Type DIAL to confirm you mean to place these calls."
        />
      )}
    </Modal>
  );
}
