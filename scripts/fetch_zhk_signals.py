#!/usr/bin/env python3
"""Two caches that tell whether a residential complex (ЖК) is already alive:

  data/uybor_listings.json   — every active apartment listing on uybor.uz for Tashkent city
                               and region, rent and sale, with coordinates. A complex with
                               rentals around it has residents; one with only sales from the
                               developer does not.
  data/zhk_osm_retail.json   — shops, pharmacies and cafés OpenStreetMap knows within 150 m
                               and 350 m of each complex (keyed by the yu_id that
                               scripts/fetch_novostroyki.py writes — run that first). One
                               Overpass query for both areas, radii computed locally.

NOT part of the daily pipeline — residents and shops arrive over months, and both sources
dislike bursts of requests. Run by hand about monthly; build_map.py only reads the committed
files and degrades to "нет данных" when they are missing.

    python3 scripts/fetch_zhk_signals.py              # both
    python3 scripts/fetch_zhk_signals.py --only osm   # when Overpass had a bad day

uybor is the only listing source used: it carries lat/lng on every row and needs ~120 calls
for the whole city. joymee has the listings too, but coordinates sit one request deep per
listing (~30k calls) — not worth it for a monthly signal. OLX blocks non-browser clients.

Sources: uybor.uz (api.uybor.uz); © OpenStreetMap contributors (ODbL) via Overpass.
"""
import json, math, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import date

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
ZHK_PATH = os.path.join(DATA_DIR, 'novostroyki.geojson')
UYBOR_OUT = os.path.join(DATA_DIR, 'uybor_listings.json')
OSM_OUT = os.path.join(DATA_DIR, 'zhk_osm_retail.json')
HEADERS = {"User-Agent": "pvz-map/1.0 (github.com/ivankorotaev777/map-)"}

# ---- uybor -------------------------------------------------------------------------------
UYBOR_URL = "https://api.uybor.uz/api/v1/listings"
# uybor's own numbering (checked against coordinates): 13 = Toshkent shahri, 12 = viloyati.
# The region holds ~25 apartment listings in total, so oblast evidence is thin by nature.
UYBOR_REGIONS = {13: 'город', 12: 'область'}
UYBOR_CATEGORY = 7                 # Квартира
UYBOR_OPS = {'rent': 0, 'sale': 1}
UYBOR_FIELDS = ["id", "lat", "lng", "op", "isNew", "priceUsd", "room", "createdAt", "region"]
UYBOR_MIN_ROWS = 2000              # the city alone had ~11.8k on 2026-10-05

# ---- Overpass ----------------------------------------------------------------------------
# lz4 first: on 2026-10-05 the main host answered most queries with 504 while lz4 took
# half a second. kumi tends to hang rather than refuse, hence the short timeout.
ENDPOINTS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
TIMEOUT = 400
# Same two OSM areas as fetch_poi.py: Toshkent shahri + Toshkent viloyati.
AREAS = (3602216724, 3600196251)
RADII_M = (150, 350)               # "у подъезда" / "рядом"
FOOD_AMENITIES = ("cafe", "restaurant", "fast_food")
CONVENIENCE_SHOPS = ("convenience", "grocery", "greengrocer")
# One query for every shop in both areas (~13k objects, ~3 MB, ~12 s), then the per-complex
# radii are computed here. Per-complex `around` queries were tried first: the mirrors
# accept one of them at a time and 504 on anything bigger, so 178 of them took an hour of
# retries on a bad day. One request is also kinder to a shared free service.


