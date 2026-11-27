import { useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { ApiError } from '../api/client';
import { useAuth } from '../auth/context';
import { useTheme } from '../theme/context';

export default function LoginPage() {
  const { user, initializing, login } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // già autenticato: vai alla pagina richiesta o alla chat
  if (!initializing && user) {
    const from = (location.state as { from?: string } | null)?.from ?? '/chat';
    return <Navigate to={from} replace />;
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username.trim(), password);
      const from = (location.state as { from?: string } | null)?.from ?? '/chat';
      navigate(from, { replace: true });
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setError(err.status === 403 ? err.message : 'Credenziali non valide.');
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
            <p className="muted small">Gestione documentale agentica</p>
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
            <label htmlFor="password">Password</label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          {error && (
            <div className="alert" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
              {error}
            </div>
          )}

          <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={submitting}>
            {submitting ? 'Accesso in corso…' : 'Accedi'}
          </button>
        </form>
      </div>
    </div>
  );
}
