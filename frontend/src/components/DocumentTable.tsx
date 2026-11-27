import type { DocumentItem } from '../api/types';
import { openDocument } from '../utils/openDocument';
import StatusBadge from './StatusBadge';

interface DocumentTableProps {
  documents: DocumentItem[];
  onDelete: (doc: DocumentItem) => void;
  /** Mappa source_id → nome leggibile (da /api/ingest/status); fallback #id */
  sourceNames?: Record<number, string>;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/** Tabella dei documenti con stato indicizzazione e azioni. */
export default function DocumentTable({ documents, onDelete, sourceNames }: DocumentTableProps) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Titolo</th>
            <th scope="col">Tipo</th>
            <th scope="col">Stato</th>
            <th scope="col">Fonte</th>
            <th scope="col">Pagine</th>
            <th scope="col">Dimensione</th>
            <th scope="col">Modificato</th>
            <th scope="col" aria-label="Azioni" />
          </tr>
        </thead>
        <tbody>
          {documents.map((d) => (
            <tr key={d.id}>
              <td>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  style={{ padding: 0, fontWeight: 550 }}
                  onClick={() => openDocument(d.id, null, d.doc_type)}
                  title={d.external_id}
                >
                  {d.title}
                </button>
              </td>
              <td className="mono faint">{d.doc_type.toUpperCase()}</td>
              <td>
                <StatusBadge status={d.status} error={d.error} />
              </td>
              <td className="muted">{sourceNames?.[d.source_id] ?? `#${d.source_id}`}</td>
              <td className="muted">{d.page_count ?? '—'}</td>
              <td className="muted">{formatSize(d.size_bytes)}</td>
              <td className="muted">{formatDate(d.modified_at)}</td>
              <td style={{ textAlign: 'right' }}>
                <button
                  type="button"
                  className="icon-btn danger"
                  aria-label={`Elimina ${d.title}`}
                  title="Elimina documento"
                  onClick={() => onDelete(d)}
                >
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
