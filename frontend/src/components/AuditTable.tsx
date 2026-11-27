import { Fragment, useState } from 'react';
import type { AuditEntry } from '../api/types';

interface AuditTableProps {
  entries: AuditEntry[];
}

function formatLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`;
}

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('it-IT');
}

/** Registro audit: riga espandibile con passi, risposta e chunk consultati. */
export default function AuditTable({ entries }: AuditTableProps) {
  const [expanded, setExpanded] = useState<number | null>(null);

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Data</th>
            <th scope="col">Utente</th>
            <th scope="col">Interrogazione</th>
            <th scope="col">Percorso</th>
            <th scope="col">Passi</th>
            <th scope="col">Documenti</th>
            <th scope="col">Latenza</th>
            <th scope="col">Avvisi</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <Fragment key={e.id}>
              <tr
                style={{ cursor: 'pointer' }}
                onClick={() => setExpanded(expanded === e.id ? null : e.id)}
                title="Clicca per i dettagli"
              >
                <td className="muted small" style={{ whiteSpace: 'nowrap' }}>
                  {formatDateTime(e.created_at)}
                </td>
                <td className="mono faint">#{e.user_id}</td>
                <td className="audit-question" title={e.question}>
                  {e.question}
                </td>
                <td>
                  <span className={`badge badge-${e.path}`}>
                    {e.path === 'fast' ? 'Fast' : 'Agentic'}
                  </span>
                </td>
                <td className="muted">{e.steps.length}</td>
                <td className="muted">{e.retrieved_chunk_ids.length}</td>
                <td className="mono">{formatLatency(e.latency_ms)}</td>
                <td>
                  {e.warnings.length > 0 ? (
                    <span className="badge badge-pending" title={e.warnings.join('\n')}>
                      {e.warnings.length}
                    </span>
                  ) : (
                    <span className="faint">—</span>
                  )}
                </td>
              </tr>
              {expanded === e.id && (
                <tr>
                  <td colSpan={8} className="audit-detail">
                    <p style={{ margin: 0 }}>
                      <strong>Risposta:</strong> {e.answer}
                    </p>
                    <p style={{ margin: 'var(--space-2) 0 0' }}>
                      <strong>Chunk consultati:</strong>{' '}
                      {e.retrieved_chunk_ids.length > 0 ? e.retrieved_chunk_ids.join(', ') : 'nessuno'}
                      {' · '}<strong>Sessione:</strong> #{e.session_id} · <strong>Audit:</strong> #{e.id}
                    </p>
                    <pre>{JSON.stringify(e.steps, null, 2)}</pre>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
