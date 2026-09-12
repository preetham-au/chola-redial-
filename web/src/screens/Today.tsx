/** The day. One screen, one decision.
 *
 *  Nothing in this console dials on its own. A pass prepares a plan each morning
 *  and afternoon and leaves it waiting; this screen is where an operator reads
 *  what is ready and approves it. If nobody approves, no call goes out.
 *
 *  The order is the client's, not the bucket order: the three days just PAST
 *  expiry first (their "1 to 3"), then the week running up to it (their "-7 to
 *  0"). That is what decides who survives when the day is approved with few
 *  hours left, so it is the first thing on the page — above the buckets, which
 *  follow it.
 */
import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CircleSlash,
  ClipboardList,
  Eye,
  EyeOff,
  FlaskConical,
  Info,
  ListChecks,
  Loader2,
  PhoneCall,
  Radio,
  RefreshCw,
} from 'lucide-react';
import { api } from '../lib/api';
import { bandRange, bucketColor, friendlyBucket, n } from '../lib/domain';
import { navigate, useAsync, useStore } from '../lib/store';
import { Card, Empty, Fact, Modal, TypeToConfirm } from '../components/ui';
import type { ApproveResult, Campaign, DayBucket, DayCampaign, DayView } from '../lib/types';

const WAVES = [
  { kind: 'auto', label: 'Morning' },
  { kind: 'auto_pm', label: 'Afternoon' },
];

/** What goes on the wire. The backend reads an empty list as "every bucket", so
 *  a partial tick MUST be sent verbatim — sending [] after unticking one bucket
 *  would dial the ones the operator just excluded. */
export const wireBuckets = (chosen: string[], all: string[]) =>
  chosen.length === all.length ? [] : chosen;

/** A campaign the server will accept into the daily plan. Disabled and hidden
 *  are both refused with a 409, so they are shown and not offered rather than
 *  failing on save. */
export const pickable = (c: Campaign) => c.enabled !== false && !c.hidden;

/** How to name the moment dialling stops, in a sentence reading "before …".
 *
 *  Each campaign carries its own dial window, so a single close time is only
 *  honest when they all share one. When they do not, `day.window` is the
 *  envelope across them and no campaign necessarily shuts at its end. */
export const closesAt = (day: DayView) =>
  day.window_varies ? 'their campaigns close' : day.window.end;

/** Which campaigns to arm and which to disarm — the only thing this screen puts
 *  on the wire that changes who gets called.
 *
 *  Only the DIFFERENCE is sent. Re-arming an already-armed campaign would
 *  overwrite the note saying why it last stopped, and disarming one that is
 *  already out would invent a "stopped by operator" it never had. A campaign
 *  the server would refuse never reaches either list. */
export function autopilotDiff(all: Campaign[], chosen: Set<number>) {
  const ok = all.filter(pickable);
  return {
    arm: ok.filter((c) => chosen.has(c.id) && !c.autopilot).map((c) => c.id),
    disarm: ok.filter((c) => !chosen.has(c.id) && c.autopilot).map((c) => c.id),
  };
}

