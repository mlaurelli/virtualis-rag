import { useNavigate } from 'react-router-dom';
import type { Citation } from '../api/types';
import { openDocument } from '../utils/openDocument';

interface CitationPanelProps {
  citations: Citation[];
  selected: number | null;
  onSelect: (n: number) => void;
  onClose: () => void;
}

/** Evidenzia in giallo il testo dello snippet (semplice: intero snippet come citazione). */
function Snippet({ text }: { text: string }) {
  return (
    <blockquote className="citation-snippet">
      <mark>{text}</mark>
    </blockquote>
  );
}

/** Pannello laterale destro con le citazioni della risposta selezionata. */
export default function CitationPanel({ citations, selected, onSelect, onClose }: CitationPanelProps) {
  const navigate = useNavigate();
  return (
    <aside className="citation-panel" aria-label="Citazioni">
      <div className="panel-head">
        <h3>Fonti citate</h3>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Chiudi pannello citazioni">
          ✕
        </button>
      </div>
      <div className="panel-body">
        {citations.length === 0 ? (
          <p className="muted small">Nessuna citazione per questa risposta.</p>
        ) : (
          citations.map((c) => (
            <article
              key={c.n}
              className={`citation-card${selected === c.n ? ' selected' : ''}`}
              onClick={() => onSelect(c.n)}
            >
              <div className="citation-title">
                <span className="citation-num">[{c.n}]</span>
                <span>{c.title}</span>
              </div>
              <div className="citation-meta">
                {c.source_name}
                {c.page !== null && <> · pag. {c.page}</>}
                {/* section_path arriva da JSON persistito: difensivo se malformato */}
                {Array.isArray(c.section_path) && c.section_path.length > 0 && (
                  <> · {c.section_path.join(' › ')}</>
                )}
              </div>
              <Snippet text={c.snippet} />
              <div className="citation-actions">
                {/* Evidenziazione nel viewer in-app solo con bbox (PDF re-ingested) */}
                {Array.isArray(c.bboxes) && c.bboxes.length > 0 && (
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      navigate(`/viewer/${c.document_id}?page=${c.page ?? c.bboxes?.[0]?.page ?? 1}`, {
                        state: { citation: c },
                      });
                    }}
                  >
                    Apri ed evidenzia
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={(e) => {
                    e.stopPropagation();
                    openDocument(c.document_id, c.page);
                  }}
                >
                  Apri originale
                </button>
              </div>
            </article>
          ))
        )}
      </div>
    </aside>
  );
}
