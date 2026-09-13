import { AlertTriangle, Check, Loader2 } from 'lucide-react';
import { n } from '../lib/domain';

export interface ProgressRow {
  campaign_id: number;
  name: string;
  state: 'done' | 'failed' | 'running';
}

/** What is happening RIGHT NOW, while the day is being dialled.
 *
 *  This is not the result bar. The result bar says what went out once the whole
 *  day is finished; this one moves campaign by campaign while it is still going,
 *  because a single approve used to post thousands of calls in one blocking
 *  request with nothing on screen but a spinner. */
export function DayProgress({
  done,
  total,
  current,
  rows,
}: {
  done: number;
  total: number;
  current: string | null;
  rows: ProgressRow[];
}) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  return (
    <>
      <div className="dialbar">
        <div className="dialbar-seg is-scheduled" style={{ width: `${pct}%` }} />
      </div>
      <div className="dialbar-keys">
        <span className="dialbar-key">
          <b>
            {n(done)} of {n(total)} campaigns
          </b>
        </span>
        {current && <span className="dialbar-key trunc">Dialling {current}…</span>}
      </div>
      <div className="grid" style={{ gap: 0, marginTop: 4 }}>
        {rows.map((r) => (
          <div className="dialrow" key={r.campaign_id}>
            {r.state === 'running' ? (
              <Loader2 className="spin" size={14} style={{ flex: '0 0 auto' }} />
            ) : r.state === 'failed' ? (
              <AlertTriangle size={14} style={{ color: 'var(--bad)', flex: '0 0 auto' }} />
            ) : (
              <Check size={14} style={{ color: 'var(--ok)', flex: '0 0 auto' }} />
            )}
            <b className="trunc">{r.name}</b>
          </div>
        ))}
      </div>
    </>
  );
}
