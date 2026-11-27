import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import type { LoginResponse } from '../api/types';
import { useAuth } from '../auth/context';
import { useTheme } from '../theme/context';

/**
 * Wizard di primo avvio: creazione del primo amministratore.
 * Mostrata solo finché GET /api/setup/status riporta needs_setup=true
 * (il gate è in App.tsx). Dopo la creazione esegue il login automatico.
 */
export default function SetupPage() {
  const { user, initializing, login } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // già autenticato: niente wizard
  if (!initializing && user) {
    return <Navigate to="/chat" replace />;
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    const cleanUsername = username.trim();
    if (!cleanUsername) {
      setError('Inserisci un nome utente.');
      return;
    }
    if (password.length < 8) {
      setError('La password deve avere almeno 8 caratteri.');
      return;
    }
    if (password !== confirm) {
      setError('Le due password non coincidono.');
      return;
    }
    setSubmitting(true);
    try {
      await api.postPublic<LoginResponse>('/setup', {
        username: cleanUsername,
        password,
      });
      // Login automatico con le credenziali appena create
      await login(cleanUsername, password);
      navigate('/chat', { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError('La configurazione iniziale è già stata completata. Accedi dalla pagina di login.');
      } else if (err instanceof ApiError && err.status === 422) {
        setError('Dati non validi: la password deve avere almeno 8 caratteri.');
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError('Impossibile contattare il server. Riprova più tardi.');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="card login-card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div className="brand">
              Virtualis<span>.</span>
            </div>
            <p className="muted small">Configurazione iniziale</p>
          </div>
          <button
            type="button"
            className="icon-btn"
            onClick={toggleTheme}
            aria-label={theme === 'dark' ? 'Passa al tema chiaro' : 'Passa al tema scuro'}
          >
            {theme === 'dark' ? '☀' : '☾'}
          </button>
        </div>

        <p className="muted small" style={{ marginBottom: 'var(--space-4)' }}>
          Nessun utente presente: crea il primo amministratore per iniziare.
        </p>

        <form onSubmit={handleSubmit} noValidate={false}>
          <div className="field">
            <label htmlFor="username">Nome utente</label>
            <input
              id="username"
              className="input"
              type="text"
              autoComplete="username"
              autoFocus
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password (minimo 8 caratteri)</label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="confirm">Conferma password</label>
            <input
              id="confirm"
              className="input"
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </div>

          {error && (
            <div className="alert" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
              {error}
            </div>
          )}

          <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={submitting}>
            {submitting ? 'Creazione in corso…' : 'Crea amministratore'}
          </button>
        </form>
      </div>
    </div>
  );
}
