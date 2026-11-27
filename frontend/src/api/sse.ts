/**
 * Helper per consumare lo stream SSE di POST /api/chat.
 * Si usa fetch + ReadableStream (non EventSource) perché serve una POST
 * con header Authorization. Ogni evento è una riga `data: {json}`.
 */

import { ApiError, clearSession, getToken } from './client';
import type { ChatStreamEvent } from './types';

export interface ChatStreamHandlers {
  onStep?: (step: Extract<ChatStreamEvent, { type: 'step' }>) => void;
  onToken?: (text: string) => void;
  /** la bozza streamata va scartata: arriva la risposta corretta (reflexion) */
  onReset?: () => void;
  onCitations?: (event: Extract<ChatStreamEvent, { type: 'citations' }>) => void;
  onAnswer?: (event: Extract<ChatStreamEvent, { type: 'answer' }>) => void;
  onDone?: (event: Extract<ChatStreamEvent, { type: 'done' }>) => void;
  onError?: (message: string) => void;
}

interface StreamChatOptions {
  message: string;
  sessionId?: number;
  signal?: AbortSignal;
}

/**
 * Invia il messaggio e processa gli eventi SSE fino a `done`/`error`.
 * Lancia ApiError per errori HTTP; gli errori nello stream arrivano via onError.
 */
export async function streamChat(
  options: StreamChatOptions,
  handlers: ChatStreamHandlers,
): Promise<void> {
  const token = getToken();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const res = await fetch('/api/chat', {
    method: 'POST',
    headers,
    body: JSON.stringify(
      options.sessionId !== undefined
        ? { message: options.message, session_id: options.sessionId }
        : { message: options.message },
    ),
    signal: options.signal,
  });

  if (!res.ok) {
    let detail = `Errore ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      // corpo non JSON
    }
    if (res.status === 401) {
      clearSession();
      window.location.assign('/login');
    }
    throw new ApiError(res.status, detail);
  }

  if (!res.body) {
    throw new ApiError(500, 'Stream non disponibile');
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const dispatch = (raw: string): void => {
    let event: ChatStreamEvent;
    try {
      event = JSON.parse(raw) as ChatStreamEvent;
    } catch {
      return; // riga non valida: ignora
    }
    switch (event.type) {
      case 'step':
        handlers.onStep?.(event);
        break;
      case 'token':
        handlers.onToken?.(event.text);
        break;
      case 'reset':
        handlers.onReset?.();
        break;
      case 'citations':
        handlers.onCitations?.(event);
        break;
      case 'answer':
        handlers.onAnswer?.(event);
        break;
      case 'done':
        handlers.onDone?.(event);
        break;
      case 'error':
        handlers.onError?.(event.message);
        break;
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // gli eventi SSE sono separati da righe vuote; ogni evento è `data: ...`
    let idx: number;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of block.split('\n')) {
        if (line.startsWith('data:')) {
          dispatch(line.slice(5).trim());
        }
      }
    }
  }

  // eventuale blocco residuo a fine stream
  const rest = buffer.trim();
  if (rest.startsWith('data:')) {
    dispatch(rest.slice(5).trim());
  }
}
