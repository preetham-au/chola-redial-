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
  Proof,
  Stranded,
  Today,
  approveArgs,
  approveModal,
  alreadyBooked,
  autopilotDiff,
  closesAt,
  dialQueue,
  mergeResults,
  outOfBand,
  outsideBand,
  panelDay,
  panelPrepare,
  panelsFor,
  lastDialled,
  pickerPrepare,
  planAge,
  prepareMessage,
  recheckMessage,
  retryArgs,
  runQueue,
  scopeMismatch,
  stoppedShort,
  straddlesBand,
  unbuilt,
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
  Agent, ApproveResult, Campaign, Config, DayCampaign, DaySpread, DialWindow, PrepareResult,
  TestCallResult,
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

  // A plan that was built and came back EMPTY — the afternoon wave after a
  // morning that booked every lead reaching it. `api/day.py` answered `approved`
  // here until 14 Sep 2026 and this screen believed it: a headline reading "0
  // calls on the clock. This wave has been approved." over a day nobody
  // approved and nothing was dialled for, with the call log as the only button.
  // Worse, the screen then offered neither Build nor Approve, so leads a later
  // re-sync pulled in could not be planned at all.
  const blank = head(day({ status: 'nothing_to_dial', totals: { ...base.totals, ready: 0 } }));
  ok('a wave whose plan came back empty says nothing was approved',
     has(blank, 'Nothing was approved') && !has(blank, 'has been approved'));
  ok('and never reads as calls on the clock', !has(blank, 'calls on the clock'));
  // The way back. Without it the panel is a dead end until the wave rolls over.
  ok('and offers to re-read Formi and build again, rather than only a call log',
     has(blank, 'Re-check and build') && !has(blank, 'Open the call log'));

  // The same defect one state over: eight campaigns committed and one that never
  // built. `{committed, not_prepared}` matched no arm of the server's ladder and
  // fell through to `approved`, so the hero read "This wave has been approved.
  // 2,140 calls on the clock." over a campaign whose ~250 leads were on no clock
  // at all — and offered the call log and nothing else. The picker is not the way
  // back either: that campaign is already armed, so `autopilotDiff` returns two
  // empty lists and Save is greyed out.
  const partlyBuilt = day({
    status: 'part_prepared',
    campaigns: base.campaigns.map((c, i) =>
      (i === 0 ? { ...c, run_status: 'not_prepared' as const, ready: 0 } : { ...c, run_status: 'committed' as const })),
  });
  ok('one campaign left unbuilt is counted off run_status, not guessed',
     unbuilt(partlyBuilt) === 1 && unbuilt(base) === 0);
  const mixedHero = head(partlyBuilt);
  ok('a wave holding an unbuilt campaign never says it has been approved',
     !has(mixedHero, 'has been approved') && has(mixedHero, 'no plan for this wave'));
  // The way back, and the only one: Build is on no other branch of this hero.
  ok('and offers to build the campaigns that have none',
     has(mixedHero, 'Build the missing plan') && !has(mixedHero, 'Open the call log'));

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

  // The facts an operator approves ON. Both of these lines reduce every campaign
  // on the panel to one value, and the modal used to take opposite ends without
  // saying so: the worst plan age beside the most flattering last-dial date.
  const mixed = modal(
    { campaigns: base.campaigns.map((c, i) =>
        ({ ...c, last_dialled: i === 0 ? '2026-06-01' : '2026-09-12' })) },
    [], ['M0'],
  );
  ok('the approve modal reports the whole range of last-dial dates, not its newest end',
     has(mixed, '2026-06-01 – 2026-09-12'));
  ok('and says out loud that the plan age beside it is the oldest one',
     has(mixed, 'Plan built (oldest)'));
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

  // The fact printed beside it, which took the opposite end of its own range.
  // One line summarising twelve campaigns is a reduction, and which reduction
  // decides what it means: `plan_built_at` takes the oldest on purpose, while
  // `last_dialled` took the NEWEST — so campaigns last dialled between June and
  // yesterday read as "yesterday", the reassuring end of a range whose other end
  // was the reason to look. Both ends now, whenever they differ.
  const dialled = (...v: (string | null)[]) => v.map((last_dialled) => ({ last_dialled }));
  ok('campaigns that were all dialled on the same day say that one day',
     lastDialled(dialled('2026-09-12', '2026-09-12')) === '2026-09-12');
  ok('and campaigns that disagree report the range, not the flattering end of it',
     lastDialled(dialled('2026-09-12', '2026-06-01', '2026-08-04')) === '2026-06-01 – 2026-09-12');
  ok('a campaign never dialled is the earliest end there is, not a blank',
     lastDialled(dialled(null, '2026-09-12')) === 'never – 2026-09-12'
     && lastDialled(dialled(null, null)) === 'never');
  ok('and a panel with no campaigns claims no date at all', lastDialled([]) === '—');

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

  // A server one deploy behind sends the campaign rows without the field at
  // all. It was typed as always present and summed with `+`, so `0 + undefined`
  // gave `NaN`, `NaN > 0` was false, and the ONE fact on this modal that can
  // talk an operator out of dialling disappeared — not degraded, not zeroed,
  // gone, leaving a modal that reads exactly like a day with no double-booking.
  const silent = every({});
  silent.campaigns.forEach((c) => { delete (c as { already_booked?: number }).already_booked; });
  const unknown = modal(silent);
  ok('a server that never sent the booked count produces no NaN in the modal',
     !has(unknown, 'NaN'));
  ok('and the missing count is said out loud, not passed off as none',
     has(unknown, 'did not say how many'));
  ok('while a server that DID answer none is not accused of silence',
     !has(modal(every({ already_booked: 0 })), 'did not say how many'));
  // The arithmetic under both, where the NaN was made.
  ok('a missing count sums to a number and marks itself unknown',
     alreadyBooked([{ already_booked: 4 }, {}]).count === 4
     && !alreadyBooked([{ already_booked: 4 }, {}]).known
     && alreadyBooked([{ already_booked: 4 }, { already_booked: 0 }]).known);
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
       <DayPanel
         agent={labelled(127, 'Tamil')}
         date={DATE}
         kind="auto"
         rev={0}
         onPick={() => {}}
         onDayChanged={() => {}}
       />,
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

  // A server that predates per-agent scoping omits the echo entirely. Compared
  // un-normalised, `null === undefined` is false and the guard refuses EVERY
  // panel for ever — the unscoped one included, which is the one state where
  // there is nothing to disagree about — while telling the operator to reload a
  // day that comes back identical. A safety check that fires in a state it was
  // never meant to judge stops the phones as surely as a broken dial.
  const silent = { ...wholeDay, agent_id: undefined };
  ok('an unscoped panel still dials when the server never said what it narrowed to',
     scopeMismatch(approveArgs(null, silent, []), silent) === null);
  // The other half: absent is not permission. That plan really does span the
  // roster, so a one-agent button over it is still refused.
  const silentScoped = scopeMismatch(approveArgs(labelled(125, 'Hindi'), silent, []), silent);
  ok('but a one-agent button over a plan the server never narrowed is still refused',
     silentScoped !== null && has(silentScoped, 'plan for every agent'));

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

  // --- slots the dial path refused for leaving the wave's band ----------------
  //
  // `_commit` skips a slot that has drifted outside its wave's hours and dials
  // the rest. Those leads were NOT called. The count reached the browser from
  // the per-run endpoints but not from the day-level approve, where it survived
  // only inside `not_dialled` — wearing that number's one explanation, "did not
  // fit before the window shut", which is a different fault with a different fix.
  const rows: ApproveResult['campaigns'] = [
    // `run_id` present, as an approved run always has one: there IS a run to
    // retry, and the row must still not offer it.
    { campaign_id: 1, name: 'Strayed campaign', status: 'approved', posted: 300, failed: 0,
      run_id: 7, out_of_band: 4 },
    { campaign_id: 2, name: 'Clean campaign', status: 'approved', posted: 90, failed: 0,
      out_of_band: 0 },
  ];
  ok('the strays of every campaign that dialled are added up',
     outOfBand(rows).count === 4 && outOfBand(rows).known);
  // A campaign that never reached `_commit` has no slots to be outside anything,
  // so its silence is not the server's silence.
  const alsoNeverStarted: ApproveResult['campaigns'] = [
    { campaign_id: 3, name: 'Never started', status: 'window_closed' }, ...rows];
  ok('and a campaign that never started is not mistaken for a server that did not say',
     outOfBand(alsoNeverStarted).known);
  // alreadyBooked's lesson, on a field one deploy younger than this bundle: the
  // default fixture above is exactly what an API without it sends.
  ok('but a server that sends no count at all is reported as unknown, not as none',
     outOfBand(result().campaigns).known === false);

  const strayed = dialres({ posted: 390, not_dialled: 4, campaigns: rows });
  ok('a wave that left leads outside its band says so in its own words, not as "did not fit"',
     has(strayed, '4 left the Morning band and were not dialled'));
  // Listed as a problem — which is also what withholds the green sentence, so
  // this check reddens if the campaign stops counting as one.
  ok('and never reads as a day where every selected lead is on the clock',
     has(clean, 'Every selected lead is on the clock')
     && !has(strayed, 'Every selected lead is on the clock'));
  ok('and names the campaign, and what actually puts those leads back',
     has(strayed, 'Strayed campaign') && has(strayed, '4 calls outside the band')
     && has(strayed, 're-plan the day'));
  // Formi never saw these, so there is nothing to send again — the lead comes
  // back by re-planning, and an offer to retry would dial nothing.
  ok('and offers no retry for calls that were never posted', !has(strayed, 'Retry'));
  // The other half of not-knowing: an older API sends no field, the strays are
  // inside `not_dialled` anyway, and the screen would otherwise blame the
  // window for them.
  const older = dialres({
    posted: 300, not_dialled: 4,
    campaigns: [{ campaign_id: 1, name: 'Old server', status: 'approved', posted: 300 }],
  });
  ok('a server too old to report strays is said to be too old, not taken as reporting none',
     has(older, 'does not report calls refused for leaving'));
  ok('and a server that does report them adds no such hedge',
     !has(strayed, 'does not report calls refused for leaving'));

  // --- Stop leaves campaigns behind, and has to say so ------------------------
  //
  // Stopping breaks the queue mid-walk and the result modal then replaces the
  // one holding the progress list, so a Stop at campaign 5 of 12 used to leave a
  // result for five campaigns and nothing at all about the other seven. Those
  // seven were never posted: their items are still `planned` and approving the
  // day again sends exactly them, which is the only thing that makes a Stop
  // safe to press — and it was on screen nowhere.
  ok('a queue that ran to the end says nothing about stopping',
     stoppedShort(12, 12) === null && stoppedShort(1, 1) === null);
  const cut = stoppedShort(12, 5);
  ok('a Stop counts the campaigns it never reached, and says they can still be sent',
     cut !== null && has(cut, '7 of 12') && has(cut, 'still planned')
     && has(cut, 'approving the day again'));

  const partial = renderToStaticMarkup(
    <DialResult res={result()} args={['2026-09-13', 'auto', [], [], undefined]}
                short={cut} onChange={() => {}} />,
  );
  ok('and that sentence survives into the result modal, which is all the operator sees',
     has(partial, '7 of 12') && has(partial, 'still planned'));
  // The five that DID dial were clean, so every count here is the clean-day
  // count -- and on those numbers alone the screen used to call twelve campaigns
  // done. A partial day is not a finished one.
  ok('and a day cut off part-way never reads as one where every lead is on the clock',
     has(clean, 'Every selected lead is on the clock')
     && !has(partial, 'Every selected lead is on the clock'));
}

