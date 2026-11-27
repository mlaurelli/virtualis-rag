import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { AuditResponse } from '../api/types';
import AuditTable from '../components/AuditTable';
import Pagination from '../components/Pagination';
import FeedbackPanel from '../components/admin/FeedbackPanel';
import SourcesPanel from '../components/admin/SourcesPanel';
import UsersPanel from '../components/admin/UsersPanel';

type AdminTab = 'users' | 'sources' | 'audit' | 'feedback';

const TABS: { id: AdminTab; label: string }[] = [
  { id: 'users', label: 'Utenti' },
  { id: 'sources', label: 'Fonti' },
  { id: 'audit', label: 'Registro audit' },
  { id: 'feedback', label: 'Feedback' },
];

export default function AdminPage() {
  const [tab, setTab] = useState<AdminTab>('users');

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Amministrazione</h1>
          <p className="subtitle">Gestione utenti, fonti documentali, registro audit e feedback.</p>
        </div>
      </div>

      <div className="admin-tabs" role="tablist" aria-label="Sezioni amministrazione">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`admin-tab${tab === t.id ? ' active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'users' && <UsersPanel />}
      {tab === 'sources' && <SourcesPanel />}
      {tab === 'audit' && <AuditPanel />}
      {tab === 'feedback' && <FeedbackPanel />}
    </div>
  );
}

const AUDIT_PAGE_SIZE = 50;

function AuditPanel() {
  const [entries, setEntries] = useState<AuditResponse['entries']>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (off: number) => {
    try {
      const res = await api.get<AuditResponse>(
        `/admin/audit?limit=${AUDIT_PAGE_SIZE}&offset=${off}`,
      );
      setEntries(res.entries);
      setTotal(res.total);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Errore di connessione.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void load(offset);
  }, [offset, load]);

  if (loading) {
    return (
      <div className="empty-state card">
        <div className="spinner" role="status" aria-label="Caricamento registro audit" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="alert" role="alert">
        {error}{' '}
        <button type="button" className="btn btn-sm" onClick={() => void load(offset)}>
          Riprova
        </button>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="empty-state card">
        <p>Nessuna interrogazione registrata.</p>
      </div>
    );
  }

  return (
    <>
      <AuditTable entries={entries} />
      <Pagination
        offset={offset}
        limit={AUDIT_PAGE_SIZE}
        total={total}
        label="voci"
        onOffsetChange={setOffset}
      />
    </>
  );
}
