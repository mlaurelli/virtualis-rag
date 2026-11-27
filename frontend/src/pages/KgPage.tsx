import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type {
  KgEntitiesResponse,
  KgEntityContext,
  KgEntitySummary,
  KgRelation,
  KgRelationRef,
} from '../api/types';
import KgGraph from '../components/KgGraph';
import { openDocument } from '../utils/openDocument';

const SEARCH_DEBOUNCE_MS = 300;
const SEARCH_LIMIT = 50;

/** Classi badge per il vocabolario chiuso dei tipi entità (fallback: altro). */
const TYPE_BADGE: Record<string, string> = {
  persona: 'kg-badge-persona',
  organizzazione: 'kg-badge-organizzazione',
  contratto: 'kg-badge-contratto',
  progetto: 'kg-badge-progetto',
  data: 'kg-badge-data',
  importo: 'kg-badge-importo',
  prodotto: 'kg-badge-prodotto',
  altro: 'kg-badge-altro',
};

function typeBadgeClass(type: string): string {
  return TYPE_BADGE[type] ?? TYPE_BADGE.altro;
}

/** Nome leggibile di una controparte (il backend può rimandare un ref vuoto). */
function refName(ref: KgRelationRef): string {
  return ref.display_name || `entità #${ref.id}`;
}

