#!/usr/bin/env python3
"""Fetch Uzum hex zones (MVT tiles) for Tashkent + region, decode to H3 sets."""
import urllib.request, gzip, math, json, time, os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import mapbox_vector_tile as mvt

ZOOM = 12
HEADERS = {"User-Agent":"Mozilla/5.0","Origin":"https://promo.uzum.uz","Referer":"https://promo.uzum.uz/"}

# Bounding boxes to cover: Tashkent + region AND Samarkand city
BBOXES = [
    ("Tashkent+region", 40.30, 42.20, 68.20, 70.80),  # lat_min, lat_max, lng_min, lng_max
    ("Samarkand",       39.55, 39.75, 66.80, 67.10),
]

def lng_to_tile_x(lng, z): return int((lng + 180.0) / 360.0 * (1 << z))
def lat_to_tile_y(lat, z):
    rad = math.radians(lat)
    return int((1 - math.log(math.tan(rad) + 1/math.cos(rad)) / math.pi) / 2 * (1 << z))

tiles = set()
for name, lat_min, lat_max, lng_min, lng_max in BBOXES:
    x_min = lng_to_tile_x(lng_min, ZOOM); x_max = lng_to_tile_x(lng_max, ZOOM)
    y_min = lat_to_tile_y(lat_max, ZOOM); y_max = lat_to_tile_y(lat_min, ZOOM)
    n_before = len(tiles)
    for x in range(x_min, x_max+1):
        for y in range(y_min, y_max+1):
            tiles.add((x, y))
    print(f"  {name}: +{len(tiles)-n_before} tiles")
tiles = sorted(tiles)
print(f"tiles: {len(tiles)}")

def fetch(x,y):
    url = f"https://api-wms.uzum.uz/franchise/api/v1/map/locations/{ZOOM}/{x}/{y}"
    # A tile that errors out is retried: giving up at once silently dropped every zone in it.
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r: raw = r.read()
            if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
            if len(raw) < 8: return None          # genuinely empty tile
            return mvt.decode(raw)
        except Exception:
            time.sleep(1 + attempt)
    return None

rec, forb = set(), set()
done = 0; t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(fetch, x, y) for x,y in tiles]
    for fut in as_completed(futs):
        done += 1
        result = fut.result()
        if not result: continue
        for layer_name, target in (('recommended', rec), ('not_allowed', forb)):
            layer = result.get(layer_name)
            if not layer: continue
            for f in layer.get('features', []):
                h = (f.get('properties') or {}).get('h3')
                if h: target.add(h)
        if done % 100 == 0:
            print(f"  {done}/{len(tiles)} rec={len(rec)} forb={len(forb)}")

print(f"\nDone: rec={len(rec)} forb={len(forb)}")

# ---- Guard against a short answer from Uzum -------------------------------------------
# On 2026-11-22 Uzum returned 592 recommended zones instead of the usual ~1700 (the count
# normally moves by a few percent a day). The map, the shortlist, the ЖК registry and the
# competitor table were all rebuilt from that third and disagreed with Uzum's own site.
# So: the last accepted answer is kept in data/uzum_zones.json, and a layer that suddenly
# shrinks by more than DROP_LIMIT is not believed — yesterday's layer is used instead and
# the map + Telegram digest say so. If the low number persists for HOLD_DAYS runs it is
# taken as Uzum's real decision and accepted, so the map can never freeze for good.
SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'uzum_zones.json')
DROP_LIMIT = 0.30     # a layer losing more than 30% of its hexes overnight is suspect
HOLD_DAYS = 2         # how many runs in a row yesterday's layer is kept before giving in
MIN_BASE = 100        # layers smaller than this are too small to judge by percentage

today = datetime.now(timezone(timedelta(hours=5))).strftime('%Y-%m-%d')   # Tashkent
try: snap = json.load(open(SNAPSHOT_PATH))
except Exception: snap = None

fresh = {'recommended': rec, 'not_allowed': forb}
suspect = {}
if snap:
    for layer, cells in fresh.items():
        before = len(snap.get(layer) or [])
        if before >= MIN_BASE and len(cells) < before * (1 - DROP_LIMIT):
            suspect[layer] = {'got': len(cells), 'expected': before}

out = {'recommended': sorted(rec), 'not_allowed': sorted(forb)}
if suspect and snap.get('suspect_days', 0) < HOLD_DAYS:
    for layer, info in suspect.items():
        other = fresh['not_allowed' if layer == 'recommended' else 'recommended']
        # A hex Uzum now reports in the other layer must not end up in both.
        out[layer] = sorted(set(snap[layer]) - other)
        print(f"WARNING: {layer} fell {info['expected']} → {info['got']}; "
              f"keeping the layer from {snap['date']}")
    out['stale'] = {'since': snap['date'], 'layers': suspect}
    snap['suspect_days'] = snap.get('suspect_days', 0) + 1
    json.dump(snap, open(SNAPSHOT_PATH, 'w'), separators=(',', ':'))
else:
    if suspect:
        print(f"NOTE: low counts persisted {HOLD_DAYS} runs — accepting them as real: {suspect}")
        out['accepted_drop'] = suspect
    json.dump({'date': today, 'suspect_days': 0,
               'recommended': out['recommended'], 'not_allowed': out['not_allowed']},
              open(SNAPSHOT_PATH, 'w'), separators=(',', ':'))

json.dump(out, open('/tmp/uzum_zones.json','w'), indent=2)

# Also pull delivery points
print("Fetching delivery_points...")
import urllib.request
req = urllib.request.Request("https://api-wms.uzum.uz/franchise/api/v1/map/delivery_points", headers=HEADERS)
data = urllib.request.urlopen(req, timeout=20).read()
open('/tmp/uzum_delivery_points.json','wb').write(data)
import json as J
d = J.loads(data)
print(f"delivery_points: {len(d['features'])}")
