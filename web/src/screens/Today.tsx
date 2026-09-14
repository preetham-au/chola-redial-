/** The day. One screen, one decision.
 *
 *  Nothing in this console dials on its own. A pass prepares a plan — the day's
 *  first, then the recall after it — and leaves it waiting; this screen is where
 *  an operator reads
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
import { navigate, useAsync, useStore, type Toast } from '../lib/store';
import { Card, Empty, Fact, Modal, TypeToConfirm } from '../components/ui';
import { DayProgress, type ProgressRow } from '../components/DayProgress';
import type {
  Agent, ApproveResult, Campaign, DayBucket, DayCampaign, DaySpread, DayView, DialState,
  PrepareResult,
} from '../lib/types';

/** The two passes a day is made of. NOT two halves of the clock: both dial the
 *  campaign's whole window. What makes the recall pass a recall is who is in it
 *  — only leads whose last call says nobody was reached, at least
 *  `same_day_gap_hours` after it. Mirrors PASS_LABEL in api/day.py. */
const PASSES = [
  { kind: 'auto', label: 'First pass' },
  { kind: 'auto_pm', label: 'Recall pass' },
];

/** The pass a run belongs to, in the words the rest of the screen uses. Runs
 *  reach this from lists that carry no kind filter (`_stranded`), so `manual`
 *  arrives here too: it is neither pass, and calling it "recall pass"
 *  misreports the one banner that reports abandoned plans. */
export const passLabel = (kind: string) =>
  PASSES.find((w) => w.kind === kind)?.label.toLowerCase() ?? kind;

/** What goes on the wire. The backend reads an empty list as "every bucket", so
 *  a partial tick MUST be sent verbatim — sending [] after unticking one bucket
 *  would dial the ones the operator just excluded. */
export const wireBuckets = (chosen: string[], all: string[]) =>
  chosen.length === all.length ? [] : chosen;

/** How this screen names an agent, in ONE place: the label the server gave it,
 *  falling back to its name. The panel heading and the approve confirmation both
 *  read from here, so the cohort named on the button the operator presses is
 *  worded exactly like the heading they pressed it under. */
export const agentLabel = (agent: Agent | null) =>
  agent ? agent.language ?? agent.name : null;

/** A panel's own read of the day, and its own Prepare. The scope is the panel's
 *  AGENT and nothing else.
 *
 *  Written as functions rather than argument lists inline in an effect, because
 *  an effect body never runs under the static renderer: as a literal
 *  `api.day(date, kind, agentId)` the one argument that decides which language a
 *  panel is about could be deleted with the whole gate still green. As a
 *  function it goes on the wire in the check and is read back.
 *
 *  `agent` null is the unscoped single-panel deployment — a backend with no
 *  /api/agents, or one agent ever. Nothing is sent and the server answers for
 *  every armed campaign, byte for byte as before scoping existed. */
export const panelDay = (agent: Agent | null, date: string, kind: string) =>
  api.day(date, kind, agent?.agent_id);

/** `resync` re-reads Formi before planning, which is what the approve modal's
 *  "Re-check now" needs and what the panel's own Prepare does not: the hourly
 *  sync keeps the local copy fresh enough to press a button against, but a plan
 *  built six hours ago is being re-checked precisely because Formi has moved
 *  since. The scope is the panel's agent either way — that is the whole reason
 *  the re-check goes through here rather than calling `api.prepareDay` with
 *  `day.agent_id`, which is the server's echo and not the panel's identity. */
export const panelPrepare = (agent: Agent | null, date: string, kind: string, resync = false) =>
  api.prepareDay(date, kind, resync, agent?.agent_id);

/** The picker's own build, scoped to the agent the picker was showing.
 *
 *  The picker arms and disarms ONE agent's campaigns — it says so twice on
 *  screen — and then built the plan for everybody. That is not merely a wider
 *  read: `_write_run` clears the day's existing `planned` run for each campaign
 *  it re-plans, so an unscoped build from a panel headed "Tamil" tore down and
 *  rebuilt the Hindi plan the other operator was mid-way through approving.
 *
 *  Takes the id rather than an `Agent` because that is what the picker holds —
 *  the rail's scope, not a panel's identity — and exported for the same reason
 *  `panelPrepare` is: `save()` is an async click the static renderer never
 *  reaches, so un-scoping it inline passed the whole gate green. */
export const pickerPrepare = (agentId: number | null, date: string, kind: string) =>
  api.prepareDay(date, kind, false, agentId ?? undefined);

/** Every `_prepare_one` status that means the campaign is in NO plan at all.
 *
 *  `resync_failed` is the warehouse read failing, so it was left out rather than
 *  planned off a stale copy. `error` is `_write_run` raising — nothing was
 *  written, and the campaign is in no plan either. They arrive by different
 *  doors and land in the same place, so both have to be said. The other five
 *  (`prepared`, `not_in_daily_plan`, `finished`, `window_closed`, `already_ran`)
 *  are ordinary answers, not failures. */
const NO_PLAN = ['resync_failed', 'error'];

/** What a prepare pass has to say for itself, whatever button started it.
 *
 *  A ready count falling from 4 to 2 is not an explanation. The two facts that
 *  explain it are the two only a `resync` pass can learn, and both used to be
 *  dropped on the floor: `stopped_in_formi` — campaigns this pass found paused
 *  in Formi and stopped, which is the 11:00 pause still sitting in the 15:00
 *  plan the button exists to catch — and any campaign left in NO plan at all
 *  (see `NO_PLAN`), which is the silent failure this whole screen exists to end.
 *
 *  BOTH lists are ids, and both are named the same way — through the panel's own
 *  `day`, which already holds every campaign on screen, with `#id` for one it
 *  does not rather than dropping it. The failure rows carry no `name` of their
 *  own: `api/day.py`'s `_prepare_one` assigns `out["name"]` only AFTER the resync
 *  block, so a `resync_failed` row never has one and reading `c.name` off it
 *  printed `#12` in production every single time.
 *
 *  Returns the toast TONE with the text. A sentence naming a campaign that was
 *  stopped, or left out of the plan entirely, rendered in the green success tone
 *  is the failure dressed as a success — the one thing this button exists to
 *  stop. Tone travels with the sentence so the two cannot drift apart.
 *
 *  Pure and exported because the handler that toasts it is an async click the
 *  static renderer never reaches — written inline, the sentence naming the
 *  stopped campaigns could be deleted with the whole gate still green.
 *
 *  `lead` is the one sentence that differs between the buttons — what was just
 *  built, or what is still ready after a re-check. EVERYTHING after it is the
 *  same judgement, because it is the same `/api/day/prepare` answer either way:
 *  "Build the plan" wrote its own green "Plan built: 0 leads ready across 0
 *  campaigns" and said nothing about a campaign it had just disarmed, so the
 *  failure the re-check path learned to report went out as a success one button
 *  over. `day` is nullable only so the panel can pass its own possibly-unloaded
 *  day straight in; an id with no day behind it prints as `#id`, as ever. */
