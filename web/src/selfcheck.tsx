/* Renders the two new pieces with the real fixtures and drives every toggle
 * through the same reducer the UI calls. Run: npm run check
 * Not part of the app bundle — nothing imports it. */

import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { applyBd, BucketDispositions, type BdAction } from './components/BucketDispositions';
import { AgentChip, AgentPauseConfirm, AgentSwitcher } from './components/AgentBar';
import { DayProgress, type ProgressRow } from './components/DayProgress';
import { CampaignPicker } from './App';
import { BucketOffWhy } from './screens/Dashboard';
import { TestCallResultView, TestNumberTable, TriggerConfirm } from './screens/TestCall';
import {
  ApproveDay,
  DayPanel,
  DialResult,
  Headline,
  PanelHead,
  Today,
  approveArgs,
  approveModal,
  autopilotDiff,
  closesAt,
  dialQueue,
  mergeResults,
  panelDay,
  panelPrepare,
  panelsFor,
  planAge,
  recheckMessage,
  retryArgs,
  runQueue,
  scopeMismatch,
  STALE_MIN,
  wireBuckets,
} from './screens/Today';
import { Row as LogRow } from './screens/CallLog';
import { selectionSplit } from './screens/CampaignVisibility';
import { ApiError, api, isOffline, retryLive } from './lib/api';
import {
  mockAgentPause,
  mockBuckets,
  mockCampaigns,
  mockConfig,
  mockDay,
  mockDialLog,
  mockTestCall,
  mockTestNumbers,
} from './lib/mock';
import {
  agentsFrom,
  bandRange,
  BUCKET_COLOR,
  BUCKET_ORDER,
  configurableBuckets,
  effectiveDispositions,
  n,
  narrowedBuckets,
  passState,
} from './lib/domain';
import type {
  Agent, ApproveResult, Campaign, Config, DayCampaign, PrepareResult, TestCallResult,
} from './lib/types';

const ROWS = configurableBuckets(mockConfig.frequency_table);
let cfg: Config = structuredClone(mockConfig);

const draw = (c: Config, focus: string | null = null) =>
  renderToStaticMarkup(
    <BucketDispositions draft={c} counts={mockBuckets} focusBucket={focus} onChange={() => {}} />,
  );

/** One click on the rendered matrix. */
const click = (a: BdAction) => {
  cfg = { ...cfg, bucket_dispositions: applyBd(cfg, ROWS, a) };
  return draw(cfg);
};

const has = (html: string, s: string) => html.includes(s);
function ok(label: string, cond: boolean) {
  if (!cond) throw new Error(`FAIL: ${label}`);
  console.log(`  ok  ${label}`);
}

console.log('valid buckets:', ROWS.join(' '));

