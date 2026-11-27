/**
 * Controlli di paginazione condivisi (documenti, registro audit, …):
 * pagina corrente, totale elementi e pulsanti precedente/successiva.
 * La navigazione avviene per offset, coerente con i parametri offset/limit
 * delle API di elenco del backend.
 */

interface PaginationProps {
  /** Offset del primo elemento visualizzato (multiplo di limit). */
  offset: number;
  /** Dimensione pagina. */
  limit: number;
  /** Totale elementi che soddisfano i filtri. */
  total: number;
  /** Etichetta plurale degli elementi (es. "documenti", "voci"). */
  label: string;
  onOffsetChange: (offset: number) => void;
}

export default function Pagination({ offset, limit, total, label, onOffsetChange }: PaginationProps) {
  const pageCount = Math.max(1, Math.ceil(total / limit));
  const page = Math.floor(offset / limit) + 1;

  return (
    <div className="pagination" role="navigation" aria-label={`Paginazione ${label}`}>
      <span className="small faint">
        Pagina {page} di {pageCount} · {total} {label}
      </span>
      <div className="pagination-buttons">
        <button
          type="button"
          className="btn btn-sm"
          disabled={offset <= 0}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          ← Precedente
        </button>
        <button
          type="button"
          className="btn btn-sm"
          disabled={offset + limit >= total}
          onClick={() => onOffsetChange(offset + limit)}
        >
          Successiva →
        </button>
      </div>
    </div>
  );
}
