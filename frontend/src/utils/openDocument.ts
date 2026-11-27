/**
 * Utility per aprire i file originali dei documenti in una nuova scheda.
 * Per i PDF aggiunge il fragment #page=N per saltare alla pagina citata.
 */

import { openProtectedFile } from '../api/client';

export function openDocument(documentId: number, page: number | null, docType?: string): void {
  const isPdf = docType ? docType.toLowerCase() === 'pdf' : true;
  const fragment = page !== null && isPdf ? `#page=${page}` : '';
  void openProtectedFile(`/documents/${documentId}/file`, fragment).catch(() => {
    // errore già gestito dal client (401) o silenzioso: il viewer non si apre
  });
}
