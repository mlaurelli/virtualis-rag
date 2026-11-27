import { useState } from 'react';
import { ApiError, submitFeedback } from '../api/client';
import type { FeedbackRating } from '../api/types';

interface MessageFeedbackProps {
  messageId: number;
}

/**
 * Feedback discreto su una risposta dell'assistente: pollice su/giù e,
 * per il giudizio negativo, un commento opzionale (invio con Invio o blur).
 * Il backend fa upsert: un secondo click sovrascrive il giudizio precedente.
 */
export default function MessageFeedback({ messageId }: MessageFeedbackProps) {
  const [rating, setRating] = useState<FeedbackRating | null>(null);
  const [comment, setComment] = useState('');
  const [commentSent, setCommentSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  const send = async (next: FeedbackRating, nextComment?: string) => {
    setSending(true);
    try {
      await submitFeedback({
        message_id: messageId,
        rating: next,
        ...(nextComment ? { comment: nextComment } : {}),
      });
      setRating(next);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Errore di connessione.');
    } finally {
      setSending(false);
    }
  };

  const handleRating = (next: FeedbackRating) => {
    setCommentSent(false);
    void send(next);
  };

  const sendComment = () => {
    const trimmed = comment.trim();
    if (!trimmed || commentSent) return;
    setCommentSent(true);
    void send(-1, trimmed);
  };

  return (
    <div className="feedback-bar">
      <button
        type="button"
        className={`feedback-btn${rating === 1 ? ' active positive' : ''}`}
        onClick={() => handleRating(1)}
        disabled={sending}
        aria-label="Risposta utile"
        aria-pressed={rating === 1}
        title="Risposta utile"
      >
        👍
      </button>
      <button
        type="button"
        className={`feedback-btn${rating === -1 ? ' active negative' : ''}`}
        onClick={() => handleRating(-1)}
        disabled={sending}
        aria-label="Risposta non utile"
        aria-pressed={rating === -1}
        title="Risposta non utile"
      >
        👎
      </button>

      {rating === -1 && (
        <input
          className="input feedback-comment"
          type="text"
          value={comment}
          placeholder="Cosa non va? (facoltativo)"
          aria-label="Commento sul feedback negativo"
          onChange={(e) => {
            setComment(e.target.value);
            setCommentSent(false);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') sendComment();
          }}
          onBlur={sendComment}
        />
      )}
      {commentSent && !error && <span className="feedback-ack">Grazie per la segnalazione.</span>}
      {error && (
        <span className="feedback-ack error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
