/**
 * Cattura screenshot QA di tutte le pagine dell'app (temi chiaro e scuro).
 *
 * PREREQUISITO: backend attivo con SPA montata e dati demo, es.:
 *   cd backend && DATA_DIR=/tmp/virtualis-qa UPLOAD_DIR=/tmp/virtualis-qa/uploads \
 *     ADMIN_AUTO_CREATE=true ADMIN_USERNAME=admin ADMIN_PASSWORD=admin123 \
 *     uv run uvicorn app.api.main:app --port 8022
 *
 * Uso (dalla cartella frontend/):
 *   npm run qa:shots
 * Variabili: QA_BASE_URL (default http://localhost:8022),
 *            QA_OUT (default ../docs/screenshots), QA_USER/QA_PASSWORD.
 */
import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const BASE = process.env.QA_BASE_URL || 'http://localhost:8022';
const OUT = process.env.QA_OUT || path.resolve(process.cwd(), '../docs/screenshots');
const USER = process.env.QA_USER || 'admin';
const PASS = process.env.QA_PASSWORD || 'admin123';

const VIEWPORT = { width: 1440, height: 900 };

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    reducedMotion: 'reduce',
  });

  async function newPage(theme) {
    const page = await context.newPage();
    await page.addInitScript((t) => localStorage.setItem('virtualis_theme', t), theme);
    return page;
  }

  async function shot(page, name) {
    await page.screenshot({ path: path.join(OUT, `${name}.png`) });
    console.log('shot:', name);
    await page.close();
  }

  // --- Login (screenshot prima del login: utenti autenticati sono rediretti) ---
  for (const theme of ['dark', 'light']) {
    const page = await newPage(theme);
    await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
    await page.fill('#username', USER);
    await page.fill('#password', PASS);
    await page.waitForTimeout(200);
    await shot(page, theme === 'dark' ? '01-login-dark' : '02-login-light');
  }

  // login reale dalla UI: valida anche il flusso di autenticazione
  {
    const p = await newPage('dark');
    await p.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
    await p.fill('#username', USER);
    await p.fill('#password', PASS);
    await p.click('button[type="submit"]');
    await p.waitForURL('**/chat**', { timeout: 15000 });
    await p.waitForLoadState('networkidle');
    console.log('login OK');
    await p.close();
  }

  const authedPage = (theme) => newPage(theme); // token già in localStorage del context

  // --- Chat empty state ---
  for (const theme of ['dark', 'light']) {
    const page = await authedPage(theme);
    await page.goto(`${BASE}/chat`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(400);
    await shot(page, theme === 'dark' ? '10-chat-empty-dark' : '11-chat-empty-light');
  }

  // --- Chat con conversazione popolata ---
  for (const theme of ['dark', 'light']) {
    const page = await authedPage(theme);
    await page.goto(`${BASE}/chat/1`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.message.assistant', { timeout: 15000 });
    await page.waitForTimeout(400);
    await shot(page, theme === 'dark' ? '12-chat-conversation-dark' : '13-chat-conversation-light');
  }

  // --- Popover citazione in hover ---
  {
    const page = await authedPage('dark');
    await page.goto(`${BASE}/chat/1`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.citation-marker');
    const marker = page.locator('.message.assistant .citation-marker').first();
    await marker.scrollIntoViewIfNeeded();
    await marker.hover();
    await page.waitForTimeout(500);
    await shot(page, '14-chat-citation-popover-dark');
  }

  // --- Pannello citazioni aperto ---
  for (const theme of ['dark', 'light']) {
    const page = await authedPage(theme);
    await page.goto(`${BASE}/chat/1`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.citation-marker');
    await page.locator('.message.assistant .citation-marker').first().click();
    await page.waitForSelector('.citation-panel');
    await page.waitForTimeout(400);
    await shot(page, theme === 'dark' ? '15-chat-citations-panel-dark' : '16-chat-citations-panel-light');
  }

  // --- Ricerca con risultati ---
  for (const theme of ['dark', 'light']) {
    const page = await authedPage(theme);
    await page.goto(`${BASE}/search`, { waitUntil: 'networkidle' });
    await page.fill('input[type="search"]', 'materiali sostituzione filtri linea B');
    await page.click('button[type="submit"]');
    await page.waitForSelector('.result-card', { timeout: 60000 });
    await page.waitForTimeout(400);
    await shot(page, theme === 'dark' ? '20-search-results-dark' : '21-search-results-light');
  }

  // --- Documenti ---
  for (const theme of ['dark', 'light']) {
    const page = await authedPage(theme);
    await page.goto(`${BASE}/documents`, { waitUntil: 'networkidle' });
    await page.waitForSelector('table, .card', { timeout: 15000 });
    await page.waitForTimeout(400);
    await shot(page, theme === 'dark' ? '30-documents-dark' : '31-documents-light');
  }

  // --- Knowledge graph (stato disabilitato) ---
  for (const theme of ['dark', 'light']) {
    const page = await authedPage(theme);
    await page.goto(`${BASE}/knowledge`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(500);
    await shot(page, theme === 'dark' ? '40-knowledge-dark' : '41-knowledge-light');
  }

  // --- Admin: utenti, feedback, audit ---
  const adminTabs = [
    ['Utenti', '50-admin-users-dark'],
    ['Feedback', '51-admin-feedback-dark'],
    ['Audit', '52-admin-audit-dark'],
  ];
  for (const [label, name] of adminTabs) {
    const page = await authedPage('dark');
    await page.goto(`${BASE}/admin`, { waitUntil: 'networkidle' });
    await page.getByRole('tab', { name: label }).click();
    await page.waitForTimeout(800);
    await shot(page, name);
  }

  // --- Viewer PDF ---
  {
    const page = await authedPage('dark');
    await page.goto(`${BASE}/viewer/3?page=1`, { waitUntil: 'networkidle' });
    await page.waitForSelector('canvas', { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(1000);
    await shot(page, '60-viewer-pdf-dark');
  }

  await browser.close();
  console.log('Fatto. Screenshot in', OUT);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
