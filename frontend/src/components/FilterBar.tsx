import type { IngestPerSource, SearchFilters } from '../api/types';

interface FilterBarProps {
  sources: IngestPerSource[];
  filters: SearchFilters;
  onChange: (filters: SearchFilters) => void;
}

const DOC_TYPES = ['pdf', 'docx', 'xlsx', 'pptx', 'txt', 'md', 'html'];

/** Barra filtri della ricerca: fonte, tipologia documento, intervallo date. */
export default function FilterBar({ sources, filters, onChange }: FilterBarProps) {
  const set = <K extends keyof SearchFilters>(key: K, value: SearchFilters[K]) => {
    onChange({ ...filters, [key]: value });
  };

  return (
    <div className="card filter-bar" role="search" aria-label="Filtri di ricerca">
      <div className="field">
        <label htmlFor="filter-source">Fonte</label>
        <select
          id="filter-source"
          className="select"
          value={filters.source_id ?? ''}
          onChange={(e) =>
            set('source_id', e.target.value === '' ? undefined : Number(e.target.value))
          }
        >
          <option value="">Tutte le fonti</option>
          {sources.map((s) => (
            <option key={s.source_id} value={s.source_id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="filter-doctype">Tipologia</label>
        <select
          id="filter-doctype"
          className="select"
          value={filters.doc_type ?? ''}
          onChange={(e) => set('doc_type', e.target.value === '' ? undefined : e.target.value)}
        >
          <option value="">Tutti i tipi</option>
          {DOC_TYPES.map((t) => (
            <option key={t} value={t}>
              {t.toUpperCase()}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="filter-from">Da data</label>
        <input
          id="filter-from"
          type="date"
          className="input"
          value={filters.date_from ?? ''}
          onChange={(e) => set('date_from', e.target.value === '' ? undefined : e.target.value)}
        />
      </div>

      <div className="field">
        <label htmlFor="filter-to">A data</label>
        <input
          id="filter-to"
          type="date"
          className="input"
          value={filters.date_to ?? ''}
          onChange={(e) => set('date_to', e.target.value === '' ? undefined : e.target.value)}
        />
      </div>

      <button
        type="button"
        className="btn btn-ghost btn-sm"
        onClick={() => onChange({})}
        aria-label="Azzera filtri"
      >
        Azzera filtri
      </button>
    </div>
  );
}
