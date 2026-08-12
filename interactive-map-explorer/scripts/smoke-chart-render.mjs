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
const externalRequests = [];
const pageErrors = [];
const consoleErrors = [];
let timelineRequests = 0;

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
  await page.getByText(/server quantiles/).waitFor({ timeout: 30_000 });
  await page.locator('.vega-embed svg').first().waitFor({ timeout: 30_000 });
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
}