def get_json(url, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries:
                print(f"    {url[:90]}…: {type(e).__name__} {e}", file=sys.stderr)
                return None
            time.sleep(3 * (i + 1))


def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def keep_previous(path, why):
    print(f"WARN: {why} — keeping the previous file", file=sys.stderr)
    if not os.path.exists(path):
        print(f"ERROR: no previous {os.path.basename(path)} to fall back to", file=sys.stderr)
        sys.exit(1)


# ============================================================================== uybor
def fetch_uybor():
    """→ list of compact rows, or None if the city pull failed."""
    rows, failed = {}, False
    for region, area in UYBOR_REGIONS.items():
        for op, op_code in UYBOR_OPS.items():
            page, got, total = 1, 0, None
            while True:
                q = urllib.parse.urlencode({
                    'limit': 100, 'page': page, 'operationType__eq': op,
                    'category__eq': UYBOR_CATEGORY, 'region__eq': region})
                d = get_json(f"{UYBOR_URL}?{q}")
                if d is None:
                    failed = failed or region == 13      # the city is what matters
                    break
                results = d.get('results') or []
                total = d.get('total') if total is None else total
                for r in results:
                    lat, lng = r.get('lat'), r.get('lng')
                    if lat is None or lng is None:
                        continue
                    if not r.get('isActive') or r.get('moderationStatus') != 'approved':
                        continue
                    price = (r.get('prices') or {}).get('usd') or r.get('price')
                    rows[r['id']] = [r['id'], round(lat, 6), round(lng, 6), op_code,
                                     1 if r.get('isNewBuilding') else 0,
                                     int(price) if isinstance(price, (int, float)) and price > 0 else None,
                                     str(r.get('room') or ''), (r.get('createdAt') or '')[:10],
                                     region]
                got += len(results)
                if len(results) < 100 or (total is not None and got >= total):
                    break
                page += 1
                time.sleep(0.5)
            print(f"  uybor {area} {op}: {got} of {total}")
            time.sleep(1)
    if failed:
        return None

    # Reposts: the same flat listed again under a new id. Keep the newest.
    seen, out = {}, []
    for r in sorted(rows.values(), key=lambda r: r[7], reverse=True):
        key = (round(r[1], 5), round(r[2], 5), r[3], r[6], r[5])
        if key in seen:
            continue
        seen[key] = True
        out.append(r)
    print(f"  uybor: {len(rows)} active listings, {len(out)} after repost dedupe")
    return out


def write_uybor(rows):
    if rows is None:
        return keep_previous(UYBOR_OUT, "uybor city pull failed")
    if len(rows) < UYBOR_MIN_ROWS and os.path.exists(UYBOR_OUT):
        prev = json.load(open(UYBOR_OUT))
        if len(prev.get('rows', [])) > len(rows) * 2:
            return keep_previous(UYBOR_OUT, f"only {len(rows)} rows vs {len(prev['rows'])} before")
    out = {"fetched": date.today().isoformat(),
           "source": "api.uybor.uz/api/v1/listings, category 7 (Квартира), regions 13 (город) + 12 (область)",
           "fields": UYBOR_FIELDS, "rows": rows}
    with open(UYBOR_OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print(f"wrote {len(rows)} listings → {os.path.relpath(UYBOR_OUT)} ({os.path.getsize(UYBOR_OUT)//1024} KB)")


# ============================================================================== Overpass
def overpass(body, rounds=4):
    """Run one Overpass query, trying each mirror, then waiting and trying them all again.
    The public mirrors answer 429 / 504 under load — a minute's pause clears it, giving up
    does not. Returns elements or None."""
    q = f"[out:json][timeout:300];\n{body}\nout center tags;"
    for rnd in range(rounds):
        for url in ENDPOINTS:
            try:
                data = urllib.parse.urlencode({"data": q}).encode()
                req = urllib.request.Request(url, data=data, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return json.loads(r.read()).get("elements", [])
            except Exception as e:
                print(f"    {url.split('/')[2]}: {type(e).__name__} {e}", file=sys.stderr)
                time.sleep(5)
        if rnd < rounds - 1:
            pause = 45 * (rnd + 1)
            print(f"    all mirrors refused — waiting {pause} s", file=sys.stderr)
            time.sleep(pause)
    return None


def coords_of(el):
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    c = el.get("center") or {}
    return c.get("lat"), c.get("lon")


def classify(tags):
    shop = tags.get("shop")
    amenity = tags.get("amenity")
    kinds = []
    if shop:
        kinds.append("shops")
        if shop == "supermarket":
            kinds.append("supermarket")
        elif shop in CONVENIENCE_SHOPS:
            kinds.append("convenience")
    if amenity == "pharmacy":
        kinds.append("pharmacy")
    elif amenity in FOOD_AMENITIES:
        kinds.append("food")
    return kinds


def fetch_osm_retail(points):
    """points: [(yu_id, lat, lng)] → {yu_id: {"150": {...}, "350": {...}}} or None."""
    amen = "|".join(("pharmacy",) + FOOD_AMENITIES)
    body = ";".join(f'area({a})->.a{i}' for i, a in enumerate(AREAS)) + ";\n(\n" + "\n".join(
        f'nwr["shop"](area.a{i});\nnwr["amenity"~"^({amen})$"](area.a{i});'
        for i in range(len(AREAS))) + "\n);"
    elements = overpass(body)
    if elements is None:
        return None
    # Keep only what we can place and classify, as (lat, lng, kinds, name).
    objs = []
    for el in elements:
        lat, lng = coords_of(el)
        if lat is None:
            continue
        tags = el.get("tags") or {}
        kinds = classify(tags)
        if kinds:
            objs.append((lat, lng, kinds, (tags.get("name") or "").strip()))
    print(f"  osm: {len(elements)} objects from Overpass, {len(objs)} placed shops/cafés/pharmacies")

    r_max = max(RADII_M)
    result = {}
    for yu_id, lat, lng in points:
        buckets = {str(r): {"shops": 0, "supermarket": 0, "convenience": 0,
                            "pharmacy": 0, "food": 0, "names": []} for r in RADII_M}
        for olat, olng, kinds, name in objs:
            if abs(olat - lat) > 0.004 or abs(olng - lng) > 0.005:
                continue             # cheap bbox before the trig: 13k objects × 178 complexes
            d = haversine_m(lat, lng, olat, olng)
            if d > r_max:
                continue
            for r in RADII_M:
                if d > r:
                    continue
                b = buckets[str(r)]
                for k in kinds:
                    b[k] += 1
                if name and name not in b["names"] and len(b["names"]) < 5:
                    b["names"].append(name)
        result[str(yu_id)] = buckets
    return result


def write_osm(by_id):
    if by_id is None:
        return keep_previous(OSM_OUT, "Overpass failed on every mirror")
    out = {"fetched": date.today().isoformat(),
           "attribution": "© OpenStreetMap contributors (ODbL)",
           "radius_m": list(RADII_M), "by_yu_id": by_id}
    with open(OSM_OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    with_shops = sum(1 for v in by_id.values() if v[str(max(RADII_M))]["shops"])
    print(f"wrote {len(by_id)} complexes → {os.path.relpath(OSM_OUT)}, "
          f"{with_shops} have a shop within {max(RADII_M)} m")


def main():
    only = sys.argv[sys.argv.index('--only') + 1] if '--only' in sys.argv else None
    if only in (None, 'uybor'):
        print("Fetching uybor.uz…")
        write_uybor(fetch_uybor())
    if only in (None, 'osm'):
        zhk = json.load(open(ZHK_PATH))
        points = []
        for f in zhk.get('features', []):
            p = f.get('properties') or {}
            if f.get('geometry', {}).get('type') != 'Point' or p.get('yu_id') is None:
                continue
            lng, lat = f['geometry']['coordinates']
            points.append((p['yu_id'], lat, lng))
        print(f"Fetching OSM retail around {len(points)} complexes…")
        write_osm(fetch_osm_retail(points))


if __name__ == "__main__":
    main()
