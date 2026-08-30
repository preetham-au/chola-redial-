import { useEffect, useMemo, useRef } from 'react';
import { AlertTriangle, Info, Lock } from 'lucide-react';
import {
  BUCKET_NOTE,
  bucketColor,
  CLASS_META,
  CLASS_ORDER,
  classOf,
  configurableBuckets,
  dispLabel,
  effectiveDispositions,
  isIntensive,
  n,
} from '../lib/domain';
import type { BucketsResponse, Config } from '../lib/types';

/* Bucket x disposition, the same shape as the dashboard heatmap — buckets down
   the runway, dispositions across — so the operator configures in the shape
   they read results in. A row is either INHERIT (no key, or an empty list) or
   CUSTOM. It narrows only: the columns are the campaign's own auto list, so a
   bucket can never re-enable something the campaign switched off. */

export type RowMode = 'inherit' | 'all' | 'none';

export type BdAction =
  | { kind: 'cell'; bucket: string; slug: string }
  | { kind: 'col'; slug: string }
  | { kind: 'row'; bucket: string; mode: RowMode }
  | { kind: 'dropExtra'; slug: string }
  | { kind: 'dropBuckets'; buckets: string[] };

/** Every edit the matrix can make, in one place so it is checkable.
 *  `rows` is the set of valid bucket keys — nothing else is ever written. */
export function applyBd(
  cfg: Config,
  rows: string[],
  a: BdAction,
): Record<string, string[]> {
  const map = { ...(cfg.bucket_dispositions ?? {}) };
  // An inheriting row seeds from the global list on its first edit, so one
  // click removes one disposition instead of silently dropping all the others.
  const baseOf = (b: string) => (map[b]?.length ? map[b] : cfg.auto_dispositions);

  switch (a.kind) {
    case 'cell': {
      const base = baseOf(a.bucket);
      map[a.bucket] = base.includes(a.slug)
        ? base.filter((s) => s !== a.slug)
        : [...base, a.slug];
      return map;
    }
    case 'col': {
      // Turning a column ON leaves inheriting rows alone — they already have it.
      const allOn = rows.every((b) => effectiveDispositions(cfg, b).includes(a.slug));
      rows.forEach((b) => {
        const own = map[b];
        if (allOn) map[b] = baseOf(b).filter((s) => s !== a.slug);
        else if (own?.length && !own.includes(a.slug)) map[b] = [...own, a.slug];
      });
      return map;
    }
    case 'row':
      if (a.mode === 'inherit') delete map[a.bucket];
      else map[a.bucket] = a.mode === 'all' ? [...cfg.auto_dispositions] : [];
      return map;
    case 'dropExtra':
      Object.keys(map).forEach((b) => {
        map[b] = map[b].filter((s) => s !== a.slug);
      });
      return map;
    case 'dropBuckets':
      a.buckets.forEach((b) => delete map[b]);
      return map;
  }
}

