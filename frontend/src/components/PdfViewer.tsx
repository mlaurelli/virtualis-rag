/**
 * Viewer PDF in-app con evidenziazione dei passaggi citati.
 *
 * Rendering della pagina corrente su canvas via pdfjs; sopra il canvas un
 * overlay SVG disegna i rettangoli dei bbox della citazione. Le coordinate
 * arrivano dal backend in punti PDF con origine BOTTOM-LEFT (formato nativo
 * Docling): la conversione nel sistema top-left del viewport pdfjs usa
 * `viewport.convertToViewportRectangle([l, b, r, t])`.
 *
 * Il modulo è caricato lazy dalla route /viewer/:documentId: pdfjs resta
 * fuori dal bundle principale.
 */

import { useEffect, useRef, useState } from 'react';
import * as pdfjs from 'pdfjs-dist';
import type { PDFDocumentProxy } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import type { CitationBBox } from '../api/types';

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

const MIN_SCALE = 0.5;
const MAX_SCALE = 3.0;
const ZOOM_STEP = 0.25;

/** Rettangolo in coordinate viewport (top-left, pixel CSS). */
interface ViewRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface PdfViewerProps {
  /** Contenuto binario del PDF (già scaricato con autenticazione). */
  data: ArrayBuffer;
  initialPage: number;
  /** Bbox della citazione (punti PDF, bottom-left): evidenziati in overlay. */
  bboxes: CitationBBox[];
}

/** Converte un bbox bottom-left (punti PDF) nel rettangolo viewport top-left. */
function toViewRect(bbox: CitationBBox, viewport: pdfjs.PageViewport): ViewRect {
  const [x1, y1, x2, y2] = viewport.convertToViewportRectangle([
    bbox.l,
    bbox.b,
    bbox.r,
    bbox.t,
  ]);
  return {
    x: Math.min(x1, x2),
    y: Math.min(y1, y2),
    width: Math.abs(x2 - x1),
    height: Math.abs(y2 - y1),
  };
}

export default function PdfViewer({ data, initialPage, bboxes }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [pageNum, setPageNum] = useState(initialPage);
  const [scale, setScale] = useState(1.25);
  const [pageSize, setPageSize] = useState<{ width: number; height: number } | null>(null);
  const [highlights, setHighlights] = useState<ViewRect[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Caricamento del documento (una sola volta per blob)
  useEffect(() => {
    let cancelled = false;
    const task = pdfjs.getDocument({ data });
    task.promise
      .then((pdf) => {
        if (!cancelled) {
          setDoc(pdf);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setError('Impossibile leggere il PDF.');
        }
      });
    return () => {
      cancelled = true;
      void task.destroy();
    };
  }, [data]);

  // Rendering pagina + ricalcolo degli highlight a ogni cambio pagina/zoom
  useEffect(() => {
    if (!doc || !canvasRef.current) {
      return;
    }
    const canvas = canvasRef.current;
    let cancelled = false;
    let renderTask: pdfjs.RenderTask | null = null;
    doc
      .getPage(pageNum)
      .then((page) => {
        if (cancelled) {
          return;
        }
        const viewport = page.getViewport({ scale });
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        setPageSize({ width: viewport.width, height: viewport.height });
        setHighlights(
          bboxes
            .filter((bbox) => bbox.page === pageNum)
            .map((bbox) => toViewRect(bbox, viewport)),
        );
        renderTask = page.render({ canvas, viewport });
        return renderTask.promise;
      })
      .catch(() => {
        if (!cancelled) {
          setError(`Impossibile renderizzare la pagina ${pageNum}.`);
        }
      });
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [doc, pageNum, scale, bboxes]);

  if (error) {
    return <p className="muted">{error}</p>;
  }
  if (!doc) {
    return (
      <div className="route-loading" role="status">
        Caricamento del PDF…
      </div>
    );
  }

  const numPages = doc.numPages;
  return (
    <div className="pdf-viewer">
      <div className="pdf-toolbar">
        <button
          type="button"
          className="btn btn-sm"
          disabled={pageNum <= 1}
          onClick={() => setPageNum((p) => Math.max(1, p - 1))}
        >
          ← Precedente
        </button>
        <span className="muted small">
          Pagina {pageNum} di {numPages}
        </span>
        <button
          type="button"
          className="btn btn-sm"
          disabled={pageNum >= numPages}
          onClick={() => setPageNum((p) => Math.min(numPages, p + 1))}
        >
          Successiva →
        </button>
        <span className="pdf-toolbar-sep" />
        <button
          type="button"
          className="btn btn-sm"
          disabled={scale <= MIN_SCALE}
          onClick={() => setScale((s) => Math.max(MIN_SCALE, s - ZOOM_STEP))}
          aria-label="Riduci zoom"
        >
          −
        </button>
        <span className="muted small">{Math.round(scale * 100)}%</span>
        <button
          type="button"
          className="btn btn-sm"
          disabled={scale >= MAX_SCALE}
          onClick={() => setScale((s) => Math.min(MAX_SCALE, s + ZOOM_STEP))}
          aria-label="Aumenta zoom"
        >
          +
        </button>
        {highlights.length > 0 && (
          <span className="muted small pdf-hint">passaggio citato evidenziato</span>
        )}
      </div>
      <div className="pdf-canvas-scroll">
        <div
          className="pdf-page-wrap"
          style={
            pageSize
              ? { width: pageSize.width, height: pageSize.height }
              : undefined
          }
        >
          <canvas ref={canvasRef} />
          {pageSize && highlights.length > 0 && (
            <svg
              className="pdf-highlight-layer"
              viewBox={`0 0 ${pageSize.width} ${pageSize.height}`}
              aria-hidden="true"
            >
              {highlights.map((rect, i) => (
                <rect
                  key={i}
                  className="pdf-highlight-rect"
                  x={rect.x}
                  y={rect.y}
                  width={rect.width}
                  height={rect.height}
                  rx={2}
                />
              ))}
            </svg>
          )}
        </div>
      </div>
    </div>
  );
}
