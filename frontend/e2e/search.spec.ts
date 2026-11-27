/**
 * E2E ricerca: ricerca ibrida reale sul corpus di test (2 documenti
 * indicizzati dalla fixture `seed`) e filtri per fonte/tipologia.
 *
 * La prima ricerca della run carica lazy il reranker (~1-2 GB, già in cache
 * HF): per questo le attese sui risultati hanno timeout generosi, senza
 * sleep fissi.
 */
import type { Page } from '@playwright/test';
import { expect, loginViaApi, test } from './fixtures';

const FIRST_RESULT_TIMEOUT = 240_000;

async function runSearch(page: Page, term: string): Promise<void> {
  await page.fill('input[type="search"]', term);
  await page.getByRole('button', { name: 'Cerca' }).click();
}

test.describe('ricerca', () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
  });

  test('un termine presente nel documento restituisce un risultato con titolo', async ({
    page,
    seed,
  }) => {
    await page.goto('/search');
    await runSearch(page, seed.searchTerm);

    const card = page.locator('.result-card').first();
    await expect(card).toBeVisible({ timeout: FIRST_RESULT_TIMEOUT });
    await expect(card.locator('.result-title')).toContainText(seed.documents[0].title);
    await expect(card.locator('.result-meta')).toContainText('upload');
  });

  test('filtro per fonte e tipologia', async ({ page, seed }) => {
    await page.goto('/search');
    await runSearch(page, seed.searchTerm);
    await expect(page.locator('.result-card').first()).toBeVisible({
      timeout: FIRST_RESULT_TIMEOUT,
    });

    // fonte "upload" (quella dei documenti di test): il risultato resta
    await page.selectOption('#filter-source', { label: 'upload' });
    await runSearch(page, seed.searchTerm);
    const filtered = page.locator('.result-card').first();
    await expect(filtered).toBeVisible({ timeout: 60_000 });
    await expect(filtered.locator('.result-title')).toContainText(seed.documents[0].title);

    // tipologia incompatibile (i documenti di test sono markdown): nessun risultato
    await page.selectOption('#filter-doctype', 'pdf');
    await runSearch(page, seed.searchTerm);
    await expect(page.getByText('Nessun risultato per questa ricerca.')).toBeVisible({
      timeout: 60_000,
    });
  });
});
