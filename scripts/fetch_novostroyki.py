#!/usr/bin/env python3
"""Collect residential complexes (ЖК) in Tashkent region into data/novostroyki.geojson.

NOT part of the daily pipeline — new developments appear over months, not hours. Run this
by hand (or on a manual/quarterly workflow) when the data should be refreshed; build_map.py
only ever reads the committed file.

Primary source is yangiuylar.uz's own API, which carries coordinates, completion date,
apartment count, storeys and price for every listed complex.

Sources: yangiuylar.uz (catalogue).
"""
import json, math, os, re, sys, time, urllib.request, urllib.error

OUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'novostroyki.geojson')
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
YU_BASE = "https://yangiuylar.uz/api"
# yangiuylar's own dictionary: 13 = Toshkent viloyati, 12 = Toshkent shahri. The city holds
# 134 of the 175 listed complexes — leaving it out would have made the city look empty of
# new housing when it is the opposite.
YU_REGION_IDS = {13: 'область', 12: 'город'}
DEDUPE_M = 150
# Keep what is still going up, plus anything finished recently enough that the residents
# have already moved in — an old complex says nothing about where demand is heading.
KEEP_YEARS = {2023, 2024, 2025, 2026}


def get(url, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries:
                print(f"    {url}: {type(e).__name__} {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))


def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def norm_name(s):
    return re.sub(r'[^a-zа-я0-9]', '', (s or '').lower())


def year_of(o):
    """completion_year is filled inconsistently (a 2028 date carrying year 2025), so the
    date wins whenever it is present."""
    d = o.get('completion_date')
    if d:
        m = re.match(r'(\d{4})', str(d))
        if m:
            return int(m.group(1))
    y = o.get('completion_year')
    try:
        return int(y)
    except (TypeError, ValueError):
        return None


def fetch_yangiuylar():
    districts = {}
    d = get(f"{YU_BASE}/district")
    if d:
        for x in d.get('data') or []:
            districts[x['id']] = x.get('name_ru') or x.get('name_uz') or ''

    objects, page = [], 1
    while True:
        d = get(f"{YU_BASE}/object?limit=100&page={page}")
        if not d:
            return None
        objects.extend(d.get('data') or [])
        meta = d.get('meta') or {}
        if not meta.get('next'):
            break
        page = meta['next']
        time.sleep(1)

    out = []
    for o in objects:
        if o.get('region_id') not in YU_REGION_IDS:
            continue
        lat, lng = o.get('latitude'), o.get('longitude')
        if lat in (None, '') or lng in (None, ''):
            continue
        year = year_of(o)
        quarter = o.get('completion_quarter')
        completion = (f"{quarter} кв {year}" if quarter and year
                      else (str(year) if year else ''))
        out.append({
            "name": (o.get('name') or '').strip(),
            "district": districts.get(o.get('district_id'), ''),
            "lat": float(lat), "lon": float(lng),
            "completion": completion,
            "year": year,
            # Everything the catalogue still shows for sale is under construction; archived
            # entries are the ones that have handed over.
            "status": "done" if o.get('is_archive') else "building",
            "apartments": o.get('number_of_apartments') or 0,
            "floors": o.get('number_of_storeys') or 0,
            "price_m2": o.get('price') or None,
            "area": YU_REGION_IDS[o['region_id']],
            "source": "yangiuylar.uz",
            "url": f"https://yangiuylar.uz/object/{o.get('slug')}" if o.get('slug') else "",
            "coord_approx": False,
        })
    return out


def dedupe(items):
    """Same complex from two catalogues: close together and named alike. Keep the record
    with more fields filled in."""
    kept = []
    for it in items:
        dup_i = None
        for i, k in enumerate(kept):
            if haversine_m(it['lat'], it['lon'], k['lat'], k['lon']) > DEDUPE_M:
                continue
            a, b = norm_name(it['name']), norm_name(k['name'])
            if a and b and (a == b or a in b or b in a):
                dup_i = i
                break
        if dup_i is None:
            kept.append(it)
            continue
        filled = lambda r: sum(1 for v in r.values() if v not in (None, '', 0, False))
        if filled(it) > filled(kept[dup_i]):
            kept[dup_i] = it
    return kept


def main():
    print("Fetching yangiuylar.uz…")
    items = fetch_yangiuylar()
    if items is None:
        print("WARN: yangiuylar unreachable — keeping the previous file", file=sys.stderr)
        if os.path.exists(OUT_PATH):
            prev = json.load(open(OUT_PATH))
            print(f"  previous file kept: {len(prev.get('features', []))} complexes")
            return
        print("ERROR: no previous file to fall back to", file=sys.stderr)
        sys.exit(1)
    import collections as _c
    print("  " + ", ".join(f"{k}: {v}" for k, v in
                           _c.Counter(i['area'] for i in items).items()))

    items = dedupe(items)
    before = len(items)
    items = [i for i in items
             if i['status'] == 'building' or (i['year'] in KEEP_YEARS)]
    print(f"  after dedupe + recency filter: {len(items)} (dropped {before - len(items)})")

    if len(items) < 5 and os.path.exists(OUT_PATH):
        prev = json.load(open(OUT_PATH))
        if len(prev.get('features', [])) > len(items) * 2:
            print(f"WARN: only {len(items)} left vs {len(prev['features'])} before — "
                  f"looks wrong, keeping the previous file", file=sys.stderr)
            return

    features = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(i['lon'], 6), round(i['lat'], 6)]},
        "properties": {k: v for k, v in i.items() if k not in ('lat', 'lon')},
    } for i in sorted(items, key=lambda x: (x['district'], x['name']))]

    out = {"type": "FeatureCollection",
           "attribution": "Каталог новостроек — yangiuylar.uz",
           "source": "yangiuylar.uz API, region_id 13 (Toshkent viloyati) + 12 (Toshkent shahri)",
           "features": features}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    apts = sum(i['apartments'] for i in items)
    building = sum(1 for i in items if i['status'] == 'building')
    print(f"\nwrote {len(features)} complexes → {os.path.relpath(OUT_PATH)} "
          f"({os.path.getsize(OUT_PATH)//1024} KB)")
    print(f"  building: {building}, done: {len(items)-building}, apartments total: {apts:,}")


if __name__ == "__main__":
    main()
