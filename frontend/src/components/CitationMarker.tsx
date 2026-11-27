import type { Citation } from '../api/types';

interface CitationMarkerProps {
  citation: Citation;
  onOpen: (n: number) => void;
}

/**
 * Marker citazione [n] inline: il click apre il pannello citazioni,
 * hover/focus mostra un'anteprima (titolo, fonte, snippet) senza click.
 * Il popover è pure CSS (:hover / :focus-within), quindi funziona anche da tastiera.
 */
export default function CitationMarker({ citation, onOpen }: CitationMarkerProps) {
  const { n } = citation;
  return (
    <span className="citation-wrap">
      <button
        type="button"
        className="citation-marker"
        onClick={() => onOpen(n)}
        aria-label={`Citazione ${n}: ${citation.title}`}
      >
        {n}
      </button>
      <span className="citation-pop" role="tooltip">
        <span className="citation-pop-title">{citation.title}</span>
        <span className="citation-pop-meta">
          {citation.source_name}
          {citation.page !== null && ` · pag. ${citation.page}`}
        </span>
        <span className="citation-pop-snippet">{citation.snippet}</span>
      </span>
    </span>
  );
}