export function Today() {
  const date = useStore((s) => s.date);
  const setDate = useStore((s) => s.setDate);
  const toast = useStore((s) => s.toast);
  const agentId = useStore((s) => s.agentId);
  const [kind, setKind] = useState('auto');
  const day = useAsync(() => api.day(date, kind), [date, kind]);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [approving, setApproving] = useState(false);
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState('');

  const d = day.data;

  // Ticked buckets default to every bucket in the plan and follow it when the
  // plan changes. Null means "not touched yet" so a re-plan cannot silently
  // resurrect a bucket the operator just unticked.
  const all = useMemo(() => (d?.buckets ?? []).map((b) => b.bucket), [d]);
  useEffect(() => {
    setPicked((p) => (p === null ? null : p.filter((b) => all.includes(b))));
  }, [all]);
  const chosen = picked ?? all;

  const prepare = async () => {
    setBusy('prepare');
    try {
      const res = await api.prepareDay(date, kind);
      toast('ok', `Plan built: ${n(res.ready)} leads ready across ${res.prepared} campaigns. Nothing has been dialled.`);
      day.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            Server clock {d?.now ?? '—'} IST · window {d?.window.start ?? '09:00'}–
            {d?.window.end ?? '20:00'}
            {d?.window_varies && ' · varies by campaign'}
          </span>
          <h1>The day</h1>
        </div>
        <div className="row" style={{ marginLeft: 'auto', gap: 8 }}>
          <input
            className="input"
            type="date"
            value={date}
            aria-label="Day"
            onChange={(e) => setDate(e.target.value)}
          />
          <div className="seg" role="group" aria-label="Wave">
            {WAVES.map((w) => (
              <button
                key={w.kind}
                className={`seg-btn${kind === w.kind ? ' is-active' : ''}`}
                onClick={() => setKind(w.kind)}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button className="btn btn-ghost" onClick={() => setPicking(true)}>
            <ListChecks /> Campaigns
          </button>
          <button className="btn btn-ghost" onClick={() => day.reload()} aria-label="Refresh">
            <RefreshCw /> Refresh
          </button>
        </div>
      </div>

      {day.error && (
        <div className="warnbox">
          <AlertTriangle />
          <span>{day.error}</span>
        </div>
      )}

      <Headline
        day={d}
        busy={busy}
        onPrepare={prepare}
        onApprove={() => setApproving(true)}
        onPick={() => setPicking(true)}
      />

      {d && d.status !== 'no_campaigns' && (
        <>
          <RedBands day={d} />
          <Buckets day={d} chosen={chosen} onChange={setPicked} />
          <Campaigns day={d} />
        </>
      )}

      {d && d.stopped.length > 0 && <Stopped day={d} />}

      {picking && (
        <PickCampaigns
          // Keyed by agent: switching scope is a different picking session, and
          // `ticked` is seeded once from what is armed. Without the remount it
          // would carry the previous agent's ticks into the new list.
          key={agentId}
          date={date}
          kind={kind}
          // What is armed right now across BOTH agents. The picker is scoped to
          // one, so this is how it can still say what the other one is running.
          planned={d?.campaigns ?? []}
          onClose={() => setPicking(false)}
          onDone={() => day.reload()}
        />
      )}

      {approving && d && (
        <ApproveDay
          day={d}
          buckets={wireBuckets(chosen, all)}
          shown={chosen}
          onClose={() => setApproving(false)}
          onDone={() => day.reload()}
        />
      )}
    </div>
  );
}

/** The one sentence that answers "what is happening today?", and the one button
 *  that changes it. Everything below this is detail. */
export function Headline({
  day,
  busy,
  onPrepare,
  onApprove,
  onPick,
}: {
  day: DayView | null;
  busy: string;
  onPrepare: () => void;
  onApprove: () => void;
  onPick: () => void;
}) {
  if (!day) {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">Loading</span>
          <h2 className="hero-h">Reading today’s plan…</h2>
        </div>
      </section>
    );
  }

  const live = !day.dry_run;
  const short = day.capacity_before_close < day.totals.ready;

  if (day.status === 'no_campaigns') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.wave}</span>
          <h2 className="hero-h">No campaign is in the daily plan.</h2>
          <p className="hero-sub">
            Pick the campaigns to run today. Their leads are then scheduled by RED — the days to
            expiry decide who is called and in what order. Picking places no call.
          </p>
        </div>
        <button className="btn btn-primary btn-hero" onClick={onPick}>
          <ListChecks /> Choose campaigns
        </button>
      </section>
    );
  }

  if (day.status === 'not_prepared') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.wave} · {day.totals.campaigns} campaigns</span>
          <h2 className="hero-h">No plan built yet for this wave.</h2>
          <p className="hero-sub">
            Building a plan writes it down and dials nothing. You approve it afterwards.
          </p>
        </div>
        <button className="btn btn-primary btn-hero" disabled={busy !== ''} onClick={onPrepare}>
          {busy === 'prepare' ? <Loader2 className="spin" /> : <ClipboardList />} Build the plan
        </button>
      </section>
    );
  }

  if (day.status === 'approved') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.wave}</span>
          <h2 className="hero-h">
            {n(day.totals.posted)} <span className="hero-h-dim">calls on the clock</span>
            {day.totals.failed > 0 && (
              <> · <span style={{ color: 'var(--bad)' }}>{n(day.totals.failed)} failed</span></>
            )}
          </h2>
          <p className="hero-sub">
            This wave has been approved. {day.totals.dropped > 0 && (
              <>{n(day.totals.dropped)} did not fit before {closesAt(day)} and return in tomorrow’s
              plan. </>
            )}
            The call log says whether each one was actually dialled.
          </p>
        </div>
        <button className="btn btn-primary btn-hero" onClick={() => navigate('calllog')}>
          <PhoneCall /> Open the call log
        </button>
      </section>
    );
  }

  return (
    <section className="hero">
      <div className="hero-body">
        <span className="eyebrow">
          {day.date} · {day.wave} · {day.totals.campaigns} campaigns
        </span>
        <h2 className="hero-h">
          {n(day.totals.ready)} <span className="hero-h-dim">calls ready. Nothing is dialled yet.</span>
        </h2>
        <p className="hero-sub">
          {!day.window_open ? (
            <>
              The {day.window.start}–{day.window.end} window has closed for today. Approving now
              would place no call; this plan returns tomorrow morning.
            </>
          ) : short ? (
            <>
              Only about {n(day.capacity_before_close)} of them still fit before {closesAt(day)}.
              Approving takes them in RED order — the rest are not dialled today and come back in
              tomorrow’s plan.
            </>
          ) : (
            <>
              Approving puts them on Formi’s clock inside {day.window.start}–{day.window.end}
              {day.window_varies && ', each within its own campaign’s window'}, best RED band first.
            </>
          )}
        </p>
      </div>
      <button
        className={`btn btn-hero ${live ? 'btn-live' : 'btn-primary'}`}
        disabled={!day.window_open || day.totals.ready === 0}
        onClick={onApprove}
      >
        {live ? <Radio /> : <FlaskConical />} {live ? 'Approve and dial' : 'Approve (simulated)'}
      </button>
    </section>
  );
}

