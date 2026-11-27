import { useRef, useState, type KeyboardEvent } from 'react';
import type { Citation } from '../api/types';
import MessageList, { type LocalMessage } from './MessageList';

interface ChatViewProps {
  messages: LocalMessage[];
  streaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onCitationClick: (citations: Citation[], n: number) => void;
}

/** Suggerimenti iniziali (UI statica) coerenti col dominio documentale. */
const SUGGESTIONS: readonly string[] = [
  'Cosa prevedono le clausole di garanzia nei contratti attivi?',
  'Confronta le due versioni più recenti del documento e evidenzia le differenze.',
  'Cerca il codice delibera e mostrami data, oggetto e documento di origine.',
];

/** Area centrale della chat: lista messaggi + barra di input. */
export default function ChatView({ messages, streaming, onSend, onStop, onCitationClick }: ChatViewProps) {
  const [draft, setDraft] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const text = draft.trim();
    if (!text || streaming) return;
    onSend(text);
    setDraft('');
    textareaRef.current?.focus();
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="chat-main">
      {messages.length === 0 ? (
        <div className="message-scroll">
          <div className="empty-state chat-welcome" style={{ height: '100%' }}>
            <h2>Come posso aiutarti?</h2>
            <p className="muted">
              Fai una domanda sui documenti aziendali: l'agente cercherà nelle fonti
              indicizzate e risponderà con citazioni verificabili.
            </p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="suggestion-chip"
                  onClick={() => onSend(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <MessageList messages={messages} onCitationClick={onCitationClick} />
      )}

      <div className="chat-input-bar">
        <div className="chat-input-inner">
          <textarea
            ref={textareaRef}
            className="textarea"
            rows={1}
            placeholder="Scrivi una domanda… (Invio per inviare, Maiusc+Invio per andare a capo)"
            aria-label="Messaggio"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          {streaming ? (
            <button type="button" className="btn btn-danger" onClick={onStop}>
              Interrompi
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary"
              onClick={submit}
              disabled={!draft.trim()}
            >
              Invia
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
