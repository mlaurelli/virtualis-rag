/**
 * Config Playwright della suite E2E Virtualis.
 *
 * Un solo browser (Chromium), un solo worker: i test condividono un unico
 * backend reale con DB SQLite e indice Qdrant embedded, quindi l'esecuzione
 * è serializzata per determinismo.
 *
 * Il ``webServer`` avvia uvicorn su porta 8033 con env dedicata: DATA_DIR in
 * ``e2e/.data`` (azzerato dallo step ``node e2e/clean-data.mjs`` di
 * ``npm run test:e2e`` prima dell'avvio di Playwright: il webServer parte
 * prima di un eventuale globalSetup, quindi la pulizia non può stare lì),
 * admin auto-creato, niente warmup né LLM. Il backend serve anche la SPA da
 * ``frontend/dist``: la webapp va buildata prima — ``npm run test:e2e`` fa
 * pulizia dati + build + test.
 */
import { defineConfig, devices } from '@playwright/test';
import {
  ADMIN_PASSWORD,
  ADMIN_USERNAME,
  BACKEND_DIR,
  BASE_URL,
  DATA_DIR,
  HEALTH_URL,
  PORT,
} from './e2e/env';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: [['list'], ['html', { open: 'never' }]],
  // Il primo test che usa la fixture `seed` include upload + indicizzazione
  // reale dei documenti (caricamento modelli fastembed al primo avvio)
  timeout: 300_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `uv run uvicorn app.api.main:app --host 127.0.0.1 --port ${PORT}`,
    cwd: BACKEND_DIR,
    url: HEALTH_URL,
    timeout: 120_000,
    reuseExistingServer: false,
    env: {
      DATA_DIR,
      UPLOAD_DIR: `${DATA_DIR}/uploads`,
      ADMIN_AUTO_CREATE: 'true',
      ADMIN_USERNAME,
      ADMIN_PASSWORD,
      // Avvio veloce e nessuna chiamata LLM: la suite non esercita l'agente
      WARMUP_ON_START: 'false',
      MOONSHOT_API_KEY: '',
      KG_ENABLED: 'false',
      JWT_SECRET: 'e2e-jwt-secret-non-usare-in-produzione',
    },
  },
});