export const prepareMessage = (out: PrepareResult, day: DayView | null,
                               lead: string): [Toast['kind'], string] => {
  const name = (id: number) => day?.campaigns.find((c) => c.id === id)?.name ?? `#${id}`;
  const said = [lead];
  const stopped = (out.stopped_in_formi ?? []).map(name);
  if (stopped.length > 0) said.push(`Stopped in Formi since: ${stopped.join(', ')}.`);
  const failed = out.campaigns
    .filter((c) => NO_PLAN.includes(c.status))
    .map((c) => name(c.campaign_id));
  if (failed.length > 0)
    said.push(`Could not plan ${failed.join(', ')} — left out of this plan.`);
  // `finished` is not an ordinary answer: `_prepare_one` reaches it through
  // `_stop`, which is `UPDATE campaigns SET autopilot=0`. The campaign is
  // DISARMED, for good and for every later pass, and nothing re-arms it but a
  // person. That went out under the green tone, in a sentence that named
  // nobody, so the campaign simply stopped appearing in tomorrow's plan.
  const disarmed = out.campaigns
    .filter((c) => c.status === 'finished')
    .map((c) => name(c.campaign_id));
  if (disarmed.length > 0)
    said.push(`Autopilot switched off for ${disarmed.join(', ')} — no lead left to call.`
      + ' Re-arm in Choose campaigns if that is wrong.');
  // A re-check that came back with no plan at all is the one this button exists
  // to catch, and it is exactly the one the tone used to call a success: every
  // campaign answering `window_closed` leaves both counts at zero and every
  // list above empty, so "Re-checked: 0 still ready across 0 campaigns" went out
  // green next to an Approve that would now dial nobody.
  const none = out.prepared === 0;
  if (none)
    said.push(out.campaigns.length === 0
      ? 'No campaign was armed for this plan, so nothing was planned.'
      : 'No campaign came back with a plan — there is nothing here to dial.');
  const wrong = stopped.length + failed.length + disarmed.length > 0 || none;
  return [wrong ? 'bad' : 'ok', said.join(' ')];
};

/** The re-check's lead sentence over that same judgement. */
export const recheckMessage = (out: PrepareResult, day: DayView) =>
  prepareMessage(out, day,
    `Re-checked: ${n(out.ready)} still ready across ${out.prepared} campaigns.`);

/** Exactly what `api.approveDay` takes, named so the approve and its Retry can
 *  pass one value between them instead of five. */
export type ApproveArgs = [string, string, string[], number[], number | undefined];

/** What the Approve button sends. Split out because the button cannot be clicked
 *  by the static check, and the agent is the one argument that must never be
 *  wrong: approving the Hindi panel must not dial Tamil.
 *
 *  The agent is asserted from the PANEL, never read off `day.agent_id`. The
 *  panel knows which agent it is; `day` is a response, and a response whose echo
 *  went missing — an older backend, a proxy that dropped the query string, a
 *  rename — would turn a panel headed "Hindi" into a whole-day dial without a
 *  word. */
export const approveArgs = (
  agent: Agent | null,
  day: DayView,
  buckets: string[],
): ApproveArgs => [day.date, day.kind, buckets, [], agent?.agent_id];

/** What the whole-day Retry sends: the argument list that just dialled, narrowed
 *  to the campaigns that never started. It carries the same agent because it is
 *  handed the same list — a retry cannot reach outside the approve it is
 *  retrying, and there is no second agent value on that path to lose.
 *
 *  This matters because an empty `campaign_ids` means EVERY armed campaign to
 *  the backend, so a retry that lost its scope would dial the language this
 *  panel never approved. */
export const retryArgs = (args: ApproveArgs, campaign_ids: number[]): ApproveArgs =>
  [args[0], args[1], args[2], campaign_ids, args[4]];

/** The day, split into one request per campaign — the queue the progress bar
 *  walks.
 *
 *  One approve used to post every campaign in a single blocking request: on
 *  12 Sep 2026 the recall pass sent 2,967 calls that way, one timeout from
 *  losing the day, with nothing on screen but a spinner. Splitting it per
 *  campaign is what makes a progress bar possible at all.
 *
 *  The WALK itself is no longer here — it is the server's, behind
 *  `/api/day/dial`, because a single campaign is still a ten-minute request and
 *  on 14 Sep 2026 one of those answered to a socket nobody was on any more,
 *  taking the other twenty-one campaigns of the queue down with it. What this
 *  still produces is the campaign LIST that goes out with the start, and the
 *  denominator the progress bar counts against.
 *
 *  Only `campaign_ids` varies down the queue: every entry carries the SAME
 *  `args` the whole-day approve would have sent, so the scope is the panel's
 *  agent, asserted once, and cannot drift campaign to campaign. `day` is read
 *  here for its campaign LIST and never for `day.agent_id` — splitting one
 *  request into twelve must not become twelve chances to re-derive the scope
 *  from the server's echo.
 *
 *  A function rather than a loop body inside the click handler, because a click
 *  handler is unreachable from the static check: written inline, the one
 *  argument that decides which language goes out could be swapped for the echo
 *  with the whole gate still green. */
export const dialQueue = (args: ApproveArgs, day: DayView) =>
  day.campaigns
    .filter((c) => c.run_status === 'planned')
    // `DayCampaign.id` IS the campaign id the approve response calls
    // `campaign_id` — the same identity under two names, which is why the
    // progress row is keyed on it and matches the result rows later.
    .map((c) => ({ campaign_id: c.id, name: c.name, args: retryArgs(args, [c.id]) }));

/** The ONLY thing `day.agent_id` is good for: noticing that this panel disagrees
 *  with itself.
 *
 *  `args[4]` is what the panel asserts is about to be dialled; `day.agent_id` is
 *  what the server says it actually narrowed this plan to. Two independent
 *  witnesses of one fact, and in every legitimate state they agree. When they
 *  disagree one of them is a lie — the operator is reading Tamil's plan under a
 *  button that would dial the whole roster, or the reverse — and the only safe
 *  answer to "which of these is right" is to dial nothing and say so.
 *
 *  Read as a CHECK, never as a source. Sourcing the scope from the echo is the
 *  bug fixed in a770682 and a refusal cannot reintroduce it: the worst this can
 *  do is decline a dial, which no phone ever rings for. */
