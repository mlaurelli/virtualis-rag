import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import { streamChat } from '../api/sse';
import type { Citation, ChatSessionDetail, ChatSessionsResponse, ChatSessionSummary } from '../api/types';
import ChatView from '../components/ChatView';
import CitationPanel from '../components/CitationPanel';
import type { LocalMessage } from '../components/MessageList';
import type { StepInfo } from '../components/StepTimeline';

let keyCounter = 0;
const nextKey = () => `m${++keyCounter}`;

/** Sessioni renderizzate inizialmente in sidebar (le altre con "Mostra altre"). */
const SESSIONS_PAGE = 100;

interface CitationPanelState {
  citations: Citation[];
  selected: number | null;
}

export default function ChatPage() {
  const { sessionId: sessionParam } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();

  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [visibleSessions, setVisibleSessions] = useState(SESSIONS_PAGE);
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [loadingSession, setLoadingSession] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [panel, setPanel] = useState<CitationPanelState | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const activeSessionRef = useRef<number | null>(null);

  const activeSessionId = sessionParam ? Number(sessionParam) : null;
  activeSessionRef.current = activeSessionId;

  const loadSessions = useCallback(async () => {
    try {
      const res = await api.get<ChatSessionsResponse>('/chat/sessions');
      setSessions(res.sessions);
      setSessionsError(null);
    } catch (err) {
      setSessionsError(err instanceof ApiError ? err.message : 'Errore di connessione');
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  // uscita dalla pagina chat: interrompe un eventuale stream ancora attivo
  useEffect(() => () => abortRef.current?.abort(), []);

  // carica lo storico quando cambia la sessione nella URL
  useEffect(() => {
    // Uno stream in corso appartiene alla sessione precedente: senza abort i
    // suoi token finirebbero in coda ai messaggi della sessione appena scelta.
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
      setStreaming(false);
    }
    if (activeSessionId === null) {
      setMessages([]);
      setPanel(null);
      return;
    }
    let cancelled = false;
    setLoadingSession(true);
    api
      .get<ChatSessionDetail>(`/chat/sessions/${activeSessionId}`)
      .then((detail) => {
        if (cancelled) return;
        setMessages(
          detail.messages.map((m) => ({
            key: `db-${m.id}`,
            id: m.id,
            role: m.role,
            content: m.content,
            citations: m.citations,
          })),
        );
        setPanel(null);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          navigate('/chat', { replace: true });
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingSession(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, navigate]);

  /** Aggiorna l'ultimo messaggio (quello in streaming) in modo immutabile. */
  const updateLast = useCallback((patch: (m: LocalMessage) => LocalMessage) => {
    setMessages((prev) => {
      if (prev.length === 0) return prev;
      const next = [...prev];
      next[next.length - 1] = patch(next[next.length - 1]);
      return next;
    });
  }, []);

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const handleSend = useCallback(
    (text: string) => {
      if (streaming) return;

      const controller = new AbortController();
      abortRef.current = controller;
      setStreaming(true);
      setPanel(null);

      setMessages((prev) => [
        ...prev,
        { key: nextKey(), role: 'user', content: text },
        {
          key: nextKey(),
          role: 'assistant',
          content: '',
          steps: [] as StepInfo[],
          streaming: true,
        },
      ]);

      const finish = () => {
        setStreaming(false);
        abortRef.current = null;
      };

      streamChat(
        { message: text, sessionId: activeSessionRef.current ?? undefined, signal: controller.signal },
        {
          onStep: (e) =>
            updateLast((m) => ({
              ...m,
              steps: [...(m.steps ?? []), { step: e.step, detail: e.detail, at: Date.now() }],
            })),
          onToken: (t) => updateLast((m) => ({ ...m, content: m.content + t })),
          // reflexion: la bozza è scartata, arrivano i token della risposta corretta
          onReset: () => updateLast((m) => ({ ...m, content: '' })),
          onCitations: (e) => updateLast((m) => ({ ...m, citations: e.citations })),
          onAnswer: (e) =>
            updateLast((m) => ({
              ...m,
              // il testo finale fa fede se lo streaming dei token non è arrivato
              content: m.content || e.text,
              warnings: e.warnings,
              confidence: e.confidence,
            })),
          onDone: (e) => {
            updateLast((m) => ({ ...m, streaming: false }));
            finish();
            // la nuova sessione prende il titolo dal primo messaggio (rinomina implicita)
            if (activeSessionRef.current === null) {
              navigate(`/chat/${e.session_id}`, { replace: true });
            }
            // L'evento `done` non espone l'id del messaggio assistant: lo si
            // recupera ricaricando la sessione (per le sessioni nuove ci pensa
            // già la navigate sopra, che ricarica lo storico dalla URL).
            void api
              .get<ChatSessionDetail>(`/chat/sessions/${e.session_id}`)
              .then((detail) => {
                const lastAssistant = [...detail.messages]
                  .reverse()
                  .find((m) => m.role === 'assistant');
                if (lastAssistant && activeSessionRef.current === e.session_id) {
                  const assistantId = lastAssistant.id;
                  updateLast((m) =>
                    m.role === 'assistant' && m.id === undefined
                      ? { ...m, id: assistantId }
                      : m,
                  );
                }
              })
              .catch(() => {
                // senza id il feedback resta nascosto fino al prossimo caricamento
              });
            void loadSessions();
          },
          onError: (message) => {
            updateLast((m) => ({
              ...m,
              streaming: false,
              content: m.content || 'Si è verificato un errore durante l\'elaborazione.',
              warnings: [...(m.warnings ?? []), message],
            }));
            finish();
          },
        },
      ).then(() => {
        // Se il server chiude lo stream senza evento `done` (connessione
        // interrotta), nessun handler ha chiamato finish(): senza questo
        // ramo l'input resterebbe bloccato in stato "streaming".
        updateLast((m) => ({ ...m, streaming: false }));
        finish();
      }).catch((err: unknown) => {
        if (controller.signal.aborted) {
          updateLast((m) => ({ ...m, streaming: false }));
        } else {
          updateLast((m) => ({
            ...m,
            streaming: false,
            warnings: [
              ...(m.warnings ?? []),
              err instanceof ApiError ? err.message : 'Errore di connessione al server.',
            ],
          }));
        }
        finish();
      });
    },
    [streaming, updateLast, navigate, loadSessions],
  );

  const handleDeleteSession = useCallback(
    async (id: number) => {
      try {
        await api.delete(`/chat/sessions/${id}`);
        if (activeSessionRef.current === id) {
          navigate('/chat', { replace: true });
        }
        void loadSessions();
      } catch {
        // sessione già rimossa o errore di rete: ricarica comunque l'elenco
        void loadSessions();
      }
    },
    [navigate, loadSessions],
  );

  const handleCitationClick = useCallback((citations: Citation[], n: number) => {
    setPanel({ citations, selected: n });
  }, []);

  return (
    <div className={`chat-layout${panel ? ' with-citations' : ''}`}>
      <aside className="chat-sidebar" aria-label="Cronologia conversazioni">
        <div className="sidebar-head">
          <button
            type="button"
            className="btn btn-primary"
            style={{ width: '100%' }}
            onClick={() => navigate('/chat')}
          >
            + Nuova conversazione
          </button>
        </div>
        <div className="session-list">
          {sessionsLoading && (
            <div aria-hidden="true">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="session-item skeleton-row">
                  <span className="skeleton skeleton-line" style={{ width: `${82 - i * 9}%` }} />
                </div>
              ))}
            </div>
          )}
          {sessionsError && (
            <div className="empty-state">
              <p className="small">{sessionsError}</p>
              <button type="button" className="btn btn-sm" onClick={() => void loadSessions()}>
                Riprova
              </button>
            </div>
          )}
          {!sessionsLoading && !sessionsError && sessions.length === 0 && (
            <div className="empty-state">
              <p className="small">Nessuna conversazione.</p>
            </div>
          )}
          {/* con migliaia di sessioni il render completo appesantisce la
              sidebar: si parte dalle prime 100, il resto su richiesta */}
          {sessions.slice(0, visibleSessions).map((s) => (
            <div
              key={s.id}
              className={`session-item${s.id === activeSessionId ? ' active' : ''}`}
              role="button"
              tabIndex={0}
              onClick={() => navigate(`/chat/${s.id}`)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') navigate(`/chat/${s.id}`);
              }}
            >
              <span className="session-title" title={s.title}>
                {s.title || 'Senza titolo'}
              </span>
              <button
                type="button"
                className="icon-btn danger"
                aria-label={`Elimina conversazione ${s.title}`}
                onClick={(e) => {
                  e.stopPropagation();
                  void handleDeleteSession(s.id);
                }}
              >
                ✕
              </button>
            </div>
          ))}
          {sessions.length > visibleSessions && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              style={{ width: '100%', marginTop: 'var(--space-2)' }}
              onClick={() => setVisibleSessions((v) => v + SESSIONS_PAGE)}
            >
              Mostra altre ({sessions.length - visibleSessions})
            </button>
          )}
        </div>
      </aside>

      {loadingSession ? (
        <div className="chat-main">
          <div className="message-scroll">
            <div
              className="message-column"
              role="status"
              aria-label="Caricamento conversazione"
            >
              <div className="message user">
                <span className="skeleton skeleton-bubble" style={{ width: '42%' }} />
              </div>
              <div className="message assistant">
                <span className="skeleton skeleton-bubble" style={{ width: '78%' }} />
              </div>
              <div className="message user">
                <span className="skeleton skeleton-bubble" style={{ width: '35%' }} />
              </div>
              <div className="message assistant">
                <span className="skeleton skeleton-bubble" style={{ width: '64%' }} />
              </div>
            </div>
          </div>
        </div>
      ) : (
        <ChatView
          messages={messages}
          streaming={streaming}
          onSend={handleSend}
          onStop={handleStop}
          onCitationClick={handleCitationClick}
        />
      )}

      {panel && (
        <CitationPanel
          citations={panel.citations}
          selected={panel.selected}
          onSelect={(n) => setPanel((p) => (p ? { ...p, selected: n } : p))}
          onClose={() => setPanel(null)}
        />
      )}
    </div>
  );
}
