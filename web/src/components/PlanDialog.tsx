import { useState } from 'react';
import { Clock, PlayCircle } from 'lucide-react';
import { api } from '../lib/api';
import { BUCKET_ORDER, BUCKET_NOTE, isIntensive, n } from '../lib/domain';
import { useAsync, useStore } from '../lib/store';
import { BucketTag, Modal } from './ui';

/** The urgent buckets: mandatory, grace period, critical window. */
export const URGENT_BUCKETS = ['M0', 'F6', 'F5'];

const today = () => new Date().toLocaleDateString('en-CA');
/** Local "HH:MM", rounded UP to the next 5 minutes — the floor has to be a time
 *  the operator can still hit by the time they finish the dialog. */
const nowHHMM = () => {
  const d = new Date();
  d.setMinutes(d.getMinutes() + (5 - (d.getMinutes() % 5)) % 5, 0, 0);
  return d.toTimeString().slice(0, 5);
};
const later = (a: string, b: string) => (a > b ? a : b);

/**
 * Options step in front of planning. Previously "Plan" fired straight into
 * POST /plan with only a date, so there was no way to say "today, urgent only" —
 * the whole book went on the clock and the operator pruned it by hand afterwards.
 */
export function PlanDialog({
  campaignId,
  date,
  replacing,
  onClose,
  onDone,
}: {
  campaignId: number;
  date: string;
  replacing: number | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useStore((s) => s.toast);
  const [scope, setScope] = useState<'all' | 'urgent' | 'custom'>('all');
  const [custom, setCustom] = useState<string[]>(URGENT_BUCKETS);
  const [busy, setBusy] = useState(false);

  const config = useAsync(() => api.config(campaignId), [campaignId]);
  const saved = config.data?.dial_window;

  // A plan for today can only occupy time that has not happened yet, so the
  // floor is the clock — both as the input's `min` and as the default start.
  const isToday = date === today();
  const floor = isToday ? nowHHMM() : '';
  const [range, setRange] = useState<{ start: string; end: string } | null>(null);
  const start = range?.start ?? later(saved?.start ?? '09:30', floor);
  const end = range?.end ?? saved?.end ?? '19:00';
  const set = (patch: Partial<{ start: string; end: string }>) =>
    setRange({ start, end, ...patch });

  const buckets = scope === 'all' ? [] : scope === 'urgent' ? URGENT_BUCKETS : custom;
  const badRange =
    start >= end
      ? 'End must be after start.'
      : isToday && start < floor
        ? `It is already ${floor} — a call cannot be placed earlier.`
        : saved && (start < saved.start || end > saved.end)
          ? `Outside this campaign's dial window ${saved.start}–${saved.end}.`
          : '';
  const ready = (scope === 'all' || buckets.length > 0) && !badRange;

  const toggle = (b: string) =>
    setCustom((cur) => (cur.includes(b) ? cur.filter((x) => x !== b) : [...cur, b]));

  const submit = async () => {
    setBusy(true);
    try {
      const r = await api.plan(campaignId, date, buckets, { start, end });
      toast('ok', `Planned run ${r.id} — ${n(r.counts.slots)} slots, nothing dialled yet.`);
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
      title={replacing ? `Re-plan ${date}` : `Plan ${date}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={submit} disabled={!ready || busy}>
            <PlayCircle /> {busy ? 'Planning…' : 'Build plan'}
          </button>
        </>
      }
    >
      {replacing && (
        <p className="warnbox" style={{ marginTop: 0 }}>
          This replaces plan run #{replacing} for {date}. Its slots — including any times you
          edited by hand — are discarded and rebuilt.
        </p>
      )}

      <p style={{ marginTop: 0 }}>Which leads should go on the clock?</p>

      <label className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
        <input type="radio" checked={scope === 'all'} onChange={() => setScope('all')} />
        <span>
          <strong>Everything due</strong>
          <br />
          <span className="cell-dim">Every bucket the cadence says is callable today.</span>
        </span>
      </label>

      <label className="row" style={{ gap: 8, alignItems: 'flex-start', marginTop: 10 }}>
        <input type="radio" checked={scope === 'urgent'} onChange={() => setScope('urgent')} />
        <span>
          <strong>Urgent only</strong>{' '}
          {URGENT_BUCKETS.map((b) => (
            <BucketTag key={b} bucket={b} />
          ))}
          <br />
          <span className="cell-dim">
            Mandatory, grace period and critical window. Everything else is still evaluated and
            recorded, it just gets no slot today.
          </span>
        </span>
      </label>

      <label className="row" style={{ gap: 8, alignItems: 'flex-start', marginTop: 10 }}>
        <input type="radio" checked={scope === 'custom'} onChange={() => setScope('custom')} />
        <span>
          <strong>Pick buckets</strong>
          <br />
          <span className="cell-dim">Choose exactly which buckets get dialled.</span>
        </span>
      </label>

      {scope === 'custom' && (
        <div className="row" style={{ flexWrap: 'wrap', gap: 8, margin: '10px 0 0 26px' }}>
          {BUCKET_ORDER.map((b) => (
            <label
              key={b}
              className="row"
              style={{ gap: 4 }}
              title={BUCKET_NOTE[b] ?? (isIntensive(b) ? 'Two calls a day, dialled first.' : '')}
            >
              <input type="checkbox" checked={custom.includes(b)} onChange={() => toggle(b)} />
              <BucketTag bucket={b} />
            </label>
          ))}
        </div>
      )}

      <hr className="rule" />

      <p style={{ marginTop: 0 }}>
        <Clock className="inline-icon" /> When may these calls land?
      </p>
      <div className="row" style={{ gap: 10, alignItems: 'flex-end' }}>
        <label className="field">
          <span className="eyebrow">From</span>
          <input
            type="time"
            value={start}
            min={floor || undefined}
            max={end}
            step={300}
            onChange={(e) => set({ start: e.target.value })}
          />
        </label>
        <span className="cell-dim" style={{ paddingBottom: 8 }}>
          to
        </span>
        <label className="field">
          <span className="eyebrow">Until</span>
          <input
            type="time"
            value={end}
            min={start}
            step={300}
            onChange={(e) => set({ end: e.target.value })}
          />
        </label>
        {saved && (start !== saved.start || end !== saved.end) && (
          <button className="btn btn-sm btn-ghost" onClick={() => setRange(null)}>
            Reset
          </button>
        )}
      </div>

      {badRange ? (
        <p className="warnbox">{badRange}</p>
      ) : (
        <p className="cell-dim" style={{ marginBottom: 0 }}>
          {isToday && start > (saved?.start ?? '09:30')
            ? `Nothing is placed before ${start} — the morning has already gone. `
            : ''}
          Slots are spread across this range. Nothing is dialled until you approve.
        </p>
      )}
    </Modal>
  );
}