export const scopeMismatch = (args: ApproveArgs, day: DayView): string | null => {
  const who = (id: number | null | undefined) => (id == null ? 'every agent' : `agent ${id}`);
  // Both sides through `?? null`, or the two spellings of "no agent" stop being
  // equal: a server that predates this branch omits the field, `null ===
  // undefined` is false, and EVERY panel — unscoped ones included — refuses for
  // ever, under advice (reload the day) that brings back the same answer. An
  // absent echo means a server that cannot narrow, which is the same fact as a
  // plan narrowed to nobody; a scoped panel over it is still refused, and
  // rightly, because that plan really does span the whole roster.
  const echo = day.agent_id ?? null;
  return (args[4] ?? null) === echo
    ? null
    : `This panel is showing the plan for ${who(echo)}, but approving it would dial ` +
      `${who(args[4])}. Nothing will be dialled until the two agree — reload the day.`;
};

/** The approve modal, wired from the panel that opens it — its agent, its plan,
 *  its ticks.
 *
 *  A function rather than JSX written inline in `DayPanel`, for the same reason
 *  `panelDay` and `panelPrepare` are functions: `ApproveDay` only mounts behind
 *  `approving && d`, and a static render can reach neither, so inline the one
 *  prop that decides WHICH LANGUAGE gets dialled could be nulled with the whole
 *  gate green. As a function the check renders it with the agent a panel would
 *  hand it and reads the cohort back out of the markup. */
export const approveModal = (
  agent: Agent | null,
  day: DayView,
  chosen: string[],
  all: string[],
  onClose: () => void,
  onDone: () => void,
) => (
  <ApproveDay
    agent={agent}
    day={day}
    buckets={wireBuckets(chosen, all)}
    shown={chosen}
    onClose={onClose}
    onDone={onDone}
  />
);

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

/** Campaigns on this panel with no run at all for this pass.
 *
 *  The whole of the `part_prepared` headline: how many campaigns were left
 *  behind while the rest of the pass was acted on. Read off `run_status`, which
 *  is the same field the Campaigns card badges "not built" with, so the hero and
 *  the list below it cannot disagree.
 *
 *  Pure and exported because a number written inline into the hero is a number
 *  no check can reach without a DOM runner. */
export const unbuilt = (day: DayView) =>
  day.campaigns.filter((c) => c.run_status === 'not_prepared').length;

/** How old a plan may get before its `already_booked` count stops meaning
 *  anything. 90 minutes is well inside the six-hour gap of 12 Sep 2026 and well
 *  outside the few minutes between a prepare pass and a prompt approval. */
export const STALE_MIN = 90;

/** How many of these leads Formi had already queued when the plan was built.
 *
 *  `already_booked` is the one fact on the approve modal that can talk the
 *  operator out of dialling, and it was summed with `+` off a field typed as
 *  always present. A server one deploy behind sends the campaign rows without
 *  it: `0 + undefined` is `NaN`, `NaN > 0` is false, and the entire warning
 *  vanished — not degraded, not zeroed, gone, with the modal reading exactly as
 *  it does on a day when nothing was double-booked.
 *
 *  So the absence is carried out separately rather than folded into a zero. A
 *  count of 0 means the engine found none; `known: false` means nobody asked,
 *  and those two must not print the same sentence. */
export function alreadyBooked(campaigns: { already_booked?: number }[]) {
  return {
    count: campaigns.reduce((s, c) => s + (c.already_booked ?? 0), 0),
    known: campaigns.every((c) => typeof c.already_booked === 'number'),
  };
}

/** When these campaigns were last dialled — as a range when they disagree.
 *
 *  A panel holds many campaigns and one line to say this in, so the line is a
 *  reduction, and WHICH reduction changes what it means. `plan_built_at` beside
 *  it takes the OLDEST, deliberately: the worst staleness on the panel is the
 *  one worth warning about. This took the newest, so twelve campaigns last
 *  dialled between June and yesterday read as "yesterday" — the reassuring end
 *  of a range whose other end was the reason to look.
 *
 *  Rather than guess which end is the cautious one for a date that is reassuring
 *  in one reading and alarming in the other, both ends are shown whenever they
 *  differ. A campaign never dialled sorts first, because never is the earliest a
 *  last dial can be. */
export function lastDialled(campaigns: { last_dialled: string | null }[]): string {
  if (campaigns.length === 0) return '—';
  const seen = [...new Set(campaigns.map((c) => c.last_dialled ?? ''))].sort();
  const lo = seen[0] || 'never';
  const hi = seen[seen.length - 1] || 'never';
  return lo === hi ? lo : `${lo} – ${hi}`;
}

/** How old this panel's plan is, in minutes, and whether that is old enough to
 *  stop trusting what it says was already booked.
 *
 *  The OLDEST plan on the panel decides. A panel holds one plan per campaign and
 *  they are not built at the same instant — one re-prepared at 14:00 beside one
 *  built at 09:00 is a screen whose `already_booked` is five hours stale for
 *  half of it, and a warning about the newest of them would be a warning about
 *  the half that is fine.
 *
 *  `plan_built_at` is `runs.created_at`: naive IST, no offset on the string
 *  (api/db.py's `now_iso`, "2026-09-13T09:00:00"). ECMA-262 parses an ISO
 *  date-TIME with no offset as the BROWSER's LOCAL time — only a browser that
 *  happens to be on IST gets the right answer from `new Date(builtAt)`. West of
 *  IST every plan reads 5h30m YOUNGER than it is — in UTC a plan built six
 *  hours ago reads as half an hour old and this warning never appears, which is
 *  12 Sep 2026 happening again with a timezone as the excuse. East of IST it
 *  reads older instead and a plan built a minute ago cries wolf. The offset the
 *  server wrote in is pinned back on here, and this is the only place in the
 *  client that parses the field.
 *
 *  `now` is passed in rather than read, so this stays a pure function of its
 *  arguments and the check can drive it at a fixed instant. */
export const planAge = (campaigns: DayCampaign[], now: number) => {
  // ISO strings sort chronologically, so the first is the oldest.
  const builtAt = campaigns
    .map((c) => c.plan_built_at)
    .filter((t): t is string => !!t)
    .sort()[0];
  const minutes = builtAt
    ? Math.round((now - new Date(`${builtAt}+05:30`).getTime()) / 60000)
    : 0;
  // A plan nothing knows the age of is not a plan known to be fresh, but it is
  // also not evidence of anything — warning on it would cry wolf on every
  // backend too old to send the field.
  return { builtAt, minutes, stale: !!builtAt && minutes >= STALE_MIN };
};

