import { createContext, useContext } from 'react';
import type { User } from '../api/types';

export interface AuthContextValue {
  user: User | null;
  /** true finché non è stato verificato il token salvato */
  initializing: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth deve essere usato dentro <AuthProvider>');
  }
  return ctx;
}
