import type { ReactNode } from 'react';
import { useEffect, useRef, useState } from 'react';
import { Inbox, Lock, X } from 'lucide-react';
import { bucketColor, isIntensive } from '../lib/domain';

export function Card({
  title,
  eyebrow,
  actions,
  children,
  flush,
}: {
  title?: string;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          {title && <h2>{title}</h2>}
          {eyebrow && <span className="eyebrow">{eyebrow}</span>}
          {actions && <div className="row" style={{ marginLeft: 'auto' }}>{actions}</div>}
        </header>
      )}
      <div className={flush ? 'card-body flush' : 'card-body'}>{children}</div>
    </section>
  );
}

export function Empty({ title, note }: { title: string; note?: string }) {
  return (
    <div className="empty">
      <Inbox />
      <h3>{title}</h3>
      {note && <p>{note}</p>}
    </div>
  );
}

export function Toggle({
  on,
  onChange,
  label,
  disabled,
  lockReason,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  disabled?: boolean;
  lockReason?: string;
}) {
  return (
    <button
      type="button"
      className={`tog${on ? ' is-on' : ''}`}
      disabled={disabled}
      aria-pressed={on}
      title={lockReason}
      onClick={() => !disabled && onChange(!on)}
    >
      <span className="tog-box" />
      <span className="tog-label">{label}</span>
      {disabled && lockReason && (
        <span className="tog-lock" aria-label={lockReason}>
          <Lock />
        </span>
      )}
    </button>
  );
}

export function BucketTag({ bucket, label }: { bucket: string; label?: string }) {
  return (
    <span
      className={`bucket-tag${isIntensive(bucket) ? ' is-intensive' : ''}`}
      style={{ color: bucketColor(bucket) }}
    >
      <span className="chip-dot" style={{ background: bucketColor(bucket) }} />
      {bucket}
      {label && <span style={{ color: 'var(--muted)', fontWeight: 400 }}>{label}</span>}
    </span>
  );
}

export function Modal({
  title,
  children,
  footer,
  onClose,
}: {
  title: string;
  children: ReactNode;
  footer: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    ref.current?.querySelector<HTMLElement>('input, button')?.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <header className="modal-head">
          <h3>{title}</h3>
        </header>
        <div className="modal-body">{children}</div>
        <footer className="modal-foot">{footer}</footer>
      </div>
    </div>
  );
}

export function Fact({ k, v, tone }: { k: string; v: ReactNode; tone?: string }) {
  return (
    <div className="confirm-fact">
      <span style={{ color: 'var(--muted)' }}>{k}</span>
      <b style={tone ? { color: tone } : undefined}>{v}</b>
    </div>
  );
}

export function Pager({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (p: number) => void;
}) {
  const last = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);
  return (
    <div className="pager">
      <span>
        {from}–{to} of {total}
      </span>
      <span className="pager-spacer" />
      <button className="btn btn-sm btn-ghost" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span>
        {page} / {last}
      </span>
      <button className="btn btn-sm btn-ghost" disabled={page >= last} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </div>
  );
}

/** Confirmation that costs a deliberate keystroke, not just a click. */
export function TypeToConfirm({
  word,
  value,
  onChange,
  hint,
}: {
  word: string;
  value: string;
  onChange: (v: string) => void;
  hint: string;
}) {
  return (
    <label className="type-to-confirm">
      <span className="field-hint">{hint}</span>
      <input
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={word}
        autoComplete="off"
        spellCheck={false}
        aria-label={`Type ${word} to confirm`}
      />
    </label>
  );
}

export function CloseButton({ onClick }: { onClick: () => void }) {
  return (
    <button className="icon-btn" onClick={onClick} aria-label="Dismiss">
      <X />
    </button>
  );
}

/** Copy-to-clipboard for policy numbers, the one thing operators paste out. */
export function Copyable({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="btn btn-sm btn-ghost mono"
      style={{ padding: '1px 4px' }}
      title="Copy"
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1200);
      }}
    >
      {done ? 'copied' : text}
    </button>
  );
}
