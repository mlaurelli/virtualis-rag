import type { DocStatus } from '../api/types';

interface StatusBadgeProps {
  status: DocStatus;
  /** messaggio d'errore per lo stato failed (mostrato in tooltip) */
  error?: string | null;
}

const STATUS_LABELS: Record<DocStatus, string> = {
  pending: 'In coda',
  processing: 'In elaborazione',
  indexed: 'Indicizzato',
  failed: 'Fallito',
};

/** Badge di stato indicizzazione; per `failed` l'errore è in tooltip. */
export default function StatusBadge({ status, error }: StatusBadgeProps) {
  const label = STATUS_LABELS[status] ?? status;
  return (
    <span
      className={`badge badge-${status}`}
      title={status === 'failed' && error ? error : undefined}
      aria-label={status === 'failed' && error ? `${label}: ${error}` : label}
    >
      {label}
    </span>
  );
}
