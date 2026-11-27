/**
 * Pagina viewer PDF: /viewer/:documentId?page=N
 *
 * Raggiunta dal CitationPanel con "Apri ed evidenzia": la citazione (con i
 * bbox del passaggio) viaggia nello state della navigazione; a un reload
 * diretto dell'URL resta la pagina (senza evidenziazione).
 *
 * Fallback: se il documento non è un PDF viene aperto in una nuova scheda
 * come blob (comportamento precedente del viewer del browser).
 */

import { useEffect, useState } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { fetchProtectedBlob } from '../api/client';
import type { Citation } from '../api/types';
import PdfViewer from '../components/PdfViewer';

type LoadState = 'loading' | 'ready' | 'not-pdf' | 'error';

export default function ViewerPage() {
  const { documentId } = useParams<{ documentId: string }>();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const citation = (location.state as { citation?: Citation } | null)?.citation;

  const [state, setState] = useState<LoadState>('loading');
  const [data, setData] = useState<ArrayBuffer | null>(null);
  const [errorDetail, setErrorDetail] = useState('');

  const rawPage = Number(searchParams.get('page'));
  const initialPage =
    Number.isInteger(rawPage) && rawPage >= 1 ? rawPage : (citation?.page ?? 1);

  useEffect(() => {
    let cancelled = false;
    fetchProtectedBlob(`/documents/${documentId}/file`)
      .then(async (blob) => {
        if (cancelled) {
          return;
        }
        if (blob.type !== 'application/pdf') {
          // Non-PDF: fallback all'apertura in una nuova scheda
          const url = URL.createObjectURL(blob);
          window.open(url, '_blank', 'noopener');
          window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
          setState('not-pdf');
          return;
        }
        setData(await blob.arrayBuffer());
        setState('ready');
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setErrorDetail(err instanceof Error ? err.message : 'Errore di rete');
          setState('error');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  return (
    <main className="viewer-page">
      <div className="viewer-head">
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate(-1)}>
          ← Torna alla chat
        </button>
        <h2 className="viewer-title">{citation?.title ?? `Documento #${documentId}`}</h2>
      </div>
      {state === 'loading' && (
        <div className="route-loading" role="status">
          Scaricamento del documento…
        </div>
      )}
      {state === 'not-pdf' && (
        <p className="muted">
          Il documento non è un PDF: è stato aperto in una nuova scheda.
        </p>
      )}
      {state === 'error' && (
        <p className="muted">Impossibile aprire il documento: {errorDetail}</p>
      )}
      {state === 'ready' && data && (
        <PdfViewer
          data={data}
          initialPage={initialPage}
          bboxes={citation?.bboxes ?? []}
        />
      )}
    </main>
  );
}
