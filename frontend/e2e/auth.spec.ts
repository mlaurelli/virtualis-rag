/**
 * E2E autenticazione: gate del setup wizard, login (ko/ok) e logout.
 * Il backend E2E auto-crea l'admin (ADMIN_AUTO_CREATE), quindi il wizard
 * di primo avvio risulta già completato e /setup redirige al login.
 */
import { expect, test } from './fixtures';
import { ADMIN_PASSWORD, ADMIN_USERNAME } from './env';

async function fillLogin(page: import('@playwright/test').Page, password: string): Promise<void> {
  await page.goto('/login');
  await page.fill('#username', ADMIN_USERNAME);
  await page.fill('#password', password);
  await page.click('button[type="submit"]');
}

test.describe('autenticazione', () => {
  test('setup già completato: /setup redirige al login', async ({ page }) => {
    await page.goto('/setup');
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('#username')).toBeVisible();
  });

  test('login con password errata mostra un errore', async ({ page }) => {
    await fillLogin(page, 'password-sbagliata-e2e');
    await expect(page.getByRole('alert')).toContainText('Credenziali non valide');
    await expect(page).toHaveURL(/\/login$/);
  });

  test('login admin arriva su /chat', async ({ page }) => {
    await fillLogin(page, ADMIN_PASSWORD);
    await page.waitForURL('**/chat**');
    await expect(page.getByRole('heading', { name: 'Come posso aiutarti?' })).toBeVisible();
  });

  test('logout riporta al login e invalida la sessione', async ({ page }) => {
    await fillLogin(page, ADMIN_PASSWORD);
    await page.waitForURL('**/chat**');

    await page.getByRole('button', { name: 'Esci' }).click();
    await expect(page).toHaveURL(/\/login$/);

    // token rimosso: una pagina protetta redirige di nuovo al login
    await page.goto('/chat');
    await expect(page).toHaveURL(/\/login$/);
  });
});
