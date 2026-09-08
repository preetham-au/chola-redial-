/** Did the call actually happen?
 *
 *  Two separate facts, never merged, because merging them is why this console
 *  could not answer the question:
 *
 *    Sent      what Formi's API answered when we posted the schedule. A 2xx
 *              says the request was ACCEPTED — nothing more.
 *    Happened  what the warehouse says later: an interaction with a call stage
 *              is a call that was dialled. This is the only proof.
 *
 *  Verification is read-only and asks the warehouse once per campaign per day,
 *  so leaving this screen open costs nothing and it can never place a call —
 *  whatever DRY_RUN says.
 */
import { useState } from 'react';
import { CheckCircle2, Clock, FlaskConical, HelpCircle, Loader2, RefreshCw, XCircle } from 'lucide-react';
import { api } from '../lib/api';
import { n, timeOf } from '../lib/domain';
import { useAsync, useStore } from '../lib/store';
import { Card, Empty, Pager } from '../components/ui';
import type { DialLogRow } from '../lib/types';

const PAGE = 100;

/** What each verify state means, said once, here. */
const VERIFIED: Record<string, { label: string; note: string; tone: string; icon: typeof Clock }> = {
  dialled: {
    label: 'Dialled',
    note: 'The warehouse has an interaction with a call stage. The call happened.',
    tone: 'var(--ok, #2e7d32)',
    icon: CheckCircle2,
  },
  queued: {
    label: 'On the clock',
    note: 'Formi holds the slot but has not dialled it yet.',
    tone: 'var(--accent)',
    icon: Clock,
  },
  pending: {
    label: 'Not checked yet',
    note: 'Sent, but the warehouse has not been read back for it yet.',
    tone: 'var(--muted)',
    icon: HelpCircle,
  },
  missing: {
    label: 'Not in the warehouse',
    note: 'We sent it and nothing came back. The warehouse lags a few minutes — check again before treating this as a lost call.',
    tone: 'var(--warn)',
    icon: XCircle,
  },
  simulated: {
    label: 'Dry run',
    note: 'Nothing was sent, so there is nothing to verify.',
    tone: 'var(--muted)',
    icon: FlaskConical,
  },
};

const meta = (v: string) => VERIFIED[v] ?? { label: v, note: '', tone: 'var(--muted)', icon: HelpCircle };

