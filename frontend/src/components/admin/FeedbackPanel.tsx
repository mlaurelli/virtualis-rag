import { useCallback, useEffect, useState } from 'react';
import { ApiError, getAdminFeedback, getAdminFeedbackStats } from '../../api/client';
import type { FeedbackEntry, FeedbackRating, FeedbackStats } from '../../api/types';

type Filter = 'all' | 'positive' | 'negative';

const FILTERS: { id: Filter; label: string; rating?: FeedbackRating }[] = [
  { id: 'all', label: 'Tutti' },
  { id: 'positive', label: 'Positivi', rating: 1 },
  { id: 'negative', label: 'Negativi', rating: -1 },
];

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('it-IT');
}

/** Pannello feedback degli utenti: statistiche e ultimi giudizi (solo admin). */
export default function FeedbackPanel() {
  const [stats, setStats] = useState<FeedbackStats | null>(null);
  const [entries, setEntries] = useState<FeedbackEntry[]>([]);
  const [filter, setFilter] = useState<Filter>('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (current: Filter) => {
    setLoading(true);
    try {
      const rating = FILTERS.find((f) => f.id === current)?.rating;
      const [statsRes, listRes] = await Promise.all([
        getAdminFeedbackStats(),
        getAdminFeedback(rating, 100),
      ]);
      setStats(statsRes);
      setEntries(listRes.entries);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Errore di connessione.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(filter);
  }, [load, filter]);

  if (loading && !stats) {
    return (
      <div className="empty-state card">
        <div className="spinner" role="status" aria-label="Caricamento feedback" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="alert" role="alert">
        {error}{' '}
        <button type="button" className="btn btn-sm" onClick={() => void load(filter)}>
          Riprova
        </button>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-5)' }}>
      {stats && (
        <div className="feedback-stats">
          <div className="card feedback-stat">
            <span className="feedback-stat-value">{stats.total}</span>
            <span className="muted small">Feedback totali</span>
          </div>
          <div className="card feedback-stat">
            <span className="feedback-stat-value">
              {stats.positive_pct !== null ? `${stats.positive_pct}%` : '—'}
            </span>
            <span className="muted small">Positivi</span>
          </div>
          <div className="card feedback-stat">
            <span className="feedback-stat-value">{stats.negative}</span>
            <span className="muted small">Negativi</span>
          </div>
        </div>
      )}

      <div className="feedback-filters" role="group" aria-label="Filtro per giudizio">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            className={`btn btn-sm${filter === f.id ? ' btn-primary' : ''}`}
            onClick={() => setFilter(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>

      {entries.length === 0 ? (
        <div className="empty-state card">
          <p>Nessun feedback{filter === 'all' ? '' : ` ${filter === 'positive' ? 'positivo' : 'negativo'}`}.</p>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Data</th>
                <th scope="col">Utente</th>
                <th scope="col">Giudizio</th>
                <th scope="col">Quesito</th>
                <th scope="col">Commento</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td className="muted small" style={{ whiteSpace: 'nowrap' }}>
                    {formatDateTime(e.created_at)}
                  </td>
                  <td>{e.username ?? '—'}</td>
                  <td>
                    <span className={`badge ${e.rating === 1 ? 'badge-indexed' : 'badge-failed'}`}>
                      {e.rating === 1 ? 'Positivo' : 'Negativo'}
                    </span>
                  </td>
                  <td className="audit-question" title={e.question}>
                    {e.question}
                  </td>
                  <td className="muted">{e.comment ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
