import { useEffect, useMemo, useState } from 'react';
import { ArrowLeft, CheckCircle2, Loader2, PauseCircle, PlayCircle, RefreshCw, Trash2 } from 'lucide-react';
import { api } from '../lib/api';
import {
  bucketColor,
  BUCKET_ORDER,
  dispLabel,
  dteLabel,
  isIntensive,
  ITEM_STATUS_TONE,
  n,
  RUN_STATUS_TONE,
  timeOf,
} from '../lib/domain';
import { navigate, useAsync, useStore } from '../lib/store';
import { ApproveDialog } from '../components/ApproveDialog';
import { PlanDialog } from '../components/PlanDialog';
import { DialTimeline } from '../components/DialTimeline';
import { BucketTag, Card, Copyable, Empty, Pager } from '../components/ui';

const PAGE_SIZE = 50;

export function PlanReview({ runId }: { runId: number | null }) {
  const campaignId = useStore((s) => s.campaignId)!;
  const date = useStore((s) => s.date);
  const toast = useStore((s) => s.toast);

  const runs = useAsync(() => api.runs(campaignId, 20), [campaignId]);
  const resolved = runId ?? runs.data?.find((r) => r.run_date === date && r.kind === 'auto')?.id ?? null;

  const run = useAsync(() => (resolved ? api.run(resolved) : Promise.resolve(null)), [resolved]);
  const config = useAsync(() => api.config(campaignId), [campaignId]);
  const all = useAsync(
    () => (resolved ? api.allItems(resolved) : Promise.resolve(null)),
    [resolved, run.data?.status],
  );

  const [bucket, setBucket] = useState('');
  const [disposition, setDisposition] = useState('');
  const [status, setStatus] = useState('');
  const [page, setPage] = useState(1);
  const [approving, setApproving] = useState(false);
  const [planning, setPlanning] = useState(false);

  useEffect(() => setPage(1), [bucket, disposition, status, resolved]);

  const items = useAsync(
    () =>
      resolved
        ? api.items(resolved, { bucket, disposition, status, page, page_size: PAGE_SIZE })
        : Promise.resolve(null),
    [resolved, bucket, disposition, status, page, run.data?.status],
  );

  const dispositions = useMemo(() => {
    const s = new Set<string>();
    all.data?.items.forEach((i) => s.add(i.disposition));
    return [...s].sort();
  }, [all.data]);

  const urgentSlots = useMemo(
    () =>
      all.data?.items.filter((i) => isIntensive(i.bucket)).length ?? 0,
    [all.data],
  );

  const dialWindow = config.data?.dial_window;

  /** Earliest editable moment on `day`: window open, or the clock if it is today. */
  const slotMin = (day: string) => {
    const open = `${day}T${dialWindow?.start ?? '09:30'}`;
    if (day !== new Date().toLocaleDateString('en-CA')) return open;
    const now = `${day}T${new Date().toTimeString().slice(0, 5)}`;
    return now > open ? now : open;
  };

  const plan = () => setPlanning(true);

  const deletePlan = async () => {
    if (!resolved) return;
    if (!confirm(`Delete plan run #${resolved}? Its ${n(run.data?.counts.slots ?? 0)} slots are discarded. This cannot be undone.`)) return;
    try {
      await api.deleteRun(resolved);
      toast('ok', `Deleted run ${resolved}.`);
      runs.reload();
      run.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    }
  };

  const pauseRun = async () => {
    if (!resolved) return;
    if (!confirm(
      `Pause run #${resolved}? Every call still ahead of the clock is cancelled in Formi and comes ` +
      `back as an editable slot. Calls already dialled are left alone.`)) return;
    try {
      const out = await api.pauseRun(resolved);
      toast('ok', `Paused. ${n(out.cancelled)} queued call(s) cancelled` +
        (out.cancel_failed ? `, ${n(out.cancel_failed)} could not be` : '') + '.');
      run.reload(); items.reload(); all.reload(); runs.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    }
  };

  const resumeRun = async () => {
    if (!resolved) return;
    try {
      const out = await api.resumeRun(resolved);
      toast('ok', out.dry_run
        ? `Resumed (dry run) — ${n(out.counts.slots)} slot(s) simulated, nothing dialled.`
        : `Resumed. ${n(out.counts.posted)} call(s) on the clock.`);
      run.reload(); items.reload(); all.reload(); runs.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    }
  };

  const rescheduleItem = async (itemId: number, newTime: string) => {
    if (!resolved || !newTime) return;
    try {
      await api.updateItemTime(resolved, itemId, newTime);
      toast('ok', `Slot rescheduled to ${newTime.replace('T', ' ')}.`);
      items.reload();
      all.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    }
  };

  if (runs.loading || run.loading) {
    return (
      <div className="page">
        <div className="empty">
          <Loader2 className="spin" />
          <h3>Loading the plan</h3>
        </div>
      </div>
    );
  }

  const r = run.data;

  if (!r) {
    return (
      <div className="page">
        <div className="page-head">
          <div>
            <span className="eyebrow">Plan review</span>
            <h1>No run for {date}</h1>
            <p>Planning writes a run and its call slots. It never dials.</p>
          </div>
        </div>
        <Card>
          <div className="empty">
            <PlayCircle />
            <h3>Nothing planned yet</h3>
            <p>Plan the day to see which leads would be called, when, and in what order.</p>
            <button className="btn btn-primary" onClick={plan}>
              <PlayCircle /> Plan {date}
            </button>
          </div>
        </Card>
        {planning && (
          <PlanDialog
            campaignId={campaignId}
            date={date}
            replacing={null}
            onClose={() => setPlanning(false)}
            onDone={() => {
              runs.reload();
              run.reload();
            }}
          />
        )}
      </div>
    );
  }

  const approvable = r.status === 'planned';
  // A paused run is a plan again: its slots are editable and it resumes rather
  // than being approved a second time.
  const editable = approvable || r.status === 'paused';

  return (
    <div className="page grid" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            {runId ? (
              <button className="btn btn-sm btn-ghost" onClick={() => navigate('runs')}>
                <ArrowLeft /> All runs
              </button>
            ) : (
              'Plan review'
            )}
          </span>
          <h1>
            Run #{r.id} · {r.run_date}
          </h1>
          <p>
            Check the spread and the mix, then approve. Approve is the only action in this console
            that places a call.
          </p>
        </div>
        <div className="row" style={{ marginLeft: 'auto' }}>
          <span className={`badge ${RUN_STATUS_TONE[r.status] ?? ''}`}>{r.status}</span>
          <span className="badge">{r.kind}</span>
          <button className="btn btn-ghost" onClick={() => { run.reload(); items.reload(); all.reload(); }}>
            <RefreshCw /> Refresh
          </button>
          {approvable && (
            <>
              <button className="btn btn-ghost" onClick={plan} title="Discard slots and re-run the engine for this date">
                <PlayCircle /> Re-plan
              </button>
              <button className="btn btn-ghost" onClick={deletePlan} style={{ color: 'var(--danger, #b8433a)' }}>
                <Trash2 /> Delete plan
              </button>
            </>
          )}
          {r.status === 'committed' && (
            <button className="btn btn-ghost" onClick={pauseRun}
                    title="Cancel the calls still ahead of the clock so the rest of the day can be edited">
              <PauseCircle /> Pause run
            </button>
          )}
          {r.status === 'paused' ? (
            <button className="btn btn-primary" onClick={resumeRun}>
              <PlayCircle /> Resume run
            </button>
          ) : (
            <button className="btn btn-primary" disabled={!approvable} onClick={() => setApproving(true)}>
              <CheckCircle2 /> Approve run
            </button>
          )}
        </div>
      </div>

      <div className="strip">
        <div className="strip-cell">
          <span className="eyebrow">Evaluated</span>
          <span className="strip-val is-dim">{n(r.counts.evaluated)}</span>
          <span className="strip-sub">leads considered</span>
        </div>
        <div className="strip-cell">
          <span className="eyebrow">Leads planned</span>
          <span className="strip-val">{n(r.counts.planned)}</span>
          <span className="strip-sub">passed every rule</span>
        </div>
        <div className="strip-cell">
          <span className="eyebrow">Call slots</span>
          <span className="strip-val">{n(r.counts.slots)}</span>
          <span className="strip-sub">F5/E0/F6/M0 get two</span>
        </div>
        <div className="strip-cell">
          <span className="eyebrow">Urgent slots</span>
          <span className="strip-val is-urgent">{n(urgentSlots)}</span>
          <span className="strip-sub">dialled first</span>
        </div>
        <div className="strip-cell">
          <span className="eyebrow">Posted</span>
          <span className={`strip-val ${r.counts.posted ? 'is-ok' : 'is-dim'}`}>{n(r.counts.posted)}</span>
          <span className="strip-sub">{r.counts.failed ? `${n(r.counts.failed)} failed` : 'none failed'}</span>
        </div>
        <div className="strip-cell grow">
          <span className="eyebrow">Config</span>
          <span className="strip-val is-dim">v{r.config_version}</span>
          <span className="strip-sub">
            window {config.data?.dial_window.start ?? '—'}–{config.data?.dial_window.end ?? '—'}
          </span>
        </div>
      </div>

      <Card
        title="Spread across the dial window"
        eyebrow={all.data ? `${n(all.data.total)} slots` : ''}
      >
        {all.data && config.data ? (
          <DialTimeline
            items={all.data.items}
            window={config.data.dial_window}
            maxPerMinute={config.data.max_per_minute}
          />
        ) : (
          <div className="empty">
            <Loader2 className="spin" />
            <h3>Building the timeline</h3>
          </div>
        )}
      </Card>

      <Card title="Planned calls" flush>
        <div className="filters">
          <span className="eyebrow">Bucket</span>
          <button className={`chip${bucket === '' ? ' is-on' : ''}`} onClick={() => setBucket('')}>
            all
          </button>
          {BUCKET_ORDER.map((b) => (
            <button
              key={b}
              className={`chip${bucket === b ? ' is-on' : ''}`}
              onClick={() => setBucket(bucket === b ? '' : b)}
            >
              <span className="chip-dot" style={{ background: bucketColor(b) }} />
              {b}
            </button>
          ))}

          <div className="topbar-sep" />

          <span className="eyebrow">Disposition</span>
          <select className="select" value={disposition} onChange={(e) => setDisposition(e.target.value)}>
            <option value="">all</option>
            {dispositions.map((d) => (
              <option key={d} value={d}>
                {dispLabel(d)}
              </option>
            ))}
          </select>

          <span className="eyebrow">Status</span>
          <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">all</option>
            {['planned', 'simulated', 'posted', 'failed', 'skipped'].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>

          <span style={{ flex: 1 }} />
          {(bucket || disposition || status) && (
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => { setBucket(''); setDisposition(''); setStatus(''); }}
            >
              Clear filters
            </button>
          )}
        </div>

        {items.data && items.data.items.length > 0 ? (
          <>
            <div className="table-wrap">
              <table className="t">
                <thead>
                  <tr>
                    <th className="n">Time</th>
                    <th className="n">Slot</th>
                    <th>Bucket</th>
                    <th className="n">DTE</th>
                    <th>Lead</th>
                    <th>Policy</th>
                    <th className="n">Number</th>
                    <th>Disposition</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {items.data.items.map((it) => (
                    <tr key={it.id}>
                      <td className="n" style={{ fontWeight: 600 }}>
                        {editable && it.status === 'planned' ? (
                          <input
                            // Keyed on the STORED value, not just the row id: after a
                            // rejected or normalised edit the reload returns the old
                            // time, and without a key change React keeps the DOM node
                            // showing what the user typed, which reads as "it saved".
                            key={`${it.id}:${it.scheduled_time}`}
                            type="datetime-local"
                            defaultValue={it.scheduled_time.slice(0, 16)}
                            // Same floor the server enforces: never before the dial
                            // window opens, and never in the past on today's run.
                            min={slotMin(it.scheduled_time.slice(0, 10))}
                            max={`${it.scheduled_time.slice(0, 10)}T${dialWindow?.end ?? '19:00'}`}
                            onBlur={(e) => {
                              const v = e.target.value;
                              if (v && v !== it.scheduled_time.slice(0, 16)) rescheduleItem(it.id, v);
                            }}
                            style={{ font: 'inherit', border: '1px solid var(--border, #ddd)',
                                     background: 'transparent', padding: '2px 4px', borderRadius: 4, width: 160 }}
                            title="Edit time; press Tab or click away to save"
                          />
                        ) : (
                          timeOf(it.scheduled_time)
                        )}
                      </td>
                      <td className="n cell-dim">{it.slot_no}</td>
                      <td>
                        <BucketTag bucket={it.bucket} />
                      </td>
                      <td
                        className="n"
                        style={{ color: it.dte <= 0 ? 'var(--b-F6)' : undefined }}
                        title={dteLabel(it.dte)}
                      >
                        {it.dte > 0 ? `+${it.dte}` : it.dte}
                      </td>
                      <td>
                        <span className="trunc">{it.lead_name ?? '—'}</span>
                      </td>
                      <td className="mono">
                        {it.policy_no ? <Copyable text={it.policy_no} /> : <span className="cell-dim">—</span>}
                      </td>
                      <td className="n mono">
                        {it.phone ? <Copyable text={it.phone} /> : <span className="cell-dim">—</span>}
                      </td>
                      <td>
                        <span className="mono" style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                          {it.disposition || '(blank)'}
                        </span>
                      </td>
                      <td>
                        <span className={`badge ${ITEM_STATUS_TONE[it.status] ?? ''}`}>{it.status}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pager
              page={items.data.page}
              pageSize={items.data.page_size}
              total={items.data.total}
              onPage={setPage}
            />
          </>
        ) : items.loading ? (
          <div className="empty">
            <Loader2 className="spin" />
            <h3>Loading calls</h3>
          </div>
        ) : (
          <Empty title="No calls match these filters" note="Clear a filter to widen the list." />
        )}
      </Card>

      {planning && (
        <PlanDialog
          campaignId={campaignId}
          date={date}
          replacing={approvable ? resolved : null}
          onClose={() => setPlanning(false)}
          onDone={() => {
            runs.reload();
            run.reload();
            items.reload();
            all.reload();
          }}
        />
      )}

      {approving && (
        <ApproveDialog
          run={r}
          urgentSlots={urgentSlots}
          onClose={() => setApproving(false)}
          onDone={() => {
            run.reload();
            items.reload();
            all.reload();
            runs.reload();
          }}
        />
      )}
    </div>
  );
}