export default function KgPage() {
  const [query, setQuery] = useState('');
  const [entities, setEntities] = useState<KgEntitySummary[] | null>(null);
  const [searching, setSearching] = useState(true);
  const [kgDisabled, setKgDisabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<KgEntityContext | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [view, setView] = useState<'tabella' | 'grafo'>('tabella');
  const requestSeq = useRef(0);

  // ricerca debounced (q vuoto elenca le prime entità del grafo)
  useEffect(() => {
    const seq = ++requestSeq.current;
    setSearching(true);
    const timer = setTimeout(() => {
      api
        .get<KgEntitiesResponse>(
          `/kg/entities?q=${encodeURIComponent(query.trim())}&limit=${SEARCH_LIMIT}`,
        )
        .then((res) => {
          if (seq !== requestSeq.current) return;
          setEntities(res.entities);
          setError(null);
        })
        .catch((err: unknown) => {
          if (seq !== requestSeq.current) return;
          if (err instanceof ApiError && err.status === 404) {
            // 404 sulla lista = knowledge graph disabilitato lato server
            setKgDisabled(true);
          } else {
            setError(err instanceof ApiError ? err.message : 'Errore di connessione al server.');
            setEntities(null);
          }
        })
        .finally(() => {
          if (seq === requestSeq.current) setSearching(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  const loadEntity = useCallback(async (id: number) => {
    setDetail(null);
    setDetailLoading(true);
    setDetailError(null);
    try {
      setDetail(await api.get<KgEntityContext>(`/kg/entities/${id}`));
    } catch (err) {
      setDetailError(err instanceof ApiError ? err.message : 'Errore di connessione al server.');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  if (kgDisabled) {
    return (
      <div className="page">
        <div className="page-narrow">
          <div className="page-header">
            <div>
              <h1>Knowledge Graph</h1>
              <p className="subtitle">Entità e relazioni estratte dai documenti indicizzati.</p>
            </div>
          </div>
          <div className="empty-state card">
            <p>Il knowledge graph non è attivo su questo server.</p>
            <p className="small">
              Per attivarlo imposta <span className="mono">KG_ENABLED=true</span> nella
              configurazione del backend e popola il grafo con{' '}
              <span className="mono">python scripts/kg_build.py</span> (oppure re-indicizza i
              documenti).
            </p>
          </div>
        </div>
      </div>
    );
  }

  const asSubject: KgRelation[] = [];
  const asObject: KgRelation[] = [];
  if (detail) {
    for (const rel of detail.relations) {
      if (rel.subject.id === detail.entity.id) {
        asSubject.push(rel);
      } else {
        asObject.push(rel);
      }
    }
  }

  const renderRelations = (relations: KgRelation[], counterpartOf: 'subject' | 'object') => {
    if (relations.length === 0) {
      return <p className="faint small">Nessuna relazione.</p>;
    }
    return (
      <ul className="kg-relations">
        {relations.map((rel) => {
          const counterpart = counterpartOf === 'subject' ? rel.object : rel.subject;
          return (
            <li key={rel.id} className="kg-relation">
              <span className="mono small">{rel.predicate}</span>
              <span className="faint" aria-hidden="true">
                →
              </span>
              <button
                type="button"
                className="kg-link"
                onClick={() => void loadEntity(counterpart.id)}
              >
                {refName(counterpart)}
              </button>
              {counterpart.type && (
                <span className={`badge ${typeBadgeClass(counterpart.type)}`}>
                  {counterpart.type}
                </span>
              )}
              {rel.document_title && (
                <span className="faint small">
                  da{' '}
                  <button
                    type="button"
                    className="kg-link"
                    onClick={() => openDocument(rel.document_id, null)}
                  >
                    {rel.document_title}
                  </button>
                </span>
              )}
            </li>
          );
        })}
      </ul>
    );
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Knowledge Graph</h1>
          <p className="subtitle">Entità e relazioni estratte dai documenti indicizzati.</p>
        </div>
      </div>

      <form className="search-bar" onSubmit={(e) => e.preventDefault()}>
        <input
          className="input"
          type="search"
          placeholder="Cerca un'entità (nome, organizzazione, contratto…)…"
          aria-label="Cerca entità"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoFocus
        />
      </form>

      {error && (
        <div className="alert" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
          {error}
        </div>
      )}

      <div className="kg-view-toggle" role="tablist" aria-label="Modalità di visualizzazione">
        <button
          type="button"
          role="tab"
          aria-selected={view === 'tabella'}
          className={`kg-view-btn${view === 'tabella' ? ' kg-view-btn-active' : ''}`}
          onClick={() => setView('tabella')}
        >
          Tabella
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === 'grafo'}
          className={`kg-view-btn${view === 'grafo' ? ' kg-view-btn-active' : ''}`}
          onClick={() => setView('grafo')}
        >
          Grafo
        </button>
      </div>

      <div className="kg-layout">
        <section aria-label="Entità">
          {view === 'grafo' ? (
            <KgGraph
              selectedId={detail?.entity.id ?? null}
              onSelect={(id) => void loadEntity(id)}
            />
          ) : searching && entities === null ? (
            <div className="empty-state card">
              <div className="spinner" role="status" aria-label="Caricamento entità" />
            </div>
          ) : entities !== null && entities.length === 0 ? (
            <div className="empty-state card">
              <p>Nessuna entità trovata{query.trim() ? ` per «${query.trim()}»` : ''}.</p>
              <p className="small">Prova con un altro termine di ricerca.</p>
            </div>
          ) : entities !== null ? (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Entità</th>
                    <th>Tipo</th>
                    <th>Documenti</th>
                  </tr>
                </thead>
                <tbody>
                  {entities.map((entity) => (
                    <tr
                      key={entity.id}
                      className={`kg-row${detail?.entity.id === entity.id ? ' kg-row-selected' : ''}`}
                      tabIndex={0}
                      onClick={() => void loadEntity(entity.id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') void loadEntity(entity.id);
                      }}
                      aria-label={`Dettaglio di ${entity.display_name}`}
                    >
                      <td>
                        <div>{entity.display_name}</div>
                        {entity.description && (
                          <div className="faint small kg-cell-desc">{entity.description}</div>
                        )}
                      </td>
                      <td>
                        <span className={`badge ${typeBadgeClass(entity.type)}`}>
                          {entity.type}
                        </span>
                      </td>
                      <td>{entity.document_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>

        <section aria-label="Dettaglio entità">
          {detailLoading ? (
            <div className="empty-state card">
              <div className="spinner" role="status" aria-label="Caricamento dettaglio" />
            </div>
          ) : detailError ? (
            <div className="alert" role="alert">
              {detailError}
            </div>
          ) : detail ? (
            <article className="card kg-detail-card">
              <div className="kg-detail-head">
                <h2>{detail.entity.display_name}</h2>
                <span className={`badge ${typeBadgeClass(detail.entity.type)}`}>
                  {detail.entity.type}
                </span>
              </div>
              {detail.entity.description && <p className="muted">{detail.entity.description}</p>}

              <h3>Documenti che la menzionano ({detail.documents.length})</h3>
              {detail.documents.length === 0 ? (
                <p className="faint small">Nessun documento.</p>
              ) : (
                <ul className="kg-doc-list">
                  {detail.documents.map((doc) => (
                    <li key={doc.document_id}>
                      <button
                        type="button"
                        className="kg-link"
                        onClick={() => openDocument(doc.document_id, null)}
                      >
                        {doc.title}
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              <h3>Relazioni come soggetto ({asSubject.length})</h3>
              {renderRelations(asSubject, 'subject')}

              <h3>Relazioni come oggetto ({asObject.length})</h3>
              {renderRelations(asObject, 'object')}
            </article>
          ) : (
            <div className="empty-state card">
              <p>Seleziona un'entità dalla tabella.</p>
              <p className="small">
                Vedrai descrizione, documenti che la menzionano e relazioni con le altre entità.
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
