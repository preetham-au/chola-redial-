import { useMemo, useRef, useState } from 'react';
import { CalendarClock, Check, Eye, FileUp, Info, Loader2, ShieldCheck, Trash2 } from 'lucide-react';
import { api } from '../lib/api';
import { CLASS_META, CLASS_ORDER, DISPOSITIONS, n, stageEffect, today } from '../lib/domain';
import { useAsync, useStore } from '../lib/store';
import { Card, Empty, Fact, Modal, TypeToConfirm } from '../components/ui';
import type { StagePreview } from '../lib/types';

/* Two modes, one screen — bulk stage changes on either a policy list or a
   RED-before sweep. Same preview/commit dance, same job history, same warnings. */

const EXTRA_STAGES = ['policy_expired'];

type Mode = 'policies' | 'expired';

export function BulkStage() {
  const [mode, setMode] = useState<Mode>('policies');
  const jobs = useAsync(() => api.stageJobs(), []);

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">Lead data</span>
          <h1>Bulk stage change</h1>
          <p>
            Move many leads to a stage at once. Preview shows exactly what would change; nothing is
            written until you commit.
          </p>
        </div>
      </div>

      <div className="seg" style={{ maxWidth: 520 }}>
        <button
          className={`seg-btn${mode === 'policies' ? ' is-active' : ''}`}
          onClick={() => setMode('policies')}
        >
          By policy number
          <small>paste a list, pick any target</small>
        </button>
        <button
          className={`seg-btn${mode === 'expired' ? ' is-active' : ''}`}
          onClick={() => setMode('expired')}
        >
          By renewal date
          <small>sweep everything past a cutoff</small>
        </button>
      </div>

      {mode === 'policies' ? <PoliciesMode onDone={jobs.reload} /> : <ExpiredMode onDone={jobs.reload} />}

      <Card title="Recent stage jobs" eyebrow="newest first" flush>
        {(jobs.data ?? []).length === 0 ? (
          <Empty title="No stage jobs yet" note="Every preview and commit is recorded here." />
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th className="n">Job</th>
                  <th>Kind</th>
                  <th>Target</th>
                  <th className="n">Changed</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {(jobs.data ?? []).map((j) => (
                  <tr key={j.id}>
                    <td className="n">#{j.id}</td>
                    <td>
                      <span className="badge">{j.kind}</span>{' '}
                      {j.mode === 'preview' && <span className="badge badge-accent">preview</span>}
                    </td>
                    <td className="mono" style={{ fontSize: 11 }}>{j.target_stage}</td>
                    <td className="n">{j.mode === 'commit' ? n(j.changed) : `~${n(j.would_change)}`}</td>
                    <td className="mono cell-dim">{j.created_at.replace('T', ' ').slice(0, 16)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

/* --- Mode 1: paste a list of policy numbers --------------------------------- */

function PoliciesMode({ onDone }: { onDone: () => void }) {
  const toast = useStore((s) => s.toast);
  const health = useStore((s) => s.health);

  const [raw, setRaw] = useState('');
  const [target, setTarget] = useState('renewed');
  const [preview, setPreview] = useState<StagePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const policies = useMemo(() => parsePolicies(raw), [raw]);
  const dupes = useMemo(() => splitOn(raw).length - policies.length, [raw, policies]);

  const run = async (commit: boolean) => {
    setBusy(true);
    try {
      const body = { policies, target_stage: target };
      if (commit) {
        const res = await api.policiesCommit(body);
        toast('ok', `${n(res.changed)} leads moved to ${target}${res.dry_run ? ' (dry run — nothing written)' : ''}.`);
        setConfirming(false);
        setPreview(null);
        onDone();
      } else {
        setPreview(await api.policiesPreview(body));
      }
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (f: File) => {
    const text = await f.text();
    setRaw((prev) => (prev ? `${prev}\n${text}` : text));
    setPreview(null);
  };

  return (
    <div className="split">
      <Card title="Policy numbers" eyebrow={`${n(policies.length)} unique`}>
        <div className="field">
          <textarea
            className="input mono"
            rows={12}
            placeholder={'POL3100011\nPOL3100248\nPOL3100517'}
            value={raw}
            onChange={(e) => { setRaw(e.target.value); setPreview(null); }}
            aria-label="Policy numbers"
          />
          <span className="field-hint">
            One per line, or separated by commas, spaces or tabs. Pasting a spreadsheet column works.
          </span>
        </div>

        <div className="row" style={{ marginTop: 12, flexWrap: 'wrap' }}>
          <button className="btn btn-ghost" onClick={() => fileRef.current?.click()}>
            <FileUp /> Upload file
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".txt,.csv,text/plain,text/csv"
            hidden
            onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
          />
          <button className="btn btn-ghost" disabled={!raw} onClick={() => { setRaw(''); setPreview(null); }}>
            <Trash2 /> Clear
          </button>
          <span style={{ flex: 1 }} />
          {dupes > 0 && (
            <span className="badge badge-warn">{n(dupes)} duplicate{dupes === 1 ? '' : 's'} dropped</span>
          )}
        </div>

        <div className="field" style={{ marginTop: 16 }}>
          <span className="eyebrow">Target stage</span>
          <select
            className="select"
            value={target}
            onChange={(e) => { setTarget(e.target.value); setPreview(null); }}
            style={{ maxWidth: 340 }}
          >
            {EXTRA_STAGES.map((s) => <option key={s} value={s}>{s}</option>)}
            {CLASS_ORDER.map((cls) => (
              <optgroup key={cls} label={CLASS_META[cls].label}>
                {DISPOSITIONS.filter((d) => d.cls === cls && d.slug).map((d) => (
                  <option key={d.slug} value={d.slug}>{d.slug}</option>
                ))}
              </optgroup>
            ))}
          </select>
          <span className="field-hint">{stageEffect(target)}</span>
        </div>

        <button
          className="btn btn-primary"
          style={{ marginTop: 16 }}
          disabled={policies.length === 0 || busy}
          onClick={() => run(false)}
        >
          {busy && !confirming ? <Loader2 className="spin" /> : <Eye />} Preview {n(policies.length)} policies
        </button>
      </Card>

      <Card title="What would change" eyebrow={preview ? 'preview only' : ''}>
        {preview ? (
          <>
            <div className="confirm-facts">
              <Fact k="Would change" v={n(preview.would_change)} tone="var(--accent)" />
              <Fact k="Already on this stage" v={n(preview.unchanged)} />
              <Fact k="Not found" v={n(Math.max(0, policies.length - preview.would_change - preview.unchanged))} />
            </div>

            <StageBreakdown by={preview.by_stage} total={preview.would_change} tone="var(--accent-dim)" />

            {preview.sample.length > 0 && (
              <>
                <div className="eyebrow" style={{ margin: '14px 0 6px' }}>Sample</div>
                <div className="sample-list">
                  {preview.sample.map((s) => (
                    <div key={s.policy_no}>
                      <span>{s.policy_no}</span>
                      <span style={{ color: 'var(--text-dim)' }}>{s.lead_name}</span>
                      <span>{s.stage} → {target}</span>
                    </div>
                  ))}
                </div>
              </>
            )}

            <button
              className="btn btn-primary"
              style={{ marginTop: 16, width: '100%', justifyContent: 'center' }}
              disabled={preview.would_change === 0}
              onClick={() => setConfirming(true)}
            >
              <Check /> Move {n(preview.would_change)} leads to {target}
            </button>
          </>
        ) : (
          <div className="infobox">
            <Info />
            <span>
              Paste your policy numbers and preview. Nothing is written until you commit, and the
              preview counts come from the same query the commit uses.
            </span>
          </div>
        )}
      </Card>

      {confirming && preview && (
        <Modal
          title="Commit stage update"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setConfirming(false)}>Cancel</button>
              <button className="btn btn-primary" disabled={busy} onClick={() => run(true)}>
                {busy ? <Loader2 className="spin" /> : <Check />} Commit {n(preview.would_change)} changes
              </button>
            </>
          }
        >
          <div className="confirm-facts">
            <Fact k="Policies submitted" v={n(policies.length)} />
            <Fact k="Leads changed" v={n(preview.would_change)} tone="var(--accent)" />
            <Fact k="Target stage" v={target} />
          </div>
          <div className="infobox">
            <Info />
            <span>
              A stage change decides whether a lead is dialled at all. Moving leads to an excluded stage
              takes them out of every future run
              {health?.dry_run ? '. The server is in dry run, so this is recorded but not written' : ''}.
            </span>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* --- Mode 2: sweep every lead whose RED is before a cutoff ------------------ */

const TARGET_EXPIRED = 'policy_expired';

function ExpiredMode({ onDone }: { onDone: () => void }) {
  const campaigns = useStore((s) => s.campaigns);
  const campaignId = useStore((s) => s.campaignId)!;
  const toast = useStore((s) => s.toast);
  const health = useStore((s) => s.health);

  const [redBefore, setRedBefore] = useState(today());
  const [ids, setIds] = useState<number[]>([campaignId]);
  const [preview, setPreview] = useState<StagePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState('');

  const body = { campaign_ids: ids, red_before: redBefore, target_stage: TARGET_EXPIRED };

  const run = async (commit: boolean) => {
    setBusy(true);
    try {
      if (commit) {
        const res = await api.expiredCommit(body);
        toast('ok', `${n(res.changed)} leads marked policy_expired${res.dry_run ? ' (dry run — nothing written)' : ''}.`);
        setConfirming(false);
        setTyped('');
        setPreview(null);
        onDone();
      } else {
        setPreview(await api.expiredPreview(body));
      }
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="split">
      <Card title="Selection">
        <div className="field" style={{ maxWidth: 260 }}>
          <span className="eyebrow">Renewal expiry date is before</span>
          <input
            className="input"
            type="date"
            value={redBefore}
            onChange={(e) => { setRedBefore(e.target.value); setPreview(null); }}
          />
          <span className="field-hint">
            Leads whose renewal expiry date falls strictly before this day.
          </span>
        </div>

        <div className="field" style={{ marginTop: 16 }}>
          <span className="eyebrow">Campaigns</span>
          <div className="row" style={{ flexWrap: 'wrap', gap: 7 }}>
            {campaigns.map((c) => (
              <button
                key={c.id}
                className={`chip${ids.includes(c.id) ? ' is-on' : ''}`}
                onClick={() => {
                  setIds(ids.includes(c.id) ? ids.filter((x) => x !== c.id) : [...ids, c.id]);
                  setPreview(null);
                }}
              >
                {c.name} · wh {c.warehouse_id}
              </button>
            ))}
          </div>
        </div>

        <button
          className="btn btn-primary"
          style={{ marginTop: 18 }}
          disabled={ids.length === 0 || busy}
          onClick={() => run(false)}
        >
          {busy && !confirming ? <Loader2 className="spin" /> : <Eye />} Preview
        </button>

        <div className="infobox" style={{ marginTop: 18 }}>
          <ShieldCheck />
          <span>
            Renewed and already-paid leads are left untouched by the server, whatever their date says.
            Target stage: <span className="mono">policy_expired</span>.
          </span>
        </div>
      </Card>

      <Card title="What would change" eyebrow={preview ? 'preview only' : ''}>
        {preview ? (
          <>
            <div className="confirm-facts">
              <Fact k="Would be marked expired" v={n(preview.would_change)} tone="var(--b-F5)" />
              <Fact k="Protected or already expired" v={n(preview.unchanged)} />
              <Fact k="Cutoff" v={redBefore} />
            </div>

            <StageBreakdown by={preview.by_stage} total={preview.would_change} tone="#8a5a3c" />

            {preview.sample.length > 0 && (
              <>
                <div className="eyebrow" style={{ margin: '14px 0 6px' }}>Sample</div>
                <div className="sample-list">
                  {preview.sample.map((s) => (
                    <div key={s.policy_no}>
                      <span>{s.policy_no}</span>
                      <span style={{ color: 'var(--text-dim)' }}>{s.lead_name}</span>
                      <span>Renewal expiry {s.red ?? '—'}</span>
                    </div>
                  ))}
                </div>
              </>
            )}

            <button
              className="btn btn-primary"
              style={{ marginTop: 16, width: '100%', justifyContent: 'center' }}
              disabled={preview.would_change === 0}
              onClick={() => setConfirming(true)}
            >
              <CalendarClock /> Mark {n(preview.would_change)} leads expired
            </button>
          </>
        ) : (
          <div className="infobox">
            <CalendarClock />
            <span>
              Choose a cutoff and preview. The count you see is the count that gets written — the
              preview and the commit run the same query.
            </span>
          </div>
        )}
      </Card>

      {confirming && preview && (
        <Modal
          title="Mark leads expired"
          onClose={() => { setConfirming(false); setTyped(''); }}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => { setConfirming(false); setTyped(''); }}>Cancel</button>
              <button
                className="btn btn-primary"
                disabled={busy || typed.trim().toUpperCase() !== 'EXPIRE'}
                onClick={() => run(true)}
              >
                {busy ? <Loader2 className="spin" /> : <Check />} Mark {n(preview.would_change)} expired
              </button>
            </>
          }
        >
          <div className="confirm-facts">
            <Fact k="Leads affected" v={n(preview.would_change)} tone="var(--b-F5)" />
            <Fact k="Cutoff" v={redBefore} />
            <Fact k="Campaigns" v={ids.length} />
            <Fact k="Target stage" v={TARGET_EXPIRED} />
          </div>
          <div className="infobox">
            <ShieldCheck />
            <span>
              Renewed and paid leads are excluded by the server, not by this screen
              {health?.dry_run ? '. The server is in dry run, so this is recorded but not written' : ''}.
            </span>
          </div>
          <TypeToConfirm
            word="EXPIRE"
            value={typed}
            onChange={setTyped}
            hint="Type EXPIRE to confirm. These leads stop being dialled."
          />
        </Modal>
      )}
    </div>
  );
}

/* --- shared bits ------------------------------------------------------------- */

function StageBreakdown({ by, total, tone }: { by: Record<string, number>; total: number; tone: string }) {
  return (
    <>
      <div className="eyebrow" style={{ margin: '14px 0 8px' }}>Moving from</div>
      <div className="skips">
        {Object.entries(by).sort((a, b) => b[1] - a[1]).map(([stage, v]) => (
          <div className="skip-row" key={stage} style={{ gridTemplateColumns: '190px 1fr 56px' }}>
            <span className="skip-name">{stage}</span>
            <span className="skip-bar">
              <span style={{ width: `${(v / Math.max(1, total)) * 100}%`, background: tone }} />
            </span>
            <span className="skip-n">{n(v)}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function splitOn(raw: string) {
  return raw.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
}

function parsePolicies(raw: string) {
  return [...new Set(splitOn(raw))];
}
