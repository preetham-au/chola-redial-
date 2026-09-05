import { useMemo, useState } from 'react';
import {
  bucketColor,
  CLASS_META,
  CLASS_ORDER,
  classOf,
  dispLabel,
  isIntensive,
  n,
} from '../lib/domain';
import type { BucketsResponse } from '../lib/types';

/* Bucket x disposition. Columns follow the runway, so the eye reads
   left-to-right as "closer to expiry" without being told. Cell fill is the
   column's own bucket hue on a square-root scale against the single largest
   cell in the table, so magnitudes stay comparable across columns. */

const COLS = ['F1', 'F2', 'F3', 'F4', 'F5', 'E0', 'F6', 'M0', 'D0'];

export function Crosstab({ data }: { data: BucketsResponse }) {
  const [onlyAuto, setOnlyAuto] = useState(false);

  const { rows, cells, colTotals, peak, grand } = useMemo(() => {
    const cells = new Map<string, number>();
    data.matrix.forEach((c) => cells.set(`${c.bucket}|${c.disposition}`, c.count));

    const rows = [...data.dispositions].sort((a, b) => {
      const ca = CLASS_ORDER.indexOf(classOf(a.disposition));
      const cb = CLASS_ORDER.indexOf(classOf(b.disposition));
      if (ca !== cb) return ca - cb;
      return b.total - a.total;
    });

    const colTotals: Record<string, number> = {};
    COLS.forEach((b) => {
      colTotals[b] = rows
        .filter((r) => !onlyAuto || r.auto)
        .reduce((a, r) => a + (cells.get(`${b}|${r.disposition}`) ?? 0), 0);
    });

    const peak = Math.max(1, ...[...cells.values()]);
    const grand = Object.values(colTotals).reduce((a, c) => a + c, 0);
    return { rows: rows.filter((r) => !onlyAuto || r.auto), cells, colTotals, peak, grand };
  }, [data, onlyAuto]);

  const fill = (bucket: string, v: number) => {
    if (v === 0) return undefined;
    const a = Math.sqrt(v / peak) * 0.52;
    return { background: hexA(bucketColor(bucket), a) };
  };

  let lastClass = '';

  return (
    <>
      <div className="filters" style={{ gap: 14 }}>
        <label className="row" style={{ gap: 7, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={onlyAuto}
            onChange={(e) => setOnlyAuto(e.target.checked)}
            style={{ accentColor: 'var(--accent)' }}
          />
          <span style={{ fontSize: 12 }}>Only dispositions in the auto-run</span>
        </label>
        <span style={{ flex: 1 }} />
        <span className="xt-legend">
          <span>fewer</span>
          <span className="xt-ramp">
            {[0.08, 0.18, 0.3, 0.42, 0.52].map((a) => (
              <span key={a} style={{ background: hexA('#e8703f', a) }} />
            ))}
          </span>
          <span>more · √ scale, peak {n(peak)}</span>
        </span>
      </div>

      <div className="xtab">
        <table className="xt">
          <thead>
            <tr>
              <th>
                <div className="xt-rowhead">
                  <span className="eyebrow">Disposition ↓ / bucket →</span>
                </div>
              </th>
              {COLS.map((b) => {
                const row = data.buckets.find((x) => x.bucket === b);
                return (
                  <th key={b}>
                    <div className="xt-colhead" style={{ ['--seg' as string]: bucketColor(b) }}>
                      <b>
                        {b}
                        {isIntensive(b) ? ' ▲' : ''}
                      </b>
                      <i>{n(row?.total ?? 0)}</i>
                    </div>
                  </th>
                );
              })}
              <th>
                <div className="xt-colhead">
                  <b style={{ color: 'var(--text-dim)' }}>ALL</b>
                  <i>{n(grand)}</i>
                </div>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const cls = classOf(r.disposition);
              const newClass = cls !== lastClass;
              lastClass = cls;
              return (
                <tr key={r.disposition || '(blank)'}>
                  <th>
                    <div className={`xt-rowhead${r.auto ? ' is-auto' : ''}`}>
                      <span
                        className="chip-dot"
                        title={CLASS_META[cls].label}
                        style={{
                          background: r.auto ? 'var(--accent)' : 'transparent',
                          border: r.auto ? 'none' : '1px solid var(--faint)',
                        }}
                      />
                      <span className="d-name" title={`${r.disposition || '(blank)'} · ${CLASS_META[cls].label}`}>
                        {dispLabel(r.disposition)}
                      </span>
                      {newClass && (
                        <span className="eyebrow" style={{ marginLeft: 'auto', fontSize: 9 }}>
                          {CLASS_META[cls].label}
                        </span>
                      )}
                    </div>
                  </th>
                  {COLS.map((b) => {
                    const v = cells.get(`${b}|${r.disposition}`) ?? 0;
                    return (
                      <td
                        key={b}
                        className={`xt-cell${v === 0 ? ' is-zero' : ''}${!r.auto && v > 0 ? ' is-manual' : ''}`}
                        style={fill(b, v)}
                        title={`${b} × ${r.disposition || '(blank)'} = ${n(v)}${
                          r.auto ? '' : ' — not in the auto-run'
                        }`}
                      >
                        {v === 0 ? '·' : n(v)}
                      </td>
                    );
                  })}
                  <td className="xt-cell" style={{ fontWeight: 600 }}>
                    {n(r.total)}
                  </td>
                </tr>
              );
            })}
            <tr className="is-total">
              <th>
                <div className="xt-rowhead">
                  <span className="eyebrow">Column total</span>
                </div>
              </th>
              {COLS.map((b) => (
                <td key={b} className="xt-cell" style={{ fontWeight: 600, background: 'var(--bg-deep)' }}>
                  {n(colTotals[b])}
                </td>
              ))}
              <td className="xt-cell" style={{ fontWeight: 700, background: 'var(--bg-deep)' }}>
                {n(grand)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="filters" style={{ borderBottom: 0, borderTop: '1px solid var(--line-soft)' }}>
        <span className="xt-legend">
          <span className="chip-dot" style={{ background: 'var(--accent)' }} /> in the auto-run
        </span>
        <span className="xt-legend">
          <span className="chip-dot" style={{ border: '1px solid var(--faint)' }} /> manual or excluded — hatched cells
        </span>
        <span className="xt-legend">▲ two calls a day, dialled first</span>
      </div>
    </>
  );
}

function hexA(hex: string, a: number) {
  const v = hex.replace('#', '');
  const r = parseInt(v.slice(0, 2), 16);
  const g = parseInt(v.slice(2, 4), 16);
  const b = parseInt(v.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${a.toFixed(3)})`;
}
