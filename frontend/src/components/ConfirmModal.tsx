import type { ReactNode } from 'react';

interface ConfirmModalProps {
  title: string;
  children: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Modale di conferma generica (es. eliminazione). */
export default function ConfirmModal({
  title,
  children,
  confirmLabel = 'Conferma',
  danger = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div
        className="card modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <h2>{title}</h2>
        <div className="muted">{children}</div>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onCancel} autoFocus>
            Annulla
          </button>
          <button
            type="button"
            className={danger ? 'btn btn-danger' : 'btn btn-primary'}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
