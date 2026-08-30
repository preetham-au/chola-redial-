import { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Eye,
  FlaskConical,
  Loader2,
  PhoneCall,
  Radio,
  ShieldAlert,
} from 'lucide-react';
import { api } from '../lib/api';
import { timeOf } from '../lib/domain';
import { useAsync, useStore } from '../lib/store';
import { Card, Empty, Fact, Modal, TypeToConfirm } from '../components/ui';
import type { TestCallAttempt, TestCallResult, TestNumber } from '../lib/types';

/* One rehearsal call to a known handset, before you approve a run of 1,600.
   The screen is deliberately literal: it shows the resolved lead and the exact
   bytes that would be POSTed. A dry-run trigger is NOT a green tick — under
   DRY_RUN the server makes no network call at all, so it proves lead resolution
   and payload shape and nothing whatsoever about Formi being reachable. */

/* `simulated` is deliberately not green: nothing was dialled. */
const STATUS_BADGE: Record<string, string> = {
  preview: 'badge',
  simulated: 'badge badge-accent',
  posted: 'badge badge-ok',
  failed: 'badge badge-bad',
  not_found: 'badge badge-warn',
};

export function TestNumberTable({
  numbers,
  selected,
  onPick,
}: {
  numbers: TestNumber[];
  selected: string | null;
  onPick: (phone: string) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="t">
        <thead>
          <tr>
            <th>Phone</th>
            <th>Label</th>
            <th>Campaign</th>
            <th>Resolves to a lead</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {numbers.map((t) => (
            <tr key={t.phone} className={t.phone === selected ? 'is-focus' : undefined}>
              <td className="mono">{t.phone}</td>
              <td>{t.label}</td>
              <td className="mono">{t.campaign_id ?? '—'}</td>
              <td>
                {t.found ? (
                  <span className="badge badge-ok">found</span>
                ) : (
                  <span className="badge badge-warn" title="On the allow-list, but no lead carries this number">
                    no lead
                  </span>
                )}
              </td>
              <td style={{ textAlign: 'right' }}>
                <button
                  className={`btn btn-sm${t.phone === selected ? ' btn-primary' : ' btn-ghost'}`}
                  onClick={() => onPick(t.phone)}
                >
                  {t.phone === selected ? 'Selected' : 'Select'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The resolved lead and the literal request. Seeing the payload is the point —
 *  that is what makes this a rehearsal instead of a black box. */
export function TestCallResultView({
  result,
  kind,
  scopeAgentId,
}: {
  result: TestCallResult;
  kind: 'preview' | 'trigger';
  scopeAgentId?: number | null;
}) {
  const { lead, would_post } = result;
  const offScope = lead != null && scopeAgentId != null && lead.agent_id !== scopeAgentId;

  return (
    <div className="grid" style={{ gap: 12 }}>
      <div className="row">
        <span className="eyebrow">{kind === 'preview' ? 'Preview' : 'Trigger result'}</span>
        <span className={STATUS_BADGE[result.status] ?? 'badge'}>{result.status}</span>
        {result.dry_run && <span className="badge badge-accent">dry run</span>}
      </div>

      {!result.found || !lead ? (
        <div className="warnbox">
          <AlertTriangle />
          <span>
            No lead carries this number. The number is on the allow-list, so the server accepted the
            request, but there is nothing to schedule — nothing was sent and nothing was recorded as
            a call.
          </span>
        </div>
      ) : (
        <>
          {offScope && (
            <div className="warnbox">
              <ShieldAlert />
              <span>
                This lead sits on <b>agent {lead.agent_id}</b>, but the console is scoped to{' '}
                <b>agent {scopeAgentId}</b>. The rehearsal will exercise the other agent&apos;s
                pipeline, not the one you are about to approve.
              </span>
            </div>
          )}

          <div className="confirm-facts">
            <Fact k="Lead" v={lead.lead_name ?? '—'} />
            <Fact k="Phone" v={lead.phone} />
            {lead.policy_no && <Fact k="Policy" v={lead.policy_no} />}
            <Fact k="Lead uuid" v={<span className="mono">{lead.lead_uuid}</span>} />
            <Fact k="Campaign" v={lead.campaign_name ? `${lead.campaign_id} · ${lead.campaign_name}` : lead.campaign_id} />
            <Fact k="Agent" v={lead.agent_id} tone={offScope ? 'var(--warn)' : undefined} />
            <Fact k="Stage" v={lead.disposition_class ? `${lead.stage} · ${lead.disposition_class}` : lead.stage} />
          </div>
        </>
      )}

      {would_post && (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            {kind === 'preview' ? 'Would post' : result.dry_run ? 'Would have posted' : 'Posted'}
          </div>
          <pre className="payload">
            <b>POST</b> {would_post.url}
            {'\n'}
            {JSON.stringify(would_post.body, null, 2)}
          </pre>
        </div>
      )}

      {kind === 'trigger' &&
        (result.dry_run ? (
          <div className="infobox">
            <FlaskConical />
            <span>
              <b>Simulated. No call was placed and no phone rang.</b> With{' '}
              <span className="mono">dry_run: true</span> the server made no network call at all — it
              resolved the lead, built the payload above and recorded the attempt. That proves lead
              resolution and payload shape. It proves <b>nothing about connectivity to Formi</b>. Set{' '}
              <span className="mono">DRY_RUN=0</span> on the server if you need to prove the pipe
              itself.
            </span>
          </div>
        ) : result.status === 'posted' ? (
          <div className="infobox">
            <CheckCircle2 />
            <span>
              Posted to Formi and accepted with{' '}
              <span className="mono">{result.http_status ?? '—'}</span>. A real call is now scheduled
              for this number.
            </span>
          </div>
        ) : (
          <div className="warnbox">
            <AlertTriangle />
            <span>
              The POST failed with <span className="mono">{result.http_status ?? 'no status'}</span>.
              The pipeline is reachable enough to answer, but Formi rejected this request — fix that
              before approving a run.
            </span>
          </div>
        ))}

      {kind === 'trigger' && !result.dry_run && (
        <div className="kv">
          <dt>http status</dt>
          <dd>{result.http_status ?? '—'}</dd>
          <dt>response</dt>
          <dd>
            <pre className="payload" style={{ marginTop: 4 }}>
              {result.response ?? '(empty)'}
            </pre>
          </dd>
        </div>
      )}
    </div>
  );
}

export function TriggerConfirm({
  phone,
  live,
  when,
  typed,
  onTyped,
  busy,
  onClose,
  onConfirm,
}: {
  phone: string;
  live: boolean;
  /** The chosen slot, or null when the server picks the next free minute. */
  when?: string | null;
  typed: string;
  onTyped: (v: string) => void;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const ready = !live || typed.trim() === phone;
  return (
    <Modal
      title={live ? `Dial ${phone} for real` : 'Simulate a test call'}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className={live ? 'btn btn-live' : 'btn btn-primary'}
            disabled={!ready || busy}
            onClick={onConfirm}
          >
            {busy ? <Loader2 className="spin" /> : live ? <Radio /> : <FlaskConical />}
            {live ? `Dial ${phone}` : 'Simulate'}
          </button>
        </>
      }
    >
      {live ? (
        <div className="warnbox">
          <Radio />
          <span>
            <b>DRY_RUN is off.</b> This schedules a real outbound call to{' '}
            <span className="mono">{phone}</span>. Somebody&apos;s phone will ring. There is no undo.
          </span>
        </div>
      ) : (
        <div className="infobox">
          <FlaskConical />
          <span>
            The server is in dry run, so this makes <b>no network call</b>. It resolves the lead,
            records the attempt as <span className="mono">simulated</span> and shows you the payload
            it would have sent. Nobody&apos;s phone rings.
          </span>
        </div>
      )}

      <div className="confirm-facts">
        <Fact k="Number" v={<span className="mono">{phone}</span>} tone={live ? 'var(--live)' : undefined} />
        <Fact
          k="Rings at"
          v={when ? <span className="mono">{when.replace('T', ' ')}</span> : 'next free minute'}
        />
        <Fact k="Calls placed" v={live ? '1 real call' : 'none — simulated'} />
      </div>

      {live && (
        <TypeToConfirm
          word={phone}
          value={typed}
          onChange={onTyped}
          hint={`Type ${phone} to confirm you mean to dial this number.`}
        />
      )}
    </Modal>
  );
}

function HistoryRow({ a }: { a: TestCallAttempt }) {
  return (
    <tr>
      <td className="mono">{a.created_at.slice(0, 10)} {timeOf(a.created_at)}</td>
      <td className="mono">{a.phone}</td>
      <td className="mono">{a.agent_id ?? '—'}</td>
      <td>
        <span className={STATUS_BADGE[a.status] ?? 'badge'}>{a.status}</span>
      </td>
      <td className="mono">{a.http_status ?? '—'}</td>
      <td>
        <span className="trunc mono" title={a.response ?? ''}>
          {a.dry_run ? 'no call made' : a.response ?? '—'}
        </span>
      </td>
    </tr>
  );
}

export function TestCall() {
  const health = useStore((s) => s.health);
  const agentId = useStore((s) => s.agentId);
  const toast = useStore((s) => s.toast);
  const live = health ? !health.dry_run : false;

  const numbers = useAsync(() => api.testNumbers(), []);
  const history = useAsync(() => api.testHistory(), []);

  const [phone, setPhone] = useState<string | null>(null);
  const [when, setWhen] = useState('');
  const [preview, setPreview] = useState<TestCallResult | null>(null);
  const [result, setResult] = useState<TestCallResult | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);

  const picked = numbers.data?.find((t) => t.phone === phone) ?? null;
  const campaign_id = picked?.campaign_id ?? undefined;

  // The rehearsal obeys the dial window of the campaign the number resolves to,
  // so the picker's bounds have to come from that campaign, not the console's.
  const config = useAsync(
    () => (campaign_id ? api.config(campaign_id) : Promise.resolve(null)),
    [campaign_id],
  );
  const dialWindow = config.data?.dial_window;

  /** Local "YYYY-MM-DDTHH:MM" for now, rounded up to the next minute. */
  const nowLocal = () => {
    const d = new Date();
    d.setSeconds(0, 0);
    d.setMinutes(d.getMinutes() + 1);
    return `${d.toLocaleDateString('en-CA')}T${d.toTimeString().slice(0, 5)}`;
  };

  // Mirrors the server's `_chosen_slot`: inside the window, never in the past.
  // The server re-checks all of it — this is only so the operator sees why.
  const whenError = !when
    ? ''
    : when < nowLocal()
      ? 'That minute has already passed.'
      : dialWindow && (when.slice(11) < dialWindow.start || when.slice(11) > dialWindow.end)
        ? `Outside the dial window ${dialWindow.start}–${dialWindow.end} of campaign ${campaign_id}.`
        : '';

  const pick = (p: string) => {
    setPhone(p);
    setPreview(null);
    setResult(null);
    setTyped('');
  };

  const runPreview = async () => {
    if (!phone) return;
    setBusy(true);
    setPreview(null);
    try {
      setPreview(await api.testPreview({ phone, campaign_id, scheduled_time: when || undefined }));
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const trigger = async () => {
    if (!phone) return;
    setBusy(true);
    try {
      const res = await api.testTrigger({ phone, campaign_id, scheduled_time: when || undefined });
      setResult(res);
      toast(
        res.dry_run ? 'info' : res.status === 'posted' ? 'ok' : 'bad',
        res.dry_run
          ? `Test call simulated for ${phone}. No network call was made.`
          : res.status === 'posted'
            ? `Test call posted for ${phone} (${res.http_status}).`
            : `Test call failed for ${phone} (${res.http_status ?? 'no status'}).`,
      );
      setConfirming(false);
      history.reload();
    } catch (e) {
      // 422 = not on the server's allow-list. Say which rule bit.
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page grid" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">Dial deliberately</span>
          <h1>Test call</h1>
          <p>
            One rehearsal call to a known handset, so you can see the pipeline move before you
            approve a run of a thousand. Preview shows the exact request; triggering schedules that
            one lead and nothing else.
          </p>
        </div>
        <div className={`dry-badge${live ? ' is-live' : ''}`} style={{ marginLeft: 'auto' }}>
          {live ? <Radio /> : <FlaskConical />}
          <span>{live ? 'LIVE — a trigger dials' : 'DRY RUN — a trigger simulates'}</span>
        </div>
      </div>

      <div className="split-3-2">
        <div className="grid" style={{ gap: 14, alignContent: 'start' }}>
          <Card
            title="Allow-listed numbers"
            eyebrow={numbers.data ? `${numbers.data.length} dialable` : ''}
            flush
          >
            {numbers.loading ? (
              <div className="empty">
                <Loader2 className="spin" />
                <h3>Loading the allow-list</h3>
              </div>
            ) : numbers.error ? (
              <Empty title="Could not read the allow-list" note={numbers.error} />
            ) : numbers.data && numbers.data.length > 0 ? (
              <TestNumberTable numbers={numbers.data} selected={phone} onPick={pick} />
            ) : (
              <Empty
                title="No numbers are allow-listed"
                note="Add one to config.test_numbers on the server. This screen cannot dial a number the server has not allowed."
              />
            )}
          </Card>

          <Card title="Rehearse" eyebrow={phone ?? 'pick a number'}>
            <label className="field">
              <span>
                <Clock className="inline-icon" /> When should it ring
              </span>
              <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                <input
                  type="datetime-local"
                  value={when}
                  disabled={!phone}
                  // Same bounds the server enforces. Any date is fine — rehearsing
                  // tomorrow morning is legitimate — but never a past minute and
                  // never outside dialling hours.
                  min={nowLocal()}
                  onChange={(e) => setWhen(e.target.value)}
                />
                {when && (
                  <button className="btn btn-sm btn-ghost" onClick={() => setWhen('')}>
                    Next free minute
                  </button>
                )}
              </div>
              <p className="field-hint">
                {whenError ? (
                  <span style={{ color: 'var(--warn)' }}>{whenError}</span>
                ) : when ? (
                  <>
                    The call is booked for <span className="mono">{when.replace('T', ' ')}</span>.
                    {dialWindow && ` Dial window ${dialWindow.start}–${dialWindow.end}.`}
                  </>
                ) : (
                  <>
                    Left empty, the server takes the next minute inside the dial window
                    {dialWindow ? ` (${dialWindow.start}–${dialWindow.end})` : ''} — or tomorrow&apos;s
                    opening if it has already shut.
                  </>
                )}
              </p>
            </label>

            <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
              <button className="btn" disabled={!phone || busy || !!whenError} onClick={runPreview}>
                {busy && !confirming ? <Loader2 className="spin" /> : <Eye />} Preview the payload
              </button>
              <button
                className={live ? 'btn btn-live' : 'btn btn-primary'}
                disabled={!phone || busy || !!whenError}
                onClick={() => setConfirming(true)}
              >
                {live ? <Radio /> : <PhoneCall />}
                {live ? `Dial ${phone ?? ''}` : 'Trigger (simulated)'}
              </button>
            </div>

            {!phone && (
              <p className="field-hint" style={{ marginTop: 10 }}>
                Select one of the allow-listed numbers above.
              </p>
            )}
            {picked && !picked.found && (
              <p className="field-hint" style={{ marginTop: 10, color: 'var(--warn)' }}>
                This number is allow-listed but no lead carries it — a trigger will come back{' '}
                <span className="mono">not_found</span> rather than scheduling anything.
              </p>
            )}

            {preview && (
              <div style={{ marginTop: 14 }}>
                <TestCallResultView result={preview} kind="preview" scopeAgentId={agentId} />
              </div>
            )}
          </Card>

          {result && (
            <Card title="Trigger result" eyebrow={phone ?? ''}>
              <TestCallResultView result={result} kind="trigger" scopeAgentId={agentId} />
            </Card>
          )}
        </div>

        <div className="grid" style={{ gap: 14, alignContent: 'start' }}>
          <Card title="What is and is not allowed">
            <div className="grid" style={{ gap: 10 }}>
              <div className="infobox">
                <ShieldAlert />
                <span>
                  <b>Only allow-listed numbers are dialable.</b> The list lives in{' '}
                  <span className="mono">config.test_numbers</span> on the server, not in this
                  screen, and <span className="mono">POST /api/test-call/trigger</span> answers{' '}
                  <span className="mono">422</span> for anything else. That is what stops &ldquo;test
                  call&rdquo; from becoming a button that dials an arbitrary customer.
                </span>
              </div>
              <div className="infobox">
                <FlaskConical />
                <span>
                  A trigger <b>ignores campaign pause</b> — a paused campaign is exactly when you
                  want to rehearse. It never ignores exclusions or the allow-list.
                </span>
              </div>
              {!live && (
                <div className="infobox">
                  <AlertTriangle />
                  <span>
                    While the server is in dry run, a successful test proves the lead resolved and
                    the payload is well-formed. It does <b>not</b> prove Formi is reachable, because
                    no request leaves the server.
                  </span>
                </div>
              )}
            </div>
          </Card>

          {/* The server keeps one history for the whole box, so this list is not
              scoped to the current agent — hence the agent column. */}
          <Card
            title="History"
            eyebrow={history.data ? `${history.data.length} attempts · all agents` : ''}
            flush
          >
            {history.loading ? (
              <div className="empty">
                <Loader2 className="spin" />
                <h3>Loading history</h3>
              </div>
            ) : history.error ? (
              <Empty title="Could not read history" note={history.error} />
            ) : history.data && history.data.length > 0 ? (
              <div className="table-wrap">
                <table className="t">
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Phone</th>
                      <th>Agent</th>
                      <th>Status</th>
                      <th>HTTP</th>
                      <th>Response</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.data.map((a) => (
                      <HistoryRow key={a.id} a={a} />
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title="No test calls yet" note="Trigger one and it lands here." />
            )}
          </Card>
        </div>
      </div>

      {confirming && phone && (
        <TriggerConfirm
          phone={phone}
          live={live}
          when={when || null}
          typed={typed}
          onTyped={setTyped}
          busy={busy}
          onClose={() => setConfirming(false)}
          onConfirm={trigger}
        />
      )}
    </div>
  );
}
