/**
 * E2E documenti: tabella dell'archivio con badge di stato e upload di un
 * file dalla UI (setInputFiles) con attesa dell'indicizzazione reale.
 */
import { expect, loginViaApi, test } from './fixtures';

test.describe('documenti', () => {
  test.beforeEach(async ({ page }) => {
    await loginViaApi(page);
  });

  test('tabella con i documenti di test e badge "Indicizzato"', async ({ page, seed }) => {
    await page.goto('/documents');
    for (const doc of seed.documents) {
      // first(): un fallimento precedente fa ripartire il worker e la fixture
      // seed carica di nuovo gli stessi documenti (titoli duplicati in lista)
      const row = page.locator('table.table tbody tr', { hasText: doc.title }).first();
      await expect(row).toBeVisible();
      await expect(row.locator('.badge-indexed')).toHaveText('Indicizzato');
    }
  });

  test('upload di un file: compare in lista e viene indicizzato', async ({ page }) => {
    await page.goto('/documents');
    const title = 'nota-e2e-upload';

    await page.locator('input[type="file"]').setInputFiles({
      name: `${title}.md`,
      mimeType: 'text/markdown',
      buffer: Buffer.from(
        '# Nota E2E\n\nContenuto di prova caricato dalla suite E2E.\n',
        'utf-8',
      ),
    });

    // conferma dell'upload accettato (202)
    await expect(
      page.getByRole('status').filter({ hasText: 'caricato' }),
    ).toBeVisible();

    // la riga compare subito (stato in coda/elaborazione) e la pagina fa
    // polling finché la coda è attiva: attendiamo il badge finale
    const row = page.locator('table.table tbody tr', { hasText: title });
    await expect(row).toBeVisible();
    await expect(row.locator('.badge-indexed')).toHaveText('Indicizzato', {
      timeout: 240_000,
    });
  });
});
