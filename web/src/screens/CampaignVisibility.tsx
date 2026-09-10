import { useMemo, useState } from 'react';
import { AlertTriangle, Eye, EyeOff, Info, Loader2 } from 'lucide-react';
import { api } from '../lib/api';
import { n } from '../lib/domain';
import { useAsync, useStore } from '../lib/store';
import { Card, Empty } from '../components/ui';
import type { Campaign } from '../lib/types';

/** Which campaigns each button actually acts on. Split rather than "apply to
 *  everything ticked": a hidden campaign in `hide` is a pointless round trip —
 *  59 of them on a bulk press — and a visible one in `unhide` would rewrite an
 *  autopilot note that hiding never wrote. */
export function selectionSplit(all: Campaign[], sel: Set<number>) {
  const picked = all.filter((c) => sel.has(c.id));
  return { hide: picked.filter((c) => !c.hidden), unhide: picked.filter((c) => c.hidden) };
}

/** Deciding once, for good, which campaigns this console shows.
 *
 *  The day's picker can hide a campaign one row at a time, in the moment you
 *  notice it should not be there. This is the other half: the whole list in one
 *  place, hidden ones included, so a batch of retired campaigns can be taken out
 *  together and any of them put back.
 *
 *  Scoped to the agent in the rail, like every other screen. It is still outside
 *  the "pick a campaign first" gate, because it has to work when the campaign
 *  switcher is empty; the agent tabs are what reaches the other agent's list, and
 *  `/api/agents` keeps a row for an agent whose campaigns are ALL hidden, so
 *  scoping can never strand one out of reach.
 */
