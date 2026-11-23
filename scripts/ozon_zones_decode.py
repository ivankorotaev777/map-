#!/usr/bin/env python3
"""Helper for scripts/fetch_ozon_zones.js — not run on its own.

Turns the raw map tiles the browser downloaded into data/ozon_zones.geojson: decodes the
vector tiles, glues together shapes that were cut at tile edges, and marks each shape as a
single building or a whole-city zone. Addresses and support sums are added afterwards by the
calling script (they need the browser session).

Needs: pip install mapbox-vector-tile shapely
"""
import json, base64, gzip, math, os, sys
import mapbox_vector_tile as mvt
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

TILES_PATH = sys.argv[1]
OUT = os.path.join(os.path.dirname(__file__), '..', 'data', 'ozon_zones.geojson')
CITY_ZONE_M2 = 1_000_000   # Ozon paints either one building or a whole town; nothing in between

src = json.load(open(TILES_PATH))
Z, n = src['z'], 2 ** src['z']

def lnglat(x, y, px, py, ext):
    fx, fy = x + px / ext, y + 1 - py / ext          # tile y runs up from the bottom edge
    return (fx / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n)))))

shapes = []
for key, b64 in src['tiles'].items():
    x, y = map(int, key.split('/'))
    raw = base64.b64decode(b64)
    if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
    for layer in mvt.decode(raw).values():
        ext = layer['extent']
        for f in layer['features']:
            if f['properties'].get('country-code') != 'UZB': continue   # tiles spill into Kazakhstan
            g = f['geometry']
            conv = lambda c: lnglat(x, y, c[0], c[1], ext) if isinstance(c[0], (int, float)) else [conv(i) for i in c]
            s = shape({'type': g['type'], 'coordinates': conv(g['coordinates'])}).buffer(0)
            if not s.is_empty: shapes.append(s)

# A hair of overlap (≈1 cm, square corners) so halves of one shape cut by a tile edge fuse.
merged = unary_union([s.buffer(1e-7, join_style=2) for s in shapes])
parts = list(merged.geoms) if merged.geom_type == 'MultiPolygon' else [merged]

def round_coords(c):
    return [round(c[0], 6), round(c[1], 6)] if isinstance(c[0], (int, float)) else [round_coords(i) for i in c]

features = []
for p in parts:
    area = p.area * 111_000 * 111_000 * math.cos(math.radians(p.centroid.y))
    city = area > CITY_ZONE_M2
    p = p.simplify(5e-5 if city else 3e-6)            # ≈5 m for a town outline, ≈30 cm for a building
    pt = p.representative_point()                     # guaranteed inside, unlike the centroid
    geom = mapping(p); geom['coordinates'] = round_coords(geom['coordinates'])
    features.append({'type': 'Feature', 'geometry': geom,
                     'properties': {'kind': 'city' if city else 'building',
                                    'lat': round(pt.y, 6), 'lng': round(pt.x, 6),
                                    'area_m2': round(area)}})
features.sort(key=lambda f: (f['properties']['kind'], -f['properties']['lat']))
json.dump({'type': 'FeatureCollection', 'features': features}, open(OUT + '.tmp', 'w'), ensure_ascii=False)
print(f"decoded: {len(shapes)} raw shapes → {sum(f['properties']['kind']=='building' for f in features)} buildings, "
      f"{sum(f['properties']['kind']=='city' for f in features)} city zones")
