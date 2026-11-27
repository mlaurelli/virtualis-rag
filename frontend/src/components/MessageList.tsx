import { memo, useEffect, useRef } from 'react';
import type { Citation } from '../api/types';
import MarkdownMessage from './MarkdownMessage';
import MessageFeedback from './MessageFeedback';
import StepTimeline, { type StepInfo } from './StepTimeline';

/** Rappresentazione locale di un messaggio (persistito o in streaming). */
export interface LocalMessage {
  key: string;
  /** id DB del messaggio persistito: necessario per il feedback */
  id?: number;
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  warnings?: string[];
  /** affidabilità stimata dalla critic avversariale (badge nella risposta) */
  confidence?: 'alta' | 'media' | 'bassa';
  steps?: StepInfo[];
  streaming?: boolean;
}

interface MessageListProps {
  messages: LocalMessage[];
  onCitationClick: (citations: Citation[], n: number) => void;
}

/** Distanza dal fondo (px) entro cui l'auto-scroll segue lo streaming. */
const NEAR_BOTTOM_PX = 120;

interface MessageItemProps {
  message: LocalMessage;
  onCitationClick: (citations: Citation[], n: number) => void;
}

/**
 * Singolo messaggio, memoizzato: durante lo streaming solo l'ultimo messaggio
 * cambia riferimento, gli altri non vengono ri-renderizzati (il re-parse
 * markdown dell'intera cronologia a ogni token diventa costoso su risposte
 * lunghe). Il confronto shallow di memo basta perché ChatPage aggiorna i
 * messaggi in modo immutabile e onCitationClick è stabile (useCallback).
 */
const MessageItem = memo(function MessageItem({ message: m, onCitationClick }: MessageItemProps) {
  return (
    <div className={`message ${m.role}`}>
      <span className="message-role">{m.role === 'user' ? 'Tu' : 'Assistente'}</span>

      {m.steps && m.steps.length > 0 && (
        <StepTimeline steps={m.steps} active={m.streaming} />
      )}

      <div className={`message-body${m.streaming ? ' stream-cursor' : ''}`}>
        {m.role === 'assistant' ? (
          <MarkdownMessage
            content={m.content}
            citations={m.citations}
            onCitationClick={onCitationClick}
          />
        ) : (
          m.content
        )}
      </div>

      {m.confidence && (
        <span
          className={`confidence-badge confidence-${m.confidence}`}
          title="Affidabilità stimata dalla verifica critica della risposta"
        >
          Affidabilità: {m.confidence}
        </span>
      )}

      {m.warnings && m.warnings.length > 0 && (
        <div className="message-warnings" role="note">
          {m.warnings.map((w, i) => (
            <div key={i} className="alert alert-warning">
              {w}
            </div>
          ))}
        </div>
      )}

      {m.role === 'assistant' && !m.streaming && m.id !== undefined && (
        <MessageFeedback messageId={m.id} />
      )}
    </div>
  );
});

export default function MessageList({ messages, onCitationClick }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);

  // Auto-scroll: salto immediato (niente `smooth`, che accumula animazioni a
  // ogni token). Con un nuovo messaggio (invio o apertura sessione) si va
  // sempre in fondo; durante lo streaming solo se l'utente è già vicino al
  // fondo, così può risalire a leggere senza essere trascinato giù.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const newMessage = messages.length !== prevCountRef.current;
    prevCountRef.current = messages.length;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    if (newMessage || nearBottom) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  return (
    <div className="message-scroll" ref={scrollRef}>
      <div className="message-column">
        {messages.map((m) => (
          <MessageItem key={m.key} message={m} onCitationClick={onCitationClick} />
        ))}
      </div>
    </div>
  );
}
