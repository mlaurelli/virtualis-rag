import { useRef, useState, type DragEvent } from 'react';
import { uploadFile, ApiError } from '../api/client';
import type { UploadResponse } from '../api/types';

interface UploadZoneProps {
  /** chiamata dopo ogni upload accettato (202) */
  onUploaded: () => void;
}

/** Zona drag&drop per il caricamento di documenti (POST /ingest/upload). */
export default function UploadZone({ onUploaded }: UploadZoneProps) {
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [isError, setIsError] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const upload = async (file: File) => {
    setBusy(true);
    setMessage(null);
    setIsError(false);
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await uploadFile<UploadResponse>('/ingest/upload', form);
      setMessage(`«${file.name}» caricato (documento #${res.document_id}), indicizzazione avviata.`);
      onUploaded();
    } catch (err) {
      setIsError(true);
      setMessage(
        err instanceof ApiError ? `Caricamento fallito: ${err.message}` : 'Caricamento fallito.',
      );
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file && !busy) void upload(file);
  };

  return (
    <div>
      <div
        className={`upload-zone${dragging ? ' dragging' : ''}`}
        role="button"
        tabIndex={0}
        aria-label="Carica un documento"
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === 'Enter') inputRef.current?.click();
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <input
          ref={inputRef}
          type="file"
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void upload(file);
          }}
        />
        {busy ? (
          <span className="upload-progress" style={{ justifyContent: 'center', margin: 0 }}>
            <span className="spinner" aria-hidden="true" /> Caricamento in corso…
          </span>
        ) : (
          <>
            <p style={{ margin: 0, fontWeight: 550 }}>
              Trascina qui un file oppure clicca per selezionarlo
            </p>
            <p className="small faint" style={{ margin: 'var(--space-1) 0 0' }}>
              Il documento sarà indicizzato automaticamente.
            </p>
          </>
        )}
      </div>
      {message && (
        <div className={isError ? 'alert' : 'alert alert-success'} role="status">
          {message}
        </div>
      )}
    </div>
  );
}
