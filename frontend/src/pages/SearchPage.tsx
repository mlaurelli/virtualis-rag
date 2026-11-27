import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import type {
  IngestPerSource,
  IngestStatus,
  SearchFilters,
  SearchResponse,
  SearchResult,
} from '../api/types';
import { openDocument } from '../utils/openDocument';
import FilterBar from '../components/FilterBar';

export default function SearchPage() {
  const [query, setQuery] = useState('');
  const [filters, setFilters] = useState<SearchFilters>({});
  const [sources, setSources] = useState<IngestPerSource[]>([]);
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // le fonti per il filtro arrivano dallo stato ingestion (accessibile a tutti i ruoli)
  useEffect(() => {
    api
      .get<IngestStatus>('/ingest/status')
      .then((s) => setSources(s.per_source))
      .catch(() => setSources([]));
  }, []);

  const runSearch = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault();
      const q = query.trim();
      if (!q) return;
      setSearching(true);
      setError(null);
      try {
        const activeFilters = Object.fromEntries(
          Object.entries(filters).filter(([, v]) => v !== undefined && v !== ''),
        ) as SearchFilters;
        const res = await api.post<SearchResponse>('/search', {
          query: q,
          filters: Object.keys(activeFilters).length > 0 ? activeFilters : undefined,
          limit: 20,
        });
        setResults(res.results);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Errore di connessione al server.');
        setResults(null);
      } finally {
        setSearching(false);
      }
    },
    [query, filters],
  );

  return (
    <div className="page">
      <div className="page-narrow">
        <div className="page-header">
          <div>
            <h1>Ricerca documenti</h1>
            <p className="subtitle">Ricerca semantica nei contenuti indicizzati.</p>
          </div>
        </div>

        <form className="search-bar" onSubmit={runSearch}>
          <input
            className="input"
            type="search"
            placeholder="Cerca nei documenti…"
            aria-label="Query di ricerca"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          <button type="submit" className="btn btn-primary" disabled={searching || !query.trim()}>
            {searching ? 'Ricerca…' : 'Cerca'}
          </button>
        </form>

        <FilterBar sources={sources} filters={filters} onChange={setFilters} />

        {error && (
          <div className="alert" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
            {error}
          </div>
        )}

        {results !== null && results.length === 0 && !error && (
          <div className="empty-state card">
            <p>Nessun risultato per questa ricerca.</p>
            <p className="small">Prova a riformulare la query o ad allargare i filtri.</p>
          </div>
        )}

        {results && results.length > 0 && (
          <div className="result-list">
            {results.map((r) => (
              <article
                key={r.chunk_id}
                className="card result-card"
                role="button"
                tabIndex={0}
                onClick={() => openDocument(r.document_id, r.page)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') openDocument(r.document_id, r.page);
                }}
                aria-label={`Apri ${r.title}${r.page !== null ? ` a pagina ${r.page}` : ''}`}
              >
                <div className="result-head">
                  <span className="result-title">{r.title}</span>
                  <span className="score" title="Punteggio di rilevanza">
                    {r.score.toFixed(2)}
                  </span>
                </div>
                <p className="result-snippet">{r.snippet}</p>
                <div className="result-meta">
                  <span>{r.source_name}</span>
                  {r.page !== null && <span>pag. {r.page}</span>}
                  {r.section_path.length > 0 && <span>{r.section_path.join(' › ')}</span>}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
