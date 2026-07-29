#!/usr/bin/env python3
"""Rebuild data/poi_region.geojson from OpenStreetMap: markets, supermarkets, banks.

Scope is the Tashkent *region* admin area (OSM relation 196251) — Tashkent city itself is a
separate area and is deliberately excluded, so the big wholesale bazaars inside the city
(Chorsu, Kuylyuk, Abu Sakhiy) are NOT here.

Overpass is a shared free service that throttles and times out, so this never fails the
pipeline: mirrors are tried in turn, and if all of them are unreachable the previously
committed file is left untouched. A stale POI file is much better than a broken build.

Data © OpenStreetMap contributors, ODbL.
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error

AREA_ID = 3600196251           # OSM relation 196251 = Toshkent viloyati
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'poi_region.geojson')
HEADERS = {"User-Agent": "pvz-map/1.0 (github.com/ivankorotaev777/map-)"}
TIMEOUT = 200

# Chains publish their own geo-analysis before opening, so "a chain store is here" is the
# strongest demand signal in this dataset — worth flagging separately from a corner shop.
CHAINS = ["korzinka", "korzinka.uz", "havas", "makro", "корзинка", "хавас", "макро"]

QUERIES = {
    "market":      '( nwr["amenity"="marketplace"](area.a); );',
    "supermarket": '( nwr["shop"="supermarket"](area.a); );',
    "bank":        '( nwr["amenity"="bank"](area.a); );',
}


def overpass(query_body):
    """Run one Overpass query, trying each mirror. Returns elements or None."""
    q = f"[out:json][timeout:180];\narea({AREA_ID})->.a;\n{query_body}\nout center tags;"
    for url in ENDPOINTS:
        try:
            data = urllib.parse.urlencode({"data": q}).encode()
            req = urllib.request.Request(url, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read()).get("elements", [])
        except Exception as e:
            print(f"    {url.split('/')[2]}: {type(e).__name__} {e}", file=sys.stderr)
            time.sleep(2)
    return None


def coords_of(el):
    if el.get("type") == "node":
        return el.get("lon"), el.get("lat")
    c = el.get("center") or {}
    return c.get("lon"), c.get("lat")


def is_chain(tags):
    hay = " ".join(str(tags.get(k) or "") for k in ("brand", "name", "operator")).lower()
    return any(c in hay for c in CHAINS)


def to_feature(el, poi_type):
    lon, lat = coords_of(el)
    if lon is None or lat is None:
        return None
    tags = el.get("tags") or {}
    props = {"type": poi_type, "name": (tags.get("name") or "").strip()}
    if poi_type == "market":
        props["profile"] = tags.get("marketplace") or tags.get("market") or "general"
    if poi_type == "supermarket":
        props["brand"] = (tags.get("brand") or "").strip()
        props["chain"] = is_chain(tags)
    props["osm_id"] = str(el.get("id"))
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": props}


def dedupe(features):
    """Drop exact coordinate repeats, then same-type points closer than ~30 m."""
    seen = set()
    rounded = []
    for f in features:
        lon, lat = f["geometry"]["coordinates"]
        key = (f["properties"]["type"], round(lon, 5), round(lat, 5))
        if key in seen:
            continue
        seen.add(key)
        rounded.append(f)

    # 30 m at this latitude ≈ 0.00027° lat and ≈ 0.00036° lon; compare in degrees to keep
    # this dependency-free, the error at 30 m is irrelevant for deduping.
    LAT_EPS, LON_EPS = 0.00027, 0.00036
    kept = []
    for f in rounded:
        lon, lat = f["geometry"]["coordinates"]
        t = f["properties"]["type"]
        dup = any(g["properties"]["type"] == t
                  and abs(g["geometry"]["coordinates"][0] - lon) < LON_EPS
                  and abs(g["geometry"]["coordinates"][1] - lat) < LAT_EPS
                  for g in kept)
        if not dup:
            kept.append(f)
    return kept


def main():
    collected = []
    failures = []
    for poi_type, body in QUERIES.items():
        print(f"Querying {poi_type}…")
        elements = overpass(body)
        if elements is None:
            print(f"  {poi_type}: all Overpass mirrors failed", file=sys.stderr)
            failures.append(poi_type)
            continue
        feats = [f for f in (to_feature(e, poi_type) for e in elements) if f]
        print(f"  {poi_type}: {len(elements)} elements → {len(feats)} points")
        collected.extend(feats)
        time.sleep(2)   # be a good citizen on a shared free service

    if failures:
        # A partial rebuild would silently delete whole categories from the map.
        print(f"\nWARN: {', '.join(failures)} unavailable — keeping the previous "
              f"{os.path.relpath(OUT_PATH)} untouched", file=sys.stderr)
        if os.path.exists(OUT_PATH):
            prev = json.load(open(OUT_PATH))
            print(f"  previous file kept: {len(prev.get('features', []))} points")
            return
        print("ERROR: no previous file to fall back to", file=sys.stderr)
        sys.exit(1)

    features = dedupe(collected)
    features.sort(key=lambda f: (f["properties"]["type"], f["properties"]["name"]))
    out = {"type": "FeatureCollection",
           "attribution": "© OpenStreetMap contributors (ODbL)",
           "source": f"Overpass, area {AREA_ID} (Toshkent viloyati), Tashkent city excluded",
           "features": features}

    # Guard against a technically-successful but empty answer wiping the layer.
    if len(features) < 50 and os.path.exists(OUT_PATH):
        prev = json.load(open(OUT_PATH))
        if len(prev.get("features", [])) > len(features) * 2:
            print(f"WARN: only {len(features)} points vs {len(prev['features'])} before — "
                  f"looks like a bad answer, keeping the previous file", file=sys.stderr)
            return

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    counts = {}
    for f in features:
        counts[f["properties"]["type"]] = counts.get(f["properties"]["type"], 0) + 1
    chains = sum(1 for f in features if f["properties"].get("chain"))
    print(f"\nwrote {len(features)} points → {os.path.relpath(OUT_PATH)} "
          f"({os.path.getsize(OUT_PATH)//1024} KB)")
    print(f"  {counts} | chain supermarkets: {chains} | deduped: {len(collected)-len(features)}")


if __name__ == "__main__":
    main()
