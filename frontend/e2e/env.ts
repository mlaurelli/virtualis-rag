/**
 * Costanti condivise dalla suite E2E (config Playwright, global-setup, fixture).
 *
 * Il backend di test gira su porta 8033 con un DATA_DIR dedicato
 * (``e2e/.data``), azzerato dallo step ``clean-data.mjs`` di
 * ``npm run test:e2e``: i test partono sempre da uno stato noto e non
 * toccano i dati di sviluppo.
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

export const E2E_DIR = here;
export const FRONTEND_DIR = path.resolve(here, '..');
export const REPO_ROOT = path.resolve(FRONTEND_DIR, '..');
export const BACKEND_DIR = path.join(REPO_ROOT, 'backend');
/** Interprete del venv backend (creato da `make setup` / `uv sync`). */
export const BACKEND_PYTHON = path.join(BACKEND_DIR, '.venv', 'bin', 'python');

/** DATA_DIR isolato del backend E2E (cancellato e ricreato a ogni run). */
export const DATA_DIR = path.join(here, '.data');

export const PORT = 8033;
export const BASE_URL = `http://localhost:${PORT}`;
/** URL per l'healthcheck del webServer: 127.0.0.1 evita ambiguità localhost/::1. */
export const HEALTH_URL = `http://127.0.0.1:${PORT}/api/health`;

export const ADMIN_USERNAME = 'admin';
export const ADMIN_PASSWORD = 'admin12345';
