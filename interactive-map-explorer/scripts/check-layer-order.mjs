/**
 * Guard the one style invariant that fails silently.
 *
 * MapLibre paints layers in list order, and a `line` or `symbol` layer drawn
 * after a `fill-extrusion` paints straight over it rather than being occluded
 * by it. So if the sandbox's extruded buildings are defined before the road
 * and label layers, every street and street name draws on top of the towers
 * and the model looks like it is made of glass -- with no error, no warning,
 * and nothing in a type check or a build to catch it. That is exactly how it
 * shipped.
 *
 * This asserts the ordering directly on the source. It is deliberately a
 * text scan rather than anything that imports the component: Map.tsx is a
 * React module that wants a DOM and a WebGL context, and there is no browser
 * automation in this repo. A regex over the layer ids is crude, but it runs in
 * a second and it covers the failure that actually happened.
 *
 *   npm run check:layers
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'src', 'components', 'Map.tsx'), 'utf8');

/** First position of a layer id in the file, which is its style definition. */
function positionOf(id) {
  const at = source.indexOf(`id: '${id}'`);
  if (at === -1) throw new Error(`layer '${id}' is not defined in Map.tsx`);
  return at;
}

// Anything that must be capable of being hidden behind a building.
const OCCLUDED_BY_MODEL = [
  'road-motorway', 'road-primary', 'road-secondary', 'road-minor', 'road-path',
  'road-label', 'water-label', 'park-label', 'poi-label',
  'place-city', 'place-suburb',
];

// The model itself. Must come after everything above.
const MODEL = [
  'sandbox-massing', 'sandbox-buildings',
  'sandbox-radius', 'sandbox-radius-rim', 'sandbox-pin',
];

const failures = [];

const lastOccluded = OCCLUDED_BY_MODEL
  .map((id) => ({ id, at: positionOf(id) }))
  .sort((a, b) => b.at - a.at)[0];

for (const id of MODEL) {
  const at = positionOf(id);
  if (at < lastOccluded.at) {
    failures.push(
      `'${id}' is defined before '${lastOccluded.id}'. Basemap layers paint ` +
      `over fill-extrusion, so the model would be drawn through.`,
    );
  }
}

// The veil goes last, over everything including the model, or the area outside
// the city stops being veiled at exactly the zoom where the model appears.
const maskAt = positionOf('city-mask');
for (const id of MODEL) {
  if (positionOf(id) > maskAt) {
    failures.push(`'${id}' is defined after 'city-mask'; the veil must be last.`);
  }
}

if (failures.length) {
  console.error('Layer order check FAILED:\n');
  for (const line of failures) console.error(`  - ${line}`);
  console.error('');
  process.exit(1);
}

console.log(
  `Layer order OK: ${MODEL.length} model layers after ` +
  `${OCCLUDED_BY_MODEL.length} basemap layers, city-mask last.`,
);
