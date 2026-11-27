/**
 * Wrapper fetch centralizzato: baseURL /api, injection Bearer token,
 * gestione 401 (logout + redirect login) e errori {detail} tipizzati.
 */

import type {
  ApiErrorBody,
  FeedbackEntry,
  FeedbackListResponse,
  FeedbackRating,
  FeedbackRequest,
  FeedbackStats,
} from './types';

const TOKEN_KEY = 'virtualis_token';
const USER_KEY = 'virtualis_user';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setSession(token: string, userJson: string): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, userJson);
}

export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function getStoredUserJson(): string | null {
  return localStorage.getItem(USER_KEY);
}

/** Errore API con status code e messaggio dal campo `detail` del backend. */
export class ApiError extends Error {
  status: number;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Callback invocata su 401: l'AuthContext la registra per fare logout. */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

async function parseError(res: Response): Promise<ApiError> {
  let detail = `Errore ${res.status}`;
  try {
    const body = (await res.json()) as ApiErrorBody;
    if (typeof body.detail === 'string') {
      detail = body.detail;
    }
  } catch {
    // corpo non JSON: mantieni il messaggio generico
  }
  return new ApiError(res.status, detail);
}

function handleUnauthorized(): void {
  clearSession();
  if (onUnauthorized) {
    onUnauthorized();
  } else if (window.location.pathname !== '/login') {
    window.location.assign('/login');
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** true per non inviare il token (es. login) */
  skipAuth?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipAuth = false } = options;

  const headers: Record<string, string> = {};
  const token = getToken();
  if (token && !skipAuth) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401 && !skipAuth) {
    handleUnauthorized();
    throw await parseError(res);
  }
  if (!res.ok) {
    throw await parseError(res);
  }
  // 204 No Content
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

/** Upload multipart (il Content-Type con boundary lo imposta il browser). */
export async function uploadFile<T>(path: string, formData: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const res = await fetch(`/api${path}`, { method: 'POST', headers, body: formData });

  if (res.status === 401) {
    handleUnauthorized();
    throw await parseError(res);
  }
  if (!res.ok) {
    throw await parseError(res);
  }
  return (await res.json()) as T;
}

/**
 * Scarica una risorsa binaria protetta (file originale) come Blob autenticato.
 * Usata dal viewer PDF in-app e da openProtectedFile.
 */
export async function fetchProtectedBlob(path: string): Promise<Blob> {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  const res = await fetch(`/api${path}`, { headers });
  if (res.status === 401) {
    handleUnauthorized();
    throw await parseError(res);
  }
  if (!res.ok) {
    throw await parseError(res);
  }
  return res.blob();
}

/**
 * URL autenticato per risorse binarie (file originali): il backend richiede
 * il Bearer token, quindi il file viene scaricato e aperto come object URL.
 */
export async function openProtectedFile(path: string, fragment = ''): Promise<void> {
  const blob = await fetchProtectedBlob(path);
  const url = URL.createObjectURL(blob);
  window.open(`${url}${fragment}`, '_blank', 'noopener');
  // revoca ritardata: il viewer del browser deve prima caricare l'URL
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  /** login senza token preesistente */
  postPublic: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body, skipAuth: true }),
};

// ---------- Feedback ----------

/** Invia (o sovrascrive) il feedback su una risposta dell'assistente. */
export function submitFeedback(body: FeedbackRequest): Promise<FeedbackEntry> {
  return api.post<FeedbackEntry>('/feedback', body);
}

/** Admin: elenco feedback, opzionalmente filtrato per giudizio. */
export function getAdminFeedback(
  rating?: FeedbackRating,
  limit = 100,
): Promise<FeedbackListResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (rating !== undefined) {
    params.set('rating', String(rating));
  }
  return api.get<FeedbackListResponse>(`/admin/feedback?${params.toString()}`);
}

/** Admin: statistiche aggregate sui feedback. */
export function getAdminFeedbackStats(): Promise<FeedbackStats> {
  return api.get<FeedbackStats>('/admin/feedback/stats');
}
