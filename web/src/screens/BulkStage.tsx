import { useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, CalendarClock, Check, Eye, FileUp, Info, Loader2, PenLine, ShieldCheck, Trash2,
} from 'lucide-react';
import { api } from '../lib/api';
import { CLASS_META, CLASS_ORDER, DISPOSITIONS, n, stageEffect, today } from '../lib/domain';
import { useAsync, useStore } from '../lib/store';
import { Card, Empty, Fact, Modal, TypeToConfirm } from '../components/ui';
import type { StagePreview } from '../lib/types';

/* Three modes, one screen — bulk edits on a policy list: a stage, a renewal
   date, or a RED-before sweep. Same preview/commit dance, same job history,
   same warnings. */

const EXTRA_STAGES = ['policy_expired'];

type Mode = 'policies' | 'red' | 'expired';

export function BulkStage() {
  const [mode, setMode] = useState<Mode>('policies');
  const jobs = useAsync(() => api.stageJobs(), []);

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">Lead data</span>
          <h1>Bulk lead edits</h1>
          <p>
            Move many leads to a stage, or correct their renewal date, at once. Preview shows exactly
            what would change; nothing is written until you commit.
          </p>
        </div>
      </div>

      <div className="seg" style={{ maxWidth: 780 }}>
        <button
          className={`seg-btn${mode === 'policies' ? ' is-active' : ''}`}
          onClick={() => setMode('policies')}
        >
          Set stage
          <small>paste a policy list, pick any target</small>
        </button>
        <button
          className={`seg-btn${mode === 'red' ? ' is-active' : ''}`}
          onClick={() => setMode('red')}
        >
          Set renewal date
          <small>correct the RED a policy list is scheduled from</small>
        </button>
        <button
          className={`seg-btn${mode === 'expired' ? ' is-active' : ''}`}
          onClick={() => setMode('expired')}
        >
          Expire by renewal date
          <small>sweep everything past a cutoff</small>
        </button>
      </div>

      {mode === 'policies' && <PoliciesMode onDone={jobs.reload} />}
      {mode === 'red' && <RedMode onDone={jobs.reload} />}
      {mode === 'expired' && <ExpiredMode onDone={jobs.reload} />}

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
                    <td className="n">{j.mode === 'commit' ? n(j.committed) : `~${n(j.would_change)}`}</td>
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
  // Empty is "every campaign the policy is in" — the ported behaviour, and the
  // right default: a renewed policy is renewed wherever it was loaded.
  const [ids, setIds] = useState<number[]>([]);
  const [preview, setPreview] = useState<StagePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const policies = useMemo(() => parsePolicies(raw), [raw]);

  const run = async (commit: boolean) => {
    setBusy(true);
    try {
      const body = { policies, target_stage: target, campaign_ids: ids };
      if (commit) {
        const res = await api.policiesCommit(body);
        toast('ok', `${n(res.applied)} leads moved to ${target}${res.dry_run ? ' (dry run — nothing written)' : ''}.`);
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

  return (
    <div className="split">
      <Card title="Policy numbers" eyebrow={`${n(policies.length)} unique`}>
        <PolicyInput raw={raw} onChange={(next) => { setRaw(next); setPreview(null); }} />

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

        <CampaignChips
          value={ids}
          onChange={(next) => { setIds(next); setPreview(null); }}
          hint={
            ids.length === 0
              ? 'None picked — every campaign the policy appears in is moved. Pick some to narrow it.'
              : `Only the copy in ${ids.length} campaign(s) is moved. A policy that is not in them ` +
                'is reported as not found.'
          }
        />

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
                  {preview.sample.map((s, i) => (
                    <div key={s.lead_id ?? i}>
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
            <Fact k="Campaigns" v={ids.length === 0 ? 'all' : ids.length} />
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

/* --- Mode 2: correct the renewal expiry date on a policy list ---------------
   The date the whole schedule is derived from: how many calls a lead gets, how
   close together, and when it stops. Correcting a wrong one moves the customer
   into the right window instead of leaving them in the wrong one.

   It is written locally, because Formi has no endpoint that writes a renewal
   date — the only lead writes it exposes are the stage bulk update and the
   schedule call. So this decides what THIS console dials from and not what the
   agent reads out on the call. Said in the UI, not just here. */

function RedMode({ onDone }: { onDone: () => void }) {
  const toast = useStore((s) => s.toast);

  const [raw, setRaw] = useState('');
  const [red, setRed] = useState(today());
  // Empty is "every campaign the policy is in", as in the stage sweep: one
  // customer's renewal date is the same date whichever list they were loaded to.
  const [ids, setIds] = useState<number[]>([]);
  const [preview, setPreview] = useState<StagePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const policies = useMemo(() => parsePolicies(raw), [raw]);

  const run = async (commit: boolean) => {
    setBusy(true);
    try {
      const body = { policies, red, campaign_ids: ids };
      if (commit) {
        const res = await api.redCommit(body);
        toast('ok', `${n(res.applied)} leads now renew on ${red}. Formi's own copy is unchanged.`);
        setConfirming(false);
        setPreview(null);
        onDone();
      } else {
        setPreview(await api.redPreview(body));
      }
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="split">
      <Card title="Policy numbers" eyebrow={`${n(policies.length)} unique`}>
        <PolicyInput raw={raw} onChange={(next) => { setRaw(next); setPreview(null); }} />

        <div className="field" style={{ marginTop: 16, maxWidth: 260 }}>
          <span className="eyebrow">New renewal expiry date</span>
          <input
            className="input"
            type="date"
            value={red}
            onChange={(e) => { setRed(e.target.value); setPreview(null); }}
          />
          <span className="field-hint">
            Every lead carrying one of these policies is scheduled from this date instead.
          </span>
        </div>

        <CampaignChips
          value={ids}
          onChange={(next) => { setIds(next); setPreview(null); }}
          hint={
            ids.length === 0
              ? 'None picked — every campaign the policy appears in is corrected. Pick some to narrow it.'
              : `Only the copy in ${ids.length} campaign(s) is corrected.`
          }
        />

        <button
          className="btn btn-primary"
          style={{ marginTop: 16 }}
          disabled={policies.length === 0 || !red || busy}
          onClick={() => run(false)}
        >
          {busy && !confirming ? <Loader2 className="spin" /> : <Eye />} Preview {n(policies.length)} policies
        </button>

        <div className="warnbox" style={{ marginTop: 18 }}>
          <AlertTriangle />
          <span>
            This changes the date <strong>this console schedules from</strong>. Formi has no endpoint
            that writes a renewal date, so its own copy — and what the agent reads out on the call —
            is untouched.
          </span>
        </div>
      </Card>

      <Card title="What would change" eyebrow={preview ? 'preview only' : ''}>
        {preview ? (
          <>
            <div className="confirm-facts">
              <Fact k="Would change" v={n(preview.would_change)} tone="var(--accent)" />
              <Fact k="Already on this date" v={n(preview.unchanged)} />
              <Fact k="New date" v={red} />
            </div>

            <StageBreakdown
              by={preview.by_stage}
              total={preview.would_change}
              tone="var(--accent-dim)"
              title="Renewing on"
            />

            {preview.sample.length > 0 && (
              <>
                <div className="eyebrow" style={{ margin: '14px 0 6px' }}>Sample</div>
                <div className="sample-list">
                  {preview.sample.map((s, i) => (
                    <div key={s.lead_id ?? i}>
                      <span>{s.policy_no}</span>
                      <span style={{ color: 'var(--text-dim)' }}>{s.lead_name}</span>
                      <span>{s.red ?? '—'} → {red}</span>
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
              <PenLine /> Set {n(preview.would_change)} leads to {red}
            </button>
          </>
        ) : (
          <div className="infobox">
            <PenLine />
            <span>
              Paste your policy numbers, choose the date and preview. Leads already on that date are
              counted as unchanged, so re-running writes nothing.
            </span>
          </div>
        )}
      </Card>

      {confirming && preview && (
        <Modal
          title="Correct renewal date"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setConfirming(false)}>Cancel</button>
              <button className="btn btn-primary" disabled={busy} onClick={() => run(true)}>
                {busy ? <Loader2 className="spin" /> : <Check />} Correct {n(preview.would_change)} leads
              </button>
            </>
          }
        >
          <div className="confirm-facts">
            <Fact k="Policies submitted" v={n(policies.length)} />
            <Fact k="Leads changed" v={n(preview.would_change)} tone="var(--accent)" />
            <Fact k="New renewal date" v={red} />
            <Fact k="Campaigns" v={ids.length === 0 ? 'all' : ids.length} />
          </div>
          <div className="infobox">
            <Info />
            <span>
              The renewal date decides how many calls a lead gets and when they stop, so this
              reschedules them from the next run onward. It is kept across syncs — until the
              warehouse itself reports a different date, and then the warehouse wins.
            </span>
          </div>
          <div className="warnbox">
            <AlertTriangle />
            <span>
              Local only. Formi's copy is unchanged, so the agent still reads out the old date on the
              call. Dry run does not apply — nothing here is sent to Formi.
            </span>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* --- Mode 3: sweep every lead whose RED is before a cutoff ------------------ */

const TARGET_EXPIRED = 'policy_expired';

function ExpiredMode({ onDone }: { onDone: () => void }) {
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
        toast('ok', `${n(res.applied)} leads marked policy_expired${res.dry_run ? ' (dry run — nothing written)' : ''}.`);
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

        <CampaignChips
          value={ids}
          onChange={(next) => { setIds(next); setPreview(null); }}
          hint="Only leads in these campaigns are swept. At least one is required."
        />

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
                  {preview.sample.map((s, i) => (
                    <div key={s.lead_id ?? i}>
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

/** A pasted or uploaded list of policy numbers. Used by both list-driven modes. */
function PolicyInput({ raw, onChange }: { raw: string; onChange: (next: string) => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const dupes = useMemo(() => splitOn(raw).length - parsePolicies(raw).length, [raw]);

  return (
    <>
      <div className="field">
        <textarea
          className="input mono"
          rows={12}
          placeholder={'POL3100011\nPOL3100248\nPOL3100517'}
          value={raw}
          onChange={(e) => onChange(e.target.value)}
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
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            const text = await file.text();
            onChange(raw ? `${raw}\n${text}` : text);
            e.target.value = '';          // so the same file can be picked twice
          }}
        />
        <button className="btn btn-ghost" disabled={!raw} onClick={() => onChange('')}>
          <Trash2 /> Clear
        </button>
        <span style={{ flex: 1 }} />
        {dupes > 0 && (
          <span className="badge badge-warn">{n(dupes)} duplicate{dupes === 1 ? '' : 's'} dropped</span>
        )}
      </div>
    </>
  );
}

/** Which campaigns a bulk change may touch.
 *
 *  Scoped to the agent in the rail, because `store.campaigns` already is — the
 *  same scope every other screen works in. `hint` exists because an empty
 *  selection does not mean the same thing in both modes: the policy sweep reads
 *  it as "every campaign", the expired sweep refuses it outright, and the
 *  difference decides how many leads move.
 */
function CampaignChips({
  value,
  onChange,
  hint,
}: {
  value: number[];
  onChange: (ids: number[]) => void;
  hint: string;
}) {
  const campaigns = useStore((s) => s.campaigns);
  return (
    <div className="field" style={{ marginTop: 16 }}>
      <div className="row" style={{ gap: 8 }}>
        <span className="eyebrow" style={{ flex: 1 }}>Campaigns</span>
        <button className="btn btn-sm btn-ghost" onClick={() => onChange(campaigns.map((c) => c.id))}>
          Select all
        </button>
        <button className="btn btn-sm btn-ghost" onClick={() => onChange([])}>
          Clear
        </button>
      </div>
      <div className="row" style={{ flexWrap: 'wrap', gap: 7 }}>
        {campaigns.map((c) => (
          <button
            key={c.id}
            className={`chip${value.includes(c.id) ? ' is-on' : ''}`}
            onClick={() =>
              onChange(value.includes(c.id) ? value.filter((x) => x !== c.id) : [...value, c.id])
            }
          >
            {c.name} · wh {c.warehouse_id}
          </button>
        ))}
      </div>
      <span className="field-hint">{hint}</span>
    </div>
  );
}

function StageBreakdown({ by, total, tone, title = 'Moving from' }:
  { by: Record<string, number>; total: number; tone: string; title?: string }) {
  return (
    <>
      <div className="eyebrow" style={{ margin: '14px 0 8px' }}>{title}</div>
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