/** The hours of a spread that fall ENTIRELY outside the band it was approved
 *  against — the only ones the card is entitled to paint red.
 *
 *  An hour counts as outside only if NO minute of it falls in the band: a band
 *  opening at 09:30 leaves the 09:00 bar half in, and reddening it would accuse
 *  calls that were on time. The far end is the other way round and exclusive: a
 *  band closing at 20:00 holds nothing at all at 20:xx.
 *
 *  A pure function rather than four lines inside the card, because this
 *  arithmetic IS the card's claim. Inlined, the `+ 59` could go, every bar would
 *  still draw, and the sentence reading "N calls landed outside the band" would
 *  start lying with the whole gate green. */
export const outsideBand = (spread: DaySpread): [string, number][] => {
  const min = (t: string) => +t.slice(0, 2) * 60 + +t.slice(3, 5);
  const [lo, hi] = [min(spread.band.start), min(spread.band.end)];
  return Object.entries(spread.hours).filter(([h]) => +h * 60 + 59 < lo || +h * 60 >= hi);
};

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

/** One panel per agent that has campaigns. Falling back to a single unscoped
 *  panel keeps this screen working on a backend without /api/agents, and on a
 *  deployment that only ever had one agent. */
export const panelsFor = (agents: Agent[] | null): (Agent | null)[] =>
  agents?.length ? agents : [null];

