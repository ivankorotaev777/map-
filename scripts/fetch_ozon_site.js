#!/usr/bin/env node
/* Ozon pickup points in Tashkent, straight from Ozon's own pickup map (ozon.uz/geo).
 *
 * NOT part of the daily pipeline — competitors open points over weeks, and Ozon's site sits
 * behind an anti-bot check that only a real browser passes. Run by hand, then run
 * scripts/fetch_marketplace_pvz.py, which merges this file with Wildberries and Yandex Maps:
 *
 *   npm i --no-save playwright && node scripts/fetch_ozon_site.js
 *
 * Uses the installed Google Chrome (channel 'chrome'); the bundled headless Chromium is what
 * the anti-bot check turns away.
 *
 * How it works: the map on /geo/tashkent-337323/ asks its own backend for the pins inside the
 * visible rectangle (POST with a viewport). We ask the same question for a grid of small
 * rectangles covering the city, then open each pin for its address.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const OUT = path.join(__dirname, '..', 'data', 'ozon_site_points.json');
const GEO_PAGE = 'https://www.ozon.uz/geo/tashkent-337323/';
// Slightly wider than the city so edge points are not cut; the merge step clips to the outline.
const BBOX = { latMin: 41.16, latMax: 41.44, lngMin: 69.09, lngMax: 69.48 };
const STEP = { lat: 0.05, lng: 0.075 };   // at zoom 14 a cell this size never clusters pins

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome',
                                          args: ['--disable-blink-features=AutomationControlled'] });
  const page = await (await browser.newContext({ locale: 'ru-RU', viewport: { width: 1400, height: 1000 } })).newPage();
  await page.goto(GEO_PAGE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(8000);   // let the anti-bot redirect settle and set its cookies

  const points = await page.evaluate(async ({ BBOX, STEP }) => {
    const HEADERS = { accept: 'application/json', 'content-type': 'application/json', 'x-o3-app-name': 'dweb_client' };
    const MSID = crypto.randomUUID(), GSID = crypto.randomUUID();
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const widget = (j, prefix) => {
      const ws = (j && j.widgetStates) || {};
      const k = Object.keys(ws).find(k => k.startsWith(prefix));
      return k ? JSON.parse(ws[k]) : null;
    };

    const pins = {};
    for (let lat = BBOX.latMin; lat < BBOX.latMax; lat += STEP.lat) {
      for (let lng = BBOX.lngMin; lng < BBOX.lngMax; lng += STEP.lng) {
        const cLat = lat + STEP.lat / 2, cLng = lng + STEP.lng / 2;
        const inner = `/geo/tashkent-337323/?azimuth=0&lat=${cLat}&long=${cLng}&msid=${MSID}&nfr=t&pid=4&pv=7&pxlw=917.000000&tab=pp`;
        const body = {
          isGeolocationOnInit: false, mapInfo: { geoSessionId: GSID },
          geolocation: { coords: {}, isAvailable: false }, form: { addressTail: '' },
          map: { viewport: { leftBottom: { latitude: lat, longitude: lng },
                             rightTop: { latitude: lat + STEP.lat, longitude: lng + STEP.lng } },
                 zoom: 14, previousCoordinates: { latitude: cLat, longitude: cLng } },
        };
        const r = await fetch('/api/entrypoint-api.bx/page/json/v2?url=' + encodeURIComponent(inner),
                              { method: 'POST', body: JSON.stringify(body), headers: HEADERS });
        const map = widget(await r.json().catch(() => null), 'addressEditMap');
        const objs = (((map || {}).markerBundle || {}).mapObjectCollection || {}).mapObjects || [];
        for (const o of objs) {
          const m = /pp=(\d+)/.exec(o.actionLink || '');
          if (!m) continue;
          pins[m[1]] = { id: m[1], lat: o.coordinates.latitude, lng: o.coordinates.longitude,
                         icon: (o.image || '').split('/').pop() };
        }
        await sleep(700);
      }
    }

    for (const p of Object.values(pins)) {
      const inner = `/geo/tashkent-337323/?azimuth=0&msid=${MSID}&nfr=t&pid=5&pp=${p.id}&pv=7&pxlw=917.000000&tab=pp`;
      const r = await fetch('/api/entrypoint-api.bx/page/json/v2?url=' + encodeURIComponent(inner), { headers: HEADERS });
      const j = await r.json().catch(() => null);
      const head = widget(j, 'pageHeaderLight');
      const det = widget(j, 'addressEditPickUpDetailFullWeb');
      p.title = head ? head.title : null;   // "Пункт Ozon: Узбекистан, Ташкент, ..."
      try { p.address = det.location.address[0].cell.centerBlock.subtitle.text; } catch { p.address = null; }
      await sleep(500);
    }
    return Object.values(pins);
  }, { BBOX, STEP });

  await browser.close();
  // The map also shows partner pickup desks (Kazakh post over the border). Only Ozon's own
  // points belong on a Tashkent competitor layer.
  const own = points.filter(p => (p.title || '').startsWith('Пункт Ozon'));
  if (own.length < 20) {
    console.error(`Only ${own.length} Ozon points came back — the site probably changed; keeping the old file.`);
    process.exit(1);
  }
  fs.writeFileSync(OUT, JSON.stringify({
    source: GEO_PAGE, fetched: new Date().toISOString().slice(0, 10),
    points: own.map(({ id, lat, lng, address }) => ({ id, lat, lng, address })),
  }, null, 1));
  console.log(`Ozon site: ${points.length} pins, ${own.length} own points → ${OUT}`);
})();
