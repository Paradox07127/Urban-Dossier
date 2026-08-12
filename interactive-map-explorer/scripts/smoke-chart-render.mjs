import assert from 'node:assert/strict';

import { chromium } from 'playwright-core';


const baseUrl = process.env.URBAN_DOSSIER_SMOKE_URL || 'http://127.0.0.1:3460';
const executablePath = process.env.CHROMIUM_PATH || '/snap/bin/chromium';
const screenshotPath =
  process.env.URBAN_DOSSIER_SMOKE_SCREENSHOT || '/tmp/urban-dossier-chart-smoke.png';

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-angle=swiftshader'],
});

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const externalRequests = [];
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') {
      await route.continue();
      return;
    }
    externalRequests.push(url.href);
    await route.abort('internetdisconnected');
  });

  console.log('smoke: opening app');
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 15_000 });
  console.log('smoke: selecting first location');
  await page.getByRole('button', { name: 'Find a place' }).click();
  const search = page.getByRole('textbox', { name: 'Find a place in New York' });
  await search.fill('Empire State');
  await search.press('Enter');

  await page.getByText('Overall score', { exact: true }).waitFor({ timeout: 30_000 });
  await page.locator('.vega-embed svg').first().waitFor({ timeout: 30_000 });
  console.log('smoke: pinning and selecting second location');
  await page.getByRole('button', { name: 'Pin current location for comparison' }).click();
  await page.getByRole('textbox', { name: 'Find a place in New York' }).fill('Times Square');
  await page.getByRole('textbox', { name: 'Find a place in New York' }).press('Enter');
  await page.getByText('Compare', { exact: true }).waitFor({ timeout: 30_000 });
  await page.getByText('Score comparison', { exact: true }).waitFor({ timeout: 30_000 });
  await page.locator('.vega-embed svg').nth(2).waitFor({ timeout: 30_000 });
  console.log('smoke: validating rendered charts');
  const captions = await page.locator('figure figcaption').allTextContents();
  const renderedCharts = await page.locator('.vega-embed svg').count();

  assert(captions.some((text) => text.includes('Score composition')));
  assert(captions.some((text) => text.includes('City score distribution')));
  assert(captions.some((text) => text.includes('Quarterly signals')));
  assert(captions.some((text) => text.includes('Score comparison')));
  assert(renderedCharts >= 4, `expected at least 4 rendered Vega SVGs, got ${renderedCharts}`);
  assert.equal(externalRequests.length, 0, `external requests attempted: ${externalRequests}`);
  assert.deepEqual(pageErrors, []);

  await page.screenshot({ path: screenshotPath, fullPage: true });
  console.log(
    JSON.stringify(
      { renderedCharts, captions, externalRequests: externalRequests.length, screenshotPath },
      null,
      2,
    ),
  );
} catch (error) {
  console.error('smoke failed:', error);
  process.exitCode = 1;
} finally {
  await browser.close();
}
