#!/usr/bin/env python3
"""Fetch Uzum hex zones (MVT tiles) for Tashkent + region, decode to H3 sets."""
import urllib.request, gzip, math, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import mapbox_vector_tile as mvt

ZOOM = 12
HEADERS = {"User-Agent":"Mozilla/5.0","Origin":"https://promo.uzum.uz","Referer":"https://promo.uzum.uz/"}
LAT_MIN, LAT_MAX = 40.30, 42.20
LNG_MIN, LNG_MAX = 68.20, 70.80

def lng_to_tile_x(lng, z): return int((lng + 180.0) / 360.0 * (1 << z))
def lat_to_tile_y(lat, z):
    rad = math.radians(lat)
    return int((1 - math.log(math.tan(rad) + 1/math.cos(rad)) / math.pi) / 2 * (1 << z))

x_min = lng_to_tile_x(LNG_MIN, ZOOM); x_max = lng_to_tile_x(LNG_MAX, ZOOM)
y_min = lat_to_tile_y(LAT_MAX, ZOOM); y_max = lat_to_tile_y(LAT_MIN, ZOOM)
tiles = [(x,y) for x in range(x_min,x_max+1) for y in range(y_min,y_max+1)]
print(f"tiles: {len(tiles)}")

def fetch(x,y):
    url = f"https://api-wms.uzum.uz/franchise/api/v1/map/locations/{ZOOM}/{x}/{y}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r: raw = r.read()
        if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
        if len(raw) < 8: return None
        return mvt.decode(raw)
    except Exception: return None

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
json.dump({
    "recommended": sorted(rec),
    "not_allowed": sorted(forb),
}, open('/tmp/uzum_zones.json','w'), indent=2)

# Also pull delivery points
print("Fetching delivery_points...")
import urllib.request
req = urllib.request.Request("https://api-wms.uzum.uz/franchise/api/v1/map/delivery_points", headers=HEADERS)
data = urllib.request.urlopen(req, timeout=20).read()
open('/tmp/uzum_delivery_points.json','wb').write(data)
import json as J
d = J.loads(data)
print(f"delivery_points: {len(d['features'])}")
