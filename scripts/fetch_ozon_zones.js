#!/usr/bin/env node
/* Ozon "blue zones" — the buildings and towns where Ozon pays support money to whoever opens
 * a new pickup point (the map on https://pvz.ozon.com/uz-ru). Writes data/ozon_zones.geojson.
 *
 * NOT part of the daily pipeline, for the same reason as fetch_ozon_site.js: Ozon's site sits
 * behind an anti-bot check that only a real browser passes. Run by hand when a refresh is due:
 *
 *   npm i --no-save playwright && pip install mapbox-vector-tile shapely
 *   node scripts/fetch_ozon_zones.js
 *
 * How it works: the map draws the blue shapes from vector tiles of a layer called
 * "RecommendedZone". We ask for that layer over the whole country at a coarse zoom to find
 * where anything is drawn at all (18 tiles out of 6,500 in Nov 2026), re-ask only those places
 * at a fine zoom for accurate outlines, hand the tiles to ozon_zones_decode.py, and finally ask
 * Ozon — the same request the site makes on a click — for each shape's address and support sum.
 */
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const OUT = path.join(__dirname, '..', 'data', 'ozon_zones.geojson');
const TILES_TMP = path.join(os.tmpdir(), 'ozon_zone_tiles.json');
const PAGE = 'https://pvz.ozon.com/uz-ru';
const API = 'https://pvz-map.ozon.com/p-api/orp-agent-api';
const COUNTRY = { latMin: 37.1, latMax: 45.7, lngMin: 55.9, lngMax: 73.3 };   // Uzbekistan
const Z_SCAN = 11, Z_FINE = 13;
const MIN_SHAPES = 20;   // fewer than this means the site changed — keep the old file

const tileX = (lng, z) => Math.floor((lng + 180) / 360 * 2 ** z);
const tileY = (lat, z) => { const r = lat * Math.PI / 180;
  return Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * 2 ** z); };

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome',
                                          args: ['--disable-blink-features=AutomationControlled'] });
  const page = await (await browser.newContext({ locale: 'ru-RU', viewport: { width: 1400, height: 1000 } })).newPage();
  await page.goto(PAGE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(9000);   // anti-bot redirect + the map iframe need a moment
  const frame = page.frames().find(f => f.url().includes('pvz-map.ozon.com'));
  if (!frame) { console.error('Map iframe not found — the page layout changed.'); process.exit(1); }

  // Download tiles from inside the map's own page, six at a time, retrying failures.
  const getTiles = async (tiles, z) => {
    const got = {}; let failed = 0;
    for (let i = 0; i < tiles.length; i += 400) {
      const part = await frame.evaluate(async ({ chunk, z, API }) => {
        const res = []; let idx = 0;
        const one = async ([x, y]) => {
          for (let a = 0; a < 4; a++) {
            try {
              const r = await fetch(`${API}/map/render-vtile?x=${x}&y=${y}&z=${z}&layerName=RecommendedZone&isCis=false&tt=${Date.now()}`, { credentials: 'include' });
              if (r.status === 204) return [x, y, 204, ''];          // nothing drawn here
              if (r.status === 200) {
                const b = new Uint8Array(await r.arrayBuffer()); let s = '';
                for (const c of b) s += String.fromCharCode(c);
                return [x, y, 200, btoa(s)];
              }
            } catch (e) {}
            await new Promise(r => setTimeout(r, 1500 * (a + 1)));
          }
          return [x, y, 0, ''];
        };
        const worker = async () => { while (idx < chunk.length) res.push(await one(chunk[idx++])); };
        await Promise.all(Array.from({ length: 6 }, worker));
        return res;
      }, { chunk: tiles.slice(i, i + 400), z, API });
      for (const [x, y, status, b64] of part) {
        if (status === 200) got[`${x}/${y}`] = b64; else if (status === 0) failed++;
      }
      process.stdout.write(`\r  zoom ${z}: ${Math.min(i + 400, tiles.length)}/${tiles.length} tiles, ${Object.keys(got).length} with zones`);
    }
    console.log();
    return { got, failed };
  };

  const scanTiles = [];
  for (let x = tileX(COUNTRY.lngMin, Z_SCAN); x <= tileX(COUNTRY.lngMax, Z_SCAN); x++)
    for (let y = tileY(COUNTRY.latMax, Z_SCAN); y <= tileY(COUNTRY.latMin, Z_SCAN); y++) scanTiles.push([x, y]);
  const scan = await getTiles(scanTiles, Z_SCAN);

  const k = 2 ** (Z_FINE - Z_SCAN), fineTiles = [];
  for (const key of Object.keys(scan.got)) {
    const [x, y] = key.split('/').map(Number);
    for (let dx = 0; dx < k; dx++) for (let dy = 0; dy < k; dy++) fineTiles.push([x * k + dx, y * k + dy]);
  }
  const fine = await getTiles(fineTiles, Z_FINE);
  // A tile that never answered would be a silent hole in the layer — better no update at all.
  if (scan.failed || fine.failed) {
    console.error(`${scan.failed + fine.failed} tiles never answered — keeping the old file, run again later.`);
    process.exit(1);
  }

  fs.writeFileSync(TILES_TMP, JSON.stringify({ z: Z_FINE, tiles: fine.got }));
  console.log(execFileSync('python3', [path.join(__dirname, 'ozon_zones_decode.py'), TILES_TMP]).toString().trim());
  const geo = JSON.parse(fs.readFileSync(OUT + '.tmp', 'utf8'));
  fs.unlinkSync(OUT + '.tmp');
  if (geo.features.length < MIN_SHAPES) {
    console.error(`Only ${geo.features.length} shapes came back — the site probably changed; keeping the old file.`);
    process.exit(1);
  }

  // Address + support money: the request the site itself makes when a place is clicked.
  let n = 0;
  for (const f of geo.features) {
    const p = f.properties;
    const info = await frame.evaluate(async ({ lat, lng, API }) => {
      for (let a = 0; a < 3; a++) {
        try {
          const r = await fetch(`${API}/task/creation-availability?location.lat=${lat}&location.lon=${lng}&layer=PvzGroup&withNearestPoints=true&isCis=false`, { credentials: 'include' });
          if (r.ok) return await r.json();
        } catch (e) {}
        await new Promise(r => setTimeout(r, 1500 * (a + 1)));
      }
      return null;
    }, { lat: p.lat, lng: p.lng, API });
    if (info) {
      const g = info.geocode || {};
      p.allowed = !!info.allowed;
      p.locality = g.locality || null;
      p.address = g.fullText || null;
      // Each option: Ozon pays `monthly` until `total` is reached, if you commit for `commit` months.
      p.support = ((((info.tariffs || {}).mb || {}).flexSupport) || [])
        .map(s => ({ monthly: s.support, total: s.totalSupport, commit: s.commitmentPeriod }));
    }
    process.stdout.write(`\r  details: ${++n}/${geo.features.length}`);
    await page.waitForTimeout(250);
  }
  console.log();
  await browser.close();

  geo.source = PAGE;
  geo.fetched = new Date().toISOString().slice(0, 10);
  fs.writeFileSync(OUT, JSON.stringify(geo));
  const b = geo.features.filter(f => f.properties.kind === 'building').length;
  console.log(`Ozon zones: ${b} buildings, ${geo.features.length - b} city zones → ${OUT}`);
})();