/** RED priority order — what actually decides who gets called first. */
function RedBands({ day }: { day: DayView }) {
  const total = day.red_bands.reduce((s, b) => s + b.ready, 0) || 1;
  return (
    <Card
      title="Who gets called first"
      eyebrow="RED order · applied before the bucket order"
    >
      <div className="table-wrap">
        <table className="t">
          <thead>
            <tr>
              <th style={{ width: 34 }}>#</th>
              <th>Band</th>
              <th>When it renews</th>
              <th className="n">Ready</th>
              <th style={{ width: '32%' }} />
            </tr>
          </thead>
          <tbody>
            {day.red_bands.map((b) => (
              <tr key={b.rank}>
                <td className="cell-dim">{b.rank + 1}</td>
                <td><b>{b.label}</b></td>
                <td className="mono cell-dim">{bandRange(b.dte_from, b.dte_to)}</td>
                <td className="n" style={{ fontWeight: 600 }}>{n(b.ready)}</td>
                <td>
                  <div className="bucket-bar">
                    <span
                      className="bucket-bar-seg"
                      style={{
                        width: `${(b.ready / total) * 100}%`,
                        background: b.dte_from === null ? 'var(--muted)' : 'var(--accent)',
                      }}
                    />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Which buckets to call today. Ticked = dialled; unticked is not lost, it comes
 *  back in tomorrow's plan. */
function Buckets({
  day,
  chosen,
  onChange,
}: {
  day: DayView;
  chosen: string[];
  onChange: (v: string[]) => void;
}) {
  const all = day.buckets.map((b) => b.bucket);
  const ready = day.buckets
    .filter((b) => chosen.includes(b.bucket))
    .reduce((s, b) => s + b.ready, 0);

  const toggle = (b: DayBucket) =>
    onChange(chosen.includes(b.bucket) ? chosen.filter((x) => x !== b.bucket) : [...chosen, b.bucket]);

  if (day.buckets.length === 0) {
    return (
      <Card title="Which buckets to call">
        <Empty title="No bucket has anything ready" note="Nothing in this wave’s plan to pick from." />
      </Card>
    );
  }

  return (
    <Card
      title="Which buckets to call"
      eyebrow={`${n(ready)} of ${n(day.totals.ready)} selected`}
      actions={
        <>
          <button className="btn btn-sm btn-ghost" onClick={() => onChange(all)}>All</button>
          <button className="btn btn-sm btn-ghost" onClick={() => onChange([])}>None</button>
        </>
      }
    >
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        {day.buckets.map((b) => {
          const on = chosen.includes(b.bucket);
          return (
            <button
              key={b.bucket}
              type="button"
              className={`tog${on ? ' is-on' : ''}`}
              aria-pressed={on}
              onClick={() => toggle(b)}
            >
              <span className="tog-box" />
              <span className="tog-label">
                <span className="chip-dot" style={{ background: bucketColor(b.bucket) }} />
                <b style={{ color: bucketColor(b.bucket) }}>{friendlyBucket(b.bucket)}</b>{' '}
                <span className="cell-dim">{n(b.ready)}</span>
              </span>
            </button>
          );
        })}
      </div>
      <p className="hero-sub" style={{ marginBottom: 0 }}>
        Buckets are listed in the order they will be dialled — best RED band first. Unticking one
        does not lose those leads; they return in tomorrow’s plan.
      </p>
    </Card>
  );
}

function Campaigns({ day }: { day: DayView }) {
  return (
    <Card title="Campaigns in today’s plan" eyebrow={`${day.totals.campaigns}`} flush>
      <div className="table-wrap">
        <table className="t">
          <thead>
            <tr>
              <th>Campaign</th>
              <th className="n">Ready</th>
              <th>Plan</th>
              <th className="n">On the clock</th>
              <th className="n">Failed</th>
            </tr>
          </thead>
          <tbody>
            {day.campaigns.map((c) => (
              <tr key={c.id}>
                <td>
                  <b>{c.name}</b> <span className="cell-dim">· agent {c.agent_id}</span>
                </td>
                <td className="n" style={{ fontWeight: 600 }}>{n(c.ready)}</td>
                <td>
                  <span className={`badge ${c.run_status === 'committed' ? 'badge-ok' : ''}`}>
                    {c.run_status === 'not_prepared' ? 'not built' : c.run_status}
                  </span>
                </td>
                <td className="n">{n(c.posted)}</td>
                <td className="n" style={c.failed ? { color: 'var(--bad)' } : undefined}>
                  {n(c.failed)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Pick today's campaigns in one place, then let RED schedule them.
 *
 *  This is the whole selection step. Before it, an operator had to open each
 *  campaign's own dashboard and flip one switch — 67 dashboards to choose the
 *  handful that run today, which is why nothing was ever in the daily plan.
 *
 *  Ticking still dials nothing. Saving arms the campaigns and builds the plan;
 *  the plan waits for the day to be approved, exactly as before. RED decides
 *  the rest: days-to-expiry puts each lead in a band and the bands are called
 *  in the client's order.
 */
function PickCampaigns({
  date,
  kind,
  onClose,
  onDone,
  planned,
}: {
  date: string;
  kind: string;
  onClose: () => void;
  onDone: () => void;
  planned: DayCampaign[];
}) {
  const toast = useStore((s) => s.toast);
  const agentId = useStore((s) => s.agentId);
  const setAgent = useStore((s) => s.setAgent);
  // Scoped to the agent in the rail, like every other screen, and asking for
  // hidden campaigns because this is one of the two places they can be put back.
  //
  // Scoping only narrows what is OFFERED. The day itself stays one plan across
  // both agents -- `api/day.py` has no notion of an agent -- so the other one's
  // armed campaigns keep running, and `planned` is here to say so out loud
  // rather than let them dial off-screen. `autopilotDiff` reads this same list,
  // so a campaign the picker cannot see is also one it can never disarm.
  const list = useAsync(
    () => (agentId === null ? Promise.resolve([]) : api.campaigns(agentId, true)),
    [agentId],
  );
  // The other agent's campaigns that are armed for today, straight from the day
  // view the parent already loaded — no second request to ask the same thing.
  const elsewhere = useMemo(
    () => planned.filter((c) => c.agent_id !== agentId),
    [planned, agentId],
  );
  const elsewhereAgents = useMemo(
    () => [...new Set(elsewhere.map((c) => c.agent_id))].sort((a, b) => a - b),
    [elsewhere],
  );
  const [ticked, setTicked] = useState<Set<number> | null>(null);
  const [filter, setFilter] = useState('');
  const [saving, setSaving] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  // The campaign whose hide is waiting to be confirmed, in-place rather than in
  // a second modal: two stacked overlays share one Escape key and closing the
  // confirm would close the picker with it.
  const [confirming, setConfirming] = useState<Campaign | null>(null);
  const [hiding, setHiding] = useState(0);

  // What is armed right now, straight from the server. Kept apart from `ticked`
  // so saving can send only what the operator actually changed — re-arming an
  // already-armed campaign would overwrite the note saying why it last stopped.
  const armed = useMemo(
    () => new Set((list.data ?? []).filter((c) => c.autopilot).map((c) => c.id)),
    [list.data],
  );
  useEffect(() => {
    if (list.data) setTicked((t) => t ?? new Set(armed));
  }, [list.data, armed]);

  const chosen = ticked ?? armed;
  const matching = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (list.data ?? [])
      .filter((c) => !needle || c.name.toLowerCase().includes(needle) || String(c.id) === needle)
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [list.data, filter]);
  const shown = useMemo(() => matching.filter((c) => !c.hidden), [matching]);
  const hiddenOnes = useMemo(() => matching.filter((c) => c.hidden), [matching]);

  const toggle = (id: number) =>
    setTicked((t) => {
      const next = new Set(t ?? armed);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const setMany = (on: boolean) =>
    setTicked((t) => {
      const next = new Set(t ?? armed);
      for (const c of shown.filter(pickable)) {
        if (on) next.add(c.id);
        else next.delete(c.id);
      }
      return next;
    });

  const { arm, disarm } = useMemo(
    () => autopilotDiff(list.data ?? [], chosen),
    [list.data, chosen],
  );

  /** Take a campaign out of circulation, or put it back.
   *
   *  Both reload this list AND the store's, because the store is what the topbar
   *  switcher and every other screen read from: without it a campaign hidden
   *  here would stay selectable up there until the next reload.
   *
   *  A hidden campaign is also un-ticked here by hand. The server disarms it, but
   *  `ticked` is local state seeded once from `armed`, so it would otherwise keep
   *  a stale tick and the footer would offer to arm something that cannot be.
   */
  const setVisible = async (c: Campaign, visible: boolean) => {
    setHiding(c.id);
    try {
      if (visible) {
        await api.unhide(c.id);
        toast('ok', `${c.name} is back in the lists. It is not in the plan — tick it to add it.`);
      } else {
        const res = await api.hide(c.id);
        setTicked((t) => {
          const next = new Set(t ?? armed);
          next.delete(c.id);
          return next;
        });
        toast(
          'ok',
          res.live_today > 0
            ? `${c.name} is hidden and will not be planned again. ${n(res.live_today)} call(s) it ` +
                'already put on today’s clock are still going out — pause it to take those back.'
            : `${c.name} is hidden. It will not be planned again.`,
        );
      }
      setConfirming(null);
      list.reload();
      if (agentId !== null) await setAgent(agentId);
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setHiding(0);
    }
  };

  const save = async () => {
    setSaving(true);
    const failed: string[] = [];
    try {
      // One campaign refusing must not strand the others half-applied, so each
      // is reported and the rest carry on.
      for (const [id, on] of [
        ...arm.map((id) => [id, true] as const),
        ...disarm.map((id) => [id, false] as const),
      ]) {
        try {
          await api.setAutopilot(id, on);
        } catch (e) {
          failed.push(`${id}: ${(e as Error).message}`);
        }
      }
      if (failed.length) toast('bad', `${failed.length} campaign(s) refused — ${failed[0]}`);

      if (chosen.size === 0) {
        // "Nothing will be dialled" is only true when the other agent is empty
        // too: this picker can no longer see it, so it must not speak for it.
        toast(
          'ok',
          elsewhere.length === 0
            ? 'No campaign is in the daily plan. Nothing will be dialled.'
            : `No campaign of agent ${agentId} is in the plan. ${elsewhere.length} on agent ` +
                `${elsewhereAgents.join(', ')} are still armed — switch the scope to change those.`,
        );
      } else {
        const res = await api.prepareDay(date, kind);
        toast(
          'ok',
          `${n(res.ready)} leads ready across ${res.prepared} campaigns, scheduled by RED. ` +
            'Nothing has been dialled.',
        );
      }
      onDone();
      onClose();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title="Which campaigns run today"
      onClose={onClose}
      footer={
        <>
          <span className="cell-dim" style={{ marginRight: 'auto' }}>
            {chosen.size} selected
            {arm.length > 0 && ` · ${arm.length} to add`}
            {disarm.length > 0 && ` · ${disarm.length} to remove`}
          </span>
          <button className="btn btn-ghost" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            onClick={save}
            disabled={saving || (arm.length === 0 && disarm.length === 0)}
          >
            {saving ? <Loader2 className="spin" /> : <ClipboardList />} Save and build the plan
          </button>
        </>
      }
    >
      <p style={{ marginTop: 0 }}>
        <Info className="inline-icon" /> Saving arms these campaigns and builds {date}’s plan from
        their leads’ RED. It places no call — the day still has to be approved.
      </p>

      <p className="cell-dim" style={{ marginTop: 0 }}>
        Agent {agentId ?? '—'} only. Switch the scope in the rail for another agent’s campaigns.
      </p>

      {/* The day is one plan across both agents, so what this picker no longer
          shows can still be dialling. Said here rather than left to be noticed
          in the table behind the modal. */}
      {elsewhere.length > 0 && (
        <div className="warnbox">
          <AlertTriangle />
          <span>
            {elsewhere.length} campaign(s) on agent {elsewhereAgents.join(', ')} are also in today’s
            plan and keep running. Saving here changes agent {agentId} alone — switch the scope to
            change those.
          </span>
        </div>
      )}

      <div className="row" style={{ gap: 8, margin: '10px 0' }}>
        <input
          className="input"
          placeholder="Filter by name or id"
          aria-label="Filter campaigns"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ flex: 1 }}
        />
        <button className="btn btn-sm btn-ghost" onClick={() => setMany(true)}>
          Select all
        </button>
        <button className="btn btn-sm btn-ghost" onClick={() => setMany(false)}>
          Clear
        </button>
      </div>

      {list.error && (
        <div className="warnbox">
          <AlertTriangle />
          <span>{list.error}</span>
        </div>
      )}
      {list.loading && <p className="cell-dim">Reading the campaign list…</p>}
      {!list.loading && matching.length === 0 && (
        <Empty title="No campaign matches" note="Clear the filter to see them all." />
      )}

      {confirming && (
        <div className="warnbox">
          <AlertTriangle />
          <span style={{ flex: 1 }}>
            Hide <b>{confirming.name}</b>? It leaves every list in this console and can no longer be
            put in a plan. Calls it already placed on today’s clock keep going out — hiding stops
            the next plan, it does not cancel a call. You can un-hide it here.
          </span>
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => setConfirming(null)}
            disabled={hiding > 0}
          >
            Cancel
          </button>
          <button
            className="btn btn-sm btn-primary"
            onClick={() => setVisible(confirming, false)}
            disabled={hiding > 0}
          >
            {hiding > 0 ? <Loader2 className="spin" /> : <EyeOff />} Hide it
          </button>
        </div>
      )}

      <div className="grid" style={{ gap: 2, maxHeight: 340, overflowY: 'auto' }}>
        {shown.map((c) => {
          const ok = pickable(c);
          return (
            <label
              key={c.id}
              className="row"
              style={{ gap: 8, padding: '4px 2px', opacity: ok ? 1 : 0.5 }}
              title={ok ? undefined : 'Disabled in Formi — it cannot be put in the plan.'}
            >
              <input
                type="checkbox"
                checked={ok && chosen.has(c.id)}
                disabled={!ok}
                onChange={() => toggle(c.id)}
              />
              <span style={{ flex: 1 }}>
                {c.name} <span className="cell-dim">· {c.id}</span>
              </span>
              {!ok && <span className="badge">disabled</span>}
              {/* Armed and paused is the one combination that looks selected and
                  produces nothing: the day query skips paused campaigns. */}
              {ok && c.paused && <span className="badge">paused — skipped today</span>}
              <button
                className="icon-btn"
                aria-label={`Hide ${c.name}`}
                title="Never schedule this campaign — take it out of the console"
                disabled={hiding > 0}
                onClick={(e) => {
                  e.preventDefault(); // the row is a <label>: don't tick the box
                  setConfirming(c);
                }}
              >
                <EyeOff />
              </button>
            </label>
          );
        })}
      </div>

      {hiddenOnes.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="row" style={{ gap: 8 }}>
            <span className="cell-dim" style={{ flex: 1 }}>
              {hiddenOnes.length} hidden — never scheduled
            </span>
            <button className="btn btn-sm btn-ghost" onClick={() => setShowHidden((v) => !v)}>
              {showHidden ? 'Hide these' : 'Show hidden'}
            </button>
          </div>
          {showHidden && (
            <div className="grid" style={{ gap: 2, maxHeight: 200, overflowY: 'auto' }}>
              {hiddenOnes.map((c) => (
                <div key={c.id} className="row" style={{ gap: 8, padding: '4px 2px', opacity: 0.6 }}>
                  <span style={{ flex: 1 }}>
                    {c.name} <span className="cell-dim">· {c.id}</span>
                  </span>
                  <button
                    className="btn btn-sm btn-ghost"
                    onClick={() => setVisible(c, true)}
                    disabled={hiding > 0}
                  >
                    {hiding === c.id ? <Loader2 className="spin" /> : <Eye />} Un-hide
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/** "Why is nothing happening for X." Campaigns that were armed and are now held
 *  — usually because Formi paused them, which this console honours. */
function Stopped({ day }: { day: DayView }) {
  return (
    <Card title="Held back" eyebrow={`${day.stopped.length} not in today’s plan`}>
      <div className="grid" style={{ gap: 6 }}>
        {day.stopped.map((c) => (
          <div key={c.id} className="row" style={{ gap: 8 }}>
            <CircleSlash size={13} style={{ color: 'var(--warn)' }} />
            <b>{c.name}</b>
            <span className="cell-dim">{c.why}</span>
          </div>
        ))}
      </div>
      <p className="hero-sub" style={{ marginBottom: 0 }}>
        A campaign paused in Formi stops here too, and resuming it there does not restart calls —
        resume it in this console when you want it back in the plan.
      </p>
    </Card>
  );
}

/** Approving is the only thing in this console that reaches Formi. */
export function ApproveDay({
  day,
  buckets,
  shown,
  onClose,
  onDone,
}: {
  day: DayView;
  /** What goes on the wire: empty means every bucket. */
  buckets: string[];
  /** What the operator actually ticked, for the sentence they read. */
  shown: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useStore((s) => s.toast);
  const live = !day.dry_run;
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  // The result STAYS on screen. It used to be summed into one toast line and
  // dropped when the modal closed -- and for `window_closed` and `error` there
  // is no run row either, so a campaign the operator ticked could fail to start
  // and leave nothing at all to look at.
  const [res, setRes] = useState<ApproveResult>();

  const ready = day.buckets
    .filter((b) => shown.includes(b.bucket))
    .reduce((s, b) => s + b.ready, 0);
  const fits = Math.min(ready, day.capacity_before_close);
  const ok = !live || typed.trim().toUpperCase() === 'DIAL';

  const submit = async () => {
    setBusy(true);
    try {
      setRes(await api.approveDay(day.date, day.kind, buckets));
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const done = () => {
    onDone();
    onClose();
  };

  if (res) {
    return (
      <Modal
        title={res.dry_run ? 'Simulated the day' : 'What went out'}
        onClose={done}
        footer={<button className="btn btn-primary" onClick={done}>Done</button>}
      >
        <DialResult res={res} buckets={buckets} onChange={setRes} />
      </Modal>
    );
  }

  return (
    <Modal
      title={live ? 'Approve and dial the day' : 'Approve the day (simulated)'}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className={live ? 'btn btn-live' : 'btn btn-primary'}
            disabled={!ok || busy || shown.length === 0}
            onClick={submit}
          >
            {busy ? <Loader2 className="spin" /> : live ? <Radio /> : <FlaskConical />}
            {live ? `Dial up to ${n(fits)} calls` : `Simulate up to ${n(fits)} calls`}
          </button>
        </>
      }
    >
      {live ? (
        <div className="warnbox">
          <Radio />
          <span>
            <b>DRY_RUN is off.</b> This puts real calls on Formi’s clock for every campaign in the
            plan. There is no undo.
          </span>
        </div>
      ) : (
        <div className="infobox">
          <FlaskConical />
          <span>
            The server is in dry run. Every call is recorded in the call log as{' '}
            <span className="mono">simulated</span> and nothing reaches Formi.
          </span>
        </div>
      )}

      <div className="confirm-facts">
        <Fact k="Day" v={`${day.date} · ${day.wave}`} />
        <Fact k="Campaigns" v={day.totals.campaigns} />
        <Fact k="Buckets" v={buckets.length === 0 ? 'all of them' : shown.join(', ')} />
        <Fact k="Selected" v={n(ready)} />
        <Fact k="Fits before close" v={n(fits)} tone={fits < ready ? 'var(--warn)' : undefined} />
        <Fact
          k="Window"
          v={`${day.window.start}–${day.window.end} IST${day.window_varies ? ' · varies by campaign' : ''}`}
        />
      </div>

      {shown.length === 0 && (
        <div className="warnbox">
          <AlertTriangle />
          <span>No bucket is ticked, so there is nothing to dial. Tick at least one.</span>
        </div>
      )}

      <div className="infobox">
        <Info />
        <span>
          Approving re-plans from {day.now} first, so only what genuinely fits before{' '}
          {closesAt(day)} is scheduled — best RED band first. Whatever does not fit is{' '}
          <b>not dialled today</b> and returns in tomorrow’s plan.
        </span>
      </div>

      {live && (
        <TypeToConfirm
          word="DIAL"
          value={typed}
          onChange={setTyped}
          hint="Type DIAL to confirm you mean to place these calls."
        />
      )}
    </Modal>
  );
}

/** Why a campaign did not dial, for the outcomes the server has no detail for. */
const WHY: Record<string, string> = {
  not_prepared: 'no plan was prepared for this campaign',
  already_committed: 'already dialled earlier today',
  already_paused: 'the run is paused — resume it to send the rest',
  nothing_to_dial: 'nothing left that fits before the window shuts',
};

/** What actually happened, kept on screen instead of summed into a toast.
 *
 *  The bar splits scheduled from not scheduled, which is what was asked for.
 *  The line under it splits that second number again, because its two halves
 *  are not the same thing and only one is a fault: `failed` is Formi refusing a
 *  call, and `not_dialled` is a slot that no longer fitted before the window
 *  shut, which returns in the next plan by itself. Retrying the second would
 *  only expire it again, so only the refused count turns red and drives Retry.
 */
export function DialResult({
  res,
  buckets,
  onChange,
}: {
  res: ApproveResult;
  buckets: string[];
  onChange: (next: ApproveResult) => void;
}) {
  const toast = useStore((s) => s.toast);
  const [busy, setBusy] = useState<number | 'day' | null>(null);

  const notScheduled = res.failed + res.not_dialled;
  const total = res.posted + notScheduled;
  const pct = (x: number) => (total ? `${(x / total) * 100}%` : '0%');

  const problems = res.campaigns.filter((c) => c.status !== 'approved' || (c.failed ?? 0) > 0);
  const clean = res.campaigns.length - problems.length;
  // `already_committed` DID dial, on an earlier approve, and approving it again
  // is a deliberate no-op — re-running it would say the same thing twice. Its
  // refused calls are a separate act, and that is the per-row button.
  const restartable = res.campaigns
    .filter((c) => c.status !== 'approved' && c.status !== 'already_committed')
    .map((c) => c.campaign_id);

  const retryCampaigns = async () => {
    setBusy('day');
    try {
      const again = await api.approveDay(res.date, res.kind, buckets, restartable);
      // Every campaign being re-run returned before `_commit`, so it contributed
      // nothing to these totals the first time: the merge is pure addition.
      const byId = new Map(again.campaigns.map((c) => [c.campaign_id, c]));
      onChange({
        ...res,
        approved: res.approved + again.approved,
        posted: res.posted + again.posted,
        failed: res.failed + again.failed,
        not_dialled: res.not_dialled + again.not_dialled,
        campaigns: res.campaigns.map((c) => byId.get(c.campaign_id) ?? c),
      });
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const retryCalls = async (campaignId: number, runId: number, refused: number) => {
    setBusy(campaignId);
    try {
      const run = await api.retryRun(runId);
      const after = run.counts.failed; // refused again on the second attempt
      const won = refused - after;
      onChange({
        ...res,
        posted: res.posted + won,
        failed: Math.max(res.failed - won, 0),
        campaigns: res.campaigns.map((c) =>
          c.campaign_id === campaignId
            ? { ...c, failed: after, posted: (c.posted ?? 0) + won }
            : c,
        ),
      });
      toast(
        after ? 'bad' : 'ok',
        after
          ? `${n(after)} of ${n(refused)} were refused again.`
          : `${n(won)} went back out.`,
      );
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <div className="dialbar">
        <div className="dialbar-seg is-scheduled" style={{ width: pct(res.posted) }} />
        <div className="dialbar-seg is-refused" style={{ width: pct(res.failed) }} />
        <div className="dialbar-seg is-not" style={{ width: pct(res.not_dialled) }} />
      </div>
      <div className="dialbar-keys">
        <span className="dialbar-key" style={{ color: 'var(--ok)' }}>
          <b>{n(res.posted)} scheduled</b>
        </span>
        <span
          className="dialbar-key"
          style={{ color: res.failed ? 'var(--bad)' : 'var(--warn)' }}
        >
          <b>{n(notScheduled)} not scheduled</b>
        </span>
      </div>

      <p className="hero-sub">
        {res.failed > 0 && (
          <>
            <b style={{ color: 'var(--bad)' }}>{n(res.failed)} refused by Formi</b>
            {res.not_dialled > 0 && ' · '}
          </>
        )}
        {res.not_dialled > 0 && (
          <>{n(res.not_dialled)} did not fit before the window shut — back in the next plan</>
        )}
        {notScheduled === 0 && 'Every selected lead is on the clock.'}
        {res.dry_run && ' Nothing reached Formi: the server is in dry run.'}
      </p>

      {problems.length > 0 && (
        <div className="grid" style={{ gap: 0, marginTop: 4 }}>
          {problems.map((c) => {
            const refused = c.failed ?? 0;
            return (
              <div className="dialrow" key={c.campaign_id}>
                <AlertTriangle
                  size={14}
                  style={{ color: refused ? 'var(--bad)' : 'var(--warn)', flex: '0 0 auto' }}
                />
                <b className="trunc">{c.name}</b>
                <span className="dialrow-why">
                  {refused > 0
                    ? `${n(refused)} refused by Formi`
                    : c.detail || WHY[c.status] || c.status}
                </span>
                {refused > 0 && c.run_id != null && (
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={busy !== null}
                    onClick={() => retryCalls(c.campaign_id, c.run_id!, refused)}
                  >
                    {busy === c.campaign_id ? <Loader2 className="spin" /> : <RefreshCw />}
                    Retry {n(refused)} calls
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {clean > 0 && problems.length > 0 && (
        <p className="hero-sub" style={{ marginBottom: 0 }}>
          {n(clean)} other {clean === 1 ? 'campaign' : 'campaigns'} dialled cleanly.
        </p>
      )}

      {restartable.length > 0 && (
        <button
          className="btn btn-primary"
          style={{ marginTop: 10 }}
          disabled={busy !== null}
          onClick={retryCampaigns}
        >
          {busy === 'day' ? <Loader2 className="spin" /> : <RefreshCw />}
          Retry {n(restartable.length)} {restartable.length === 1 ? 'campaign' : 'campaigns'}
        </button>
      )}
    </>
  );
}