export function CampaignVisibility() {
  const toast = useStore((s) => s.toast);
  const agentId = useStore((s) => s.agentId);
  const bootstrap = useStore((s) => s.bootstrap);
  // Nothing before the scope is known: loading unscoped first would flash the
  // other agent's campaigns into a list whose buttons hide things.
  const list = useAsync(
    () => (agentId === null ? Promise.resolve([]) : api.campaigns(agentId, true)),
    [agentId],
  );

  const [filter, setFilter] = useState('');
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState('');

  const matching = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (list.data ?? [])
      .filter(
        (c) => !needle || c.name.toLowerCase().includes(needle) || String(c.id) === needle,
      )
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [list.data, filter]);

  const shown = useMemo(() => matching.filter((c) => !c.hidden), [matching]);
  const hidden = useMemo(() => matching.filter((c) => c.hidden), [matching]);
  const split = useMemo(() => selectionSplit(matching, sel), [matching, sel]);

  const toggle = (id: number) =>
    setSel((s) => {
      const next = new Set(s);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const pickMany = (rows: Campaign[], on: boolean) =>
    setSel((s) => {
      const next = new Set(s);
      rows.forEach((c) => (on ? next.add(c.id) : next.delete(c.id)));
      return next;
    });

  /** One at a time, on purpose: 59 concurrent writes is how a small sqlite
   *  console starts returning "database is locked" halfway through a batch and
   *  leaves the operator guessing which half took. One refusal is reported and
   *  the rest carry on. */
  const apply = async (rows: Campaign[], visible: boolean) => {
    const failed: string[] = [];
    let live = 0;
    for (let i = 0; i < rows.length; i++) {
      setBusy(`${visible ? 'Un-hiding' : 'Hiding'} ${i + 1} of ${rows.length}…`);
      try {
        if (visible) await api.unhide(rows[i].id);
        else live += (await api.hide(rows[i].id)).live_today;
      } catch (e) {
        failed.push(`${rows[i].name}: ${(e as Error).message}`);
      }
    }
    const done = rows.length - failed.length;
    if (visible) {
      toast('ok', `${done} campaign(s) are back in the lists — none of them armed. Put them in a day from “The day”.`);
    } else {
      toast(
        'ok',
        `${done} campaign(s) hidden. They will not be planned again.` +
          (live > 0
            ? ` ${n(live)} call(s) they already put on today’s clock are still going out — pause them to take those back.`
            : ''),
      );
    }
    if (failed.length) toast('bad', `${failed.length} refused — ${failed[0]}`);

    setSel(new Set());
    setConfirming(false);
    setBusy('');
    list.reload();
    // Not just this screen's list: the agent tab in the rail carries the campaign
    // count, and setAgent only recomputes the row for the agent it loads. bootstrap
    // re-reads /api/agents so the count moves, and keeps the agent selected.
    await bootstrap();
  };

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">Settings</span>
          <h1>Campaign visibility</h1>
          <p>
            Hidden campaigns leave every list in this console and can never be put in a plan. Nothing
            in Formi changes — this is only what you want to see and schedule here.
          </p>
          <p className="cell-dim">
            Agent {agentId ?? '—'} only. Switch the scope in the rail for another agent’s campaigns.
          </p>
        </div>
        <div className="row" style={{ marginLeft: 'auto' }}>
          <span className="badge">{shown.length} shown</span>
          <span className="badge">{hidden.length} hidden</span>
        </div>
      </div>

      <div className="row" style={{ gap: 8 }}>
        <input
          className="input"
          placeholder="Filter by name or campaign id"
          aria-label="Filter campaigns"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ flex: 1, maxWidth: 420 }}
        />
        {busy && (
          <span className="cell-dim row" style={{ gap: 6 }}>
            <Loader2 className="spin" /> {busy}
          </span>
        )}
      </div>

      {list.error && (
        <div className="warnbox">
          <AlertTriangle />
          <span>{list.error}</span>
        </div>
      )}

      {confirming && (
        <div className="warnbox">
          <AlertTriangle />
          <span style={{ flex: 1 }}>
            Hide <b>{split.hide.length} campaign(s)</b>? They leave every list in this console and can
            no longer be put in a plan. Any that are armed are disarmed, and un-hiding does not
            re-arm them. Calls already placed on today’s clock keep going out — hiding stops the next
            plan, it does not cancel a call.
          </span>
          <button className="btn btn-sm btn-ghost" onClick={() => setConfirming(false)} disabled={!!busy}>
            Cancel
          </button>
          <button className="btn btn-sm btn-danger" onClick={() => apply(split.hide, false)} disabled={!!busy}>
            {busy ? <Loader2 className="spin" /> : <EyeOff />} Hide {split.hide.length}
          </button>
        </div>
      )}

      <Card
        title="Shown in the console"
        eyebrow={`${shown.length} campaign${shown.length === 1 ? '' : 's'}`}
      >
        <div className="row" style={{ gap: 8, marginBottom: 10 }}>
          <button className="btn btn-sm btn-ghost" onClick={() => pickMany(shown, true)} disabled={!!busy}>
            Select all {shown.length}
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => pickMany(shown, false)} disabled={!!busy}>
            Clear
          </button>
          <span className="cell-dim" style={{ marginLeft: 'auto' }}>
            {split.hide.length} selected
          </span>
          <button
            className="btn btn-sm btn-danger"
            disabled={!!busy || split.hide.length === 0 || confirming}
            onClick={() => setConfirming(true)}
          >
            <EyeOff /> Hide selected
          </button>
        </div>

        {list.loading && <p className="cell-dim">Reading the campaign list…</p>}
        {!list.loading && shown.length === 0 && (
          <Empty
            title="Nothing is shown"
            note={filter ? 'Clear the filter to see them all.' : 'Every campaign is hidden. Un-hide one below.'}
          />
        )}

        <div className="grid" style={{ gap: 2 }}>
          {shown.map((c) => (
            <Row key={c.id} c={c} ticked={sel.has(c.id)} busy={!!busy} onTick={() => toggle(c.id)} />
          ))}
        </div>
      </Card>

      <Card title="Hidden" eyebrow={`${hidden.length} never scheduled`}>
        {hidden.length === 0 ? (
          <Empty title="No campaign is hidden" note="Everything Formi sends is shown here." />
        ) : (
          <>
            <div className="row" style={{ gap: 8, marginBottom: 10 }}>
              <button className="btn btn-sm btn-ghost" onClick={() => pickMany(hidden, true)} disabled={!!busy}>
                Select all {hidden.length}
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => pickMany(hidden, false)} disabled={!!busy}>
                Clear
              </button>
              <span className="cell-dim" style={{ marginLeft: 'auto' }}>
                {split.unhide.length} selected
              </span>
              <button
                className="btn btn-sm btn-primary"
                disabled={!!busy || split.unhide.length === 0}
                onClick={() => apply(split.unhide, true)}
              >
                <Eye /> Un-hide selected
              </button>
            </div>
            <div className="grid" style={{ gap: 2 }}>
              {hidden.map((c) => (
                <Row key={c.id} c={c} ticked={sel.has(c.id)} busy={!!busy} onTick={() => toggle(c.id)} />
              ))}
            </div>
          </>
        )}
        <div className="infobox" style={{ marginTop: 12 }}>
          <Info />
          <span>
            A campaign Formi sends for the first time is shown automatically — nothing has to be
            enabled for a new campaign to appear here.
          </span>
        </div>
      </Card>
    </div>
  );
}

function Row({
  c,
  ticked,
  busy,
  onTick,
}: {
  c: Campaign;
  ticked: boolean;
  busy: boolean;
  onTick: () => void;
}) {
  return (
    <label className="row" style={{ gap: 8, padding: '4px 2px', opacity: c.hidden ? 0.6 : 1 }}>
      <input type="checkbox" checked={ticked} disabled={busy} onChange={onTick} />
      <span style={{ flex: 1 }}>
        {c.name} <span className="cell-dim">· {c.id}</span>
      </span>
      {c.autopilot && <span className="badge badge-accent">in the daily plan</span>}
      {!c.enabled && <span className="badge">disabled in Formi</span>}
      {c.paused && <span className="badge badge-warn">paused</span>}
    </label>
  );
}
