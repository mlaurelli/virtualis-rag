import { useEffect, useState, type ReactNode } from 'react';
import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { api } from './api/client';
import type { SetupStatus } from './api/types';
import { RequireAdmin, RequireAuth } from './auth/AuthContext';
import { useAuth } from './auth/context';
import ErrorBoundary from './components/ErrorBoundary';
import Layout from './components/Layout';
import AdminPage from './pages/AdminPage';
import ChatPage from './pages/ChatPage';
import DocumentsPage from './pages/DocumentsPage';
import KgPage from './pages/KgPage';
import LoginPage from './pages/LoginPage';
import M365Page from './pages/M365Page';
import SearchPage from './pages/SearchPage';
import SetupPage from './pages/SetupPage';

// Lazy: il viewer PDF include pdfjs-dist (pesante) e resta fuori dal bundle principale
const ViewerPage = lazy(() => import('./pages/ViewerPage'));

type SetupState = 'loading' | 'needed' | 'done';

/**
 * Gate del wizard di primo avvio: interroga GET /api/setup/status e, finché
 * non esiste alcun utente, dirotta tutto su /setup. Un utente autenticato
 * conta come setup completato (login automatico subito dopo la creazione).
 * Se il backend non risponde non blocca la UI (stato 'done').
 */
function SetupGate({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const location = useLocation();
  const [state, setState] = useState<SetupState>('loading');

  useEffect(() => {
    let cancelled = false;
    api
      .get<SetupStatus>('/setup/status')
      .then((s) => {
        if (!cancelled) setState(s.needs_setup ? 'needed' : 'done');
      })
      .catch(() => {
        if (!cancelled) setState('done');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state === 'loading') {
    return (
      <div className="route-loading" role="status">
        Caricamento…
      </div>
    );
  }
  const onSetup = location.pathname === '/setup';
  if (state === 'needed' && !user) {
    return onSetup ? <>{children}</> : <Navigate to="/setup" replace />;
  }
  if (onSetup) {
    return <Navigate to={user ? '/chat' : '/login'} replace />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    // boundary globale: un errore di rendering mostra un messaggio con
    // pulsante "Ricarica" invece di lasciare la pagina bianca
    <ErrorBoundary>
      <SetupGate>
        <Routes>
          <Route path="/setup" element={<SetupPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/chat/:sessionId" element={<ChatPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/documents" element={<DocumentsPage />} />
            <Route path="/m365" element={<M365Page />} />
            <Route path="/knowledge" element={<KgPage />} />
            <Route
              path="/viewer/:documentId"
              element={
                <Suspense
                  fallback={
                    <div className="route-loading" role="status">
                      Caricamento…
                    </div>
                  }
                >
                  <ViewerPage />
                </Suspense>
              }
            />
            <Route
              path="/admin"
              element={
                <RequireAdmin>
                  <AdminPage />
                </RequireAdmin>
              }
            />
          </Route>
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </SetupGate>
    </ErrorBoundary>
  );
}