// --- first render -----------------------------------------------------------
let html = draw(cfg);
ok('matrix renders every valid bucket row', ROWS.every((b) => has(html, `data-bucket="${b}"`)));
ok('renders a column per campaign disposition', (html.match(/bd-colhead/g) ?? []).length >= 12);
ok('F1/F5 are custom, the rest inherit', (html.match(/badge-accent">custom/g) ?? []).length === 2);
ok('inheriting rows say so', (html.match(/"badge">inherits</g) ?? []).length === ROWS.length - 2);
ok('affected-lead counts come from the buckets endpoint', has(html, 'bd-affected'));
ok('no M0 warning while M0 inherits', !has(html, 'M0</b> is narrowed'));

// --- inherit -> custom by clicking a cell -----------------------------------
ok('F2 inherits the global list', effectiveDispositions(cfg, 'F2') === cfg.auto_dispositions);
html = click({ kind: 'cell', bucket: 'F2', slug: 'voicemail' });
ok('clicking a cell seeds F2 from the global list minus one', cfg.bucket_dispositions!.F2.length === cfg.auto_dispositions.length - 1);
ok('F2 now drops only voicemail', narrowedBuckets(cfg).find((x) => x.bucket === 'F2')!.dropped.join() === 'voicemail');
ok('F2 renders as custom', (html.match(/badge-accent">custom/g) ?? []).length === 3);

// --- narrowing only ---------------------------------------------------------
cfg = { ...cfg, bucket_dispositions: { ...cfg.bucket_dispositions, F3: ['do_not_call', 'did_not_pick'] } };
ok('a bucket cannot re-enable do_not_call', !effectiveDispositions(cfg, 'F3').includes('do_not_call'));
html = draw(cfg);
ok('the inert slug is shown locked, not silently dropped', has(html, 'bd-colhead is-locked'));
html = click({ kind: 'dropExtra', slug: 'do_not_call' });
ok('stripping the inert slug clears the locked column', !has(html, 'bd-colhead is-locked'));

// --- row quick actions ------------------------------------------------------
html = click({ kind: 'row', bucket: 'F4', mode: 'all' });
ok('"all" pins an explicit full copy', cfg.bucket_dispositions!.F4.length === cfg.auto_dispositions.length);
html = click({ kind: 'row', bucket: 'F4', mode: 'none' });
ok('"none" warns that empty means inherit, not "dial nothing"', has(html, 'empty ⇒ inherits'));
ok('an empty row still dials the global list', effectiveDispositions(cfg, 'F4') === cfg.auto_dispositions);
html = click({ kind: 'row', bucket: 'F4', mode: 'inherit' });
ok('"inherit" removes the key entirely', !('F4' in cfg.bucket_dispositions!));

// --- column toggle down all buckets ----------------------------------------
// Tri-state: a mixed column goes fully on first, then fully off.
ok('telephony_failed starts mixed', !effectiveDispositions(cfg, 'F1').includes('telephony_failed'));
html = click({ kind: 'col', slug: 'telephony_failed' });
ok('mixed column turns on down all buckets', ROWS.every((b) => effectiveDispositions(cfg, b).includes('telephony_failed')));
ok('column header counts every bucket on', has(html, `>${ROWS.length}/${ROWS.length}<`));
html = click({ kind: 'col', slug: 'telephony_failed' });
ok('clicking again turns it off down all buckets', ROWS.every((b) => !effectiveDispositions(cfg, b).includes('telephony_failed')));
ok('column header counts none on and strikes the label', has(html, `>0/${ROWS.length}<`) && has(html, 'bd-colhead is-dead'));

// --- M0 warning -------------------------------------------------------------
html = click({ kind: 'cell', bucket: 'M0', slug: 'did_not_pick' });
ok('narrowing M0 warns about the RED−1 / RED last chance', has(html, 'last chance') && has(html, 'M0</b> is narrowed'));

// --- unknown bucket = the server 422 ---------------------------------------
const bad: Config = { ...cfg, bucket_dispositions: { ...cfg.bucket_dispositions, F9: ['did_not_pick'] } };
ok('an unknown bucket key is caught client-side', configurableBuckets(bad.frequency_table).indexOf('F9') === -1);
ok('and surfaced as the 422 it would be', has(draw(bad), 'The server rejects the whole save with a 422'));

// --- focus from the dashboard link -----------------------------------------
ok('a linked bucket row is highlighted', has(draw(cfg, 'F1'), 'class="is-focus"'));

// --- dashboard skip sentence ------------------------------------------------
const why = renderToStaticMarkup(<BucketOffWhy config={mockConfig} />);
ok('names the operator’s own choice', has(why, 'is configured not to chase'));
ok('names the bucket and the disposition', has(why, '>F1</button> is configured not to chase') && has(why, 'voicemail'));
ok('distinguishes itself from MANUAL_ONLY', has(why, 'not a property of the disposition'));
ok('says it will never clear itself', has(why, 'never clear itself'));
ok('offers no sentence when nothing is narrowed', renderToStaticMarkup(<BucketOffWhy config={{ ...mockConfig, bucket_dispositions: {} }} />).includes('Open the per-bucket matrix'));

// --- campaign fixtures ------------------------------------------------------
ok('seed has ~14 campaigns', mockCampaigns.length === 14);
ok('across several agents', new Set(mockCampaigns.map((c) => c.agent_id)).size >= 4);
ok('with mixed enabled/paused state', mockCampaigns.some((c) => !c.enabled) && mockCampaigns.some((c) => c.paused));

// --- agent as a scope -------------------------------------------------------
const agents = agentsFrom(mockCampaigns);
const A127 = agents.find((a) => a.agent_id === 127)!;

ok('agents are derived from the campaign list, not hardcoded', agents.length === new Set(mockCampaigns.map((c) => c.agent_id)).size);
ok('each agent carries its campaign count', A127.campaigns === mockCampaigns.filter((c) => c.agent_id === 127).length);
ok('and how many of them are paused', A127.paused_campaigns === mockCampaigns.filter((c) => c.agent_id === 127 && c.paused).length);
ok(
  '`enabled` is a count of enabled campaigns, matching the live server',
  typeof A127.enabled === 'number' && A127.enabled === mockCampaigns.filter((c) => c.agent_id === 127 && c.enabled).length,
);
ok('an agent with a running campaign is not "paused"', !A127.paused);
ok(
  'an agent counts as paused only when every ENABLED campaign is paused',
  agentsFrom([
    { id: 1, agent_id: 9, warehouse_id: 1, name: 'a', enabled: true, paused: true },
    { id: 2, agent_id: 9, warehouse_id: 2, name: 'b', enabled: false, paused: false },
  ])[0].paused === true,
);

let sw = renderToStaticMarkup(<AgentSwitcher agents={agents} agentId={127} onPick={() => {}} />);
ok('the switcher renders one tab per agent', agents.every((a) => has(sw, `data-agent="${a.agent_id}"`)));
ok('the active scope is marked for assistive tech', has(sw, 'aria-selected="true"'));
ok('exactly one tab is active', (sw.match(/aria-selected="true"/g) ?? []).length === 1);
ok('each tab shows its campaign count', has(sw, `${A127.campaigns} camp`));
ok('and flags paused campaigns on the tab', has(sw, `${A127.paused_campaigns}⏸`));

const allPaused = agents.map((a) => (a.agent_id === 127 ? { ...a, paused: true } : a));
ok('a fully paused agent is styled as such', has(renderToStaticMarkup(<AgentSwitcher agents={allPaused} agentId={131} onPick={() => {}} />), 'is-paused'));

ok('the topbar restates the scope on every screen', has(renderToStaticMarkup(<AgentChip agent={A127} />), 'agent 127'));
ok('and says so when the whole agent is paused', has(renderToStaticMarkup(<AgentChip agent={{ ...A127, paused: true }} />), 'all paused'));

const pauseAll = renderToStaticMarkup(
  <AgentPauseConfirm agent={A127} mode="pause" running={3} busy={false} onClose={() => {}} onConfirm={() => {}} />,
);
ok('pause-all is confirmed, not one-click', has(pauseAll, 'role="dialog"'));
ok('the confirmation says it hits every campaign on the agent', has(pauseAll, 'every campaign on agent 127'));
ok('names the blast radius as a number', has(pauseAll, `Pause all ${A127.campaigns}`) && has(pauseAll, 'Campaigns affected'));
ok('and names the 409 consequence', has(pauseAll, '409'));
const resumeAll = renderToStaticMarkup(
  <AgentPauseConfirm agent={{ ...A127, paused: true }} mode="resume" running={0} busy={false} onClose={() => {}} onConfirm={() => {}} />,
);
ok('resume-all warns it un-pauses campaigns somebody paused by hand', has(resumeAll, 'paused individually'));

// The picker is already scoped, so it must not offer a second agent's campaigns.
const scoped = mockCampaigns.filter((c) => c.agent_id === 127);
const picker = renderToStaticMarkup(<CampaignPicker campaigns={scoped} campaignId={scoped[0].id} onPick={() => {}} />);
ok('the campaign picker no longer groups agents together', !has(picker, '<optgroup'));
ok('it lists only the scoped agent’s campaigns', (picker.match(/<option/g) ?? []).length === scoped.length);
ok('an agent with no campaigns says so instead of going blank', has(renderToStaticMarkup(<CampaignPicker campaigns={[]} campaignId={null} onPick={() => {}} />), 'agent has no campaigns'));

// --- test call --------------------------------------------------------------
const nums = renderToStaticMarkup(<TestNumberTable numbers={mockTestNumbers} selected="9379747274" onPick={() => {}} />);
ok('the allow-list is listed, not typed in', has(nums, '9379747274') && !has(nums, '<input'));
ok('each number says whether it resolves to a lead', has(nums, '>found<') && has(nums, '>no lead<'));
ok('the selected number is marked', has(nums, 'is-focus') && has(nums, 'Selected'));

const dry = mockTestCall('9379747274');
ok('the dry-run fixture makes no network call', dry.dry_run && dry.status === 'simulated' && dry.http_status === null);

const previewed = mockTestCall('9379747274', undefined, 'preview');
ok('a preview is labelled a preview, never "simulated"', previewed.status === 'preview');
ok('and is not badged as a success', !has(renderToStaticMarkup(<TestCallResultView result={previewed} kind="preview" />), 'badge-ok'));

const prev = renderToStaticMarkup(<TestCallResultView result={dry} kind="preview" scopeAgentId={125} />);
ok('preview resolves the lead', has(prev, 'Lead uuid') && has(prev, dry.lead!.lead_uuid));
ok('preview shows the literal request URL', has(prev, dry.would_post!.url));
ok('and the literal body, in the mono face', has(prev, 'class="payload"') && has(prev, 'scheduled_time'));

const trig = renderToStaticMarkup(<TestCallResultView result={dry} kind="trigger" scopeAgentId={125} />);
ok('a dry-run trigger is never dressed as a green success', !has(trig, 'badge-ok'));
ok('it says plainly that no call was placed', has(trig, 'No call was placed and no phone rang'));
ok('and that it proves nothing about connectivity', has(trig, 'nothing about connectivity to Formi'));
ok('while still claiming what it does prove', has(trig, 'resolution and payload shape'));

const offScope = renderToStaticMarkup(<TestCallResultView result={dry} kind="preview" scopeAgentId={999} />);
ok('a lead on another agent is flagged against the current scope', has(offScope, 'agent 999') && has(offScope, 'warnbox'));

const posted: TestCallResult = { ...dry, dry_run: false, status: 'posted', http_status: 200, response: '{"ok":true}' };
const live = renderToStaticMarkup(<TestCallResultView result={posted} kind="trigger" scopeAgentId={125} />);
ok('a real posted call renders status, http status and body', has(live, 'badge-ok') && has(live, '200') && has(live, 'ok'));
ok('and only then says a call is scheduled', has(live, 'A real call is now scheduled'));

const failed = renderToStaticMarkup(<TestCallResultView result={{ ...posted, status: 'failed', http_status: 502, response: 'upstream timeout' }} kind="trigger" />);
ok('a failed post is a warning, not a shrug', has(failed, 'warnbox') && has(failed, '502'));

const missing = renderToStaticMarkup(<TestCallResultView result={mockTestCall('9845012345')} kind="trigger" />);
ok('an allow-listed number with no lead explains itself', has(missing, 'nothing to schedule') && !has(missing, 'class="payload"'));

const confirmDry = renderToStaticMarkup(
  <TriggerConfirm phone="9379747274" live={false} typed="" onTyped={() => {}} busy={false} onClose={() => {}} onConfirm={() => {}} />,
);
ok('the dry-run confirmation promises no network call', has(confirmDry, 'no network call') && !has(confirmDry, 'btn btn-live'));
ok('with no time picked it says the server chooses one', has(confirmDry, 'next free minute'));

const confirmAt = renderToStaticMarkup(
  <TriggerConfirm phone="9379747274" live={false} when="2026-09-01T15:07" typed="" onTyped={() => {}} busy={false} onClose={() => {}} onConfirm={() => {}} />,
);
ok('a hand-picked time is shown before you commit to it', has(confirmAt, '2026-09-01 15:07'));

const confirmLive = renderToStaticMarkup(
  <TriggerConfirm phone="9379747274" live typed="" onTyped={() => {}} busy={false} onClose={() => {}} onConfirm={() => {}} />,
);
ok('a live trigger is warm-styled and destructive', has(confirmLive, 'btn btn-live') && has(confirmLive, 'warnbox'));
ok('it names the number being dialled', has(confirmLive, 'Dial 9379747274'));
ok('and costs a typed confirmation of that exact number', has(confirmLive, 'placeholder="9379747274"'));
ok('the dial button stays disabled until the number is typed', has(confirmLive, 'disabled=""'));
ok(
  'and enables on an exact match only',
  !renderToStaticMarkup(
    <TriggerConfirm phone="9379747274" live typed="9379747274" onTyped={() => {}} busy={false} onClose={() => {}} onConfirm={() => {}} />,
  ).includes('disabled=""'),
);

// --- the day screen: the only place an operator agrees to place calls --------
// Every assertion here is safety-bearing. The failure this guards against is a
// screen that reads as "calls went out" when nothing was approved, or as
// "approved" when the operator had unticked half the plan.
{
  const base = mockDay('2026-09-09', 'auto');
  const day = (over: Partial<typeof base>) => ({ ...base, ...over });
  const head = (d: typeof base) =>
    renderToStaticMarkup(
      <Headline day={d} busy="" onPrepare={() => {}} onApprove={() => {}} onPick={() => {}} />,
    );

  ok('every bucket ticked sends the empty list the server reads as "all"',
     wireBuckets(['M0', 'F5'], ['M0', 'F5']).length === 0);
  ok('a partial tick goes on the wire verbatim, never as "all"',
     wireBuckets(['M0'], ['M0', 'F5']).join() === 'M0');

  const waiting = head(base);
  ok('an unapproved day says so in the headline', has(waiting, 'Nothing is dialled yet'));
  ok('and never claims calls are on the clock', !has(waiting, 'calls on the clock'));

  ok('a day with no campaigns points at the campaign list, not at a dial button',
     has(head(day({ status: 'no_campaigns' })), 'Choose campaigns'));
  ok('and says picking campaigns places no call',
     has(head(day({ status: 'no_campaigns' })), 'places no call'));

  // Both of those hand Headline the status by hand; this pins the fixture that
  // produces it. An unscoped `mockDay` always has campaigns, so nothing else
  // here evaluates the empty-scope branch -- pausing an agent empties its
  // roster and is the one way offline to reach it. Restored campaign by
  // campaign: one of 125's was already paused, so a blanket un-pause would
  // leave the fixture disagreeing with itself for every check after this one.
  const paused125 = mockCampaigns.filter((c) => c.agent_id === 125).map((c) => c.paused);
  mockAgentPause(125, true);
  ok('an agent with every campaign paused is a day with no campaigns, offline too',
     mockDay('2026-09-09', 'auto', 125).status === 'no_campaigns');
  mockCampaigns.filter((c) => c.agent_id === 125).forEach((c, i) => { c.paused = paused125[i]; });

  // The picker's one wire-bearing decision. Everything it sends changes who is
  // dialled once the day is approved, so it sends the difference and nothing else.
  {
    const c = (id: number, over: Partial<Campaign> = {}): Campaign =>
      ({ id, agent_id: 1, warehouse_id: id, name: `c${id}`, enabled: true, paused: false, ...over });
    const all = [c(1), c(2, { autopilot: true }), c(3, { autopilot: true }), c(4, { enabled: false }),
                 c(5, { hidden: true })];

    const d = autopilotDiff(all, new Set([1, 2, 4, 5]));
    ok('picking a campaign that is already in the plan sends nothing for it',
       !d.arm.includes(2) && !d.disarm.includes(2));
    ok('a newly ticked campaign is armed', d.arm.join() === '1');
    ok('an unticked campaign that was in the plan is taken out', d.disarm.join() === '3');
    ok('a disabled campaign is never armed, however it was ticked',
       !d.arm.includes(4) && !d.disarm.includes(4));
    // A stale tick on a campaign hidden since the picker opened would be a 409
    // on save, and the operator would have to work out which of 69 rows it was.
    ok('a hidden campaign is never armed, however it was ticked',
       !d.arm.includes(5) && !d.disarm.includes(5));
    ok('ticking nothing takes every armed campaign out and arms none',
       autopilotDiff(all, new Set()).arm.length === 0 &&
       autopilotDiff(all, new Set()).disarm.join() === '2,3');
    ok('re-saving without a change puts nothing on the wire',
       autopilotDiff(all, new Set([2, 3])).arm.length === 0 &&
       autopilotDiff(all, new Set([2, 3])).disarm.length === 0);

    // The settings screen ticks both lists with one Set, and its two buttons
    // each fire one request per row. Sending a campaign to the wrong button is
    // dozens of pointless writes, and un-hide rewrites a note hide never wrote.
    const s = selectionSplit(all, new Set([1, 2, 5]));
    ok('Hide selected acts only on the campaigns that are visible',
       s.hide.map((x) => x.id).join() === '1,2');
    ok('Un-hide selected acts only on the campaigns that are hidden',
       s.unhide.map((x) => x.id).join() === '5');
    ok('an untouched campaign is in neither list',
       !s.hide.some((x) => x.id === 3) && !s.unhide.some((x) => x.id === 3));
  }
  ok('an unbuilt plan offers to build it, and says building dials nothing',
     has(head(day({ status: 'not_prepared' })), 'dials nothing'));

  const shortDay = head(day({ capacity_before_close: 40 }));
  ok('when the day is too short the headline says how many actually fit',
     has(shortDay, 'still fit before 20:00'));
  ok('and says the rest come back tomorrow rather than vanishing',
     has(shortDay, 'tomorrow'));

  // Each campaign carries its own dial window, editable on its own. `day.window`
  // is the ENVELOPE across the armed ones, so naming its end as "the" close time
  // is only honest while they all agree.
  {
    const mixed = day({ capacity_before_close: 40, window_varies: true });
    ok('a shared window is still named outright', has(shortDay, 'before 20:00'));
    ok('windows that differ are never given one close time',
       !has(head(mixed), 'before 20:00') && has(head(mixed), 'their campaigns close'));
    ok('a shared window still yields the one close time everybody keeps',
       closesAt(base) === '20:00');
    ok('approving names each campaign’s own window when they differ',
       has(head(day({ window_varies: true })), 'own campaign'));
  }

  const shut = head(day({ window_open: false }));
  ok('outside 09:00–20:00 the approve button is dead', has(shut, 'disabled=""'));
  ok('and says why, instead of failing silently', has(shut, 'has closed for today'));

  const empty = head(day({ totals: { ...base.totals, ready: 0 } }));
  ok('a plan with nothing ready cannot be approved', has(empty, 'disabled=""'));

  const modal = (over: Partial<typeof base>, buckets: string[], shown: string[]) =>
    renderToStaticMarkup(
      <ApproveDay agent={null} day={day(over)} buckets={buckets} shown={shown} onClose={() => {}} onDone={() => {}} />,
    );

  const dry = modal({}, [], ['M0', 'F5', 'E0', 'F4']);
  ok('a dry-run approval promises nothing reaches Formi', has(dry, 'nothing reaches Formi'));
  ok('and is not dressed as a live dial', !has(dry, 'btn btn-live') && has(dry, 'Simulate up to'));
  ok('a dry run needs no typed confirmation', !has(dry, 'disabled=""'));

  const liveDay = modal({ dry_run: false }, [], ['M0', 'F5', 'E0', 'F4']);
  ok('a live approval is warm-styled and warns there is no undo',
     has(liveDay, 'btn btn-live') && has(liveDay, 'no undo'));
  ok('and costs the typed word DIAL', has(liveDay, 'placeholder="DIAL"') && has(liveDay, 'disabled=""'));

  const partial = modal({}, ['M0'], ['M0']);
  ok('a partial tick is named in the confirmation, not summarised as "all"',
     has(partial, '>M0<') && !has(partial, 'all of them'));
  ok('the confirmation states the RED order that decides who survives a short day',
     has(partial, 'best RED band first'));

  const none = modal({}, [], []);
  ok('unticking every bucket blocks the approve button rather than dialling all of them',
     has(none, 'disabled=""') && has(none, 'nothing to dial'));
}

// --- has a call already been placed for these leads? ------------------------
//
// The engine does skip every lead Formi has already queued — but it checks at
// PLAN time. On 12 Sep 2026 the plans were built in the morning and approved six
// hours later, so every call booked in Formi in between was invisible to the
// approval. `already_booked` is therefore only as good as the plan's age, which
// makes that age arithmetic this screen has to get right.
{
  const base = mockDay('2026-09-09', 'auto');
  // `runs.created_at` written the way the server writes it: naive IST, no
  // offset (api/db.py's `now_iso`).
  const ist = (ms: number) => new Date(ms + 330 * 60000).toISOString().slice(0, 19);
  const built = (...ago: (number | null)[]) =>
    ago.map((m) => ({ plan_built_at: m === null ? null : ist(Date.now() - m * 60000) })) as
      DayCampaign[];

  ok('a plan built minutes ago is not stale', !planAge(built(5), Date.now()).stale);
  ok('and one past the threshold is', planAge(built(STALE_MIN + 1), Date.now()).stale);
  ok('a plan the server gave no build time for warns about nothing — a backend '
     + 'too old to send the field must not cry wolf on every approval',
     !planAge(built(null, null), Date.now()).stale
     && planAge(built(null, null), Date.now()).minutes === 0);
  ok('the OLDEST plan on the panel decides, not the freshest one beside it',
     planAge(built(5, STALE_MIN + 1, 2), Date.now()).stale);

  // The half that cannot be proved on a machine already on IST, and the half
  // that bites hardest: `plan_built_at` carries no offset, and ECMA-262 reads an
  // ISO date-TIME without one as the BROWSER'S LOCAL time. From a browser in UTC
  // a bare `new Date(builtAt)` makes every plan 5h30m YOUNGER than it is, so a
  // plan built six hours ago reads as half an hour old and the stale warning
  // never appears at all; east of IST it reads older instead and a plan built a
  // minute ago cries wolf. node re-reads process.env.TZ per call, so the non-IST
  // browser is reachable from here rather than only from a CI box abroad.
  const under = (tz: string, f: () => number) => {
    const before = process.env.TZ;
    process.env.TZ = tz;
    try {
      return f();
    } finally {
      if (before === undefined) delete process.env.TZ;
      else process.env.TZ = before;
    }
  };
  // 09:00 IST IS 03:30Z. Both sides written out, so neither reads a clock.
  const age = () =>
    planAge([{ plan_built_at: '2026-09-13T09:00:00' }] as DayCampaign[],
            Date.parse('2026-09-13T03:30:00Z') + 45 * 60000).minutes;
  // Named for what it proves on ANY host, not for the offset. This dev box is
  // Asia/Calcutta, where the naive parse and the pinned one are the same instant
  // — so it says nothing about the offset (dropping `+05:30` leaves it green)
  // and everything about the arithmetic: /60000 mistyped as /6000 reddens here.
  // The offset claim belongs to the check below, which is its only guard.
  ok('a plan built 45 minutes ago is 45 minutes old', age() === 45);
  ok('and is the same age from a browser that is not on IST, either side of it',
     under('UTC', age) === 45 && under('Pacific/Auckland', age) === 45
     && under('America/New_York', age) === 45);
  // That check has teeth only while node honours a mid-process `process.env.TZ`
  // write. If it ever stops, `under` silently becomes a no-op, every zone reads
  // as IST, the line above passes with the offset deleted, and the `+05:30` fix
  // is unguarded with the whole gate green — verified: neuter `under` and
  // dropping the offset passes everything. So prove the mechanism bites, by
  // asserting the UNPINNED parse really is wrong under UTC: a naive `new Date`
  // reads 09:00 IST as 09:00Z, five and a half hours late, making the plan
  // 330 minutes YOUNGER than its true 45.
  const bare = (tz: string) => under(tz, () => Math.round(
    (Date.parse('2026-09-13T03:30:00Z') + 45 * 60000
     - new Date('2026-09-13T09:00:00').getTime()) / 60000));
  ok('and the forced timezone really bites — an unpinned parse IS wrong under UTC, '
     + 'so a TZ write node stopped honouring goes red here rather than quiet',
     bare('UTC') === 45 - 330 && bare('Asia/Calcutta') === 45);

  const modal = (over: Partial<typeof base>) =>
    renderToStaticMarkup(
      <ApproveDay agent={null} day={{ ...base, ...over }} buckets={[]} shown={['M0']}
                  onClose={() => {}} onDone={() => {}} />,
    );
  const every = (over: Partial<DayCampaign>) =>
    ({ campaigns: base.campaigns.map((c) => ({ ...c, ...over })) });

  ok('the fixture ships a FRESH plan, or neither check below proves anything',
     !planAge(base.campaigns, Date.now()).stale);
  const fresh = modal({});
  ok('a fresh plan is approved with no staleness warning in the way',
     !has(fresh, 'Re-check now'));
  ok('and still says how many leads Formi had already booked when it was built',
     has(fresh, 'already on Formi'));

  const old = modal(every({ plan_built_at: ist(Date.now() - 6 * 60 * 60000) }));
  ok('a plan built six hours ago says so, in hours, before anybody dials it',
     has(old, 'This plan is 6h 0m old'));
  ok('and offers the re-check rather than only complaining', has(old, 'Re-check now'));

  ok('nothing already booked says nothing, rather than "0 leads were"',
     !has(modal(every({ already_booked: 0 })), 'already on Formi'));
  ok('a campaign that has never dialled reads as never, not as a blank',
     has(modal(every({ last_dialled: null })), 'never'));
}

// --- one panel per agent: two languages, two decisions ----------------------
//
// Agents 125 and 127 used to share one plan, one "ready" number and one Approve
// button, so approving one language approved the other with it. Each panel now
// reads its own agent's day. Its heading is the label the SERVER gave that
// agent (AGENT_LANGUAGES) — a language name written into this client would be a
// fact about one deployment dressed up as a fact about the console.
{
  const DATE = '2026-09-13';
  const labelled = (id: number, language: string | null): Agent => ({
    ...agentsFrom(mockCampaigns).find((a) => a.agent_id === id)!,
    language,
  });
  const head = (agent: Agent | null, day = agent ? mockDay(DATE, 'auto', agent.agent_id) : null) =>
    renderToStaticMarkup(<PanelHead agent={agent} day={day} onReload={() => {}} />);

  // The fixture arms the same first two campaigns for every agent, so the two
  // counts would coincide by accident and "its own, not the sum" would prove
  // nothing. Pausing one of 127's makes them differ; restored straight after,
  // so every check below this one still sees the fixture it was written for.
  const six = mockCampaigns.find((c) => c.id === 6)!;
  six.paused = true;
  const hindi = head(labelled(125, 'Hindi'));
  const tamil = head(labelled(127, 'Tamil'));
  const hin = mockDay(DATE, 'auto', 125).totals.ready;
  const tam = mockDay(DATE, 'auto', 127).totals.ready;
  six.paused = false;

  ok('the two agents really are having different days, or nothing below is tested',
     hin > 0 && tam > 0 && hin !== tam);
  ok('each agent gets its own panel, headed by the label the server gave it',
     has(hindi, '>Hindi</h2>') && has(tamil, '>Tamil</h2>'));
  ok('a panel counts its own agent’s leads…',
     has(hindi, `${n(hin)} ready`) && has(tamil, `${n(tam)} ready`));
  ok('…and never the two languages added together',
     !has(hindi + tamil, `${n(hin + tam)} ready`));
  ok('the heading is whatever the server labelled the agent, not a name this client knows',
     has(head(labelled(125, 'Kannada')), '>Kannada</h2>'));
  ok('an agent the deployment never labelled is headed by its name, not an invented language',
     has(head(labelled(125, null)), '>Agent 125</h2>') && !has(head(labelled(125, null)), 'Hindi'));

  ok('one panel per agent, from the server’s list and nowhere else',
     panelsFor(agentsFrom(mockCampaigns)).length === agents.length);
  ok('a backend without /api/agents still gets a day — one unscoped panel, not none',
     panelsFor(null).length === 1 && panelsFor(null)[0] === null);
  ok('and so does a deployment whose agent list comes back empty',
     panelsFor([]).length === 1 && panelsFor([])[0] === null);
  ok('that unscoped panel invents no heading at all', !has(head(null), '<h2'));

  // Each panel's Refresh re-reads that panel. A single screen-wide one could
  // not honestly reload two panels that load independently, so there is none.
  ok('every panel carries its own Refresh, named for the agent it reloads',
     has(hindi, 'aria-label="Refresh Hindi"') && has(tamil, 'aria-label="Refresh Tamil"'));

  // The button itself cannot be clicked here, so what it would SEND is checked
  // instead. This is the only call that reaches Formi: getting the agent wrong
  // dials a language nobody approved.
  //
  // `wholeDay` is deliberately the UNSCOPED day — `agent_id === null` — handed to
  // a scoped panel. It is the one fixture that tells the two possible sources
  // apart: the panel's own identity, or the scope the server happened to echo
  // back. Reading the echo passes every other check in this file and turns a
  // panel headed "Hindi" into a whole-day dial the moment the echo goes missing.
  const wholeDay = mockDay(DATE, 'auto');
  ok('the day fixture really is unscoped, or the two sources cannot be told apart',
     wholeDay.agent_id === null);
  // The date/kind clause is not decoration: `ApproveArgs[0]` and `[1]` are both
  // `string`, so transposing them type-checks and goes out on the wire as a
  // request for the wrong day. `DATE` and `'auto'` cannot be swapped unnoticed.
  const tamilArgs = approveArgs(labelled(127, 'Tamil'), wholeDay, []);
  ok('approving a panel dials that panel’s agent and nobody else, on the day and wave it is showing',
     tamilArgs[4] === 127 && tamilArgs[0] === DATE && tamilArgs[1] === 'auto');
  ok('and takes it from the panel, never from the agent the response echoed back',
     approveArgs(labelled(125, 'Hindi'), wholeDay, [])[4] === 125);
  ok('an unscoped panel still approves the whole day, exactly as before scoping',
     approveArgs(null, wholeDay, [])[4] === undefined);

  // The whole-day Retry is the second call that reaches Formi. An empty
  // `campaign_ids` means EVERY armed campaign to the backend, so a retry that
  // lost its scope dials the language this panel never approved.
  const dialled = approveArgs(labelled(125, 'Hindi'), wholeDay, ['M0']);
  ok('a retry dials the same agent the approve it is retrying dialled',
     retryArgs(dialled, [7])[4] === 125);
  ok('with the same day, wave and buckets, narrowed only to the campaigns that never started',
     retryArgs(dialled, [7])[0] === DATE && retryArgs(dialled, [7])[1] === 'auto' &&
     retryArgs(dialled, [7])[2].join() === 'M0' && retryArgs(dialled, [7])[3].join() === '7');
  ok('and an unscoped approve retries unscoped, exactly as before scoping',
     retryArgs(approveArgs(null, wholeDay, []), [7])[4] === undefined);

  // The operator has to be told WHICH cohort the button in front of them dials.
  // Everything else in that facts block — day, campaigns, buckets, selected,
  // window — reads identically whether one language or both are about to go out.
  //
  // `ApproveDay` only mounts behind `approving && d`, neither of which a static
  // render reaches, so the panel's wiring is `approveModal` and it is rendered
  // here with the agent a panel would hand it. Nulling that agent inside
  // `approveModal` is exactly the mis-scope Finding 2 named, and it now shows up
  // in the markup as the wrong cohort.
  const approveFor = (a: Agent | null) =>
    renderToStaticMarkup(
      approveModal(a, a ? mockDay(DATE, 'auto', a.agent_id) : wholeDay, ['M0'], ['M0', 'F5'],
                   () => {}, () => {}),
    );
  const tamilApprove = approveFor(labelled(127, 'Tamil'));
  ok('the approve confirmation names the cohort it is about to dial',
     has(tamilApprove, '<b>Tamil</b>'));
  ok('and the panel hands that modal its own agent, never a wider one',
     !has(tamilApprove, 'whole roster'));
  ok('the cohort is worded like the panel heading above it, not re-derived',
     has(approveFor(labelled(125, null)), '<b>Agent 125</b>'));
  ok('an unscoped approve says out loud that it dials every agent',
     has(approveFor(null), 'every agent — the whole roster') &&
     !has(approveFor(null), '<b>Tamil</b>'));

  // The other end of the same thread: what `DayPanel` hands DOWN. Only the
  // heading is reachable statically — `d` is null with no effects, so nothing
  // below it renders — but that is one real call site pinned rather than none.
  ok('a panel heads itself with the agent it was handed, not with nobody',
     has(renderToStaticMarkup(
       <DayPanel agent={labelled(127, 'Tamil')} date={DATE} kind="auto" rev={0} onPick={() => {}} />,
     ), '>Tamil</h2>'));

  const screen = renderToStaticMarkup(<Today />);
  ok('the day screen renders its panels before any plan has arrived',
     has(screen, '<h1>The day</h1>') && has(screen, 'Reading today’s plan'));
  ok('and holds no Approve button of its own — approving is per agent',
     !has(screen, 'Approve'));
}

// --- the day goes out one campaign at a time --------------------------------
//
// One Approve used to post every campaign in a single blocking request: 2,967
// calls on 12 Sep 2026, one timeout away from losing the day, with nothing on
// screen but a spinner. The loop that walks the day lives in a click handler
// the static renderer never reaches, so the QUEUE it walks is a pure function
// and is driven here instead.
{
  const DATE = '2026-09-13';
  const labelled = (id: number, language: string | null): Agent => ({
    ...agentsFrom(mockCampaigns).find((a) => a.agent_id === id)!,
    language,
  });
  // Again the UNSCOPED day handed to a scoped panel — the one fixture that tells
  // the panel's own identity apart from the scope the server echoed back.
  const wholeDay = mockDay(DATE, 'auto');
  const tamilDay = mockDay(DATE, 'auto', 127);
  const hindiDay = mockDay(DATE, 'auto', 125);
  ok('the day fixture really is unscoped, or the two sources cannot be told apart',
     wholeDay.agent_id === null && tamilDay.agent_id === 127);

  const queue = dialQueue(approveArgs(labelled(127, 'Tamil'), tamilDay, ['M0']), tamilDay);
  ok('a day is split into one request per campaign, never posted as one',
     queue.length > 1 && queue.length === tamilDay.campaigns.filter((c) => c.run_status === 'planned').length);
  ok('each request names exactly one campaign, and each campaign exactly once',
     queue.every((e) => e.args[3].length === 1) &&
     new Set(queue.map((e) => e.args[3][0])).size === queue.length);
  ok('every request carries the day, wave and buckets the operator approved',
     queue.every((e) => e.args[0] === DATE && e.args[1] === 'auto' && e.args[2].join() === 'M0'));
  ok('and the progress bar can name each one while it is going',
     queue.every((e) => e.name.length > 0 && e.campaign_id === e.args[3][0]));

  // Splitting one request into twelve must not become twelve chances to
  // re-derive the scope from the server's echo. `wholeDay` echoes nothing, so a
  // queue that read the echo would go out unscoped — every armed campaign on
  // every agent, for a panel headed "Tamil".
  const fromEcho = dialQueue(approveArgs(labelled(127, 'Tamil'), wholeDay, []), wholeDay);
  ok('every campaign in the queue is scoped to the PANEL’s agent, not to the response’s echo',
     fromEcho.length > 0 && fromEcho.every((e) => e.args[4] === 127));
  ok('and an unscoped panel still queues unscoped, exactly as before scoping',
     dialQueue(approveArgs(null, wholeDay, []), wholeDay).every((e) => e.args[4] === undefined));
  // A campaign already dialled this morning must not be queued again by the
  // afternoon approve — the backend would no-op it, but each no-op is a round
  // trip and a row on the operator's progress list saying nothing happened.
  const committed = {
    ...wholeDay,
    campaigns: wholeDay.campaigns.map((c) => ({ ...c, run_status: 'committed' as const })),
  };
  ok('only campaigns still waiting on an approval are queued',
     wholeDay.campaigns.length > 0 &&
     dialQueue(approveArgs(null, committed, []), committed).length === 0);

  // --- the panel's two witnesses of its own scope ----------------------------
  //
  // `args[4]` is what the panel asserts it is about to dial; `day.agent_id` is
  // what the server says it narrowed this plan to. They agree in every
  // legitimate state. The echo is read HERE and nowhere else — as a check that
  // can only refuse a dial, never as a source that could widen one.
  ok('a panel whose plan and whose button name the same agent is free to dial',
     scopeMismatch(approveArgs(labelled(125, 'Hindi'), hindiDay, []), hindiDay) === null);
  ok('and so is an unscoped panel reading an unscoped day',
     scopeMismatch(approveArgs(null, wholeDay, []), wholeDay) === null);

  const dialsWider = scopeMismatch(approveArgs(null, hindiDay, []), hindiDay);
  ok('one agent’s plan under a whole-roster dial is refused, not sent',
     dialsWider !== null && has(dialsWider, 'plan for agent 125') &&
     has(dialsWider, 'would dial every agent'));
  const dialsNarrower = scopeMismatch(approveArgs(labelled(127, 'Tamil'), wholeDay, []), wholeDay);
  ok('and so is the whole roster’s plan under a one-agent dial',
     dialsNarrower !== null && has(dialsNarrower, 'plan for every agent') &&
     has(dialsNarrower, 'would dial agent 127'));

  // The refusal has to reach the operator, not just the console: the modal is
  // rendered through the same `approveModal` a panel wires, with a plan and an
  // agent that disagree.
  const disagrees = renderToStaticMarkup(
    approveModal(labelled(127, 'Tamil'), wholeDay, ['M0'], ['M0', 'F5'], () => {}, () => {}),
  );
  ok('a modal whose plan and button disagree says so in words the operator can act on',
     has(disagrees, 'disagree about who gets called') && has(disagrees, 'plan for every agent'));
  ok('and the dial button is dead while they do — a dry run has no other reason to be',
     has(disagrees, 'disabled=""'));

  // --- what is on screen while it runs ---------------------------------------
  const rows: ProgressRow[] = [
    { campaign_id: 1, name: 'Renewal Hindi 1', state: 'done' },
    { campaign_id: 2, name: 'Renewal Hindi 2', state: 'failed' },
    { campaign_id: 3, name: 'Renewal Hindi 3', state: 'running' },
  ];
  const bar = renderToStaticMarkup(
    <DayProgress done={2} total={5} current="Renewal Hindi 3" rows={rows} />,
  );
  ok('the bar fills to the share of CAMPAIGNS finished, not of calls placed',
     has(bar, 'width:40%'));
  ok('and says the same thing in numbers, for anyone who cannot read a bar',
     has(bar, '2 of 5 campaigns'));
  ok('the campaign being dialled right now is named, so a slow one is not a hang',
     has(bar, 'Dialling Renewal Hindi 3'));
  ok('a campaign that failed is flagged on its own row while the rest carry on',
     has(bar, 'lucide-triangle-alert') && has(bar, 'color:var(--bad)') &&
     has(bar, 'Renewal Hindi 2'));
  ok('one that is still going reads as going, not as done',
     has(bar, 'lucide-loader-circle spin'));
  ok('and a day with no failures shows no failure at all',
     !has(renderToStaticMarkup(
       <DayProgress done={1} total={1} current={null} rows={rows.slice(0, 1)} />),
       'lucide-triangle-alert'));
  ok('with nothing to dial the bar is empty rather than dividing by zero',
     has(renderToStaticMarkup(<DayProgress done={0} total={0} current={null} rows={[]} />), 'width:0%'));

  // --- folding the per-campaign answers back into one result ------------------
  const part = (over: Partial<ApproveResult>): ApproveResult => ({
    date: DATE, kind: 'auto', wave: 'morning', dry_run: true, buckets: ['M0'],
    approved: 1, posted: 100, failed: 0, not_dialled: 0, campaigns: [], ...over,
  });
  const stopped = mergeResults([], mockDay(DATE, 'auto'));
  ok('stopping before the first campaign still leaves a result on screen, not a crash',
     stopped.approved === 0 && stopped.posted === 0 && stopped.failed === 0 &&
     stopped.not_dialled === 0 && stopped.campaigns.length === 0);
  ok('and it is still this day’s result, not a blank one',
     stopped.date === DATE && stopped.kind === 'auto');
  const whole = mergeResults(
    [part({ campaigns: [{ campaign_id: 1, name: 'A', status: 'approved', posted: 100 }] }),
     part({ posted: 40, failed: 3, not_dialled: 7,
            campaigns: [{ campaign_id: 2, name: 'B', status: 'approved', posted: 40, failed: 3 }] })],
    mockDay(DATE, 'auto'),
  );
  ok('every campaign’s numbers are added, never overwritten by the last one home',
     whole.approved === 2 && whole.posted === 140 && whole.failed === 3 && whole.not_dialled === 7);
  ok('and every campaign keeps its own row in the result',
     whole.campaigns.length === 2 && whole.campaigns.map((c) => c.name).join() === 'A,B');
}

// --- the dial result: what went out, and what did not -----------------------
//
// Asked for on 13 Sep 2026. The numbers were always in the approve response and
// were summed into a single toast line, then dropped when the modal closed --
// so a campaign the operator ticked could fail to start and leave nothing on
// screen. `not_dialled` and `failed` are the two halves of "not scheduled" and
// only one of them is a fault, which is the distinction under test here.
{
  const result = (over: Partial<ApproveResult> = {}): ApproveResult => ({
    date: '2026-09-13', kind: 'auto', wave: 'Morning', dry_run: false, buckets: 'all',
    approved: 2, posted: 300, failed: 0, not_dialled: 0,
    campaigns: [
      { campaign_id: 1, name: 'Clean campaign', status: 'approved', posted: 300, failed: 0 },
    ],
    ...over,
  });
  const dialres = (over: Partial<ApproveResult> = {}) =>
    renderToStaticMarkup(
      <DialResult res={result(over)} args={['2026-09-13', 'auto', [], [], undefined]} onChange={() => {}} />,
    );

  const clean = dialres();
  ok('a clean day reads as everything scheduled and offers no retry',
     has(clean, '300 scheduled') && has(clean, '0 not scheduled')
     && !has(clean, 'Retry'));

  const split = dialres({ posted: 300, failed: 12, not_dialled: 340 });
  ok('the bar splits scheduled from not scheduled',
     has(split, '300 scheduled') && has(split, '352 not scheduled'));
  ok('and splits that second number again, because only one half is a fault',
     has(split, '12 refused by Formi') && has(split, '340 did not fit'));

  const shut = dialres({
    approved: 0, posted: 0,
    campaigns: [{
      campaign_id: 1, name: 'Late campaign', status: 'window_closed', run_id: 7,
      detail: 'the 10:00-19:00 window has closed (it is 19:24)',
    }],
  });
  ok('a campaign that never started says why, on its own row',
     has(shut, 'Late campaign') && has(shut, 'window has closed (it is 19:24)'));
  ok('and can be re-run, since it never dialled',
     has(shut, 'Retry 1 campaign'));

  const refused = dialres({
    failed: 12,
    campaigns: [{
      campaign_id: 1, name: 'Refused campaign', status: 'approved',
      posted: 288, failed: 12, run_id: 7,
    }],
  });
  ok('a campaign Formi refused calls for offers to send exactly those again',
     has(refused, 'Retry 12 calls'));
  // Re-approving it would be a no-op -- `_approve_one` answers already_committed
  // -- and would read as an offer to dial the other 288 a second time.
  ok('but is not offered as a whole-campaign re-run', !has(refused, 'Retry 1 campaign'));
}

// --- the call log: proof, not paperwork -------------------------------------
{
  const rows = mockDialLog('2026-09-09').rows;
  ok('the offline call-log fixture is entirely dry-run, so it can never read as proof',
     rows.every((r) => r.dry_run && r.outcome === 'simulated' && r.verified === 'simulated'));

  const sim = renderToStaticMarkup(<LogRow r={rows[0]} />);
  ok('a simulated row says Dry run and never Dialled', has(sim, 'Dry run') && !has(sim, 'Dialled'));

  const real = renderToStaticMarkup(
    <LogRow r={{ ...rows[0], dry_run: false, outcome: 'posted', verified: 'dialled', duration_sec: 47 }} />,
  );
  ok('only a warehouse-verified row reads as Dialled', has(real, 'Dialled') && has(real, '47s'));

  const lost = renderToStaticMarkup(
    <LogRow r={{ ...rows[0], dry_run: false, outcome: 'posted', verified: 'missing' }} />,
  );
  ok('a call Formi accepted but never dialled is not shown as a success',
     has(lost, 'Not in the warehouse') && !has(lost, 'Dialled'));

  const failedPost = renderToStaticMarkup(
    <LogRow r={{ ...rows[0], dry_run: false, outcome: 'failed', http_status: 502, response: 'upstream timeout', verified: 'missing' }} />,
  );
  ok('a rejected post shows the server’s own words', has(failedPost, 'upstream timeout') && has(failedPost, 'badge-bad'));
}

// --- the urgency ramp is written twice, so assert it agrees ------------------
// BUCKET_COLOR fills the charts and the bucket dots; --b-F1..--b-D0 in app.css
// dresses the D0 stripes and the F5 headline. Two copies of one palette is
// exactly the shape of bug that had the reports counting three different
// numbers for one day, so the copies are compared rather than trusted.
{
  const css = readFileSync('src/styles/app.css', 'utf8');
  const declared = new Map<string, string>();
  for (const [, key, hex] of css.matchAll(/--b-(F\d|E0|M0|D0):\s*(#[0-9a-fA-F]{6})/g)) {
    declared.set(key, hex.toLowerCase());
  }
  ok('app.css declares a colour for every bucket', declared.size === BUCKET_ORDER.length);
  const drifted = BUCKET_ORDER.filter((b) => declared.get(b) !== BUCKET_COLOR[b].toLowerCase());
  ok(
    `the stylesheet ramp matches BUCKET_COLOR${drifted.length ? ` (drifted: ${drifted.join(', ')})` : ''}`,
    drifted.length === 0,
  );
}

// --- autopilot pass state --------------------------------------------------
// A pass fires once a day and is never retried, so "10:00 has gone by and it is
// not in fired_today" is the ONLY signal that this morning's calls never went
// out. The clock has to be the server's: the times are IST.
{
  const fired = ['auto'];
  ok('a pass that fired reads as ran', passState('10:00', fired, 'auto', '16:00') === 'ran');
  ok('a pass still ahead of the server clock is waiting',
     passState('15:00', fired, 'auto_pm', '11:20') === 'waiting');
  ok('a pass whose time has gone by and never fired is flagged missed',
     passState('15:00', fired, 'auto_pm', '15:00') === 'missed');
  ok('a server too old to send its clock claims nothing',
     passState('15:00', fired, 'auto_pm', '') === 'waiting');
}

// --- RED band ranges read in the client's terms, not raw dte ---------------
{
  // The whole point: the just-lapsed band is dte -1..-3, and printing that
  // verbatim under a renewal column reads as a negative countdown.
  ok('the just-lapsed band reads as days PAST red',
     bandRange(-1, -3) === 'RED+1 … RED+3');
  ok('the run-up band starts at RED and counts backwards',
     bandRange(7, 0) === 'RED … RED−7');
  ok('the catch-all band says so in words',
     bandRange(null, null) === 'everything else');
  ok('a single-day band is not written as a range',
     bandRange(0, 0) === 'RED');
  ok('argument order does not change the reading',
     bandRange(-3, -1) === bandRange(-1, -3));
}

// --- scope leak guard (async: exercises the api layer's offline fallback) ----
(async () => {
  const visible = mockCampaigns.filter((c) => !c.hidden);
  ok('the fixtures carry a hidden campaign, or nothing below is being tested',
     visible.length < mockCampaigns.length);

  const all = await api.campaigns();
  ok('an unscoped call returns every campaign that is not hidden',
     all.length === visible.length);
  ok('a hidden campaign never reaches a screen, even if the server ignores ?include_hidden',
     all.every((c) => !c.hidden));
  const withHidden = await api.campaigns(undefined, true);
  ok('asking for them gets them back — the picker is the way out of hidden',
     withHidden.length === mockCampaigns.length);

  const only127 = await api.campaigns(127);
  ok(
    'a scoped call cannot leak another agent’s campaigns, even if the server ignores ?agent_id',
    only127.length > 0 && only127.every((c) => c.agent_id === 127),
  );
  const derived = await api.agents();
  ok('the agent list survives a backend without /api/agents', derived.length === agents.length);

  // The day is scoped by agent — one language — and the offline fixture has to
  // be scoped too, or two panels show the same campaigns under two languages.
  const dayAll = await api.day('2026-09-13', 'auto');
  const day127 = await api.day('2026-09-13', 'auto', 127);
  ok('an unscoped day still spans every agent, exactly as before scoping',
     new Set(dayAll.campaigns.map((c) => c.agent_id)).size > 1 && dayAll.agent_id === null);
  ok('a scoped day cannot leak another agent’s campaigns, offline either',
     day127.campaigns.length > 0 && day127.campaigns.every((c) => c.agent_id === 127)
     && day127.agent_id === 127);
  ok('scoping a day actually narrows it', day127.campaigns.length < dayAll.campaigns.length);

  // --- a 500 is not "unreachable" --------------------------------------------
  // One crashing endpoint must not blank the console into fixtures. fetch is
  // stubbed rather than trusted: everything above runs with no fetch at all, so
  // the sticky offline flag is already set and has to be cleared first.
  const stub = (status: number) => {
    (globalThis as { fetch?: unknown }).fetch = async () => ({
      ok: false, status, statusText: 'error',
      json: async () => ({ error: `boom ${status}` }),
    });
  };

  retryLive();
  stub(500);
  let raised: unknown = null;
  try { await api.health(); } catch (e) { raised = e; }
  ok('a 500 is surfaced as an error, not swallowed into mock data',
     raised instanceof ApiError && raised.status === 500);
  ok('and the console stays live — one broken endpoint is not an unreachable backend',
     !isOffline());

  stub(503);
  await api.health();
  ok('a 502/503/504 IS the proxy speaking for a dead backend, so that still goes offline',
     isOffline());
  retryLive();

  // --- agent scoping on the wire ---------------------------------------------
  // Everything above runs through the offline fixture, which cannot show what
  // the client actually SENDS. An unscoped day must send no `agent_id` at all:
  // `agent_id=undefined` is a string the server reads as a campaign nobody owns.
  const sent: string[] = [];
  (globalThis as { fetch?: unknown }).fetch = async (url: unknown, init?: RequestInit) => {
    sent.push(`${String(url)} ${String(init?.body ?? '')}`);
    return { ok: true, status: 200, json: async () => ({ campaigns: [] }) };
  };
  retryLive();

  await api.day('2026-09-13', 'auto');
  ok('an unscoped day sends no agent_id at all, not an empty or undefined one',
     !sent[0].includes('agent_id'));
  await api.day('2026-09-13', 'auto', 127);
  ok('a scoped day puts the agent on the query string', sent[1].includes('agent_id=127'));
  await api.prepareDay('2026-09-13', 'auto', false, 127);
  ok('prepare carries the agent in its body', sent[2].includes('"agent_id":127'));
  await api.approveDay('2026-09-13', 'auto', [], [], 127);
  ok('approve — the only call that reaches Formi — carries the agent too',
     sent[3].includes('"agent_id":127'));

  // --- and the panel's own two calls, on the same wire ------------------------
  // Above pins the API layer; this pins the PANEL's use of it. Both of the calls
  // that establish what a panel is about live in an effect, which the static
  // renderer never runs — so they are functions of the panel's agent rather than
  // argument lists written inline, and are driven here with the agent a panel
  // would hand them. Written inline they could be un-scoped with the whole gate
  // still green, which is the exact accident this screen was split up to prevent.
  sent.length = 0;
  await panelDay(A127, '2026-09-13', 'auto');
  ok('a panel reads its own agent’s day, scoped from the panel and nothing else',
     sent[0].includes('agent_id=127'));
  await panelPrepare(A127, '2026-09-13', 'auto');
  // `resync` false is half of what this line asserts. The panel's ordinary
  // Prepare must NOT re-read Formi: `resync` on runs `_resync_status`, which
  // pauses campaigns and re-pulls every campaign's leads out of Metabase, and a
  // warehouse that hiccups answers `resync_failed` — the campaign is left
  // unplanned. That is the right price for the re-check, which exists because
  // the plan is hours old, and the wrong one for a button pressed every morning
  // against a copy the hourly sync already keeps fresh. Flipping the default
  // used to pass this whole gate.
  ok('and builds the plan for that same agent alone — never for both languages, '
     + 'and does NOT re-read Formi doing it',
     sent[1].includes('"agent_id":127') && sent[1].includes('"resync":false'));
  await panelDay(null, '2026-09-13', 'auto');
  ok('an unscoped panel still reads the whole day, byte for byte as before scoping',
     !sent[2].includes('agent_id'));
  await panelPrepare(null, '2026-09-13', 'auto');
  ok('and still builds the whole day’s plan', !sent[3].includes('"agent_id":'));

  // The approve modal's "Re-check now" is the same prepare pass with `resync`
  // on, and it runs from an async click handler the static renderer never
  // reaches. Routed through `panelPrepare` so the scope comes from the PANEL's
  // agent — `day.agent_id` is the server's echo, and sourcing scope from it is
  // the defect a770682 took out of the dial path. Rebuilding the whole roster's
  // plan from a panel headed "Tamil" is that defect wearing a different button.
  await panelPrepare(A127, '2026-09-13', 'auto', true);
  ok('the re-check re-reads Formi for the panel’s own agent alone, never the roster',
     sent[4].includes('"agent_id":127') && sent[4].includes('"resync":true'));

  // …and it has to SAY what the re-read found. Two facts come back that no other
  // call can learn, and the operator cannot see either one: the ready count
  // dropping from 4 to 2 as the modal closes explains nothing. `stopped_in_formi`
  // is the campaign Formi paused at 11:00 that was still in the 15:00 plan — the
  // whole reason this button exists — and `resync_failed` is a campaign whose
  // warehouse read failed, which is now in NO plan rather than planned off a
  // stale copy. Pinned here because the toast fires from an async click handler
  // the static renderer never reaches; the sentence is a pure function so this
  // is the one door into it.
  const rd = mockDay('2026-09-13', 'auto', 127);
  const [paused, broken] = rd.campaigns;
  const prep = (over: Partial<PrepareResult>): PrepareResult => ({
    date: '2026-09-13', kind: 'auto', wave: 'Morning', ready: 2, prepared: 2,
    campaigns: [], ...over,
  });
  // The failure rows carry NO `name`, because the server never sends one:
  // `_prepare_one` builds `resync_failed` off `out`, which holds `campaign_id`
  // alone, and assigns `out["name"]` only AFTER the resync block. Seeding a name
  // here pinned a branch production cannot reach — where production always
  // rendered `#12`, this check watched a name it had invented itself.
  const failed = (campaign_id: number, status: string) => ({ campaign_id, status });
  const say = (over: Partial<PrepareResult>) => recheckMessage(prep(over), rd)[1];
  const tone = (over: Partial<PrepareResult>) => recheckMessage(prep(over), rd)[0];

  // The WHOLE sentence, not the name inside it. `has(…, paused.name)` alone went
  // red only because tsc calls an unreferenced local unused — that proves the
  // name is interpolated somewhere, not that the sentence still reports what
  // happened. Reworded while still interpolating, it used to pass.
  ok('the re-check names the campaign Formi had paused since the plan was built, '
     + 'rather than only letting the ready count drop',
     has(say({ stopped_in_formi: [paused.id] }), `Stopped in Formi since: ${paused.name}.`)
     && !has(say({}), 'Stopped in Formi'));
  // BOTH failures, not just the one. `error` is `_write_run` raising, which also
  // writes nothing and also leaves the campaign in no plan — filtering
  // `resync_failed` alone let exactly the silent failure this button exists to
  // end walk past it. The other five statuses are ordinary answers and name
  // nobody.
  ok('and names every campaign left in NO plan at all — the warehouse read that '
     + 'failed AND the plan write that threw, neither of which will dial',
     has(say({ campaigns: [failed(broken.id, 'resync_failed'),
                           { campaign_id: paused.id, name: paused.name, status: 'prepared' }] }),
         `Could not plan ${broken.name} — left out of this plan.`)
     && has(say({ campaigns: [failed(broken.id, 'error')] }),
            `Could not plan ${broken.name} — left out of this plan.`)
     && !has(say({ campaigns: ['prepared', 'not_in_daily_plan', 'finished', 'window_closed',
                               'already_ran'].map((s) => failed(broken.id, s)) }),
             'Could not plan'));
  // An id this panel does not hold. Both lists resolve ids through
  // `day.campaigns`, and a `find` that cannot miss pins nothing: this used to be
  // seeded from `rd.campaigns`, so `?? `#${id}`` -> `?? ''` and
  // `.find(c => c.id === id)` -> `.find(() => true)` both stayed green.
  ok('and a campaign outside this panel’s view is still named, as #id rather than dropped',
     has(say({ stopped_in_formi: [999] }), 'Stopped in Formi since: #999.')
     && has(say({ campaigns: [failed(999, 'resync_failed')] }),
            'Could not plan #999 — left out of this plan.'));
  // Tone travels with the sentence. "left out of this plan" rendered in the green
  // success tone is the failure dressed as a success, which is the exact reading
  // mistake the whole re-check exists to stop.
  ok('and a re-check that names a stopped or unplanned campaign is not toasted as success',
     tone({}) === 'ok'
     && tone({ stopped_in_formi: [paused.id] }) === 'bad'
     && tone({ campaigns: [failed(broken.id, 'error')] }) === 'bad');
  retryLive();

  // --- and the day actually goes out one campaign at a time -------------------
  //
  // `dialQueue` proves the QUEUE is one request per campaign; nothing above
  // proves the dial walks it. That loop used to live in a click handler, where
  // collapsing it back into a single whole-day `api.approveDay(...args)` — the
  // 2,967-calls-in-one-request bug of 12 Sep 2026 — passed the whole gate green.
  // The click is still out of reach, but the `await` is not: `runQueue` is the
  // loop with React on the far side of a callback, driven here on the same
  // stubbed fetch and read back off the wire.
  const tamil = mockDay('2026-09-13', 'auto', 127);
  const queue = dialQueue(approveArgs(A127, tamil, []), tamil);
  const bodies = () =>
    sent.map((s) => JSON.parse(s.slice(s.indexOf(' ') + 1)) as
      { campaign_ids: number[]; agent_id?: number });

  sent.length = 0;
  await runQueue(queue, () => {}, () => false);
  ok('the dial puts each campaign on the wire in its own request, exactly once',
     queue.length > 1 && bodies().length === queue.length &&
     bodies().every((b, i) =>
       b.campaign_ids.length === 1 && b.campaign_ids[0] === queue[i].campaign_id));
  // Twelve requests where there was one is twelve chances to lose the scope.
  // A dropped agent dials every armed campaign on every agent — the whole
  // roster, from a panel headed "Tamil".
  ok('and every one of them still carries the panel’s agent, unchanged down the queue',
     bodies().every((b) => b.agent_id === 127));

  // Stopping has to stop the DIALLING, not just the spinner: the campaigns it
  // never reached stay `planned` and stay approvable, which is only true if the
  // requests were never sent.
  sent.length = 0;
  await runQueue(queue, () => {}, () => sent.length > 0);
  ok('stopping the queue stops the phones — no request goes out after the stop',
     queue.length > 1 && sent.length === 1);
  retryLive();

  console.log('\nall checks passed');
})();
// A thrown ok() inside the async block surfaces as an unhandled rejection,
// which node exits non-zero on — same failure signal as the sync checks.
