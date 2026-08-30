/* Renders the two new pieces with the real fixtures and drives every toggle
 * through the same reducer the UI calls. Run: npm run check
 * Not part of the app bundle — nothing imports it. */

import { renderToStaticMarkup } from 'react-dom/server';
import { applyBd, BucketDispositions, type BdAction } from './components/BucketDispositions';
import { AgentChip, AgentPauseConfirm, AgentSwitcher } from './components/AgentBar';
import { CampaignPicker } from './App';
import { BucketOffWhy } from './screens/Dashboard';
import { TestCallResultView, TestNumberTable, TriggerConfirm } from './screens/TestCall';
import { api } from './lib/api';
import { mockBuckets, mockCampaigns, mockConfig, mockTestCall, mockTestNumbers } from './lib/mock';
import {
  agentsFrom,
  configurableBuckets,
  effectiveDispositions,
  narrowedBuckets,
} from './lib/domain';
import type { Config, TestCallResult } from './lib/types';

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
ok('column header counts 8 of 8 on', has(html, '>8/8<'));
html = click({ kind: 'col', slug: 'telephony_failed' });
ok('clicking again turns it off down all buckets', ROWS.every((b) => !effectiveDispositions(cfg, b).includes('telephony_failed')));
ok('column header counts 0 of 8 and strikes the label', has(html, '>0/8<') && has(html, 'bd-colhead is-dead'));

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

// --- scope leak guard (async: exercises the api layer's offline fallback) ----
(async () => {
  const all = await api.campaigns();
  ok('an unscoped call returns every campaign', all.length === mockCampaigns.length);
  const only127 = await api.campaigns(127);
  ok(
    'a scoped call cannot leak another agent’s campaigns, even if the server ignores ?agent_id',
    only127.length > 0 && only127.every((c) => c.agent_id === 127),
  );
  const derived = await api.agents();
  ok('the agent list survives a backend without /api/agents', derived.length === agents.length);

  console.log('\nall checks passed');
})();
// A thrown ok() inside the async block surfaces as an unhandled rejection,
// which node exits non-zero on — same failure signal as the sync checks.
