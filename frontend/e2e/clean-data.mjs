/**
 * Azzera il DATA_DIR del backend E2E (``e2e/.data``).
 *
 * Eseguito da ``npm run test:e2e`` PRIMA di ``playwright test``: in
 * Playwright il ``webServer`` (uvicorn) parte prima del ``globalSetup``,
 * quindi la pulizia non può vivere nel global-setup — cancellerebbe il DB
 * del server appena avviato. Qui invece il server non è ancora partito e
 * trova sempre una directory vuota: DB, indice vettoriale e admin iniziale
 * vengono ricreati da zero a ogni run.
 */
import { rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dataDir = path.join(path.dirname(fileURLToPath(import.meta.url)), '.data');
rmSync(dataDir, { recursive: true, force: true });
console.log('[e2e] DATA_DIR azzerato:', dataDir);