export function CallLog() {
  const date = useStore((s) => s.date);
  const setDate = useStore((s) => s.setDate);
  const toast = useStore((s) => s.toast);
  const [verified, setVerified] = useState('');
  const [page, setPage] = useState(1);
  const [checking, setChecking] = useState(false);

  const summary = useAsync(() => api.dialLogSummary(date), [date]);
  const log = useAsync(
    () => api.dialLog({ date, verified: verified || undefined, limit: PAGE, offset: (page - 1) * PAGE }),
    [date, verified, page],
  );

  const verify = async () => {
    setChecking(true);
    try {
      await api.verifyDialLog(date, true);
      toast('ok', 'Checked against the warehouse.');
      summary.reload();
      log.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setChecking(false);
    }
  };

  // Rolled up from the summary rather than fetched twice: the same numbers the
  // day screen shows, so the two screens cannot disagree.
  const states: Record<string, number> = {};
  let sent = 0;
  let talk = 0;
  (summary.data?.campaigns ?? []).forEach((c) => {
    sent += c.sent;
    talk += c.talk_time_sec;
    Object.entries(c.verified).forEach(([k, v]) => {
      states[k] = (states[k] ?? 0) + v;
    });
  });

  const rows = log.data?.rows ?? [];

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">Every call this console sent, and what became of it</span>
          <h1>Call log</h1>
        </div>
        <div className="row" style={{ marginLeft: 'auto', gap: 8 }}>
          <input
            className="input"
            type="date"
            value={date}
            aria-label="Day"
            onChange={(e) => { setDate(e.target.value); setPage(1); }}
          />
          <button className="btn btn-ghost" disabled={checking} onClick={verify}>
            {checking ? <Loader2 className="spin" /> : <RefreshCw />} Check the warehouse
          </button>
        </div>
      </div>

      <div className="strip">
        <div className="strip-cell">
          <span className="strip-val">{n(sent)}</span>
          <span className="strip-sub">sent</span>
        </div>
        {['dialled', 'queued', 'pending', 'missing', 'simulated'].map((k) =>
          states[k] ? (
            <div className="strip-cell" key={k}>
              <span className="strip-val" style={{ color: meta(k).tone }}>{n(states[k])}</span>
              <span className="strip-sub">{meta(k).label.toLowerCase()}</span>
            </div>
          ) : null,
        )}
        <div className="strip-cell">
          <span className="strip-val">{Math.round(talk / 60)}m</span>
          <span className="strip-sub">talk time</span>
        </div>
      </div>

      <Card
        title="Calls"
        eyebrow={`${n(log.data?.total ?? 0)} on ${date}`}
        flush
        actions={
          <select
            className="select"
            value={verified}
            aria-label="Filter by outcome"
            onChange={(e) => { setVerified(e.target.value); setPage(1); }}
          >
            <option value="">All outcomes</option>
            {Object.keys(VERIFIED).map((k) => (
              <option key={k} value={k}>{VERIFIED[k].label}</option>
            ))}
          </select>
        }
      >
        {log.loading && rows.length === 0 ? (
          <div style={{ padding: 18 }}><Loader2 className="spin" /></div>
        ) : rows.length === 0 ? (
          <Empty
            title="No call was sent on this day"
            note="Approving a day writes a row here for every call, dry run included."
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="t">
                <thead>
                  <tr>
                    <th>Slot</th>
                    <th>Lead</th>
                    <th>Policy</th>
                    <th>Bucket</th>
                    <th>Sent</th>
                    <th>Happened</th>
                    <th className="n">Talk</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => <Row key={r.id} r={r} />)}
                </tbody>
              </table>
            </div>
            <Pager
              page={page}
              pageSize={PAGE}
              total={log.data?.total ?? 0}
              onPage={setPage}
            />
          </>
        )}
      </Card>

      <Card title="What these words mean">
        <div className="grid" style={{ gap: 6 }}>
          {Object.entries(VERIFIED).map(([k, v]) => (
            <div key={k} className="row" style={{ gap: 8, alignItems: 'baseline' }}>
              <b style={{ color: v.tone, minWidth: 150 }}>{v.label}</b>
              <span className="cell-dim">{v.note}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

export function Row({ r }: { r: DialLogRow }) {
  const m = meta(r.verified);
  const Icon = m.icon;
  return (
    <tr>
      <td className="mono">{r.scheduled_time ? timeOf(r.scheduled_time) : '—'}</td>
      <td>
        {r.lead_name || <span className="cell-dim">unnamed</span>}
        {r.phone && <span className="cell-dim mono"> · {r.phone}</span>}
      </td>
      <td className="mono cell-dim">{r.policy_no ?? '—'}</td>
      <td className="cell-dim">{r.bucket ?? '—'}</td>
      <td>
        <span className={`badge ${r.outcome === 'failed' ? 'badge-bad' : r.outcome === 'posted' ? 'badge-ok' : ''}`}>
          {r.outcome}
        </span>
        {r.http_status ? <span className="cell-dim mono"> {r.http_status}</span> : null}
        {/* The server's own words, not a paraphrase: a failure nobody can read
            is a failure nobody fixes. */}
        {r.outcome === 'failed' && r.response ? (
          <div className="cell-dim trunc" title={r.response}>{r.response}</div>
        ) : null}
      </td>
      <td>
        <span className="row" style={{ gap: 5, color: m.tone }}>
          <Icon size={13} /> {m.label}
        </span>
        {r.call_disposition ? <div className="cell-dim">{r.call_disposition}</div> : null}
      </td>
      <td className="n">{r.duration_sec ? `${r.duration_sec}s` : '—'}</td>
    </tr>
  );
}
