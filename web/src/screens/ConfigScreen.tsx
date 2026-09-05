import { useEffect, useMemo, useState } from 'react';
import {
  ArrowDown,
  ArrowUp,
  CircleSlash,
  Info,
  Loader2,
  Lock,
  Play,
  RotateCcw,
  Save,
} from 'lucide-react';
import { api } from '../lib/api';
import {
  bucketColor,
  CLASS_META,
  CLASS_ORDER,
  configurableBuckets,
  DIAL_WINDOW_CEIL,
  DIAL_WINDOW_FLOOR,
  DISPOSITIONS,
  dialWindowError,
  dispLabel,
  isIntensive,
  narrowedBuckets,
} from '../lib/domain';
import { useAsync, useCampaign, useStore } from '../lib/store';
import { useCampaignPause } from '../components/AgentBar';
import { BucketDispositions } from '../components/BucketDispositions';
import { Card, Toggle } from '../components/ui';
import type { Config, FrequencyRow } from '../lib/types';

export function ConfigScreen({ focusBucket = null }: { focusBucket?: string | null }) {
  const campaignId = useStore((s) => s.campaignId)!;
  const date = useStore((s) => s.date);
  const toast = useStore((s) => s.toast);
  const campaign = useCampaign();
  const togglePause = useCampaignPause();

  const loaded = useAsync(() => api.config(campaignId), [campaignId]);
  const history = useAsync(() => api.configHistory(campaignId), [campaignId]);
  // Only for the live lead counts on the matrix. The screen works without it.
  const buckets = useAsync(() => api.buckets(campaignId, date), [campaignId, date]);

  const [draft, setDraft] = useState<Config | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (loaded.data) setDraft(structuredClone(loaded.data));
  }, [loaded.data]);

  const dirty = useMemo(
    () => !!draft && !!loaded.data && JSON.stringify(draft) !== JSON.stringify(loaded.data),
    [draft, loaded.data],
  );

  if (!draft) {
    return (
      <div className="page">
        <div className="empty">
          <Loader2 className="spin" />
          <h3>Loading configuration</h3>
        </div>
      </div>
    );
  }

  const windowError = dialWindowError(draft.dial_window.start, draft.dial_window.end);
  const second = draft.second_call_dispositions ?? [];
  // Mirrors the server's 422 on an unknown bucket key, so the save button is
  // dead before the round trip rather than after it.
  const badBuckets = Object.keys(draft.bucket_dispositions ?? {}).filter(
    (b) => !configurableBuckets(draft.frequency_table).includes(b),
  );
  const blocked = !!windowError || badBuckets.length > 0;
  const patch = (p: Partial<Config>) => setDraft({ ...draft, ...p });

  const save = async () => {
    if (blocked) return;
    setSaving(true);
    try {
      // An empty per-bucket list means inherit, so drop the key rather than
      // sending noise the server would only read back as inherit anyway.
      const pruned = Object.fromEntries(
        Object.entries(draft.bucket_dispositions ?? {}).filter(([, v]) => v.length > 0),
      );
      const next = await api.saveConfig(campaignId, { ...draft, bucket_dispositions: pruned });
      toast('ok', `Saved as version ${next.version}. Earlier versions are kept.`);
      loaded.reload();
      history.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const move = (i: number, d: -1 | 1) => {
    const order = [...draft.bucket_priority];
    const j = i + d;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    patch({ bucket_priority: order });
  };

  const setFreq = (i: number, p: Partial<FrequencyRow>) => {
    const t = draft.frequency_table.map((r, k) => (k === i ? { ...r, ...p } : r));
    patch({ frequency_table: t });
  };

  const autoSet = new Set(draft.auto_dispositions);
  const toggleDisp = (slug: string, on: boolean) => {
    const next = on
      ? [...draft.auto_dispositions, slug]
      : draft.auto_dispositions.filter((s) => s !== slug);
    patch({ auto_dispositions: next });
  };

  return (
    <div className="page grid" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">{campaign?.name}</span>
          <h1>Campaign configuration</h1>
          <p>
            Saving writes a new version. Nothing is overwritten, and a run always records the version
            it used.
          </p>
        </div>
        <div className="row" style={{ marginLeft: 'auto' }}>
          <span className="badge">current v{loaded.data?.version}</span>
          <button className="btn btn-ghost" disabled={!dirty} onClick={() => setDraft(structuredClone(loaded.data!))}>
            <RotateCcw /> Discard changes
          </button>
          <button className="btn btn-primary" disabled={!dirty || blocked || saving} onClick={save}>
            {saving ? <Loader2 className="spin" /> : <Save />} Save as v{(loaded.data?.version ?? 0) + 1}
          </button>
        </div>
      </div>

      {/* Pause sits at the top because it is the one switch that stops everything. */}
      <div className="strip">
        <div className="strip-cell grow">
          <span className="eyebrow">Campaign state</span>
          <span className={`strip-val ${campaign?.paused ? 'is-bad' : 'is-ok'}`}>
            {campaign?.paused ? 'Paused' : 'Running'}
          </span>
          <span className="strip-sub">
            {campaign?.paused
              ? 'Approve and commit return 409 while paused. Planning still works.'
              : 'Runs can be approved. Planning and approving are separate steps.'}
          </span>
        </div>
        <div className="strip-cell" style={{ justifyContent: 'center' }}>
          <button
            className={campaign?.paused ? 'btn btn-primary' : 'btn btn-danger'}
            disabled={!campaign}
            onClick={() => campaign && togglePause(campaign)}
          >
            {campaign?.paused ? <Play /> : <CircleSlash />}
            {campaign?.paused ? 'Resume campaign' : 'Pause campaign'}
          </button>
        </div>
      </div>

      <div className="split-3-2">
        <Card title="Frequency table" eyebrow="calls per bucket" flush>
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th>Bucket</th>
                  <th>Label</th>
                  <th className="n">From dte</th>
                  <th className="n">To dte</th>
                  <th className="n">Calls / week</th>
                  <th className="n">Calls / day</th>
                </tr>
              </thead>
              <tbody>
                {draft.frequency_table.map((row, i) => (
                  <tr key={row.bucket}>
                    <td>
                      <span className="row" style={{ gap: 7 }}>
                        <span className="chip-dot" style={{ background: bucketColor(row.bucket) }} />
                        <b className="mono" style={{ color: bucketColor(row.bucket) }}>{row.bucket}</b>
                        {isIntensive(row.bucket) && <span className="badge badge-warn">urgent</span>}
                      </span>
                    </td>
                    <td>
                      <input
                        className="input"
                        style={{ width: 148, fontFamily: 'var(--sans)' }}
                        value={row.label}
                        onChange={(e) => setFreq(i, { label: e.target.value })}
                      />
                    </td>
                    <td className="n">
                      <NumIn v={row.from_dte} onChange={(v) => setFreq(i, { from_dte: v })} min={-30} max={365} />
                    </td>
                    <td className="n">
                      <NumIn v={row.to_dte} onChange={(v) => setFreq(i, { to_dte: v })} min={-30} max={365} />
                    </td>
                    <td className="n">
                      <NumIn v={row.calls_per_week} onChange={(v) => setFreq(i, { calls_per_week: v })} min={0} max={14} />
                    </td>
                    <td className="n">
                      <NumIn v={row.calls_per_day} onChange={(v) => setFreq(i, { calls_per_day: v })} min={0} max={4} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card-body" style={{ borderTop: '1px solid var(--line-soft)' }}>
            <div className="infobox">
              <Info />
              <span>
                dte counts down to renewal: <span className="mono">+45</span> is 45 days before expiry,{' '}
                <span className="mono">0</span> is expiry day, <span className="mono">−3</span> is three days
                into grace. A bucket with calls per day above zero is dialled twice daily and ignores the
                weekly budget.
              </span>
            </div>
          </div>
        </Card>

        <div className="grid" style={{ gap: 14, alignContent: 'start' }}>
          <Card title="Dial window" eyebrow="09:00 – 19:00 only">
            <div className="row" style={{ gap: 14, alignItems: 'flex-end' }}>
              <label className="field">
                <span className="eyebrow">Start</span>
                <input
                  className={`input${windowError ? ' is-bad' : ''}`}
                  type="time"
                  min={DIAL_WINDOW_FLOOR}
                  max={DIAL_WINDOW_CEIL}
                  step={300}
                  value={draft.dial_window.start}
                  onChange={(e) => patch({ dial_window: { ...draft.dial_window, start: e.target.value } })}
                />
              </label>
              <label className="field">
                <span className="eyebrow">End</span>
                <input
                  className={`input${windowError ? ' is-bad' : ''}`}
                  type="time"
                  min={DIAL_WINDOW_FLOOR}
                  max={DIAL_WINDOW_CEIL}
                  step={300}
                  value={draft.dial_window.end}
                  onChange={(e) => patch({ dial_window: { ...draft.dial_window, end: e.target.value } })}
                />
              </label>
              <span className="field-hint" style={{ paddingBottom: 6 }}>
                {windowError ? (
                  <span style={{ color: 'var(--bad)' }}>{windowError}</span>
                ) : (
                  <span className="mono">
                    {span(draft.dial_window.start, draft.dial_window.end)} of dialling
                  </span>
                )}
              </span>
            </div>
            <div style={{ marginTop: 12 }}>
              <WindowBar start={draft.dial_window.start} end={draft.dial_window.end} />
            </div>
          </Card>

          <Card title="Caps and gaps">
            <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Num label="Calls per day cap" hint="per lead, across all buckets" v={draft.calls_per_day_cap} onChange={(v) => patch({ calls_per_day_cap: v })} min={1} max={5} />
              <Num label="Same-day gap (h)" hint="between two calls to one lead" v={draft.same_day_gap_hours} onChange={(v) => patch({ same_day_gap_hours: v })} min={0} max={12} step={0.5} />
              <Num label="Shift from last (h)" hint="rotates the time of day" v={draft.shift_from_last_hours} onChange={(v) => patch({ shift_from_last_hours: v })} min={0} max={12} step={0.5} />
              <Num label="Max per minute" hint="load stagger ceiling" v={draft.max_per_minute} onChange={(v) => patch({ max_per_minute: v })} min={1} max={120} />
              <Num label="Max per run" hint="0 = unlimited" v={draft.max_per_run} onChange={(v) => patch({ max_per_run: v })} min={0} max={100000} />
              <Num label="Max attempts" hint="0 = unlimited" v={draft.max_attempts} onChange={(v) => patch({ max_attempts: v })} min={0} max={99} />
            </div>
            <div className="field" style={{ marginTop: 12 }}>
              <span className="eyebrow">Mandatory days (dte)</span>
              <input
                className="input"
                value={draft.mandatory_days.join(', ')}
                onChange={(e) =>
                  patch({
                    mandatory_days: e.target.value
                      .split(',')
                      .map((s) => Number(s.trim()))
                      .filter((x) => Number.isFinite(x)),
                  })
                }
              />
              <span className="field-hint">
                Leads at these dte values become M0 and are dialled whatever the cadence says.
              </span>
            </div>

            <hr className="rule" />

            <div className="field">
              <span className="eyebrow">Who gets the second call of the day</span>
              <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
                <button
                  className={`btn btn-sm ${second.length ? 'btn-ghost' : 'btn-primary'}`}
                  onClick={() => patch({ second_call_dispositions: [] })}
                >
                  Everyone in F5 / E0 / F6 / M0
                </button>
                {[...new Set(draft.auto_dispositions)].sort().map((slug) => (
                  <button
                    key={slug}
                    className={`btn btn-sm ${second.includes(slug) ? 'btn-primary' : 'btn-ghost'}`}
                    onClick={() =>
                      patch({
                        second_call_dispositions: second.includes(slug)
                          ? second.filter((s) => s !== slug)
                          : [...second, slug],
                      })
                    }
                  >
                    {dispLabel(slug)}
                  </button>
                ))}
              </div>
              <span className="field-hint">
                {second.length
                  ? 'Only these dispositions earn the afternoon call. Read at PLAN time, so it ' +
                    'reflects the LAST call’s outcome — to gate on this morning’s ' +
                    'result, sync dispositions after the first wave and plan the afternoon as its ' +
                    'own run.'
                  : 'Every lead in the twice-a-day buckets gets both calls. Pick dispositions to ' +
                    'give the afternoon call only to leads that were not reached.'}
              </span>
            </div>
          </Card>
        </div>
      </div>

      <div className="split-3-2">
        <Card
          title="Dispositions in the auto-run"
          eyebrow={`${draft.auto_dispositions.length} selected`}
          flush
        >
          <div className="card-body grid" style={{ gap: 12 }}>
            {CLASS_ORDER.map((cls) => {
              const slugs = DISPOSITIONS.filter((d) => d.cls === cls);
              if (!slugs.length) return null;
              const meta = CLASS_META[cls];
              return (
                <div className="classgroup" key={cls}>
                  <div className="classgroup-head">
                    <h4>{meta.label}</h4>
                    {!meta.autoEligible && (
                      <span className="badge">
                        <Lock size={10} /> not auto-dialable
                      </span>
                    )}
                    <span className="classgroup-note">{meta.blurb}</span>
                  </div>
                  <div
                    className="classgroup-body"
                    style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(232px, 1fr))' }}
                  >
                    {slugs.map((d) => (
                      <Toggle
                        key={d.slug || '(blank)'}
                        on={autoSet.has(d.slug)}
                        disabled={!meta.autoEligible}
                        lockReason={
                          cls === 'excluded'
                            ? 'Rejected server-side. Regulatory (TRAI/NCPR) or already renewed.'
                            : `${meta.label}: ${meta.blurb}`
                        }
                        onChange={(v) => toggleDisp(d.slug, v)}
                        label={
                          <>
                            {d.slug === '' ? '(blank)' : d.slug}
                            {d.note && (
                              <span style={{ color: 'var(--faint)' }}> · {d.note}</span>
                            )}
                          </>
                        }
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <div className="grid" style={{ gap: 14, alignContent: 'start' }}>
          <Card title="Bucket priority" eyebrow="top dials first">
            <div className="reorder">
              {draft.bucket_priority.map((b, i) => (
                <div className="reorder-item" key={b}>
                  <span className="reorder-rank mono">{i + 1}</span>
                  <span className="chip-dot" style={{ background: bucketColor(b) }} />
                  <b className="mono" style={{ color: bucketColor(b) }}>{b}</b>
                  <span className="cell-dim" style={{ fontSize: 11.5 }}>
                    {draft.frequency_table.find((f) => f.bucket === b)?.label ??
                      (b === 'M0' ? 'Mandatory (RED-1 / RED)' : 'Disposition callback')}
                  </span>
                  <span className="reorder-btns">
                    <button className="icon-btn" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move ${b} up`}>
                      <ArrowUp />
                    </button>
                    <button
                      className="icon-btn"
                      disabled={i === draft.bucket_priority.length - 1}
                      onClick={() => move(i, 1)}
                      aria-label={`Move ${b} down`}
                    >
                      <ArrowDown />
                    </button>
                  </span>
                </div>
              ))}
            </div>
            <div className="infobox" style={{ marginTop: 12 }}>
              <Info />
              <span>
                When <span className="mono">max per run</span> bites, the lowest-priority buckets are
                dropped first.
              </span>
            </div>
          </Card>

          <Card title="Version history" eyebrow="newest first" flush>
            <div className="table-wrap">
              <table className="t">
                <thead>
                  <tr>
                    <th className="n">Version</th>
                    <th>Created</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(history.data ?? []).map((v) => (
                    <tr key={v.version}>
                      <td className="n" style={{ fontWeight: 600 }}>v{v.version}</td>
                      <td className="mono cell-dim">{v.created_at.replace('T', ' ')}</td>
                      <td>
                        {v.version === loaded.data?.version && (
                          <span className="badge badge-accent">in use</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      </div>

      <Card
        title="Dispositions per bucket"
        eyebrow={
          narrowedBuckets(draft).length
            ? `${narrowedBuckets(draft).length} bucket${
                narrowedBuckets(draft).length === 1 ? '' : 's'
              } narrowed`
            : 'all inheriting'
        }
        flush
      >
        <BucketDispositions
          draft={draft}
          counts={buckets.data}
          focusBucket={focusBucket}
          onChange={(bucket_dispositions) => patch({ bucket_dispositions })}
        />
      </Card>
    </div>
  );
}

function NumIn({
  v,
  onChange,
  min,
  max,
  step,
}: {
  v: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <input
      className="input"
      type="number"
      style={{ width: 76, textAlign: 'right' }}
      value={v}
      min={min}
      max={max}
      step={step}
      onChange={(e) => onChange(Number(e.target.value))}
    />
  );
}

function Num({
  label,
  hint,
  v,
  onChange,
  min,
  max,
  step,
}: {
  label: string;
  hint: string;
  v: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <label className="field">
      <span className="eyebrow">{label}</span>
      <NumIn v={v} onChange={onChange} min={min} max={max} step={step} />
      <span className="field-hint">{hint}</span>
    </label>
  );
}

/** The window drawn against the legal 09:00–19:00 band, so an out-of-range
 *  edit is visible before the server rejects it. */
function WindowBar({ start, end }: { start: string; end: string }) {
  const day = [8 * 60, 20 * 60];
  const pct = (m: number) => ((m - day[0]) / (day[1] - day[0])) * 100;
  const toMin = (s: string) => {
    const [h, m] = s.split(':').map(Number);
    return (h || 0) * 60 + (m || 0);
  };
  const s = toMin(start);
  const e = toMin(end);
  return (
    <div>
      <div
        style={{
          position: 'relative',
          height: 22,
          background: 'var(--bg-deep)',
          border: '1px solid var(--line-soft)',
          borderRadius: 3,
        }}
      >
        <div
          style={{
            position: 'absolute',
            left: `${pct(9 * 60)}%`,
            width: `${pct(19 * 60) - pct(9 * 60)}%`,
            top: 0,
            bottom: 0,
            background: 'rgba(255,255,255,0.03)',
            borderLeft: '1px dashed var(--line)',
            borderRight: '1px dashed var(--line)',
          }}
        />
        <div
          style={{
            position: 'absolute',
            left: `${Math.max(0, Math.min(100, pct(s)))}%`,
            width: `${Math.max(1, Math.min(100, pct(e)) - Math.max(0, pct(s)))}%`,
            top: 3,
            bottom: 3,
            background: 'var(--accent-wash)',
            border: '1px solid var(--accent-dim)',
            borderRadius: 2,
          }}
        />
      </div>
      <div className="row" style={{ justifyContent: 'space-between', marginTop: 4 }}>
        <span className="eyebrow">08:00</span>
        <span className="eyebrow">09:00 legal floor</span>
        <span className="eyebrow">19:00 ceiling</span>
        <span className="eyebrow">20:00</span>
      </div>
    </div>
  );
}

function span(a: string, b: string) {
  const [ah, am] = a.split(':').map(Number);
  const [bh, bm] = b.split(':').map(Number);
  const mins = bh * 60 + bm - (ah * 60 + am);
  if (mins <= 0) return '—';
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`;
}
