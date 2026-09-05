import { bucketColor, BUCKET_NOTE, isIntensive, n } from '../lib/domain';
import type { BucketRow } from '../lib/types';

/* THE RUNWAY — the one object this console is built around.
   A single days-to-expiry axis running +45 down to -3, with a hard gate at 0.
   Segment width = how many days the bucket spans (its real position in time).
   Bar height   = how many leads sit there.
   M0 and D0 are drawn off the axis because they are not positions in time:
   M0 is an override flag, D0 is driven by a promised callback date. */

export function Runway({
  buckets,
  selected,
  onSelect,
  metric = 'total',
}: {
  buckets: BucketRow[];
  selected?: string | null;
  onSelect?: (b: string | null) => void;
  metric?: 'total' | 'eligible';
}) {
  const span: Record<string, number> = { F1: 14, F2: 8, F3: 8, F4: 8, F5: 7, E0: 2, F6: 2 };
  const onAxis = ['F1', 'F2', 'F3', 'F4', 'F5', 'E0', 'F6']
    .map((b) => buckets.find((x) => x.bucket === b))
    .filter(Boolean) as BucketRow[];
  const offAxis = ['M0', 'D0']
    .map((b) => buckets.find((x) => x.bucket === b))
    .filter(Boolean) as BucketRow[];

  const val = (b: BucketRow) => (metric === 'eligible' ? b.eligible : b.total);
  const peak = Math.max(1, ...buckets.map(val));

  const seg = (b: BucketRow, flexBasis: number) => {
    const dim = selected && selected !== b.bucket;
    const h = Math.max(4, Math.round((val(b) / peak) * 58));
    return (
      <button
        key={b.bucket}
        type="button"
        className={`runway-seg${dim ? ' is-off' : ''}${selected === b.bucket ? ' is-on' : ''} is-${b.bucket}`}
        style={{ ['--seg' as string]: bucketColor(b.bucket), flex: `${flexBasis} 1 0` }}
        onClick={() => onSelect?.(selected === b.bucket ? null : b.bucket)}
        title={`${b.bucket} · ${b.label} — ${n(b.total)} leads, ${n(b.eligible)} eligible${
          BUCKET_NOTE[b.bucket] ? `\n${BUCKET_NOTE[b.bucket]}` : ''
        }`}
        aria-pressed={selected === b.bucket}
      >
        <span className="runway-bar" style={{ height: h }} />
      </button>
    );
  };

  const tick = (b: BucketRow, flexBasis: number) => (
    <div
      key={b.bucket}
      className="runway-tick"
      style={{ ['--seg' as string]: bucketColor(b.bucket), flex: `${flexBasis} 1 0` }}
    >
      <b>
        {b.bucket}
        {isIntensive(b.bucket) ? ' ▲' : ''}
      </b>
      <em>{n(val(b))}</em>
      <i>{b.label}</i>
    </div>
  );

  return (
    <div className="runway-scroll">
    <div className="runway">
      <div className="runway-scale">
        <span>← 45 days to expiry</span>
        <span>expiry · grace −3d</span>
        <span>off the axis →</span>
      </div>

      <div className="runway-track">
        {onAxis.filter((b) => b.bucket !== 'F6').map((b) => seg(b, span[b.bucket]))}
        <div className="runway-gate">
          <span>RED</span>
        </div>
        {onAxis.filter((b) => b.bucket === 'F6').map((b) => seg(b, span[b.bucket]))}
        <div style={{ width: 20, flex: 'none' }} />
        {offAxis.map((b) => seg(b, 6))}
      </div>

      <div className="runway-foot">
        {onAxis.filter((b) => b.bucket !== 'F6').map((b) => tick(b, span[b.bucket]))}
        <div style={{ width: 9, flex: 'none' }} />
        {onAxis.filter((b) => b.bucket === 'F6').map((b) => tick(b, span[b.bucket]))}
        <div style={{ width: 20, flex: 'none' }} />
        {offAxis.map((b) => tick(b, 6))}
      </div>
    </div>
    </div>
  );
}
