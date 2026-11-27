import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api, ApiError } from '../../api/client';
import type { Source, SourcesResponse, SyncResponse } from '../../api/types';

/** Pannello gestione fonti documentali (solo admin). */
export default function SourcesPanel() {
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [name, setName] = useState('');
  const [path, setPath] = useState('');
  const [creating, setCreating] = useState(false);
  const [syncingId, setSyncingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get<SourcesResponse>('/admin/sources');
      setSources(res.sources);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Errore di connessione.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setError(null);
    setNotice(null);
    try {
      await api.post<Source>('/admin/sources', {
        name: name.trim(),
        type: 'local_fs',
        config: { path: path.trim() },
        enabled: true,
      });
      setName('');
      setPath('');
      setNotice('Fonte creata. Avvia una sincronizzazione per indicizzarne i documenti.');
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Creazione fallita.');
    } finally {
      setCreating(false);
    }
  };

  const toggleEnabled = async (s: Source) => {
    try {
      await api.patch<Source>(`/admin/sources/${s.id}`, { enabled: !s.enabled });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Aggiornamento fallito.');
    }
  };

  const triggerSync = async (s: Source) => {
    setSyncingId(s.id);
    setNotice(null);
    setError(null);
    try {
      const res = await api.post<SyncResponse>('/ingest/sync', { source_id: s.id });
      setNotice(
        res.started
          ? `Sincronizzazione avviata per «${s.name}».`
          : `Sincronizzazione non avviata per «${s.name}».`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Sincronizzazione fallita.');
    } finally {
      setSyncingId(null);
    }
  };

  if (loading) {
    return (
      <div className="empty-state card">
        <div className="spinner" role="status" aria-label="Caricamento fonti" />
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-5)' }}>
      <form className="card" onSubmit={handleCreate} aria-label="Aggiungi fonte locale">
        <h3>Aggiungi fonte (cartella locale)</h3>
        <div style={{ display: 'flex', gap: 'var(--space-3)', flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div className="field" style={{ margin: 0, flex: 1, minWidth: 160 }}>
            <label htmlFor="src-name">Nome</label>
            <input
              id="src-name"
              className="input"
              required
              placeholder="Es. Archivio locale"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="field" style={{ margin: 0, flex: 2, minWidth: 240 }}>
            <label htmlFor="src-path">Percorso sul server</label>
            <input
              id="src-path"
              className="input mono"
              required
              placeholder="/srv/documenti/archivio"
              value={path}
              onChange={(e) => setPath(e.target.value)}
            />
          </div>
          <button type="submit" className="btn btn-primary" disabled={creating}>
            {creating ? 'Creazione…' : 'Aggiungi fonte'}
          </button>
        </div>
      </form>

      {notice && (
        <div className="alert alert-success" role="status">
          {notice}
        </div>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}

      {sources.length === 0 ? (
        <div className="empty-state card">
          <p>Nessuna fonte configurata.</p>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Nome</th>
                <th scope="col">Tipo</th>
                <th scope="col">Configurazione</th>
                <th scope="col">Stato</th>
                <th scope="col">Ultima sync</th>
                <th scope="col" aria-label="Azioni" />
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id}>
                  <td style={{ fontWeight: 550 }}>{s.name}</td>
                  <td className="mono faint">{s.type}</td>
                  <td className="mono small muted">{JSON.stringify(s.config)}</td>
                  <td>
                    <span className={`badge ${s.enabled ? 'badge-indexed' : 'badge-neutral'}`}>
                      {s.enabled ? 'Abilitata' : 'Disabilitata'}
                    </span>
                  </td>
                  <td className="muted small">
                    {s.last_sync_at ? new Date(s.last_sync_at).toLocaleString('it-IT') : 'mai'}
                  </td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => void toggleEnabled(s)}
                    >
                      {s.enabled ? 'Disabilita' : 'Abilita'}
                    </button>{' '}
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      disabled={!s.enabled || syncingId === s.id}
                      onClick={() => void triggerSync(s)}
                    >
                      {syncingId === s.id ? 'Avvio…' : 'Sincronizza'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