export function BucketDispositions({
  draft,
  onChange,
  counts,
  focusBucket,
}: {
  draft: Config;
  onChange: (map: Record<string, string[]>) => void;
  counts: BucketsResponse | null;
  focusBucket: string | null;
}) {
  const map = draft.bucket_dispositions ?? {};
  const rows = configurableBuckets(draft.frequency_table);
  const bodyRef = useRef<HTMLTableSectionElement>(null);

  // Only reachable from a hand-edited or older config; the UI never writes one.
  const unknown = Object.keys(map).filter((b) => !rows.includes(b));

  const cols = useMemo(() => {
    const uniq = [...new Set(draft.auto_dispositions)];
    return uniq.sort((a, b) => {
      const d = CLASS_ORDER.indexOf(classOf(a)) - CLASS_ORDER.indexOf(classOf(b));
      return d !== 0 ? d : a.localeCompare(b);
    });
  }, [draft.auto_dispositions]);

  /** Slugs a bucket lists that the campaign does not: inert, but shown so a
   *  config that carries them is not silently misread. */
  const extras = useMemo(() => {
    const global = new Set(draft.auto_dispositions);
    const out: string[] = [];
    Object.values(map).forEach((list) =>
      list.forEach((s) => {
        if (!global.has(s) && !out.includes(s)) out.push(s);
      }),
    );
    return out;
  }, [map, draft.auto_dispositions]);

  const leadCount = useMemo(() => {
    const m = new Map<string, number>();
    counts?.matrix.forEach((c) => m.set(`${c.bucket}|${c.disposition}`, c.count));
    return m;
  }, [counts]);

  useEffect(() => {
    if (!focusBucket) return;
    bodyRef.current
      ?.querySelector<HTMLElement>(`[data-bucket="${focusBucket}"]`)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [focusBucket]);

  const act = (a: BdAction) => onChange(applyBd(draft, rows, a));
  const isOn = (bucket: string, slug: string) =>
    effectiveDispositions(draft, bucket).includes(slug);
  const toggleCell = (bucket: string, slug: string) => act({ kind: 'cell', bucket, slug });
  const toggleCol = (slug: string) => act({ kind: 'col', slug });
  const setMode = (bucket: string, mode: RowMode) => act({ kind: 'row', bucket, mode });
  const dropExtra = (slug: string) => act({ kind: 'dropExtra', slug });

  const customCount = rows.filter((b) => (map[b]?.length ?? 0) > 0).length;
  const m0Narrowed = (map.M0?.length ?? 0) > 0 && effectiveDispositions(draft, 'M0').length < cols.length;

  return (
    <>
      <div className="filters" style={{ gap: 14 }}>
        <span className="xt-legend">
          <span className="bd-swatch is-on" /> dialled
        </span>
        <span className="xt-legend">
          <span className="bd-swatch is-inherit" /> inherited from the campaign list
        </span>
        <span className="xt-legend">
          <span className="bd-swatch is-off" /> excluded for this bucket
        </span>
        <span style={{ flex: 1 }} />
        <span className="xt-legend">
          {customCount === 0
            ? 'every bucket inherits — this behaves exactly as before'
            : `${customCount} of ${rows.length} buckets customised`}
        </span>
      </div>

      <div className="bd-wrap">
        <table className="xt bd">
          <thead>
            <tr>
              <th>
                <div className="bd-rowhead">
                  <span className="eyebrow">Bucket ↓ / disposition →</span>
                </div>
              </th>
              {cols.map((slug) => {
                const on = rows.filter((b) => isOn(b, slug)).length;
                return (
                  <th key={slug || '(blank)'}>
                    <button
                      type="button"
                      className={`bd-colhead${on === 0 ? ' is-dead' : ''}`}
                      title={`${slug || '(blank)'} — click to toggle down all ${rows.length} buckets (${on} on now)`}
                      onClick={() => toggleCol(slug)}
                    >
                      <b>{short(slug)}</b>
                      <i>
                        {on}/{rows.length}
                      </i>
                    </button>
                  </th>
                );
              })}
              {extras.map((slug) => (
                <th key={`x-${slug}`}>
                  <button
                    type="button"
                    className="bd-colhead is-locked"
                    title={`${lockNote(slug)} Click to strip it from every bucket.`}
                    onClick={() => dropExtra(slug)}
                  >
                    <b>{short(slug)}</b>
                    <i>
                      <Lock size={9} />
                    </i>
                  </button>
                </th>
              ))}
              <th>
                <div className="bd-colhead is-plain">
                  <b>NOT DIALLED</b>
                  <i>leads</i>
                </div>
              </th>
            </tr>
          </thead>
          <tbody ref={bodyRef}>
            {rows.map((b) => {
              const own = map[b];
              const custom = own !== undefined;
              const emptyCustom = custom && own.length === 0;
              const freq = draft.frequency_table.find((f) => f.bucket === b);
              const dropped = cols.filter((s) => !isOn(b, s));
              const affected = counts
                ? dropped.reduce((a, s) => a + (leadCount.get(`${b}|${s}`) ?? 0), 0)
                : null;
              return (
                <tr key={b} data-bucket={b} className={focusBucket === b ? 'is-focus' : ''}>
                  <th>
                    <div className="bd-rowhead">
                      <span className="chip-dot" style={{ background: bucketColor(b) }} />
                      <b className="mono" style={{ color: bucketColor(b) }}>
                        {b}
                        {isIntensive(b) ? ' ▲' : ''}
                      </b>
                      <span className="cell-dim trunc bd-label" title={BUCKET_NOTE[b] ?? freq?.label}>
                        {freq ? `${freq.from_dte}→${freq.to_dte}d` : b === 'M0' ? 'RED−1 / RED' : 'callback'}
                      </span>
                      <span className={`badge${custom && !emptyCustom ? ' badge-accent' : ''}`}>
                        {custom && !emptyCustom ? 'custom' : 'inherits'}
                      </span>
                      {emptyCustom && (
                        <span className="badge badge-warn" title="An empty list is not “dial nothing”. The server reads it as inherit, and saving drops the key.">
                          empty ⇒ inherits
                        </span>
                      )}
                      {b === 'M0' && dropped.length > 0 && (
                        <span
                          className="badge badge-warn"
                          title="M0 is the RED−1 / RED last-chance call. Narrowing it can suppress that call entirely."
                        >
                          <AlertTriangle size={10} /> last chance
                        </span>
                      )}
                      <span className="bd-acts">
                        <button
                          type="button"
                          className={`chip${!custom ? ' is-on' : ''}`}
                          onClick={() => setMode(b, 'inherit')}
                          title="Follow the campaign list, including any future change to it"
                        >
                          inherit
                        </button>
                        <button
                          type="button"
                          className="chip"
                          onClick={() => setMode(b, 'all')}
                          title="Pin an explicit copy of the campaign list, then remove from it"
                        >
                          all
                        </button>
                        <button
                          type="button"
                          className="chip"
                          onClick={() => setMode(b, 'none')}
                          title="Clears every tick. Note an empty list means inherit, not “dial nothing”."
                        >
                          none
                        </button>
                      </span>
                    </div>
                  </th>
                  {cols.map((slug) => {
                    const on = isOn(b, slug);
                    const v = leadCount.get(`${b}|${slug}`) ?? 0;
                    return (
                      <td
                        key={slug || '(blank)'}
                        className={`xt-cell bd-cell ${
                          on ? (custom && !emptyCustom ? 'is-on' : 'is-inherit') : 'is-off'
                        }`}
                        style={on ? { background: hexA(bucketColor(b), custom && !emptyCustom ? 0.4 : 0.16) } : undefined}
                        title={`${b} × ${slug || '(blank)'} — ${
                          on ? 'dialled' : 'not dialled'
                        }${counts ? ` · ${n(v)} leads` : ''}${
                          custom && !emptyCustom ? '' : ' · inherited, click to customise this bucket'
                        }`}
                        onClick={() => toggleCell(b, slug)}
                        role="button"
                        tabIndex={0}
                        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && toggleCell(b, slug)}
                      >
                        {counts ? (v === 0 ? '·' : n(v)) : on ? '✓' : '·'}
                      </td>
                    );
                  })}
                  {extras.map((slug) => (
                    <td
                      key={`x-${slug}`}
                      className="xt-cell bd-cell is-locked"
                      title={lockNote(slug)}
                    >
                      {own?.includes(slug) ? <Lock size={10} /> : '·'}
                    </td>
                  ))}
                  <td className="xt-cell bd-affected">
                    {affected === null ? '—' : affected === 0 ? '·' : n(affected)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {unknown.length > 0 && (
        <div className="card-body" style={{ paddingBottom: 0 }}>
          <div className="warnbox">
            <AlertTriangle />
            <span>
              Unknown bucket {unknown.map((u) => <b key={u} className="mono">{u} </b>)} in this
              config. The server rejects the whole save with a 422. Valid keys are the
              frequency-table buckets plus <span className="mono">M0</span> and{' '}
              <span className="mono">D0</span>.{' '}
              <button
                className="btn btn-sm btn-ghost"
                onClick={() => act({ kind: 'dropBuckets', buckets: unknown })}
              >
                Remove {unknown.length === 1 ? 'it' : 'them'}
              </button>
            </span>
          </div>
        </div>
      )}

      {m0Narrowed && (
        <div className="card-body" style={{ paddingBottom: 0 }}>
          <div className="warnbox">
            <AlertTriangle />
            <span>
              <b className="mono">M0</b> is narrowed. M0 is the mandatory RED−1 / RED call — the last
              chance before the policy lapses. A lead whose disposition falls outside this row gets no
              last-chance call at all.
            </span>
          </div>
        </div>
      )}

      <div className="card-body" style={{ borderTop: '1px solid var(--line-soft)' }}>
        <div className="infobox">
          <Info />
          <span>
            A bucket with no row of its own <b>inherits</b> the campaign list above — an empty list is
            inherit too, not “dial nothing”. A bucket only ever <b>narrows</b>: the columns are the
            campaign's own auto list, so no bucket can re-enable{' '}
            <span className="mono">do_not_call</span> or anything else excluded server-side under
            TRAI/NCPR.{' '}
            {counts
              ? 'Numbers are today’s leads in that bucket and disposition; the last column is what this row stops dialling.'
              : 'Lead counts are unavailable — the bucket matrix did not load.'}
          </span>
        </div>
      </div>
    </>
  );
}

/** Column headers are vertical; the longest slug is 48 characters. */
function short(slug: string): string {
  const s = slug === '' ? '(blank)' : dispLabel(slug);
  return s.length > 20 ? `${s.slice(0, 19)}…` : s;
}

function lockNote(slug: string): string {
  const cls = classOf(slug);
  if (cls === 'excluded')
    return `${slug} is rejected server-side whatever any bucket asks for. Regulatory (TRAI/NCPR) or already renewed.`;
  return `${slug} is not in the campaign's auto list, so listing it here has no effect. A bucket narrows only — it cannot add. (${CLASS_META[cls].label})`;
}

function hexA(hex: string, a: number) {
  const v = hex.replace('#', '');
  return `rgba(${parseInt(v.slice(0, 2), 16)}, ${parseInt(v.slice(2, 4), 16)}, ${parseInt(
    v.slice(4, 6),
    16,
  )}, ${a})`;
}
