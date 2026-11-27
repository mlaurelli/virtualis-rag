import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { api, ApiError } from '../../api/client';
import type { AdminUser, AdminUsersResponse, Role } from '../../api/types';

/** Pannello gestione utenti (solo admin). */
export default function UsersPanel() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newRole, setNewRole] = useState<Role>('user');
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get<AdminUsersResponse>('/admin/users');
      setUsers(res.users);
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
    setFormError(null);
    try {
      await api.post<AdminUser>('/admin/users', {
        username: newUsername.trim(),
        password: newPassword,
        role: newRole,
      });
      setNewUsername('');
      setNewPassword('');
      setNewRole('user');
      await load();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : 'Creazione fallita.');
    } finally {
      setCreating(false);
    }
  };

  const patchUser = async (id: number, body: Record<string, unknown>) => {
    try {
      await api.patch<AdminUser>(`/admin/users/${id}`, body);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Aggiornamento fallito.');
    }
  };

  if (loading) {
    return (
      <div className="empty-state card">
        <div className="spinner" role="status" aria-label="Caricamento utenti" />
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-5)' }}>
      <form className="card" onSubmit={handleCreate} aria-label="Crea nuovo utente">
        <h3>Nuovo utente</h3>
        <div style={{ display: 'flex', gap: 'var(--space-3)', flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div className="field" style={{ margin: 0, flex: 1, minWidth: 160 }}>
            <label htmlFor="new-username">Nome utente</label>
            <input
              id="new-username"
              className="input"
              required
              value={newUsername}
              onChange={(e) => setNewUsername(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="field" style={{ margin: 0, flex: 1, minWidth: 160 }}>
            <label htmlFor="new-password">Password</label>
            <input
              id="new-password"
              className="input"
              type="password"
              required
              minLength={8}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label htmlFor="new-role">Ruolo</label>
            <select
              id="new-role"
              className="select"
              value={newRole}
              onChange={(e) => setNewRole(e.target.value as Role)}
            >
              <option value="user">user</option>
              <option value="admin">admin</option>
            </select>
          </div>
          <button type="submit" className="btn btn-primary" disabled={creating}>
            {creating ? 'Creazione…' : 'Crea utente'}
          </button>
        </div>
        {formError && (
          <div className="alert" role="alert" style={{ marginTop: 'var(--space-3)' }}>
            {formError}
          </div>
        )}
      </form>

      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}

      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Utente</th>
              <th scope="col">Ruolo</th>
              <th scope="col">Stato</th>
              <th scope="col">Creato</th>
              <th scope="col" aria-label="Azioni" />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td>
                  <span style={{ fontWeight: 550 }}>{u.username}</span>{' '}
                  <span className="mono faint">#{u.id}</span>
                </td>
                <td>
                  <select
                    className="select"
                    style={{ width: 'auto', padding: 'var(--space-1) var(--space-2)' }}
                    aria-label={`Ruolo di ${u.username}`}
                    value={u.role}
                    onChange={(e) => void patchUser(u.id, { role: e.target.value })}
                  >
                    <option value="user">user</option>
                    <option value="admin">admin</option>
                  </select>
                </td>
                <td>
                  <span className={`badge ${u.is_active ? 'badge-indexed' : 'badge-neutral'}`}>
                    {u.is_active ? 'Attivo' : 'Disattivato'}
                  </span>
                </td>
                <td className="muted small">
                  {new Date(u.created_at).toLocaleDateString('it-IT')}
                </td>
                <td style={{ textAlign: 'right' }}>
                  <button
                    type="button"
                    className={u.is_active ? 'btn btn-danger btn-sm' : 'btn btn-sm'}
                    onClick={() => void patchUser(u.id, { is_active: !u.is_active })}
                  >
                    {u.is_active ? 'Disattiva' : 'Riattiva'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
