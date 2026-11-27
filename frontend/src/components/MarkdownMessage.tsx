import { Children, Fragment, cloneElement, isValidElement, useMemo, type ReactElement, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Citation } from '../api/types';
import CitationMarker from './CitationMarker';

interface MarkdownMessageProps {
  content: string;
  citations?: Citation[];
  onCitationClick: (citations: Citation[], n: number) => void;
}

/** Tag il cui testo interno può contenere marker [n] da rendere cliccabili. */
type TextTag = 'p' | 'li' | 'td' | 'th' | 'h1' | 'h2' | 'h3' | 'h4' | 'strong' | 'em' | 'del';

const TEXT_TAGS: readonly TextTag[] = [
  'p',
  'li',
  'td',
  'th',
  'h1',
  'h2',
  'h3',
  'h4',
  'strong',
  'em',
  'del',
];

/**
 * Rendering markdown delle risposte dell'assistente (GFM: tabelle incluse).
 * react-markdown non renderizza HTML raw di default: il markup grezzo nel
 * testo viene mostrato come testo, nessun rischio XSS.
 * I marker [n] restano bottoni cliccabili con anteprima al passaggio del mouse:
 * i figli testuali dei componenti blocco/inline vengono ri-attraversati e i
 * marker sostituiti con <CitationMarker>.
 */
export default function MarkdownMessage({ content, citations, onCitationClick }: MarkdownMessageProps) {
  const components = useMemo<Components>(() => {
    const replaceMarkers = (node: ReactNode): ReactNode => {
      if (typeof node === 'string') {
        const parts = node.split(/(\[\d+\])/g);
        if (parts.length === 1) return node;
        return parts.map((part, i) => {
          const match = /^\[(\d+)\]$/.exec(part);
          const citation = match ? citations?.find((c) => c.n === Number(match[1])) : undefined;
          return citation ? (
            <CitationMarker
              key={i}
              citation={citation}
              onOpen={(n) => onCitationClick(citations ?? [], n)}
            />
          ) : (
            <Fragment key={i}>{part}</Fragment>
          );
        });
      }
      if (Array.isArray(node)) {
        return Children.map(node, (child) => replaceMarkers(child));
      }
      if (isValidElement(node)) {
        // nel codice i marker restano testo letterale
        if (node.type === 'code' || node.type === 'pre') return node;
        const el = node as ReactElement<{ children?: ReactNode }>;
        if (el.props.children != null) {
          return cloneElement(el, undefined, replaceMarkers(el.props.children));
        }
      }
      return node;
    };

    const withMarkers = (Tag: TextTag) => {
      function TagWithMarkers({ children }: { children?: ReactNode }) {
        return <Tag>{replaceMarkers(children)}</Tag>;
      }
      TagWithMarkers.displayName = `md-${Tag}`;
      return TagWithMarkers;
    };

    const map: Partial<Record<TextTag, ReturnType<typeof withMarkers>>> = {};
    for (const tag of TEXT_TAGS) map[tag] = withMarkers(tag);
    return map as Components;
  }, [citations, onCitationClick]);

  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
