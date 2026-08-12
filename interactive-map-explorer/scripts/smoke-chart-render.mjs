import assert from 'node:assert/strict';
import { unlinkSync, writeFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { chromium } from 'playwright-core';


const baseUrl = process.env.URBAN_DOSSIER_SMOKE_URL || 'http://127.0.0.1:3460';
const executablePath = process.env.CHROMIUM_PATH || '/snap/bin/chromium';
const screenshotPath =
  process.env.URBAN_DOSSIER_SMOKE_SCREENSHOT || '/tmp/urban-dossier-chart-smoke.png';
const methodologyScreenshotPath = '/tmp/urban-dossier-methodology-smoke.png';
const reportPath = fileURLToPath(new URL('../dist/offline-export-smoke.html', import.meta.url));

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-angle=swiftshader'],
});
const externalRequests = [];
const pageErrors = [];
const consoleErrors = [];
let timelineRequests = 0;
let methodologyRequests = 0;

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === '127.0.0.1' || url.hostname === 'localhost') {
      if (url.pathname === '/api/timeline') timelineRequests += 1;
      if (url.pathname === '/api/methodology') methodologyRequests += 1;
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
  await page.getByText(/95% range .* · point estimate/).waitFor({ timeout: 30_000 });
  await page.getByText(/server quantiles/).waitFor({ timeout: 30_000 });
  await page.getByText(/modeled NO/, { exact: false }).waitFor({ timeout: 30_000 });
  await page.getByText('NYCCAS 2023–24 annual-average model · context only, 0% of overall', {
    exact: true,
  }).waitFor({ timeout: 30_000 });
  await page.getByText(/heat vulnerability · HVI [1-5]\/5/).waitFor({ timeout: 30_000 });
  await page.getByText(
    'NYC DOHMH mortality-risk quintile · ZCTA 2020 · context only, 0% of overall',
    { exact: true },
  ).waitFor({ timeout: 30_000 });
  await page.locator('.vega-embed svg').first().waitFor({ timeout: 30_000 });
  console.log('smoke: downloading and opening self-contained report offline');
  await page.evaluate(() => {
    window.__udDownloadLinks = [];
    const originalCreateObjectURL = URL.createObjectURL.bind(URL);
    URL.createObjectURL = function recordDownloadBlob(value) {
      window.__udDownloadBlob = value;
      return originalCreateObjectURL(value);
    };
    const originalClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function recordDownloadClick() {
      window.__udDownloadLinks.push({ download: this.download, href: this.href });
      return originalClick.call(this);
    };
  });
  const [exportResponse] = await Promise.all([
    page.waitForResponse((response) => response.url().endsWith('/api/export/html')),
    page.getByRole('button', { name: 'Download offline HTML report' }).click(),
  ]);
  assert.equal(exportResponse.status(), 200);
  const attachment = exportResponse.headers()['content-disposition'] || '';
  const exportFilename = attachment.match(/filename="([^"]+)"/)?.[1] || '';
  assert.match(exportFilename, /^urban-dossier-.+\.html$/);
  await page.waitForFunction(() => window.__udDownloadLinks?.length === 1);
  const downloadLink = await page.evaluate(() => window.__udDownloadLinks[0]);
  assert.equal(downloadLink.download, exportFilename);
  assert.match(downloadLink.href, /^blob:/);
  const exportBodyBase64 = await page.evaluate(async () => {
    const bytes = new Uint8Array(await window.__udDownloadBlob.arrayBuffer());
    let binary = '';
    for (let index = 0; index < bytes.length; index += 32_768) {
      binary += String.fromCharCode(...bytes.subarray(index, index + 32_768));
    }
    return btoa(binary);
  });
  const exportBody = Buffer.from(exportBodyBase64, 'base64');
  assert(exportBody.length > 700_000, `export response is unexpectedly small: ${exportBody.length}`);
  assert.equal(exportBody.subarray(0, 15).toString(), '<!doctype html>');
  writeFileSync(reportPath, exportBody);
  const offlineRequests = [];
  const offlineErrors = [];
  const offlineConsoleErrors = [];
  const offlinePage = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  offlinePage.on('pageerror', (error) => offlineErrors.push(error.message));
  offlinePage.on('console', (message) => {
    if (message.type() === 'error') offlineConsoleErrors.push(message.text());
  });
  const blockOfflineNetwork = async (route) => {
    offlineRequests.push(route.request().url());
    await route.abort('internetdisconnected');
  };
  await offlinePage.route('http://**/*', blockOfflineNetwork);
  await offlinePage.route('https://**/*', blockOfflineNetwork);
  await offlinePage.goto(pathToFileURL(reportPath).href, {
    waitUntil: 'domcontentloaded',
    timeout: 15_000,
  });
  await offlinePage.waitForTimeout(1_000);
  console.log('smoke: offline runtime', await offlinePage.evaluate(() => ({
    scripts: document.scripts.length,
    charts: document.querySelectorAll('.chart').length,
    rendered: document.querySelectorAll('.vega-embed svg').length,
    renderErrors: Array.from(document.querySelectorAll('.render-error'), (node) => node.textContent),
    vega: typeof window.vega,
    vegaLite: typeof window.vegaLite,
    vegaEmbed: typeof window.vegaEmbed,
  })));
  await offlinePage.locator('.vega-embed svg').first().waitFor({ timeout: 30_000 });
  const exportedCharts = await offlinePage.locator('.vega-embed svg').count();
  const exportStamp = await offlinePage.locator('[data-testid="generated-at"]').textContent();
  assert(exportedCharts >= 3, `expected at least 3 exported Vega SVGs, got ${exportedCharts}`);
  await offlinePage.getByText('methodology v3.9.0', { exact: true }).waitFor();
  await offlinePage.getByText('overall tier', { exact: true }).waitFor();
  await offlinePage.getByText('modeled NO context', { exact: true }).waitFor();
  await offlinePage.getByText('heat vulnerability', { exact: true }).waitFor();
  assert.match(exportStamp || '', /^generated \d{4}-\d{2}-\d{2}T/);
  assert.equal(await offlinePage.locator('.render-error').count(), 0);
  assert.deepEqual(offlineRequests, []);
  assert.deepEqual(offlineErrors, []);
  assert.deepEqual(offlineConsoleErrors, []);
  await offlinePage.close();
  const presentationCheck = await page.evaluate(async () => {
    const contract = await fetch('/api/presentation/classes').then((response) => response.json());
    return {
      overall: contract.univariate.categories.overall,
      bivariate: contract.bivariate,
      mapColor: window.__udMap.getPaintProperty('hex-overlay-fill', 'fill-color'),
    };
  });
  console.log('smoke: pinning and selecting second location');
  await page.getByRole('button', { name: 'Pin current location for comparison' }).click();
  await page.getByRole('textbox', { name: 'Find a place in New York' }).fill('Times Square');
  await page.getByRole('textbox', { name: 'Find a place in New York' }).press('Enter');
  await page.getByText('Compare', { exact: true }).waitFor({ timeout: 30_000 });
  await page.getByText('Score comparison', { exact: true }).waitFor({ timeout: 30_000 });
  await page.getByLabel('Comparison delta map legend').waitFor({ timeout: 30_000 });
  await page.waitForFunction(
    () => window.__udMap?.getSource('comparisonDelta')?._data?.geojson?.features?.length >= 5,
    null,
    { timeout: 30_000 },
  );
  await page.locator('.vega-embed svg').nth(2).waitFor({ timeout: 30_000 });
  console.log('smoke: validating rendered charts');
  const captions = await page.locator('figure figcaption').allTextContents();
  const renderedCharts = await page.locator('.vega-embed svg').count();
  const deltaMap = await page.evaluate(() => {
    const map = window.__udMap;
    const sourceData = map.getSource('comparisonDelta')._data.geojson;
    return {
      features: sourceData.features.length,
      layers: [
        'comparison-radius-fill',
        'comparison-radius-line',
        'comparison-connector',
        'comparison-endpoints',
      ].filter((id) => Boolean(map.getLayer(id))),
      connectorColor: map.getPaintProperty('comparison-connector', 'line-color'),
    };
  });

  assert(captions.some((text) => text.includes('Score composition')));
  assert(captions.some((text) => text.includes('City score distribution')));
  assert(captions.some((text) => text.includes('Quarterly signals')));
  assert(captions.some((text) => text.includes('Score comparison')));
  assert(renderedCharts >= 4, `expected at least 4 rendered Vega SVGs, got ${renderedCharts}`);
  assert(deltaMap.features >= 5, `expected comparison GeoJSON features, got ${deltaMap.features}`);
  assert.equal(deltaMap.layers.length, 4);
  assert(
    JSON.stringify(deltaMap.connectorColor).includes('overall_delta'),
    `connector is not reading the backend overall_delta field: ${JSON.stringify(deltaMap.connectorColor)}`,
  );
  const mapColorJson = JSON.stringify(presentationCheck.mapColor);
  for (const edge of presentationCheck.overall.breaks) {
    assert(mapColorJson.includes(String(edge)), `overview map is missing server break ${edge}`);
  }
  for (const color of presentationCheck.overall.colors) {
    assert(mapColorJson.includes(color), `overview map is missing server color ${color}`);
  }
  console.log('smoke: toggling bivariate map');
  await page.getByRole('button', { name: 'Safety by Transit bivariate map' }).click();
  await page.getByLabel('Bivariate map legend').waitFor({ timeout: 30_000 });
  await page.waitForFunction(
    () => window.__udMap?.getSource('hexOverlay')?._data?.geojson?.features?.[0]
      ?.properties?.bivariate_color,
    null,
    { timeout: 30_000 },
  );
  const bivariateMap = await page.evaluate(() => {
    const features = window.__udMap.getSource('hexOverlay')._data.geojson.features;
    return {
      features: features.length,
      first: features[0].properties,
      fillColor: window.__udMap.getPaintProperty('hex-overlay-fill', 'fill-color'),
    };
  });
  assert(bivariateMap.features > 0);
  assert(JSON.stringify(bivariateMap.fillColor).includes('bivariate_color'));
  assert.equal(
    bivariateMap.first.bivariate_color,
    presentationCheck.bivariate.matrix[bivariateMap.first.y_class][bivariateMap.first.x_class],
  );
  console.log('smoke: animating real-period timeline');
  await page.evaluate(() => {
    const map = window.__udMap;
    window.__timelineStateMutations = [];
    const original = map.setGlobalStateProperty.bind(map);
    map.setGlobalStateProperty = (name, value) => {
      window.__timelineStateMutations.push({ name, value });
      return original(name, value);
    };
  });
  await page.getByRole('button', { name: 'Collision timeline map' }).click();
  await page.getByLabel('Timeline map controls').waitFor({ timeout: 30_000 });
  await page.waitForFunction(
    () => window.__udMap?.getSource('hexOverlay')?._data?.geojson?.metadata?.periods?.length > 1,
    null,
    { timeout: 30_000 },
  );
  const timelineBefore = await page.evaluate(() => ({
    period: window.__udMap.getGlobalState().timeline_period,
    fillColor: window.__udMap.getPaintProperty('hex-overlay-fill', 'fill-color'),
    data: window.__udMap.getSource('hexOverlay')._data.geojson,
  }));
  const timelineRequestsBeforePlay = timelineRequests;
  await page.getByRole('button', { name: 'Play timeline' }).click();
  await page.waitForFunction(
    (initialPeriod) => window.__udMap.getGlobalState().timeline_period !== initialPeriod,
    timelineBefore.period,
    { timeout: 5_000 },
  );
  await page.getByRole('button', { name: 'Pause timeline' }).click();
  const timelineMap = await page.evaluate(() => ({
    period: window.__udMap.getGlobalState().timeline_period,
    mutations: window.__timelineStateMutations,
    featureCount: window.__udMap.getSource('hexOverlay')._data.geojson.features.length,
  }));
  assert(JSON.stringify(timelineBefore.fillColor).includes('global-state'));
  assert(JSON.stringify(timelineBefore.fillColor).includes('timeline_period'));
  assert(timelineBefore.data.metadata.periods.some(
    (item) => item.period === timelineBefore.period &&
      Object.hasOwn(timelineBefore.data.features[0].properties, item.color_property),
  ));
  assert.notEqual(timelineMap.period, timelineBefore.period);
  assert(timelineMap.featureCount > 0);
  assert(timelineMap.mutations.filter((item) => item.name === 'timeline_period').length >= 2);
  assert.equal(
    timelineRequests,
    timelineRequestsBeforePlay,
    'animation tick refetched timeline data',
  );
  assert.equal(externalRequests.length, 0, `external requests attempted: ${externalRequests}`);
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(consoleErrors, []);

  await page.screenshot({ path: screenshotPath, fullPage: true });
  console.log('smoke: opening shareable methodology publication');
  await page.goto(`${baseUrl}/methodology`, {
    waitUntil: 'domcontentloaded',
    timeout: 15_000,
  });
  await page.getByTestId('methodology-version-verified').waitFor({ timeout: 15_000 });
  await page.getByText('code = registry = v3.9.0', { exact: true }).waitFor();
  await page.getByText('Runtime dataset coverage', { exact: true }).waitFor();
  const methodologyAudit = {
    metricRows: await page.locator('table tbody tr').count(),
    datasetRows: await page.locator('section').filter({ hasText: 'Runtime dataset coverage' })
      .locator('.font-mono').count(),
    mapCanvases: await page.locator('.maplibregl-canvas').count(),
  };
  assert(methodologyAudit.metricRows >= 19);
  assert(methodologyAudit.datasetRows >= 15);
  assert.equal(methodologyAudit.mapCanvases, 0);
  assert.equal(methodologyRequests, 1);
  await page.screenshot({ path: methodologyScreenshotPath, fullPage: true });
  console.log(
    JSON.stringify(
      {
        renderedCharts,
        captions,
        presentation: {
          breaks: presentationCheck.overall.breaks,
          colors: presentationCheck.overall.colors,
        },
        bivariateMap,
        timelineMap: {
          periods: timelineBefore.data.metadata.periods.length,
          from: timelineBefore.period,
          to: timelineMap.period,
          features: timelineMap.featureCount,
          stateMutations: timelineMap.mutations.length,
          requests: timelineRequests,
          fillColor: timelineBefore.fillColor,
        },
        deltaMap,
        offlineExport: {
          charts: exportedCharts,
          generatedAt: exportStamp,
          externalRequests: offlineRequests.length,
          filename: exportFilename,
          browserDownloadLink: downloadLink.href.startsWith('blob:'),
        },
        methodology: {
          ...methodologyAudit,
          requests: methodologyRequests,
          screenshotPath: methodologyScreenshotPath,
        },
        externalRequests: externalRequests.length,
        screenshotPath,
      },
      null,
      2,
    ),
  );
} catch (error) {
  console.error('smoke failed:', error);
  console.error('page errors:', pageErrors);
  console.error('console errors:', consoleErrors);
  process.exitCode = 1;
} finally {
  await browser.close();
  try { unlinkSync(reportPath); } catch {}
}
