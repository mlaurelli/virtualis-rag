/**
 * E2E chat: empty state, rendering markdown della risposta, marker di
 * citazione (popover in hover, pannello al click) e feedback 👍/👎.
 * La sessione usata è quella popolata dalla fixture `seed` (niente LLM).
 */
import type { Page } from '@playwright/test';
import { expect, loginViaApi, test, type SeedData } from './fixtures';

async function openSeededSession(page: Page, seed: SeedData): Promise<void> {
  await page.goto(`/chat/${seed.sessionId}`);
  await expect(page.locator('.message.assistant').first()).toBeVisible();
}

test.describe('chat', () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
  });

  test('empty state con tre suggerimenti', async ({ page }) => {
    await page.goto('/chat');
    await expect(page.getByRole('heading', { name: 'Come posso aiutarti?' })).toBeVisible();
    await expect(page.locator('.suggestion-chip')).toHaveCount(3);
  });

  test('risposta in markdown renderizzata con marker citazione', async ({ page, seed }) => {
    await openSeededSession(page, seed);
    const assistant = page.locator('.message.assistant').first();

    // markdown: almeno un elemento <strong> renderizzato
    await expect(assistant.locator('.message-body strong').first()).toBeVisible();

    // il marker [1] è un bottone cliccabile con etichetta accessibile
    // (la risposta cita due volte la fonte: se ne verifica uno)
    const marker = assistant.getByRole('button', { name: /Citazione 1:/ }).first();
    await expect(marker).toBeVisible();
    await expect(marker).toHaveText('1');
  });

  test('hover sul marker mostra il popover, click apre il pannello citazioni', async ({
    page,
    seed,
  }) => {
    await openSeededSession(page, seed);
    const marker = page.locator('.message.assistant .citation-marker').first();

    await marker.hover();
    const pop = page.locator('.citation-pop').first();
    await expect(pop).toBeVisible();
    await expect(pop.locator('.citation-pop-title')).toHaveText(seed.documents[0].title);

    await marker.click();
    const panel = page.locator('.citation-panel');
    await expect(panel).toBeVisible();
    await expect(panel.getByRole('heading', { name: 'Fonti citate' })).toBeVisible();
    await expect(panel.locator('.citation-title')).toContainText(seed.documents[0].title);
  });

  test('bottoni feedback presenti e click su 👍 cambia stato', async ({ page, seed }) => {
    await openSeededSession(page, seed);
    const up = page.getByRole('button', { name: 'Risposta utile' });
    const down = page.getByRole('button', { name: 'Risposta non utile' });
    await expect(up).toBeVisible();
    await expect(down).toBeVisible();

    await up.click();
    await expect(up).toHaveAttribute('aria-pressed', 'true');
    await expect(up).toHaveClass(/active/);
  });
});
