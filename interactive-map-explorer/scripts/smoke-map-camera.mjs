import assert from 'node:assert/strict';

import { chromium } from 'playwright-core';


const baseUrl = process.env.URBAN_DOSSIER_SMOKE_URL || 'http://127.0.0.1:3456';
const executablePath = process.env.CHROMIUM_PATH || '/snap/bin/chromium';

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-angle=swiftshader'],
});

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 15_000 });
  await page.waitForFunction(() => window.__udMap?.loaded(), null, { timeout: 30_000 });
  await page.waitForTimeout(1_000);

  const before = await page.evaluate(() => {
    const map = window.__udMap;
    map.stop();
    window.__udCameraEvents = [];
    for (const event of ['movestart', 'moveend']) {
      map.on(event, () => window.__udCameraEvents.push({
        event,
        center: map.getCenter().toArray(),
        zoom: map.getZoom(),
        time: performance.now(),
      }));
    }
    return { center: map.getCenter().toArray(), zoom: map.getZoom() };
  });

  // Zoom around a point well away from the map center. MapLibre must move the
  // center to keep that screen-space anchor fixed; React must not fly it back.
  await page.mouse.move(1180, 280);
  await page.mouse.wheel(0, -550);
  await page.waitForTimeout(1_500);

  const after = await page.evaluate(() => ({
    center: window.__udMap.getCenter().toArray(),
    zoom: window.__udMap.getZoom(),
    events: window.__udCameraEvents,
  }));
  const starts = after.events.filter((entry) => entry.event === 'movestart');
  const ends = after.events.filter((entry) => entry.event === 'moveend');

  assert.equal(starts.length, 1, `one wheel gesture triggered ${starts.length} camera moves`);
  assert.equal(ends.length, 1, `one wheel gesture ended ${ends.length} camera moves`);
  assert(after.zoom > before.zoom, 'wheel gesture did not zoom in');
  assert(
    Math.abs(after.center[0] - before.center[0]) > 1e-4 ||
      Math.abs(after.center[1] - before.center[1]) > 1e-4,
    'off-center zoom did not retain its pointer-anchored center',
  );
  assert.deepEqual(after.center, ends[0].center, 'camera moved again after the user zoom ended');

  // The feedback guard must not swallow real camera commands from React.
  await page.getByRole('button', { name: 'Find a place' }).click();
  const search = page.getByRole('textbox', { name: 'Find a place in New York' });
  await search.fill('Empire State');
  await search.press('Enter');
  await page.waitForFunction(() => {
    const center = window.__udMap.getCenter();
    return window.__udMap.getZoom() >= 14.9 &&
      Math.abs(center.lat - 40.7484) < 0.01 &&
      Math.abs(center.lng - -73.9857) < 0.01;
  }, null, { timeout: 5_000 });

  console.log('Map camera smoke passed: user zoom stays anchored; React camera commands still run.');
} finally {
  await browser.close();
}