export function Today() {
  const date = useStore((s) => s.date);
  const setDate = useStore((s) => s.setDate);
  const agentId = useStore((s) => s.agentId);
  const [kind, setKind] = useState('auto');
  const [picking, setPicking] = useState(false);
  // Bumped when the picker re-plans the day. Every panel loads independently,
  // so the one action that changes all of them has to reach all of them —
  // otherwise saving the picker leaves each panel showing the plan it replaced.
  const [rev, setRev] = useState(0);
  const agents = useAsync(() => api.agents(), []);

  return (
    <div className="page grid" style={{ gap: 18 }}>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            {/* The pass, not an hour. Both dial the campaign's whole window. */}
            {PASSES.find((p) => p.kind === kind)?.label}
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
          <div className="seg" role="group" aria-label="Pass">
            {PASSES.map((w) => (
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
        </div>
      </div>

      {panelsFor(agents.data).map((a) => (
        <DayPanel
          key={a?.agent_id ?? 'all'}
          agent={a}
          date={date}
          kind={kind}
          rev={rev}
          onPick={() => setPicking(true)}
          onDayChanged={() => setRev((r) => r + 1)}
        />
      ))}

      {picking && (
        <PickCampaigns
          // Keyed by agent: switching scope is a different picking session, and
          // `ticked` is seeded once from what is armed. Without the remount it
          // would carry the previous agent's ticks into the new list.
          key={agentId}
          date={date}
          kind={kind}
          onClose={() => setPicking(false)}
          onDone={() => setRev((r) => r + 1)}
        />
      )}
    </div>
  );
}

/** The panel's own header: whose day this is, how many of THEIR leads are ready,
 *  and the button that re-reads this one panel.
 *
 *  There is no screen-wide Refresh any more — one button cannot honestly reload
 *  two panels that load independently of each other.
 *
 *  The heading is the label the SERVER gave the agent (AGENT_LANGUAGES). An
 *  agent the deployment never labelled is headed by its name: "125 is Hindi" is
 *  a fact about one deployment, and this client must never claim to know it. */
export function PanelHead({
  agent,
  day,
  onReload,
}: {
  agent: Agent | null;
  day: DayView | null;
  onReload: () => void;
}) {
  const who = agentLabel(agent);
  return (
    <div className="row" style={{ gap: 8, alignItems: 'baseline' }}>
      {who && <h2 style={{ margin: 0 }}>{who}</h2>}
      <span className="eyebrow">
        {agent?.language ? `${agent.name} · ` : ''}
        {n(day?.totals.ready ?? 0)} ready
      </span>
      <button
        className="btn btn-sm btn-ghost"
        style={{ marginLeft: 'auto' }}
        onClick={onReload}
        aria-label={who ? `Refresh ${who}` : 'Refresh'}
      >
        <RefreshCw /> Refresh
      </button>
    </div>
  );
}

/** One agent's half of the day. Each panel owns its own plan, its own bucket
 *  ticks and its own Approve — two languages that used to be added together
 *  into one number are now two decisions. */
export function DayPanel({
  agent,
  date,
  kind,
  rev,
  onPick,
  onDayChanged,
}: {
  agent: Agent | null;
  date: string;
  kind: string;
  /** Bumped by the shell when the picker re-plans the day. */
  rev: number;
  onPick: () => void;
  /** For the actions inside this panel whose REACH is the whole day, not this
   *  agent — warehouse verification is one endpoint over one date. Reloading
   *  only the panel that was clicked would leave the others showing counts the
   *  server has already replaced. */
  onDayChanged: () => void;
}) {
  const toast = useStore((s) => s.toast);
  const day = useAsync(() => panelDay(agent, date, kind), [date, kind, agent?.agent_id, rev]);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [approving, setApproving] = useState(false);
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

  const prepare = async (resync = false) => {
    setBusy('prepare');
    try {
      const res = await panelPrepare(agent, date, kind, resync);
      toast(...prepareMessage(res, d,
        `Plan built: ${n(res.ready)} leads ready across ${res.prepared} campaigns.`
        + ' Nothing has been dialled.'));
      day.reload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="grid" style={{ gap: 18 }}>
      <PanelHead agent={agent} day={d} onReload={() => day.reload()} />

      {day.error && (
        <div className="warnbox">
          <AlertTriangle />
          <span>{day.error}</span>
        </div>
      )}

      {d && <Stranded day={d} />}

      <Headline
        day={d}
        busy={busy}
        onPrepare={prepare}
        onApprove={() => setApproving(true)}
        onPick={onPick}
      />

      {d && d.status !== 'no_campaigns' && (
        <>
          <RedBands day={d} />
          <Buckets day={d} chosen={chosen} onChange={setPicked} />
          <Campaigns day={d} />
          <Proof day={d} onReload={onDayChanged} />
        </>
      )}

      {/* Outside the guard above, deliberately: an agent whose every campaign is
          paused IS a day with no campaigns, and "held back" is the only thing on
          the panel that says why. */}
      {d && d.stopped.length > 0 && <Stopped day={d} />}

      {approving &&
        d &&
        approveModal(agent, d, chosen, all, () => setApproving(false), () => day.reload())}
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
  /** `resync` re-reads Formi first. The `nothing_to_dial` build wants it — the
   *  plan is empty precisely because what Formi holds has moved since — and the
   *  `not_prepared` build does not, for the reason `panelPrepare` documents. */
  onPrepare: (resync?: boolean) => void;
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
          <span className="eyebrow">{day.date} · {day.pass_label}</span>
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
          <span className="eyebrow">{day.date} · {day.pass_label} · {day.totals.campaigns} campaigns</span>
          <h2 className="hero-h">No plan built yet for this pass.</h2>
          <p className="hero-sub">
            Building a plan writes it down and dials nothing. You approve it afterwards.
          </p>
        </div>
        {/* `() => onPrepare()`, not `onPrepare`: passed bare, React hands the
            click event straight into `resync`, and a MouseEvent is truthy. The
            everyday Build would re-read Formi for every campaign. */}
        <button
          className="btn btn-primary btn-hero"
          disabled={busy !== ''}
          onClick={() => onPrepare()}
        >
          {busy === 'prepare' ? <Loader2 className="spin" /> : <ClipboardList />} Build the plan
        </button>
      </section>
    );
  }

  if (day.status === 'nothing_to_dial') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.pass_label} · {day.totals.campaigns} campaigns</span>
          <h2 className="hero-h">
            0 <span className="hero-h-dim">leads in this pass’s plan. Nothing was approved.</span>
          </h2>
          <p className="hero-sub">
            A plan was built and came back empty — every lead that reaches this pass has already
            been booked, or sits outside {day.window.start}–{day.window.end}. No call went out and
            none is waiting to. Building again re-reads Formi first, so leads booked or freed since
            this plan was built are counted properly.
          </p>
        </div>
        {/* Build, not Approve. This state used to answer `approved`, which left
            the screen offering neither — so leads a later re-sync pulled in
            could not be planned at all without reloading into the other pass. */}
        <button
          className="btn btn-primary btn-hero"
          disabled={busy !== ''}
          onClick={() => onPrepare(true)}
        >
          {busy === 'prepare' ? <Loader2 className="spin" /> : <RefreshCw />} Re-check and build
        </button>
      </section>
    );
  }

  if (day.status === 'part_prepared') {
    const missing = unbuilt(day);
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.pass_label} · {day.totals.campaigns} campaigns</span>
          <h2 className="hero-h">
            {n(missing)}{' '}
            <span className="hero-h-dim">
              {missing === 1 ? 'campaign has' : 'campaigns have'} no plan for this pass.
            </span>
          </h2>
          <p className="hero-sub">
            The rest of the pass has been dialled — {n(day.totals.posted)} calls went on the clock.
            These have no plan at all, so nothing of theirs can be approved. Building writes one and
            dials nothing; the campaigns that already went out answer “already ran” and are left
            exactly as they are, note included.
          </p>
        </div>
        {/* Plain build, not a re-check: the usual way into this state is one
            campaign whose prepare failed on a warehouse read, and the local copy
            is what it needs to be planned off. `() => onPrepare()` for the reason
            the `not_prepared` branch gives — a bare handler passes the click
            event as `resync`, and a MouseEvent is truthy. */}
        <button
          className="btn btn-primary btn-hero"
          disabled={busy !== ''}
          onClick={() => onPrepare()}
        >
          {busy === 'prepare' ? <Loader2 className="spin" /> : <ClipboardList />}
          {` Build the missing ${missing === 1 ? 'plan' : 'plans'}`}
        </button>
      </section>
    );
  }

  if (day.status === 'approved') {
    return (
      <section className="hero">
        <div className="hero-body">
          <span className="eyebrow">{day.date} · {day.pass_label}</span>
          <h2 className="hero-h">
            {n(day.totals.posted)} <span className="hero-h-dim">calls on the clock</span>
            {day.totals.failed > 0 && (
              <> · <span style={{ color: 'var(--bad)' }}>{n(day.totals.failed)} failed</span></>
            )}
          </h2>
          <p className="hero-sub">
            This pass has been approved. {day.totals.dropped > 0 && (
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
          {day.date} · {day.pass_label} · {day.totals.campaigns} campaigns
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
        <Empty title="No bucket has anything ready" note="Nothing in this pass’s plan to pick from." />
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
}: {
  date: string;
  kind: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useStore((s) => s.toast);
  const agentId = useStore((s) => s.agentId);
  const setAgent = useStore((s) => s.setAgent);
  // Scoped to the agent in the rail, like every other screen, and asking for
  // hidden campaigns because this is one of the two places they can be put back.
  //
  // Scoping narrows what is OFFERED here and what saving here re-plans:
  // `pickerPrepare` builds this agent's half of the day and leaves every other
  // agent's plan exactly as it was. Those other agents keep their armed
  // campaigns and keep dialling them from their own panels, which is why
  // `elsewhere` below is read from an UNSCOPED `api.day` — the campaigns this
  // picker cannot see are named on screen rather than left to dial off it.
  // `autopilotDiff` reads this same narrowed list, so a campaign the picker
  // cannot see is also one it can never disarm.
  const list = useAsync(
    () => (agentId === null ? Promise.resolve([]) : api.campaigns(agentId, true)),
    [agentId],
  );
  // What is armed right now across EVERY agent. Read here rather than handed
  // down: the day screen no longer holds one plan spanning both languages, it
  // holds one per panel, and the warning below is about the ones this picker
  // cannot see. Only fetched while the modal is open.
  const plan = useAsync(() => api.day(date, kind), [date, kind]);
  const elsewhere = (plan.data?.campaigns ?? []).filter((c) => c.agent_id !== agentId);
  const elsewhereAgents = [...new Set(elsewhere.map((c) => c.agent_id))].sort((a, b) => a - b);
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
        // Scoped: this picker armed one agent's campaigns, so it builds one
        // agent's plan. Unscoped, `_write_run` tore down the OTHER agent's
        // `planned` runs and rebuilt them — from a modal that says twice that it
        // changes agent {agentId} alone.
        const res = await pickerPrepare(agentId, date, kind);
        toast(
          'ok',
          `${n(res.ready)} leads ready across ${res.prepared} campaigns on agent ${agentId}, ` +
            'scheduled by RED. Nothing has been dialled.',
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
        <Info className="inline-icon" /> Saving arms these campaigns and builds agent{' '}
        {agentId ?? '—'}’s half of {date}’s plan from their leads’ RED. It places no call — the
        day still has to be approved, and no other agent’s plan is touched.
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

/** Plans nobody ever approved. Left alone, this is silent: the run stays
 *  `planned` for ever and those leads are simply never called. */
export function Stranded({ day }: { day: DayView }) {
  if (day.stranded.length === 0) return null;
  // NOT the sum of the rows' `slots`. An unapproved plan is built again for the
  // same leads every day it sits, so that sum multiplies one backlog by the
  // days it waited — 544 people read as "7,616 calls never dialled", a number
  // nobody could act on and nothing else in the console agreed with. The rows
  // below still carry their own `slots`, which is true of each run.
  const leads = day.stranded_leads;
  const campaigns = new Set(day.stranded.map((r) => r.campaign_id)).size;
  return (
    <div className="warnbox">
      <AlertTriangle />
      <span>
        <b>
          {n(leads)} {leads === 1 ? 'lead was' : 'leads were'} planned on {campaigns}{' '}
          {campaigns === 1 ? 'campaign' : 'campaigns'} and never dialled.
        </b>{' '}
        {day.stranded
          .slice(0, 4)
          .map((r) => `${r.name} · ${r.run_date} ${passLabel(r.kind)} (${n(r.slots)})`)
          .join(', ')}
        {day.stranded.length > 4 && ` and ${day.stranded.length - 4} more`}. Those leads return
        in a later plan; they were not called on the day they were planned for.
      </span>
    </div>
  );
}

/** The two facts that answer "did it schedule properly": which hours the calls
 *  landed in, and what the warehouse says actually happened. */
export function Proof({ day, onReload }: { day: DayView; onReload: () => void }) {
  const toast = useStore((s) => s.toast);
  const [busy, setBusy] = useState(false);
  const hours = Object.entries(day.spread.hours).sort(([a], [b]) => +a - +b);
  const total = hours.reduce((s, [, v]) => s + v, 0);
  if (total === 0) return null;

  const peak = Math.max(...hours.map(([, v]) => v));
  const outside = outsideBand(day.spread);
  // The spread counts `simulated` rows beside `posted` ones, so under DRY_RUN
  // these bars are drawn entirely from calls that never left the building. The
  // shape is still worth showing — it is the schedule this pass WOULD have
  // dialled — but every sentence over it has to stay in the conditional, and
  // the bars cannot wear the same green a live day earns.
  const live = !day.dry_run;

  const check = async () => {
    setBusy(true);
    try {
      // `/api/dial-log/verify` takes a date and nothing else: it re-reads the
      // whole day, every agent, however narrow the card the button sits in. So
      // the sentence names its real reach, and `onReload` is wired to refresh
      // every panel — a day-wide action that refreshes one panel leaves the
      // others showing counts the server has already replaced.
      await api.verifyDialLog(day.date, true);
      toast('ok', 'Read the warehouse back for the whole day — every agent, not just this panel.');
      onReload();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={live ? 'Where the calls landed' : 'Where the calls would have landed'}
      eyebrow={`${n(total)} ${live ? 'on the clock' : 'simulated, none dialled'}`
        + ` · band ${day.spread.band.start}–${day.spread.band.end}`}
    >
      <div className="row" style={{ gap: 4, alignItems: 'flex-end', height: 64 }}>
        {hours.map(([h, v]) => (
          <div
            key={h}
            style={{ flex: 1, textAlign: 'center' }}
            title={`${h}:00 — ${n(v)} calls`}
          >
            <div
              style={{
                height: `${(v / peak) * 48}px`,
                background: outside.some(([o]) => o === h)
                  ? 'var(--bad)'
                  : live ? 'var(--ok)' : 'var(--faint)',
                borderRadius: 2,
              }}
            />
            <span className="eyebrow">{h}</span>
          </div>
        ))}
      </div>

      {outside.length > 0 && (
        <p className="hero-sub" style={{ color: 'var(--bad)' }}>
          {n(outside.reduce((s, [, v]) => s + v, 0))} calls {live ? 'landed' : 'were scheduled'}
          {' '}outside the {day.spread.band.start}–{day.spread.band.end} band.
        </p>
      )}

      {!live && (
        <div className="warnbox">
          <AlertTriangle />
          <span>
            The server is in dry run. Nothing on this card was dialled — these are the calls this
            pass <b>would</b> have placed. The warehouse has no interaction to read back, so the
            counts below stay where they are however often you check.
          </span>
        </div>
      )}

      <div className="dialbar-keys">
        {Object.entries(day.dial_log).map(([state, count]) => (
          <span key={state} className="dialbar-key">
            <b>{n(count)}</b> {state}
          </span>
        ))}
        <button
          className="btn btn-ghost btn-sm"
          disabled={busy}
          title="Re-reads the warehouse for the whole day, every agent"
          onClick={check}
        >
          {busy ? <Loader2 className="spin" /> : <RefreshCw />} Check now
        </button>
      </div>
      <p className="hero-sub" style={{ marginBottom: 0 }}>
        A call reads <span className="mono">dialled</span> only once the warehouse shows a real
        interaction for it. <span className="mono">pending</span> means it was accepted by Formi
        and not yet read back. <b>Check now</b> re-reads the whole day, every agent — every panel
        on this screen refreshes with it.
      </p>
    </Card>
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

/** Give up on the STATUS endpoint after this many polls in a row fail.
 *
 *  Not a limit on the dial. The walk runs in the API process; nothing the
 *  browser does reaches it, so a blip on the way to a status read is a blind
 *  console, never a dead day. Reporting one as the other is how an operator
 *  comes to dial a day twice. */
const POLL_GIVE_UP = 5;

/** Follow the server's walk to its end, reporting every answer on the way.
 *
 *  What is left of `runQueue` once the queue moved server-side: the loop that
 *  used to place the calls now only watches them being placed. Still a function
 *  rather than a click handler's body, for the same reason as before — a click
 *  is unreachable from the static check and an `await` is not, so this is the
 *  part the check drives against a stubbed `fetch` and reads what really went on
 *  the wire.
 *
 *  React stays on the other side of `on`, so this runs outside a DOM. */
export async function pollDial(
  on: (state: DialState) => void,
  wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  every = 1000,
): Promise<DialState> {
  let misses = 0;
  for (;;) {
    try {
      const state = await api.dialStatus();
      misses = 0;
      on(state);
      if (!state.running) return state;
    } catch (e) {
      // One failed read is a blip; POLL_GIVE_UP of them in a row is a console
      // that has lost the server, and saying so beats a bar frozen for ever.
      if (++misses >= POLL_GIVE_UP) throw e;
    }
    await wait(every);
  }
}

/** The progress list, rebuilt from the server's answer rather than accumulated.
 *
 *  Derived, not remembered: the walk's own `results` and `current` are the whole
 *  truth about where it has got to, so a modal reopened half way through renders
 *  the same list as one that was never closed — which is the point of the walk
 *  living on the server at all.
 *
 *  Names come from the queue because the walk has none for the campaigns it
 *  could not load: a campaign disarmed mid-walk answers `no_result` with an
 *  empty name, and a row reading "campaign 1744" in the one list the operator
 *  watches is the same defect as no row at all. */
export function dialRows(
  state: DialState,
  /** Anything that knows the campaigns' names — `dialQueue`'s output in the
   *  screen, a literal in the check. It is read for names and nothing else. */
  queue: { campaign_id: number; name: string }[],
): ProgressRow[] {
  const named = new Map(queue.map((c) => [c.campaign_id, c.name]));
  const rows: ProgressRow[] = state.results.map((r) => ({
    campaign_id: r.campaign_id,
    name: r.name || named.get(r.campaign_id) || `campaign ${r.campaign_id}`,
    // `approved` with refused calls in it is not a clean campaign — the same
    // test the per-campaign walk used to make before it drew the row green.
    state: r.status === 'approved' && !r.failed ? 'done' : 'failed',
  }));
  if (state.current) {
    rows.push({
      campaign_id: state.current.campaign_id,
      name: state.current.name || named.get(state.current.campaign_id) || '',
      state: 'running',
    });
  }
  return rows;
}

/** What a Stop left behind, in the one place the operator reads afterwards.
 *
 *  Stopping breaks the walk between campaigns, and the result modal then
 *  REPLACES the
 *  one holding the progress list — so the campaigns the queue never reached
 *  vanished with it and the operator was left reading a result for five
 *  campaigns having ticked twelve, with nothing saying the other seven had not
 *  been dialled. They are not lost: a campaign never reached was never posted,
 *  so its plan items are still `planned` and approving the day again sends
 *  exactly those. That is the sentence.
 *
 *  `reached` is the walk's own `done` — campaigns it started and reported — so
 *  this needs no flag from the Stop button: a run that ended on its own reached
 *  all of them and says nothing. */
export function stoppedShort(queued: number, reached: number): string | null {
  const left = queued - reached;
  if (left < 1) return null;
  return `Stopped: ${n(left)} of ${n(queued)} ${left === 1 ? 'campaign was' : 'campaigns were'}`
    + ' never reached, so nothing was dialled for them. They are still planned and still'
    + ' approvable — approving the day again sends only those.';
}

/** Approving is the only thing in this console that reaches Formi. */
export function ApproveDay({
  agent,
  day,
  buckets,
  shown,
  onClose,
  onDone,
}: {
  /** Whose panel this Approve belongs to. The dial is scoped from here — from
   *  the panel's own identity — and not from anything `day` echoed back. */
  agent: Agent | null;
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
  const [progress, setProgress] = useState<ProgressRow[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  // How far the walk got, for the Stop sentence. From the server, not from the
  // length of `progress`: a modal opened over a walk already running has no row
  // for the campaigns it missed, and counting rows would call them unreached.
  const [walked, setWalked] = useState({ total: 0, done: 0 });

  const ready = day.buckets
    .filter((b) => shown.includes(b.bucket))
    .reduce((s, b) => s + b.ready, 0);
  const fits = Math.min(ready, day.capacity_before_close);
  const ok = !live || typed.trim().toUpperCase() === 'DIAL';
  // The one list that dials. Handed to the result below as-is, so the Retry
  // re-sends this exact scope rather than a second copy of it.
  const args = approveArgs(agent, day, buckets);
  // The panel's two witnesses of its own scope, compared. Non-null means they
  // disagree, and nothing is offered until they stop.
  const mismatch = scopeMismatch(args, day);
  // The campaigns that go out with the start, and the progress bar's
  // denominator. Read once, used for both, so they cannot count differently.
  const queue = dialQueue(args, day);

  // Has a call already been placed for these leads? The engine answered that at
  // PLAN time; these two say how long ago that was and how many it caught.
  const { builtAt, minutes: ageMin, stale } = planAge(day.campaigns, Date.now());
  const booked = alreadyBooked(day.campaigns);
  const [rechecking, setRechecking] = useState(false);

  /** Re-read Formi and rebuild the plan, so leads booked since it was built drop
   *  out of it. This is the existing prepare pass, not a new one.
   *
   *  Scoped through `panelPrepare` from this modal's OWN `agent` — the panel's
   *  identity — and never from `day.agent_id`. The echo is what a770682 took out
   *  of the dial path, and rebuilding the whole roster's plan from a panel headed
   *  "Hindi" is the same defect wearing a different button. */
  const recheck = async () => {
    setRechecking(true);
    try {
      const out = await panelPrepare(agent, day.date, day.kind, true);
      toast(...recheckMessage(out, day));
      onDone();
      onClose();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setRechecking(false);
    }
  };

  /** Watch the walk to its end and put its result on screen.
   *
   *  Used by the button AND by a modal reopened over a walk already in flight,
   *  which is what moving the dial to the server bought: closing this, reloading
   *  the tab or shutting the laptop does not stop the day, and coming back
   *  rejoins it mid-walk rather than starting a second one. */
  const watch = async () => {
    setBusy(true);
    try {
      const final = await pollDial((state) => {
        setProgress(dialRows(state, queue));
        setCurrent(state.current?.name ?? null);
        setWalked({ total: state.total, done: state.done });
      });
      setRes(final.result);
    } catch (e) {
      // The walk is the server's and is still going. Say the console lost sight
      // of it — never that the day failed, which is what makes an operator dial
      // it a second time. The progress list stays up; Dial rejoins the walk.
      toast('bad', `${(e as Error).message} The dial is still running on the server.`);
    } finally {
      setCurrent(null);
      setBusy(false);
    }
  };

  /** Hand the day to the server and watch. Returns as soon as it has started. */
  const submit = async () => {
    if (mismatch) return; // a panel that disagrees with itself dials nothing
    // An empty `campaign_ids` means EVERY armed campaign to the backend, so a
    // day with nothing planned left must not be sent as one — that is the whole
    // roster, dialled from a button that offered none of it.
    if (queue.length === 0) return;
    try {
      await api.startDial(...retryArgs(args, queue.map((c) => c.campaign_id)));
    } catch (e) {
      toast('bad', (e as Error).message);
      return;
    }
    await watch();
  };

  // A walk for THIS day already running when the modal opens is rejoined, not
  // restarted. Another day's walk is left alone: its progress belongs to a
  // screen this is not.
  useEffect(() => {
    api.dialStatus()
      .then((state) => { if (state.running && state.date === day.date) void watch(); })
      // No walk to rejoin is the ordinary case, and an unreachable server is
      // already said loudly enough by everything else on this screen.
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const done = () => {
    onDone();
    onClose();
  };

  if (res) {
    // The walk's own numbers, carried into the modal that replaces the progress
    // list — the campaigns it never reached are accounted for here or nowhere.
    const short = stoppedShort(walked.total, walked.done);
    return (
      <Modal
        title={short ? 'Stopped part-way' : res.dry_run ? 'Simulated the day' : 'What went out'}
        onClose={done}
        footer={<button className="btn btn-primary" onClick={done}>Done</button>}
      >
        <DialResult res={res} args={args} short={short} onChange={setRes} />
      </Modal>
    );
  }

  return (
    <Modal
      title={live ? 'Approve and dial the day' : 'Approve the day (simulated)'}
      onClose={onClose}
      footer={
        <>
          {/* Stopping leaves the campaigns it never reached `planned` — they stay
              on the screen and stay approvable. Closing the tab does the same. */}
          <button
            className="btn btn-ghost"
            onClick={() => (busy ? void api.stopDial().catch(() => {}) : onClose())}
          >
            {busy ? 'Stop after this campaign' : 'Cancel'}
          </button>
          <button
            className={live ? 'btn btn-live' : 'btn btn-primary'}
            disabled={!ok || busy || shown.length === 0 || queue.length === 0 || mismatch !== null}
            onClick={submit}
          >
            {busy ? <Loader2 className="spin" /> : live ? <Radio /> : <FlaskConical />}
            {live ? `Dial up to ${n(fits)} calls` : `Simulate up to ${n(fits)} calls`}
          </button>
        </>
      }
    >
      {mismatch && (
        <div className="warnbox">
          <AlertTriangle />
          <span>
            <b>This plan and this button disagree about who gets called.</b> {mismatch}
          </span>
        </div>
      )}

      {busy && (
        <DayProgress
          done={progress.filter((r) => r.state !== 'running').length}
          total={queue.length}
          current={current}
          rows={progress}
        />
      )}

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
        {/* First fact, above the day itself: every other number here is the same
            shape whichever cohort is being dialled, so this is the one line that
            tells the operator WHOSE calls they are about to place. Worded by
            `agentLabel`, the same rule as the heading on the panel whose button
            opened this modal.

            Which of the two wordings shows is decided by `args[4]` — the agent
            id actually going on the wire — and not by a second reading of
            `agent`. A fact the operator approves has to be a fact about the
            request they are approving, so a scope lost between here and
            `approveArgs` reads as "the whole roster" instead of quietly keeping
            the language name above it. */}
        <Fact
          k="Who this dials"
          v={args[4] === undefined ? 'every agent — the whole roster' : agentLabel(agent)}
        />
        <Fact k="Day" v={`${day.date} · ${day.pass_label}`} />
        <Fact k="Campaigns" v={day.totals.campaigns} />
        <Fact k="Buckets" v={buckets.length === 0 ? 'all of them' : shown.join(', ')} />
        <Fact k="Selected" v={n(ready)} />
        <Fact k="Fits before close" v={n(fits)} tone={fits < ready ? 'var(--warn)' : undefined} />
        <Fact
          k="Window"
          v={`${day.window.start}–${day.window.end} IST${day.window_varies ? ' · varies by campaign' : ''}`}
        />
        {/* "oldest" said out loud: this is the worst staleness on the panel, not
            every campaign's age, and the fact beside it is a range for the same
            reason — one line summarising many campaigns has to say which of them
            it is summarising. */}
        <Fact
          k="Plan built (oldest)"
          v={builtAt ? `${Math.floor(ageMin / 60)}h ${ageMin % 60}m ago` : '—'}
        />
        <Fact k="Last dialled" v={lastDialled(day.campaigns)} />
      </div>

      {/* Not the same thing as none. A silent zero here reads as "nobody was
          double-booked", which is the claim this server did not make. */}
      {!booked.known && (
        <div className="warnbox">
          <AlertTriangle />
          <span>
            This server did not say how many of these leads Formi had already queued when the plan
            was built. Treat the counts above as a ceiling — some of these calls may be second
            calls.
          </span>
        </div>
      )}

      {booked.count > 0 && (
        <p className="hero-sub">
          {n(booked.count)} {booked.count === 1 ? 'lead was' : 'leads were'} already on Formi’s
          clock when this plan was built and {booked.count === 1 ? 'was' : 'were'} left out of it.
        </p>
      )}


      {stale && (
        <div className="warnbox">
          <AlertTriangle />
          <span>
            <b>This plan is {Math.floor(ageMin / 60)}h {ageMin % 60}m old.</b> Any call placed in
            Formi since it was built — by you or by anyone — is not accounted for in it.
            <button
              className="btn btn-ghost btn-sm"
              style={{ marginLeft: 8 }}
              disabled={rechecking || busy}
              onClick={recheck}
            >
              {rechecking ? <Loader2 className="spin" /> : <RefreshCw />} Re-check now
            </button>
          </span>
        </div>
      )}

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
  not_dialled: 'the plan was refused before any call went out — nothing was dialled',
  no_result: 'it was no longer in the daily plan when the day was approved — nothing was dialled',
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
  args,
  short,
  onChange,
}: {
  res: ApproveResult;
  /** The argument list that produced this result — buckets, campaigns and the
   *  panel's agent. The Retry re-sends it narrowed, so it cannot dial wider than
   *  the approve it is retrying and there is no second scope here to get wrong. */
  args: ApproveArgs;
  /** `stoppedShort(...)` when the queue was cut off part-way: the campaigns
   *  below are only the ones it reached, and the rest have to be accounted for
   *  here or they are accounted for nowhere. */
  short?: string | null;
  onChange: (next: ApproveResult) => void;
}) {
  const toast = useStore((s) => s.toast);
  const [busy, setBusy] = useState<number | 'day' | null>(null);

  const notScheduled = res.failed + res.not_dialled;
  const total = res.posted + notScheduled;
  const pct = (x: number) => (total ? `${(x / total) * 100}%` : '0%');

  const problems = res.campaigns.filter(
    (c) => c.status !== 'approved' || (c.failed ?? 0) > 0);
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
      const again = await api.approveDay(...retryArgs(args, restartable));
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
      {short && (
        <div className="warnbox" style={{ marginBottom: 10 }}>
          <AlertTriangle />
          <span>{short}</span>
        </div>
      )}
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
        {/* Earned, not assumed. A campaign that was skipped, refused or never
            started contributes nothing to `notScheduled` — the totals of a
            campaign that produced no result are all zero — so the counts alone
            cannot tell a clean day from a day that did nothing. */}
        {notScheduled === 0 && problems.length === 0 && !short
          && 'Every selected lead is on the clock.'}
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
                  {refused > 0 && `${n(refused)} refused by Formi`}
                  {refused === 0 && (c.detail || WHY[c.status] || c.status)}
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
