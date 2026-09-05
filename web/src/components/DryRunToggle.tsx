import { useState } from 'react';
import { FlaskConical, Radio } from 'lucide-react';
import { api } from '../lib/api';
import { useStore } from '../lib/store';
import { Modal, TypeToConfirm } from './ui';

/* The rail's dry-run badge, made clickable. This is the one control that decides
   whether approving a plan reaches real customers, so going live costs a typed
   word -- the same shape as the approve dialog, which costs DIAL. Going back to
   a dry run costs one click: a switch that is hard to flip back to safe is a
   switch nobody flips in a hurry. */
export function DryRunToggle() {
  const health = useStore((s) => s.health);
  const toast = useStore((s) => s.toast);
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);

  if (!health) return null;
  const live = !health.dry_run;

  const apply = async (dryRun: boolean, confirm = '') => {
    setBusy(true);
    try {
      const res = await api.setDryRun(dryRun, confirm);
      // The server is the only source of truth for this; echo what it reports
      // rather than what was asked for.
      useStore.setState({ health: { ...health, dry_run: res.dry_run } });
      toast(
        res.dry_run ? 'ok' : 'bad',
        res.dry_run
          ? 'Dry run. Approving now only simulates — nothing reaches Formi.'
          : 'LIVE DIALLING is on. Approving now calls real customers.',
      );
      setOpen(false);
      setTyped('');
    } catch (e) {
      toast('bad', (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        type="button"
        className={`dry-badge${live ? ' is-live' : ''}`}
        disabled={busy}
        onClick={() => (live ? apply(true) : setOpen(true))}
        title={live ? 'Switch back to a dry run' : 'Turn live dialling on'}
      >
        {live ? <Radio /> : <FlaskConical />}
        <div>
          <span>{live ? 'Live dialling' : 'Dry run'}</span>
          <small>{live ? 'Click to stop dialling' : 'Approve only simulates'}</small>
        </div>
      </button>

      {open && (
        <Modal
          title="Turn on live dialling"
          onClose={() => setOpen(false)}
          footer={
            <>
              <button className="btn btn-ghost" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn-live"
                disabled={busy || typed.trim().toUpperCase() !== 'GO LIVE'}
                onClick={() => apply(false, typed)}
              >
                {busy ? 'Switching…' : 'Go live'}
              </button>
            </>
          }
        >
          <p>
            Every approve from here on posts to Formi and dials real customers. Nothing
            queued so far is affected — this changes what the <em>next</em> approve does.
          </p>
          <p className="field-hint">
            Held in the server's environment, so a restart returns it to whatever the
            deployment's .env says. Check the badge before approving; do not rely on
            this having stayed on.
          </p>
          <TypeToConfirm
            word="GO LIVE"
            value={typed}
            onChange={setTyped}
            hint="Type GO LIVE to confirm"
          />
        </Modal>
      )}
    </>
  );
}
