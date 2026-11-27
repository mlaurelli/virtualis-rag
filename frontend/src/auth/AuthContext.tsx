/**
 * Provider di autenticazione: utente corrente, login/logout,
 * guard per route protette e per area admin.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import {
  api,
  clearSession,
  getStoredUserJson,
  getToken,
  setSession,
  setUnauthorizedHandler,
} from '../api/client';
import type { LoginResponse, User } from '../api/types';
import { AuthContext, useAuth } from './context';

function readStoredUser(): User | null {
  const raw = getStoredUserJson();
  if (!raw) return null;
  try {
    return JSON.parse(raw) as User;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(readStoredUser);
  const [initializing, setInitializing] = useState(true);

  const logout = useCallback(() => {
    clearSession();
    setUser(null);
  }, []);

  // registra il handler 401 del client API
  useEffect(() => {
    setUnauthorizedHandler(logout);
  }, [logout]);

  // all'avvio valida il token salvato con /auth/me
  useEffect(() => {
    let cancelled = false;
    async function check() {
      if (!getToken()) {
        setInitializing(false);
        return;
      }
      try {
        const me = await api.get<User>('/auth/me');
        if (!cancelled) setUser(me);
      } catch {
        // token non valido o backend irraggiungibile: il client ha già
        // gestito l'eventuale 401; in ogni caso riparti pulito
        if (!cancelled) {
          clearSession();
          setUser(null);
        }
      } finally {
        if (!cancelled) setInitializing(false);
      }
    }
    void check();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.postPublic<LoginResponse>('/auth/login', { username, password });
    setSession(res.access_token, JSON.stringify(res.user));
    setUser(res.user);
  }, []);

  const value = useMemo(
    () => ({ user, initializing, login, logout }),
    [user, initializing, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Guard: richiede autenticazione. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, initializing } = useAuth();
  const location = useLocation();

  if (initializing) {
    return (
      <div className="route-loading" role="status">
        Caricamento…
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  return <>{children}</>;
}

/** Guard: richiede ruolo admin. */
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { user, initializing } = useAuth();

  if (initializing) {
    return (
      <div className="route-loading" role="status">
        Caricamento…
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  if (user.role !== 'admin') {
    return <Navigate to="/chat" replace />;
  }
  return <>{children}</>;
}
