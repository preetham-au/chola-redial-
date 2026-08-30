import { useState } from 'react';
import { CircleSlash, FlaskConical, Info, Loader2, Radio } from 'lucide-react';
import { api } from '../lib/api';
import { n } from '../lib/domain';
import { useStore } from '../lib/store';
import { Fact, Modal, TypeToConfirm } from './ui';
import type { Run } from '../lib/types';

/* Approve is the only path that dials. Under DRY_RUN it costs one click and
   says so; live it costs a typed word. The wording never changes between the
   button, the dialog and the toast. */

export function ApproveDialog({
  run,
  urgentSlots,
  onClose,
  onDone,
}: {
  run: Run;
  urgentSlots: number;
  onClose: () => void;
  onDone: () => void;
}) {
  const health = useStore((s) => s.health);
  const toast = useStore((s) => s.toast);
  const campaign = useStore((s) => s.campaigns.find((c) => c.id === s.campaignId) ?? null);
  const live = health ? !health.dry_run : false;
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);

  const blocked = campaign?.paused === true;
  const ready = !blocked && (!live || typed.trim().toUpperCase() === 'DIAL');

  const submit = async () => {
    setBusy(true);
    try {
      const res = await api.approve(run.id);
      const simulated = res.dry_run !== false;
      toast(
        'ok',
        simulated
          ? `Run ${run.id} simulated. ${n(run.counts.slots)} calls recorded, none sent to Formi.`
          : `Run ${run.id} approved. ${n(run.counts.slots)} calls queued for dialling.`,
      );
      onDone();
      onClose();
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={live ? 'Approve and dial' : 'Approve (simulated)'}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className={live ? 'btn btn-live' : 'btn btn-primary'}
            disabled={!ready || busy}
            onClick={submit}
          >
            {busy ? <Loader2 className="spin" /> : live ? <Radio /> : <FlaskConical />}
            {live ? `Dial ${n(run.counts.slots)} calls` : `Simulate ${n(run.counts.slots)} calls`}
          </button>
        </>
      }
    >
      {blocked && (
        <div className="warnbox">
          <CircleSlash />
          <span>
            This campaign is paused. Resume it from the topbar — or from the rail if the whole
            agent is paused — before approving. The server returns 409 while it is paused.
          </span>
        </div>
      )}

      {live ? (
        <div className="warnbox">
          <Radio />
          <span>
            <b>DRY_RUN is off.</b> Approving posts every one of these calls to Formi. There is no undo.
          </span>
        </div>
      ) : (
        <div className="infobox">
          <FlaskConical />
          <span>
            The server is in dry run. Items will be marked <span className="mono">simulated</span> and
            nothing reaches Formi. Set <span className="mono">DRY_RUN=0</span> on the server to dial for real.
          </span>
        </div>
      )}

      <div className="confirm-facts">
        {/* Scope first: the wrong agent means the wrong script at the wrong cohort. */}
        <Fact k="Agent" v={campaign?.agent_id ?? '—'} tone="var(--accent)" />
        <Fact k="Campaign" v={campaign?.name ?? '—'} />
        <Fact k="Run" v={`#${run.id} · ${run.kind}`} />
        <Fact k="Date" v={run.run_date} />
        <Fact k="Leads" v={n(run.counts.planned)} />
        <Fact k="Call slots" v={n(run.counts.slots)} />
        <Fact k="Of which F5 / F6 / M0" v={n(urgentSlots)} tone="var(--b-F5)" />
        <Fact k="Config version" v={`v${run.config_version}`} />
      </div>

      {live && (
        <TypeToConfirm
          word="DIAL"
          value={typed}
          onChange={setTyped}
          hint="Type DIAL to confirm you mean to place these calls."
        />
      )}

      <div className="infobox">
        <Info />
        <span>
          Approving does not re-plan. It dials exactly the {n(run.counts.slots)} slots listed on this
          screen, at the times shown.
        </span>
      </div>
    </Modal>
  );
}