// --- the stranded warning counts people, not slots ---------------------------
//
// An unapproved plan is built again for the same leads the next morning, so a
// backlog that sat a fortnight has a run row per day it waited. Summing the
// rows' `slots` multiplies the backlog by that wait: 544 people were reported
// as "7,616 calls were planned and never dialled" — a number nothing else in
// the console agreed with and nobody could act on. The rows below still carry
// their own `slots`, which is true of each run; the headline is the distinct
// lead count the server now sends.
{
  const base = mockDay('2026-09-13', 'auto');
  const runs = Array.from({ length: 14 }, (_, i) => ({
    run_id: 800 + i, campaign_id: 1, name: 'Backlog campaign',
    run_date: `2026-08-${String(20 + i).padStart(2, '0')}`, kind: 'auto', slots: 544,
  }));
  const html = renderToStaticMarkup(
    <Stranded day={{ ...base, stranded: runs, stranded_leads: 544 }} />,
  );
  ok('the stranded headline counts the people behind the backlog',
     has(html, '544 leads were planned'));
  ok('and never the same backlog once per day it sat unapproved',
     !has(html, '7,616') && !has(html, '7616'));
  ok('while each run row still reports its own slots, which is true of that run',
     has(html, 'Backlog campaign · 2026-08-20 morning (544)'));
  ok('a day with nothing stranded still shows no warning at all',
     renderToStaticMarkup(<Stranded day={base} />) === '');
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

// --- where the calls landed: the band is judged by the MINUTE, not the hour --
//
// The whole point of the proof card is that a bar painted red accuses a wave of
// dialling outside its half of the day. With a 13:30 boundary the 13:00 hour is
// half in and half out of BOTH waves, so the obvious `hour < start || hour >=
// end` is wrong twice over and wrong in the direction that cries wolf.
{
  const AM: DialWindow = { start: '09:00', end: '13:30' };
  const PM: DialWindow = { start: '13:30', end: '20:00' };
  const out = (hours: Record<string, number>, band: DialWindow) =>
    outsideBand({ band, hours }).map(([h]) => h).join();

  ok('the 13:00 hour is INSIDE a band ending 13:30 — 13:00-13:29 is in it',
     out({ '13': 4 }, AM) === '');
  ok('and inside a band STARTING 13:30 as well — 13:30-13:59 is in it',
     out({ '13': 4 }, PM) === '');
  ok('a band closing at 20:00 holds nothing at 20:xx', out({ '20': 1 }, PM) === '20');
  ok('an hour wholly before the band opens is outside it', out({ '8': 3 }, AM) === '8');
  // Caught, so a rewrite that reaches for the first hour without checking there
  // IS one reddens THIS line by name rather than a bare stack trace.
  const empty = () => { try { return out({}, AM) === ''; } catch { return false; } };
  ok('a day with nothing on the clock answers with nothing, rather than throwing', empty());
  ok('the hours in the band are kept and only the strays are returned',
     out({ '8': 1, '9': 2, '13': 3, '20': 4 }, AM) === '8,20');

  // The other side of the same arithmetic. `outsideBand` is right to let the
  // 13:00 hour through, but letting it through SILENTLY leaves a half-hour
  // escape hatch on the exact boundary the card polices: hour-sized buckets
  // cannot say whether a 13:0x call belonged to the morning or the afternoon.
  const cut = (hours: Record<string, number>, band: DialWindow) =>
    straddlesBand({ band, hours }).map(([h]) => h).join();
  ok('the hour a 13:30 boundary cuts in half is named as unjudgeable, under either wave',
     cut({ '9': 1, '13': 4 }, AM) === '13' && cut({ '13': 4, '15': 2 }, PM) === '13');
  ok('an hour the band opens or closes exactly ON is not ambiguous — nothing is split',
     cut({ '9': 1, '20': 2 }, { start: '09:00', end: '20:00' }) === '');
  ok('and an hour wholly outside the band is a stray, not an ambiguity — it is already judged',
     cut({ '8': 1, '20': 2 }, AM) === '' && out({ '8': 1, '20': 2 }, AM) === '8,20');

  // The card itself: it is the one place an operator sees the verdict, and the
  // sentence under the bars is the only part of it that names a number.
  // `dry_run` is explicit on every render below: the fixture ships `true`, and
  // a card that only ever gets tested in dry run is exactly how the dry-run
  // wording got missed in the first place.
  const proof = (
    spread: DaySpread,
    dial_log: Record<string, number> = { dialled: 7 },
    dry_run = false,
  ) =>
    renderToStaticMarkup(
      <Proof
        day={{ ...mockDay('2026-09-13', 'auto'), dry_run, spread, dial_log }}
        onReload={() => {}}
      />,
    );
  ok('a day with nothing on the clock shows no proof card at all, rather than an empty one',
     proof({ band: AM, hours: {} }) === '');
  const kept = proof({ band: AM, hours: { '9': 40, '13': 12 } });
  ok('a wave that stayed inside its band says how many calls it put on the clock',
     has(kept, '52 on the clock') && has(kept, 'band 09:00–13:30'));
  ok('and is not accused of landing outside it', !has(kept, 'landed outside'));
  ok('nor painted as having strayed', !has(kept, 'var(--bad)'));
  ok('each hour gets its own bar, labelled with what landed in it',
     has(kept, '9:00 — 40 calls') && has(kept, '13:00 — 12 calls'));
  // The COUNT, not the word: "dialled" also appears in the card's own sentence
  // explaining what it means, so `has(kept, 'dialled')` stays green with the
  // whole read-back deleted.
  ok('the warehouse read-back is shown beside the hours, or the card proves half a thing',
     has(kept, 'dialbar-key') && has(kept, '<b>7</b>'));
  const strayed = proof({ band: AM, hours: { '9': 40, '19': 12, '20': 3 } });
  ok('a wave that dialled past its band is told so, counting only the strays',
     has(strayed, '15 calls landed outside the 09:00–13:30 band'));
  // Two stray bars and the sentence under them. The hour that kept the band is
  // NOT one of them — reddening the whole day would say nothing.
  ok('and only the stray hours are painted red',
     (strayed.match(/var\(--bad\)/g) ?? []).length === 3);

  // DRY_RUN. `_spread` counts `simulated` rows beside `posted` ones, so the
  // card draws a full histogram out of calls that never left the building. The
  // bars are still worth seeing — that IS the schedule — but the sentences over
  // them are the ones an operator reads as "the day went out".
  const rehearsed = proof({ band: AM, hours: { '9': 40, '13': 12 } }, { dialled: 7 }, true);
  ok('a rehearsed wave never claims its calls are on the clock',
     !has(rehearsed, 'on the clock') && has(rehearsed, '52 simulated, none dialled'));
  ok('and says so in full, rather than leaving it to the eyebrow',
     has(rehearsed, 'Nothing on this card was dialled'));
  ok('and does not wear the green a dialled day earns',
     has(kept, 'var(--ok)') && !has(rehearsed, 'var(--ok)'));
  // The heading is the largest text on the card and the first thing read.
  ok('and its heading is in the conditional too',
     has(kept, 'Where the calls landed')
     && has(rehearsed, 'Where the calls would have landed'));
  // On screen: the card has to SAY it cannot judge the boundary hour, not just
  // decline to redden it. Silence there reads as a pass.
  ok('the card says outright which hour it cannot answer for, and how many calls that is',
     has(kept, '13:00 is')
     && has(kept, 'cut in half by the 09:00–13:30 band')
     && has(kept, 'which of those 12 calls kept the band'));
  ok('and the unresolved bar is neither the green of a pass nor the red of an accusation',
     has(kept, 'var(--warn)') && !has(kept, 'var(--bad)'));
  const clear = proof({ band: { start: '09:00', end: '20:00' }, hours: { '9': 40, '13': 12 } });
  ok('a band that opens and closes on the hour has nothing to disclaim',
     !has(clear, 'cut in half') && !has(clear, 'var(--warn)'));

  const rehearsedStray = proof({ band: AM, hours: { '9': 40, '19': 12, '20': 3 } }, {}, true);
  ok('a rehearsed wave that strayed is told where it WOULD have strayed, not where it landed',
     has(rehearsedStray, '15 calls were scheduled outside the 09:00–13:30 band')
     && !has(rehearsedStray, 'calls landed outside'));

  // "Check now" sits in a card rendered once PER PANEL, but the endpoint behind
  // it takes a date and nothing else — it re-reads the warehouse for every agent
  // on the day. A button that quietly does more than the panel it sits in has to
  // say so, and its refresh has to reach as far as its effect: reloading only
  // the panel that was clicked leaves the others showing counts the server has
  // already replaced.
  ok('the whole-day reach of the warehouse check is on the card, not just in the handler',
     has(kept, 'every agent') && has(kept, 'every panel on this screen refreshes with it'));
  // The wiring is a click, which this renderer cannot reach. Read the source
  // instead, the way the colour ramp and the prepare call sites are read.
  const today = readFileSync('src/screens/Today.tsx', 'utf8');
  ok('and the card is wired to refresh every panel, not only its own',
     today.includes('<Proof day={d} onReload={onDayChanged} />'));
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

  // The picker's Save has the same wire to get wrong, and got it wrong. It arms
  // ONE agent's campaigns — it says so twice on screen — and then built the plan
  // for everybody; `_write_run` clears each campaign's existing `planned` run
  // before writing, so an unscoped build from a picker scoped to Tamil tore down
  // the Hindi plan the other operator was mid-way through approving.
  sent.length = 0;
  await pickerPrepare(127, '2026-09-13', 'auto');
  ok('the picker builds the plan for the agent it was arming, and no other',
     sent[0].includes('"agent_id":127'));
  // `resync` would re-read Formi for every campaign on Save — the wrong price
  // for a button pressed while choosing, and `resync_failed` leaves a campaign
  // the operator JUST ticked in no plan at all.
  ok('and does not re-read Formi to do it', sent[0].includes('"resync":false'));
  await pickerPrepare(null, '2026-09-13', 'auto');
  ok('an unscoped picker still builds the whole day, exactly as before scoping',
     !sent[1].includes('agent_id'));
  sent.length = 0;
  // The wire above is pinned; the CALL SITE is a click the static renderer never
  // reaches, and dropping `pickerPrepare` for a bare `api.prepareDay(date, kind)`
  // inside `save()` is exactly how this defect got in — with the whole gate
  // green. So the source is read, the same way the colour ramp is: every build
  // in this screen goes through one of the two scoped helpers, and there is no
  // third caller to forget the agent in. (app.css is read at src/styles; run
  // from web/ either way.)
  const src = readFileSync('src/screens/Today.tsx', 'utf8');
  ok('and nothing in the day screen calls prepare outside those two scoped helpers',
     src.split('api.prepareDay(').length - 1 === 2);
  // The picker's own comment used to assert the opposite of all of this — that
  // `api/day.py` has no notion of an agent — which stopped being true when this
  // branch landed. A comment that contradicts the checks above is how the next
  // reader talks themselves into un-scoping one of these calls.
  ok('and the picker no longer says the day API cannot tell agents apart',
     !src.includes('has no notion of an agent'));

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

  // `finished` is not an ordinary answer. `_prepare_one` reaches it through
  // `_stop` — `UPDATE campaigns SET autopilot=0` — so the campaign is disarmed
  // for good and for every later wave, and only a person re-arms it. That went
  // out green, in a sentence naming nobody; the campaign just stopped appearing
  // in tomorrow's plan and no screen ever said why.
  const fin = { campaigns: [failed(broken.id, 'finished')], ready: 2, prepared: 1 };
  ok('a campaign the re-check switched autopilot off for is named, not silently disarmed',
     has(say(fin), `Autopilot switched off for ${broken.name}`) && tone(fin) === 'bad');
  ok('and a re-check that disarmed nothing says nothing about autopilot',
     !has(say({}), 'Autopilot switched off'));

  // The whole reason the tone exists. Every campaign answering `window_closed`
  // — the ordinary afternoon answer — leaves ready and prepared at zero with
  // every list above empty, so the one reading that means NOTHING WILL DIAL was
  // the one reading that came out green: "Re-checked: 0 still ready across 0
  // campaigns", next to an Approve button that would now call nobody.
  const shutout = { ready: 0, prepared: 0,
                    campaigns: [failed(paused.id, 'window_closed'),
                                failed(broken.id, 'window_closed')] };
  ok('a re-check where no campaign came back with a plan is never toasted as success',
     tone(shutout) === 'bad' && has(say(shutout), 'nothing here to dial'));
  ok('and an empty scope says it was empty rather than reporting a clean zero',
     tone({ ready: 0, prepared: 0 }) === 'bad'
     && has(say({ ready: 0, prepared: 0 }), 'No campaign was armed'));

  // The same answer, one button over. "Build the plan" and "Re-check and build"
  // are both `DayPanel.prepare`, and it wrote its own verdict: a flat
  // `toast('ok', 'Plan built: 0 leads ready across 0 campaigns. Nothing has been
  // dialled.')` — green tick, cheerful zero — for a pass that had just switched
  // autopilot off, or come back with no plan at all. Everything the re-check
  // learned to say went unsaid one button over, off the SAME `/api/day/prepare`
  // body. So the judgement is one function now and both leads run through it.
  const built = (over: Partial<PrepareResult>) =>
    prepareMessage(prep(over), rd, 'Plan built: none.');
  const off = { campaigns: [failed(paused.id, 'finished')] };
  ok('a build that switched autopilot off for a campaign is never toasted as success',
     built(off)[0] === 'bad' && has(built(off)[1], 'Autopilot switched off'));
  ok('and a build that came back with no plan at all is not reported as a clean zero',
     built({ ready: 0, prepared: 0 })[0] === 'bad');
  ok('and a build where nothing went wrong is still the plain success it was',
     built({})[0] === 'ok');
  // The helper being right is half of it: the defect was the CALL SITE, an
  // inline toast that never asked. Both prepares are async clicks the static
  // renderer never reaches, so the wiring is read off the source — a third
  // opinion written beside either one reddens here.
  const afterPrepare = src.split('await panelPrepare(').slice(1).map((s) => s.slice(0, 260));
  ok('and every build in the day screen toasts that shared judgement, not its own',
     afterPrepare.length === 2
     && afterPrepare.every((s) => /toast\(\.\.\.(prepare|recheck)Message\(/.test(s)));
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

  // --- a campaign the approve answered nothing for did not dial ---------------
  //
  // `approve_day` loops the campaigns that are armed RIGHT NOW and `continue`s
  // past any requested id that no longer is — so it answers 200 with
  // `campaigns: []`, every count zero. `[].every(...)` is `true`: the progress
  // row went green, the merge added nothing, and the result read "Every
  // selected lead is on the clock" over ~250 leads nobody dialled. The stub
  // below is that answer exactly, zeroes and all, so the only thing that can
  // redden these three lines is the screen telling the truth about it.
  (globalThis as { fetch?: unknown }).fetch = async () => ({
    ok: true, status: 200,
    json: async () => ({
      date: '2026-09-13', kind: 'auto', wave: 'morning', dry_run: false,
      buckets: 'all', approved: 0, posted: 0, failed: 0, not_dialled: 0, campaigns: [],
    }),
  });
  retryLive();
  const states: ProgressRow['state'][] = [];
  const none = await runQueue(queue, (_c, s) => states.push(s), () => false);
  ok('a campaign the approve answered nothing for is never reported as done',
     states.length === queue.length * 2 && !states.includes('done'));
  const merged = mergeResults(none, tamil);
  ok('and it keeps a row of its own, under its own name, in the result the operator reads',
     merged.campaigns.length === queue.length
     && merged.campaigns.every((c) => c.status === 'no_result')
     && merged.campaigns.map((c) => c.name).join() === queue.map((c) => c.name).join());
  const nothing = renderToStaticMarkup(
    <DialResult res={merged} args={approveArgs(A127, tamil, [])} onChange={() => {}} />,
  );
  ok('and a day where nothing went out never reads as one where everything did',
     !has(nothing, 'Every selected lead is on the clock')
     && has(nothing, queue[0].name) && has(nothing, 'no longer in the daily plan'));
  retryLive();

  // --- and a campaign whose request never came back is on that list too -------
  //
  // A 502 from the proxy, a timeout, a commit that lost a SQLite lock: the POST
  // throws before there is any body to read. The campaign was started, so it has
  // a progress row — and the progress list is REPLACED by the result modal, so
  // without a part of its own it is named nowhere afterwards: absent from the
  // totals, from the problem rows and from the Retry, under a bar reading
  // "11 approved". The stub below throws for every campaign in the queue.
  (globalThis as { fetch?: unknown }).fetch = async () => ({
    ok: false, status: 500, statusText: 'error',
    json: async () => ({ error: 'gateway blew up' }),
  });
  retryLive();
  const blew = mergeResults(await runQueue(queue, () => {}, () => false), tamil);
  ok('a campaign whose approve threw still contributes a row to the result',
     blew.campaigns.length === queue.length
     && blew.campaigns.every((c) => c.status === 'request_failed')
     && blew.campaigns.map((c) => c.name).join() === queue.map((c) => c.name).join());
  const thrown = renderToStaticMarkup(
    <DialResult res={blew} args={approveArgs(A127, tamil, [])} onChange={() => {}} />,
  );
  // Named, with what went wrong, and offered again — the operator's standing
  // rule is that a failure is logged where it can be triggered a second time.
  ok('and is named, with what went wrong, where it can be sent again',
     has(thrown, queue[0].name) && has(thrown, 'gateway blew up')
     && has(thrown, 'Retry') && !has(thrown, 'Every selected lead is on the clock'));
  retryLive();

  console.log('\nall checks passed');
})();
// A thrown ok() inside the async block surfaces as an unhandled rejection,
// which node exits non-zero on — same failure signal as the sync checks.
