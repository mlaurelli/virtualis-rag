/**
 * Fixture Playwright della suite E2E.
 *
 * Fixture worker-scoped ``seed``: una volta per run prepara i dati di test
 * sul backend reale (porta 8033, DATA_DIR isolato ricreato a ogni run):
 *
 * 1. login API come admin (creato dal bootstrap ADMIN_AUTO_CREATE);
 * 2. upload di 2 documenti markdown tramite la pipeline reale
 *    (``POST /api/ingest/upload``) e attesa dello stato ``indexed`` (polling);
 * 3. una sessione chat con domanda/risposta + citazioni, inserita direttamente
 *    nel DB SQLite (``seed_chat.py``) perché la risposta dell'agente
 *    richiederebbe il LLM: la suite non dipende da servizi esterni.
 *
 * Helper ``loginViaApi``: autentica la pagina via localStorage (stesso
 * meccanismo del login UI) per i test che non verificano il form di login.
 */
import {
  test as base,
  request,
  type APIRequestContext,
  type Page,
} from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import {
  ADMIN_PASSWORD,
  ADMIN_USERNAME,
  BACKEND_PYTHON,
  BASE_URL,
  DATA_DIR,
  E2E_DIR,
} from './env';

export interface SeedDocument {
  id: number;
  title: string;
}

export interface SeedData {
  sessionId: number;
  question: string;
  documents: SeedDocument[];
  /** Termine distintivo presente solo nel primo documento. */
  searchTerm: string;
}

const POLL_INTERVAL_MS = 2_000;
/** Prima esecuzione: i modelli di embedding (già in cache HF) vanno caricati. */
const INDEX_TIMEOUT_MS = 300_000;

const SEARCH_TERM = 'termocoppia';

const DOC_MANUALE = {
  filename: 'manuale-linea-b.md',
  content: `# Manuale di manutenzione — Linea B

## Sostituzione filtri

I filtri della linea B vanno sostituiti ogni 2.000 ore di esercizio.
La ${SEARCH_TERM} del bruciatore principale va verificata a ogni sostituzione
e sostituita se la lettura supera 40 mV.

## Materiali

- Filtro cartuccia FB-200 (codice ricambio 88-FB200)
- Guarnizione termica GT-7
`,
};

const DOC_POLIZZA = {
  filename: 'polizza-garanzia-2026.md',
  content: `# Polizza garanzia 2026

## Clausole di garanzia

La garanzia convenzionale copre 24 mesi dalla messa in servizio.
Sono escluse le parti soggette a usura ordinaria.

## Interventi

Gli interventi on-site sono garantiti entro 48 ore lavorative dalla chiamata.
`,
};

const QUESTION = 'Ogni quanto vanno sostituiti i filtri della linea B?';
const ANSWER = `I filtri della linea B vanno sostituiti **ogni 2.000 ore di esercizio** [1].

Punti principali:

- A ogni sostituzione va verificata anche la **${SEARCH_TERM}** del bruciatore [1]
- Il ricambio è il filtro cartuccia FB-200
`;

async function loginToken(ctx: APIRequestContext): Promise<string> {
  const res = await ctx.post('/api/auth/login', {
    data: { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
  });
  if (!res.ok()) {
    throw new Error(`login E2E fallito: HTTP ${res.status()} — ${await res.text()}`);
  }
  const body = (await res.json()) as { access_token: string };
  return body.access_token;
}

async function uploadDocument(
  ctx: APIRequestContext,
  token: string,
  filename: string,
  content: string,
): Promise<SeedDocument> {
  const res = await ctx.post('/api/ingest/upload', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: filename,
        mimeType: 'text/markdown',
        buffer: Buffer.from(content, 'utf-8'),
      },
    },
  });
  if (!res.ok()) {
    throw new Error(`upload ${filename} fallito: HTTP ${res.status()} — ${await res.text()}`);
  }
  const body = (await res.json()) as { document_id: number };
  // il titolo del documento è lo stem del nome file (vedi connectors/upload.py)
  return { id: body.document_id, title: filename.replace(/\.md$/, '') };
}

async function waitIndexed(ctx: APIRequestContext, token: string, docs: SeedDocument[]): Promise<void> {
  const pending = new Map(docs.map((d) => [d.id, d.title]));
  const deadline = Date.now() + INDEX_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const res = await ctx.get('/api/documents?limit=200', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok()) {
      throw new Error(`lettura documenti fallita: HTTP ${res.status()}`);
    }
    const body = (await res.json()) as {
      documents: { id: number; status: string; error: string | null }[];
    };
    for (const doc of body.documents) {
      if (!pending.has(doc.id)) continue;
      if (doc.status === 'failed') {
        throw new Error(`indicizzazione fallita per «${pending.get(doc.id)}»: ${doc.error}`);
      }
      if (doc.status === 'indexed') {
        pending.delete(doc.id);
      }
    }
    if (pending.size === 0) return;
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
  throw new Error(
    `timeout (${INDEX_TIMEOUT_MS / 1000}s) in attesa dell'indicizzazione di: ${[...pending.values()].join(', ')}`,
  );
}

function seedChatSession(manuale: SeedDocument): number {
  const payload = {
    username: ADMIN_USERNAME,
    title: 'Sessione E2E citazioni',
    question: QUESTION,
    answer: ANSWER,
    citations: [
      {
        n: 1,
        document_id: manuale.id,
        title: manuale.title,
        page: null,
        section_path: ['Sostituzione filtri'],
        snippet: 'I filtri della linea B vanno sostituiti ogni 2.000 ore di esercizio.',
        source_name: 'upload',
        bboxes: [],
      },
    ],
  };
  const out = execFileSync(
    BACKEND_PYTHON,
    [
      path.join(E2E_DIR, 'seed_chat.py'),
      path.join(DATA_DIR, 'virtualis.db'),
      JSON.stringify(payload),
    ],
    { encoding: 'utf-8' },
  );
  return Number(out.trim());
}

/** Autentica la pagina come admin senza passare dal form (stesso storage del login UI). */
export async function loginViaApi(page: Page): Promise<void> {
  const res = await page.request.post('/api/auth/login', {
    data: { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
  });
  if (!res.ok()) {
    throw new Error(`login E2E fallito: HTTP ${res.status()} — ${await res.text()}`);
  }
  const body = (await res.json()) as { access_token: string; user: unknown };
  await page.addInitScript(
    ([token, user]) => {
      localStorage.setItem('virtualis_token', token);
      localStorage.setItem('virtualis_user', user);
    },
    [body.access_token, JSON.stringify(body.user)] as const,
  );
}

export const test = base.extend<object, { seed: SeedData }>({
  seed: [
    // oxlint-disable-next-line no-empty-pattern -- firma richiesta da Playwright: il primo argomento non serve
    async ({}, use) => {
      const ctx = await request.newContext({ baseURL: BASE_URL });
      const token = await loginToken(ctx);
      const manuale = await uploadDocument(ctx, token, DOC_MANUALE.filename, DOC_MANUALE.content);
      const polizza = await uploadDocument(ctx, token, DOC_POLIZZA.filename, DOC_POLIZZA.content);
      await waitIndexed(ctx, token, [manuale, polizza]);
      const sessionId = seedChatSession(manuale);
      await use({
        sessionId,
        question: QUESTION,
        documents: [manuale, polizza],
        searchTerm: SEARCH_TERM,
      });
      await ctx.dispose();
    },
    { scope: 'worker', timeout: INDEX_TIMEOUT_MS + 60_000 },
  ],
});

export { expect } from '@playwright/test';
