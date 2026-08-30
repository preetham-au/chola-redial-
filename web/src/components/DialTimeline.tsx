import { useMemo } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { bucketColor, n } from '../lib/domain';
import type { DialWindow, PlanItem } from '../lib/types';

/* How the day's calls sit inside the dial window. The question this answers
   is "are they stacked at one time?", so the bars are absolute counts per
   15 minutes with the per-minute ceiling drawn as a reference line. */

const STACK = ['M0', 'F6', 'F5', 'F4', 'F3', 'F2', 'F1', 'D0'];
const BIN = 15;

export function DialTimeline({
  items,
  window: win,
  maxPerMinute,
  height = 190,
}: {
  items: PlanItem[];
  window: DialWindow;
  maxPerMinute: number;
  height?: number;
}) {
  const { rows, ceiling, peak, showCeiling } = useMemo(() => {
    const s = toMin(win.start);
    const e = toMin(win.end);
    const binStart = Math.floor(s / BIN) * BIN;
    const map = new Map<number, Record<string, number>>();
    for (let m = binStart; m < e; m += BIN) map.set(m, {});

    items.forEach((it) => {
      const m = toMin(it.scheduled_time.slice(11, 16));
      const bin = Math.floor(m / BIN) * BIN;
      const slot = map.get(bin);
      if (!slot) return;
      slot[it.bucket] = (slot[it.bucket] ?? 0) + 1;
    });

    const rows = [...map.entries()].map(([m, counts]) => ({
      t: fromMin(m),
      total: Object.values(counts).reduce((a, c) => a + c, 0),
      ...counts,
    }));
    const ceiling = maxPerMinute * BIN;
    const peak = Math.max(1, ...rows.map((r) => r.total));
    // Only plot the stagger ceiling when it is near the actual load — otherwise
    // it flattens the bars and hides the thing the chart is for.
    return { rows, ceiling, peak, showCeiling: ceiling <= peak * 2 };
  }, [items, win, maxPerMinute]);

  return (
    <div>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={rows} margin={{ top: 8, right: 8, left: -14, bottom: 0 }} barCategoryGap={1}>
          <CartesianGrid stroke="#1b282e" vertical={false} />
          <XAxis
            dataKey="t"
            tickLine={false}
            axisLine={{ stroke: '#24343b' }}
            interval={3}
            tick={{ fill: '#71868f' }}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            width={44}
            tick={{ fill: '#71868f' }}
            domain={[0, Math.ceil((showCeiling ? ceiling : peak) * 1.1)]}
          />
          <Tooltip cursor={{ fill: 'rgba(255,255,255,0.04)' }} content={<Tip />} />
          {showCeiling && (
            <ReferenceLine
              y={ceiling}
              stroke="#4fb8c9"
              strokeDasharray="3 3"
              label={{
                value: `stagger ceiling ${maxPerMinute}/min`,
                position: 'insideTopRight',
                fill: '#4fb8c9',
                fontSize: 10,
                fontFamily: 'JetBrains Mono',
              }}
            />
          )}
          {STACK.map((b) => (
            <Bar key={b} dataKey={b} stackId="a" fill={bucketColor(b)} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <div className="row" style={{ flexWrap: 'wrap', gap: 12, paddingTop: 8 }}>
        {STACK.map((b) => (
          <span key={b} className="xt-legend" style={{ gap: 5 }}>
            <span className="chip-dot" style={{ background: bucketColor(b) }} />
            {b}
          </span>
        ))}
        <span className="xt-legend" style={{ marginLeft: 'auto' }}>
          {BIN}-minute bins · window {win.start}–{win.end} · busiest bin {n(peak)}
          {showCeiling ? '' : ` · well under the ${maxPerMinute}/min ceiling`}
        </span>
      </div>
    </div>
  );
}

function Tip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  const rows = payload.filter((p: any) => p.value > 0).reverse();
  const total = rows.reduce((a: number, p: any) => a + p.value, 0);
  return (
    <div className="chart-tip">
      <span className="k">{label} · {BIN} min</span>
      {rows.map((p: any) => (
        <div key={p.dataKey} className="row" style={{ gap: 8 }}>
          <span className="chip-dot" style={{ background: p.color }} />
          <span className="mono" style={{ fontSize: 11 }}>{p.dataKey}</span>
          <span className="v" style={{ marginLeft: 'auto' }}>{n(p.value)}</span>
        </div>
      ))}
      <div className="row" style={{ gap: 8, borderTop: '1px solid var(--line)', marginTop: 4, paddingTop: 4 }}>
        <span className="mono" style={{ fontSize: 11, color: 'var(--muted)' }}>total</span>
        <span className="v" style={{ marginLeft: 'auto' }}>{n(total)}</span>
      </div>
    </div>
  );
}

function toMin(hhmm: string) {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

function fromMin(m: number) {
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}
