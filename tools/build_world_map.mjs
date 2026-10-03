// Builds backend/app/static/world.json — the country shapes for the admin portal's Map tab.
//
// Run once (the output is committed, so nothing needs this at runtime or in the image):
//   npm i world-atlas topojson-client d3-geo      # in a scratch directory
//   node build_world_map.mjs > ../backend/app/static/world.json
//
// Data: Natural Earth admin-0 countries (public domain) via the `world-atlas` package,
// 1:50m scale — detailed enough to show small countries, light enough to ship (~0.5 MB).
// Projected here (Natural Earth projection) into a 1000 x 500-ish box and emitted as SVG
// path strings, so the page needs no mapping library and no network.
import { createRequire } from "node:module";
import { feature } from "topojson-client";
import { geoNaturalEarth1, geoPath } from "d3-geo";
const require = createRequire(import.meta.url);
const topo = require("world-atlas/countries-50m.json");

const W = 1000;
const countries = feature(topo, topo.objects.countries).features.filter((f) => f.properties.name !== "Antarctica");
const projection = geoNaturalEarth1().fitWidth(W, { type: "FeatureCollection", features: countries });
const path = geoPath(projection).digits(1);   // 0.1 px is far finer than a screen shows

const out = countries
  .map((f) => ({ name: f.properties.name, d: path(f) }))
  .filter((c) => c.d);
const [, , , bottom] = [0, 0, W, Math.ceil(geoPath(projection).bounds({ type: "FeatureCollection", features: countries })[1][1])];
process.stdout.write(JSON.stringify({ width: W, height: bottom, source: "Natural Earth (public domain) via world-atlas 1:50m", countries: out }));
