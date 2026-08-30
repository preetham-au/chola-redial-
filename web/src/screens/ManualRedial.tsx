import { useMemo, useState } from 'react';
import { CalendarPlus, Eye, Loader2, Lock, PhoneOutgoing, ShieldAlert } from 'lucide-react';
import { api } from '../lib/api';
import {
  bucketColor,
  BUCKET_NOTE,
  BUCKET_ORDER,
  CLASS_META,
  CLASS_ORDER,
  DISPOSITIONS,
  isIntensive,
  n,
  timeOf,
} from '../lib/domain';
import { navigate, useAsync, useStore } from '../lib/store';
import { BucketTag, Card, Fact, Modal, Toggle } from '../components/ui';
import type { ManualPreview } from '../lib/types';

export function ManualRedial() {
  const campaignId = useStore((s) => s.campaignId)!;
  const date = useStore((s) => s.date);
  const toast = useStore((s) => s.toast);
  const health = useStore((s) => s.health);

  const buckets = useAsync(() => api.buckets(campaignId, date), [campaignId, date]);

  const [dispositions, setDispositions] = useState<string[]>(['positive_followup', 'payment_link_sent']);
  const [picked, setPicked] = useState<string[]>(['F5', 'F6', 'M0']);
  const [preview, setPreview] = useState<ManualPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const counts = useMemo(() => {
    const m = new Map<string, number>();
    buckets.data?.matrix.forEach((c) => m.set(`${c.bucket}|${c.disposition}`, c.count));
    return m;
  }, [buckets.data]);

  const inWarehouse = (slug: string) =>
    picked.reduce((a, b) => a + (counts.get(`${b}|${slug}`) ?? 0), 0);

  const ready = dispositions.length > 0 && picked.length > 0;

  const runPreview = async () => {
    setBusy(true);
    setPreview(null);
    try {
      setPreview(await api.manualPreview({ campaign_id: campaignId, dispositions, buckets: picked, date }));
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const schedule = async () => {
    setBusy(true);
    try {
      const run = await api.manualSchedule({
        campaign_id: campaignId,
        dispositions,
        buckets: picked,
        date,
      });
      toast('ok', `Manual run ${run.id} scheduled with ${n(run.counts.slots)} slots. Approve it to dial.`);
      setConfirming(false);
      navigate(`plan/${run.id}`);
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toggleDisp = (slug: string, on: boolean) =>
    setDispositions(on ? [...dispositions, slug] : dispositions.filter((s) => s !== slug));

  return (
    <div className="page grid" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">Dial deliberately</span>
          <h1>Manual redial</h1>
          <p>
            Reach leads the auto-run leaves alone — the ones who picked up and gave you a date.
            Preview the count first; scheduling creates a run you still have to approve.
          </p>
        </div>
      </div>

      <div className="split-3-2">
        <Card title="Dispositions" eyebrow={`${dispositions.length} selected`} flush>
          <div className="card-body grid" style={{ gap: 12 }}>
            {CLASS_ORDER.map((cls) => {
              const slugs = DISPOSITIONS.filter((d) => d.cls === cls);
              if (!slugs.length) return null;
              const meta = CLASS_META[cls];
              const locked = !meta.manualEligible;
              return (
                <div className="classgroup" key={cls}>
                  <div className="classgroup-head">
                    <h4>{meta.label}</h4>
                    {locked && (
                      <span className="badge badge-bad">
                        <ShieldAlert size={10} /> rejected server-side
                      </span>
                    )}
                    <span className="classgroup-note">{meta.blurb}</span>
                  </div>
                  <div
                    className="classgroup-body"
                    style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))' }}
                  >
                    {slugs.map((d) => {
                      const inWh = inWarehouse(d.slug);
                      return (
                        <Toggle
                          key={d.slug || '(blank)'}
                          on={dispositions.includes(d.slug)}
                          disabled={locked}
                          lockReason={
                            locked
                              ? 'Excluded class. The server rejects these even if the UI asks — TRAI/NCPR, or the policy is already renewed.'
                              : undefined
                          }
                          onChange={(v) => toggleDisp(d.slug, v)}
                          label={
                            <>
                              {d.slug === '' ? '(blank)' : d.slug}
                              {inWh > 0 && (
                                <span className="mono" style={{ color: 'var(--faint)' }}> · {n(inWh)}</span>
                              )}
                            </>
                          }
                        />
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        <div className="grid" style={{ gap: 14, alignContent: 'start' }}>
          <Card title="Buckets" eyebrow={`${picked.length} selected`}>
            <div className="row" style={{ flexWrap: 'wrap', gap: 7 }}>
              {BUCKET_ORDER.map((b) => (
                <button
                  key={b}
                  className={`chip${picked.includes(b) ? ' is-on' : ''}`}
                  title={BUCKET_NOTE[b]}
                  onClick={() =>
                    setPicked(picked.includes(b) ? picked.filter((x) => x !== b) : [...picked, b])
                  }
                >
                  <span className="chip-dot" style={{ background: bucketColor(b) }} />
                  {b}
                  {isIntensive(b) && ' ▲'}
                </button>
              ))}
            </div>
            <div className="row" style={{ gap: 8, marginTop: 10 }}>
              <button className="btn btn-sm btn-ghost" onClick={() => setPicked([...BUCKET_ORDER])}>
                Select all
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => setPicked(['F5', 'F6', 'M0'])}>
                Urgent only
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => setPicked([])}>
                Clear
              </button>
            </div>
          </Card>

          <Card title="Preview" eyebrow={`for ${date}`}>
            <button className="btn btn-primary" disabled={!ready || busy} onClick={runPreview}>
              {busy && !confirming ? <Loader2 className="spin" /> : <Eye />} Preview the count
            </button>

            {!ready && (
              <p className="field-hint" style={{ marginTop: 10 }}>
                Pick at least one disposition and one bucket.
              </p>
            )}

            {preview && (
              <>
                <div className="confirm-facts" style={{ marginTop: 14 }}>
                  <Fact k="Leads matched" v={n(preview.count)} />
                  <Fact k="Call slots" v={n(preview.slots)} />
                  <Fact
                    k="Urgent buckets included"
                    v={picked.filter(isIntensive).join(' ') || 'none'}
                    tone={picked.some(isIntensive) ? 'var(--b-F5)' : undefined}
                  />
                </div>

                {!!preview.already_scheduled && (
                  <p className="warnbox">
                    <b>{n(preview.already_scheduled)}</b> of these leads already have a call
                    booked in Formi for this date. The manual screen overrides the
                    no-double-book rule on purpose — scheduling here calls them twice.
                  </p>
                )}

                {preview.sample.length > 0 && (
                  <>
                    <div className="eyebrow" style={{ margin: '12px 0 6px' }}>Sample</div>
                    <div className="sample-list">
                      {/* one lead can hold two slots, so uuid alone is not unique */}
                      {preview.sample.map((s, i) => (
                        <div key={`${s.lead_uuid}-${i}`}>
                          <span>{s.policy_no ?? '—'}</span>
                          <span style={{ color: 'var(--text-dim)' }}>{s.lead_name}</span>
                          <span>{s.bucket} · {timeOf(s.scheduled_time)}</span>
                        </div>
                      ))}
                    </div>
                  </>
                )}

                <button
                  className="btn btn-primary"
                  style={{ marginTop: 14, width: '100%', justifyContent: 'center' }}
                  disabled={preview.count === 0}
                  onClick={() => setConfirming(true)}
                >
                  <CalendarPlus /> Schedule {n(preview.slots)} calls
                </button>
              </>
            )}
          </Card>

          <Card title="What manual mode can and cannot do">
            <div className="infobox">
              <Lock />
              <span>
                Manual mode ignores the auto-run allow-list — that is the point. It never ignores
                exclusions. <span className="mono">do_not_call</span>, <span className="mono">dnc</span>,{' '}
                <span className="mono">wrong_number</span> and{' '}
                <span className="mono">number_not_working</span> are rejected by the server whatever
                this screen sends. That is regulatory, not a preference.
              </span>
            </div>
          </Card>
        </div>
      </div>

      {confirming && preview && (
        <Modal
          title="Schedule a manual run"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button className="btn btn-primary" disabled={busy} onClick={schedule}>
                {busy ? <Loader2 className="spin" /> : <PhoneOutgoing />} Schedule run
              </button>
            </>
          }
        >
          <div className="infobox">
            <CalendarPlus />
            <span>
              This creates a run in <span className="mono">planned</span> status. Nothing is dialled until
              you approve it on the next screen
              {health && health.dry_run ? ', and the server is in dry run' : ''}.
            </span>
          </div>
          <div className="confirm-facts">
            <Fact k="Date" v={date} />
            <Fact k="Dispositions" v={dispositions.length} />
            <Fact k="Buckets" v={picked.join(' ')} />
            <Fact k="Leads" v={n(preview.count)} />
            <Fact k="Call slots" v={n(preview.slots)} />
          </div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
            {picked.map((b) => (
              <BucketTag key={b} bucket={b} />
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}
