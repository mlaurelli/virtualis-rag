import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type {
  DocStatus,
  DocumentItem,
  DocumentsResponse,
  IngestStatus,
  Source,
  SourcesResponse,
  SyncResponse,
} from '../api/types';
import ConfirmModal from '../components/ConfirmModal';
import DocumentTable from '../components/DocumentTable';
import Pagination from '../components/Pagination';
import UploadZone from '../components/UploadZone';

const PAGE_SIZE = 50;
// polling frequente solo con coda attiva; a coda vuota basta un ritmo blando
const POLL_ACTIVE_MS = 4000;
const POLL_IDLE_MS = 15000;
// il pulsante di sync resta disabilitato almeno per questo tempo dopo la 202:
// la scansione della cartella parte in background e la coda può restare
// vuota per qualche secondo prima che compaiano i primi documenti
const SYNC_HOLD_MS = 8000;
// il messaggio di conferma avvio sync sparisce da solo
const SYNC_NOTICE_MS = 10000;

/** "Ultimo aggiornamento" della sorgente: gg/mm hh:mm. */
function formatLastSync(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('it-IT', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Firma dei contatori dello stato ingestion: se cambia, la tabella va ricaricata. */
function ingestSignature(status: IngestStatus): string {
  return [
    status.queue.pending,
    status.queue.processing,
    ...status.per_source.flatMap((s) => [s.indexed, s.failed]),
  ].join('|');
}

export default function DocumentsPage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<DocStatus | ''>('');
  const [ingest, setIngest] = useState<IngestStatus | null>(null);
  const [toDelete, setToDelete] = useState<DocumentItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  // sorgenti local_fs abilitate: il tipo arriva solo da /admin/sources
  // (route admin-only: se la chiamata fallisce il pulsante resta nascosto)
  const [localFsSources, setLocalFsSources] = useState<Source[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [syncNotice, setSyncNotice] = useState<string | null>(null);
  const syncTimersRef = useRef<number[]>([]);
  // filtri correnti letti dal polling senza ricreare i callback a ogni cambio
  const queryRef = useRef({ status: statusFilter, offset });
  queryRef.current = { status: statusFilter, offset };
  const ingestSigRef = useRef<string | null>(null);

  const loadDocuments = useCallback(async (status: DocStatus | '', off: number) => {
    try {
      const params = new URLSearchParams({ offset: String(off), limit: String(PAGE_SIZE) });
      if (status) params.set('status', status);
      const res = await api.get<DocumentsResponse>(`/documents?${params.toString()}`);
      setDocuments(res.documents);
      setTotal(res.total);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Errore di connessione al server.');
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll dello stato ingestion; la tabella viene ricaricata SOLO quando i
  // contatori cambiano davvero (confronto per valore, non per identità
  // dell'oggetto): copre sia le indicizzazioni lente sia quelle più veloci
  // dell'intervallo di polling (la coda risulta scarica ma indexed cresce).
  const loadIngestStatus = useCallback(async () => {
    try {
      const status = await api.get<IngestStatus>('/ingest/status');
      setIngest(status);
      const sig = ingestSignature(status);
      if (ingestSigRef.current !== null && ingestSigRef.current !== sig) {
        const { status: currentStatus, offset: currentOffset } = queryRef.current;
        void loadDocuments(currentStatus, currentOffset);
      }
      ingestSigRef.current = sig;
    } catch {
      // lo stato della coda è accessorio: ignora errori
    }
  }, [loadDocuments]);

  useEffect(() => {
    setLoading(true);
    void loadDocuments(statusFilter, offset);
  }, [statusFilter, offset, loadDocuments]);

  const queueActive = ingest !== null && (ingest.queue.pending > 0 || ingest.queue.processing > 0);

  // polling dello stato indicizzazione: 4 s con coda attiva (o sync appena
  // avviata: la scansione server-side precede i primi pending), 15 s a riposo
  useEffect(() => {
    void loadIngestStatus();
    const interval = setInterval(() => {
      void loadIngestStatus();
    }, queueActive || syncing ? POLL_ACTIVE_MS : POLL_IDLE_MS);
    return () => clearInterval(interval);
  }, [queueActive, syncing, loadIngestStatus]);

  // sorgenti configurate (una tantum): serve il tipo per capire se esiste
  // una cartella di rete (local_fs) sincronizzabile manualmente
  useEffect(() => {
    api
      .get<SourcesResponse>('/admin/sources')
      .then((res) =>
        setLocalFsSources(res.sources.filter((s) => s.type === 'local_fs' && s.enabled)),
      )
      .catch(() => {
        // utente non admin o errore: il pulsante di sync non viene mostrato
      });
  }, []);

  // timer della sync (hold del pulsante, auto-chiusura del messaggio)
  useEffect(
    () => () => {
      for (const t of syncTimersRef.current) window.clearTimeout(t);
    },
    [],
  );

  const handleSync = useCallback(async () => {
    setSyncing(true);
    setSyncNotice(null);
    try {
      // body {} = sincronizza tutte le sorgenti local_fs abilitate
      await api.post<SyncResponse>('/ingest/sync', {});
      setSyncNotice('Sincronizzazione avviata: i nuovi file compariranno a breve.');
      // riallinea subito lo stato coda (e con esso il polling veloce)
      void loadIngestStatus();
      syncTimersRef.current.push(
        // dopo l'hold minimo il pulsante torna attivo se la coda è scarica
        // (con coda attiva resta comunque disabilitato via queueActive)
        window.setTimeout(() => setSyncing(false), SYNC_HOLD_MS),
        window.setTimeout(() => setSyncNotice(null), SYNC_NOTICE_MS),
      );
    } catch (err) {
      setSyncing(false);
      setError(err instanceof ApiError ? err.message : 'Avvio della sincronizzazione fallito.');
    }
  }, [loadIngestStatus]);

  // last_sync_at più recente tra le local_fs: preferisce il valore live del
  // polling (/ingest/status), con fallback su quello caricato una tantum
  const lastSyncAt = useMemo(() => {
    const ids = new Set(localFsSources.map((s) => s.id));
    const values = [
      ...(ingest?.per_source ?? [])
        .filter((s) => ids.has(s.source_id))
        .map((s) => s.last_sync_at),
      ...localFsSources.map((s) => s.last_sync_at),
    ].filter((v): v is string => v !== null);
    if (values.length === 0) return null;
    return values.reduce((a, b) => (a > b ? a : b));
  }, [localFsSources, ingest]);

  const syncBusy = syncing || queueActive;

  const handleDelete = async () => {
    if (!toDelete) return;
    setDeleting(true);
    try {
      await api.delete(`/documents/${toDelete.id}`);
      setToDelete(null);
      await loadDocuments(statusFilter, offset);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Eliminazione fallita.');
      setToDelete(null);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Documenti</h1>
          <p className="subtitle">Archivio indicizzato e stato della coda di ingestion.</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
          {localFsSources.length > 0 && (
            <div className="field" style={{ margin: 0, alignItems: 'flex-end' }}>
              <span className="faint small">
                {lastSyncAt
                  ? `Ultimo aggiornamento: ${formatLastSync(lastSyncAt)}`
                  : 'Mai sincronizzata'}
              </span>
              <button
                type="button"
                className="btn"
                disabled={syncBusy}
                onClick={() => void handleSync()}
              >
                {syncBusy ? 'In sincronizzazione…' : 'Aggiorna da cartella di rete'}
              </button>
            </div>
          )}
          <div className="field" style={{ margin: 0 }}>
            <label htmlFor="doc-status-filter" className="faint small">
              Filtra per stato
            </label>
            <select
              id="doc-status-filter"
              className="select"
              style={{ minWidth: 180 }}
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value as DocStatus | '');
                setOffset(0); // il cambio filtro riparte dalla prima pagina
              }}
            >
              <option value="">Tutti gli stati</option>
              <option value="pending">In coda</option>
              <option value="processing">In elaborazione</option>
              <option value="indexed">Indicizzati</option>
              <option value="failed">Falliti</option>
            </select>
          </div>
        </div>
      </div>

      <UploadZone onUploaded={() => void loadDocuments(statusFilter, offset)} />

      {syncNotice && (
        <div className="upload-progress" role="status">
          {syncNotice}
        </div>
      )}

      {queueActive && (
        <div className="upload-progress" role="status">
          <span className="spinner" aria-hidden="true" />
          Indicizzazione in corso: {ingest.queue.pending} in coda, {ingest.queue.processing} in
          elaborazione…
        </div>
      )}

      {ingest && ingest.recent.some((r) => r.status === 'failed') && (
        <div className="alert" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
          Ultimi fallimenti:{' '}
          {ingest.recent
            .filter((r) => r.status === 'failed')
            .map((r) => `${r.title}${r.error ? ` (${r.error})` : ''}`)
            .join(' · ')}
        </div>
      )}

      {error && (
        <div className="alert" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
          {error}
        </div>
      )}

      {loading ? (
        <div className="table-wrap" role="status" aria-label="Caricamento documenti">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Titolo</th>
                <th scope="col">Tipo</th>
                <th scope="col">Stato</th>
                <th scope="col">Fonte</th>
                <th scope="col">Pagine</th>
                <th scope="col">Dimensione</th>
                <th scope="col">Modificato</th>
                <th scope="col" aria-label="Azioni" />
              </tr>
            </thead>
            <tbody>
              {[0, 1, 2, 3, 4].map((i) => (
                <tr key={i} aria-hidden="true">
                  <td>
                    <span className="skeleton skeleton-line" style={{ width: `${68 - i * 7}%` }} />
                  </td>
                  <td>
                    <span className="skeleton skeleton-line" style={{ width: 44 }} />
                  </td>
                  <td>
                    <span className="skeleton skeleton-line" style={{ width: 72 }} />
                  </td>
                  <td>
                    <span className="skeleton skeleton-line" style={{ width: 30 }} />
                  </td>
                  <td>
                    <span className="skeleton skeleton-line" style={{ width: 24 }} />
                  </td>
                  <td>
                    <span className="skeleton skeleton-line" style={{ width: 52 }} />
                  </td>
                  <td>
                    <span className="skeleton skeleton-line" style={{ width: 64 }} />
                  </td>
                  <td />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : documents.length === 0 ? (
        <div className="empty-state card">
          <p>Nessun documento{statusFilter ? ' con questo stato' : ''}.</p>
          <p className="small">Carica un file qui sopra o chiedi a un amministratore di sincronizzare una fonte.</p>
        </div>
      ) : (
        <>
          <DocumentTable
            documents={documents}
            onDelete={setToDelete}
            sourceNames={
              ingest
                ? Object.fromEntries(ingest.per_source.map((s) => [s.source_id, s.name]))
                : undefined
            }
          />
          <Pagination
            offset={offset}
            limit={PAGE_SIZE}
            total={total}
            label="documenti"
            onOffsetChange={setOffset}
          />
        </>
      )}

      {toDelete && (
        <ConfirmModal
          title="Elimina documento"
          danger
          confirmLabel={deleting ? 'Eliminazione…' : 'Elimina'}
          onConfirm={() => void handleDelete()}
          onCancel={() => setToDelete(null)}
        >
          Eliminare definitivamente «{toDelete.title}»? Saranno rimossi anche i chunk indicizzati e
          il file originale.
        </ConfirmModal>
      )}
    </div>
  );
}
