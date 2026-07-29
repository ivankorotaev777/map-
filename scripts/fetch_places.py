#!/usr/bin/env python3
"""Settlement names for the region + the Tashkent city outline.

Both are needed to describe a candidate location in words a person can act on: "3.2 km from
Chirchiq, 28 km from the city limits" beats a pair of coordinates. The city outline is the
edge distances are measured from — not the city centre, since what matters is how far out
of town a courier has to drive.

NOT part of the daily pipeline: settlement names and an administrative boundary do not
change week to week. Run by hand; build_map.py reads the committed file.

Data © OpenStreetMap contributors (ODbL).
"""
import json, os, sys, time, urllib.request, urllib.parse

OUT = os.path.join(os.path.dirname(__file__), '..', 'data', 'places_region.geojson')
REGION_AREA = 3600196251      # Toshkent viloyati
CITY_REL = 2216724            # Toshkent shahri
ENDPOINTS = ["https://overpass-api.de/api/interpreter",
             "https://lz4.overpass-api.de/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter"]
HEADERS = {"User-Agent": "pvz-map/1.0 (github.com/ivankorotaev777/map-)"}
PLACE_KINDS = ("city", "town", "village", "hamlet", "suburb", "neighbourhood")


def overpass(query):
    for url in ENDPOINTS:
        try:
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(url, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.loads(r.read()).get("elements", [])
        except Exception as e:
            print(f"    {url.split('/')[2]}: {type(e).__name__} {e}", file=sys.stderr)
            time.sleep(2)
    return None


def main():
    print("Settlements in the region…")
    kinds = "|".join(PLACE_KINDS)
    els = overpass(f'[out:json][timeout:180];area({REGION_AREA})->.a;'
                   f'( node["place"~"^({kinds})$"](area.a); );out tags center;')
    if els is None:
        print("WARN: Overpass unavailable — keeping the previous file", file=sys.stderr)
        if os.path.exists(OUT):
            prev = json.load(open(OUT))
            print(f"  kept {len(prev.get('features', []))} places")
            return
        sys.exit(1)
    places = []
    for el in els:
        t = el.get('tags') or {}
        name = t.get('name:ru') or t.get('name') or ''
        if not name or el.get('lat') is None:
            continue
        places.append({"type": "Feature",
                       "geometry": {"type": "Point",
                                    "coordinates": [round(el['lon'], 6), round(el['lat'], 6)]},
                       "properties": {"name": name,
                                      "name_uz": t.get('name') or '',
                                      "kind": t.get('place'),
                                      "osm_id": el.get('id')}})
    print(f"  {len(places)} settlements")

    print("Tashkent city outline…")
    city = overpass(f'[out:json][timeout:180];rel({CITY_REL});out geom;')
    city_geom = None
    if city:
        from shapely.geometry import LineString, MultiPolygon, mapping
        from shapely.ops import linemerge, polygonize
        lines = []
        for m in (city[0].get('members') or []):
            if m.get('type') != 'way' or m.get('role') not in ('outer', ''):
                continue
            pts = [(p['lon'], p['lat']) for p in (m.get('geometry') or []) if p]
            if len(pts) >= 2:
                lines.append(LineString(pts))
        polys = [p for p in polygonize(linemerge(lines)) if p.is_valid and p.area > 0]
        if polys:
            g = polys[0] if len(polys) == 1 else MultiPolygon(polys)
            city_geom = mapping(g.simplify(0.001, preserve_topology=True))
            print(f"  outline captured ({len(polys)} ring(s))")
    if city_geom is None:
        print("  WARN: city outline unavailable", file=sys.stderr)
        if os.path.exists(OUT):
            prev = json.load(open(OUT))
            city_geom = prev.get('city_outline')
            if city_geom:
                print("  reusing the previous outline")

    if len(places) < 50 and os.path.exists(OUT):
        prev = json.load(open(OUT))
        if len(prev.get('features', [])) > len(places) * 2:
            print(f"WARN: only {len(places)} places — keeping the previous file", file=sys.stderr)
            return

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({"type": "FeatureCollection",
                   "attribution": "© OpenStreetMap contributors (ODbL)",
                   "city_outline": city_geom,
                   "features": places}, f, ensure_ascii=False)
    print(f"\nwrote {len(places)} places → {os.path.relpath(OUT)} "
          f"({os.path.getsize(OUT)//1024} KB), city outline: {'yes' if city_geom else 'NO'}")


if __name__ == "__main__":
    main()
