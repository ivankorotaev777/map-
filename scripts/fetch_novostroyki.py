#!/usr/bin/env python3
"""Collect residential complexes (ЖК) in Tashkent city + region into data/novostroyki.geojson.

NOT part of the daily pipeline — new developments appear over months, not hours. Run this
by hand (or on a manual/quarterly workflow) when the data should be refreshed; build_map.py
only ever reads the committed file. Run scripts/fetch_zhk_signals.py afterwards: it keys its
caches by the yu_id written here.

Primary source is yangiuylar.uz's own API, which carries coordinates, completion date,
apartment count, storeys and price for every listed complex. Three more of its endpoints
are joined in client-side (the API ignores every server-side filter, so each is paginated
in full and matched on object_id): the developer, the apartment layouts (room mix) and the
infrastructure the developer declares nearby ("супермаркет — 5 минут пешком").

Sources: yangiuylar.uz (catalogue).
"""
import json, math, os, re, sys, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import date

OUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'novostroyki.geojson')
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
YU_BASE = "https://yangiuylar.uz/api"
# yangiuylar's own dictionary: 13 = Toshkent viloyati, 12 = Toshkent shahri. The city holds
# ~137 of the 180 listed complexes — leaving it out would have made the city look empty of
# new housing when it is the opposite. "Новый Ташкент" is filed under 13 (область).
YU_REGION_IDS = {13: 'область', 12: 'город'}
DEDUPE_M = 150
# Everything stays in the file: a complex handed over in 2022 is the MOST inhabited kind of
# ЖК, which is exactly what the negotiation registry wants. `recent` marks what the map's
# growth signal should still count (going up, or finished recently enough to drive demand).
RECENT_FROM_YEAR = 2023
# yangiuylar "place" categories that mean shopping. Used for the developer-declared
# "супермаркет в N минутах" flag — a weaker signal than OSM, but it covers complexes OSM
# has not mapped yet.
RETAIL_PLACE_IDS = {2, 4, 6, 12}      # Торговый центр, Базар, Маркет, Рынок
METRO_PLACE_ID = 1


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


def get_all(path):
    """Every row of a paginated endpoint, or None if any page failed.

    The API answers `limit` up to 100 and ignores every filter, so this is the only way to
    read it. Unpaginated reads are what left 164 of 172 complexes without a district: the
    default page holds 15 districts out of 205."""
    rows, page = [], 1
    while True:
        d = get(f"{YU_BASE}/{path}?limit=100&page={page}")
        if not d:
            return None
        rows.extend(d.get('data') or [])
        meta = d.get('meta') or {}
        if not meta.get('next'):
            return rows
        page = meta['next']
        time.sleep(1)


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


def district_from_address(address):
    """Fallback for the ~1% of complexes whose district_id is missing from the dictionary:
    the free-text address usually still names the district."""
    s = address or ''
    m = re.search(r'([А-ЯЁ][а-яё\-]+(?:ский|ий|ой))\s+район', s)
    if m:
        return f"{m.group(1)} район"
    m = re.search(r"([A-Za-z'ʻʼ‘’`]+)\s+tumani", s)
    if m:
        return f"{m.group(1)} tumani"
    return ''


def room_mix(rows):
    """Apartment layouts → how many of each size the developer lists. Counts layouts, not
    apartments: number_of_apartments is null on most rows."""
    mix = Counter()
    prices = []
    for r in rows:
        rooms = r.get('rooms')
        if isinstance(rooms, int) and rooms > 0:
            mix['4+' if rooms >= 4 else str(rooms)] += 1
        p = r.get('price')
        if isinstance(p, (int, float)) and p > 0:
            prices.append(p)
    return ({k: mix[k] for k in ('1', '2', '3', '4+') if mix[k]},
            min(prices) if prices else None)


def fetch_companies(ids):
    """Developer name and phone per company_id. One call each (~60); any failure just
    leaves them empty — a missing developer must not block the refresh."""
    out = {}
    for cid in sorted(ids):
        d = get(f"{YU_BASE}/company/{cid}") or {}
        out[cid] = {'name': (d.get('name') or '').strip(), 'phone': norm_phone(d.get('phone'))}
        time.sleep(0.5)
    return out


def norm_phone(p):
    """'998781228822' → '+998 78 122 88 22'; anything else is passed through as typed."""
    digits = re.sub(r'\D', '', str(p or ''))
    if len(digits) == 12 and digits.startswith('998'):
        return f"+{digits[:3]} {digits[3:5]} {digits[5:8]} {digits[8:10]} {digits[10:]}"
    if len(digits) == 9:
        return f"+998 {digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:]}"
    return (p or '').strip()


def fetch_nearby():
    """Developer-declared infrastructure per object_id, with the two dictionaries resolved:
    {object_id: [{category, name, minutes, on_foot}]}. None if any endpoint failed."""
    links = get_all('nearby-place')
    items = get_all('place-item')
    cats = get(f"{YU_BASE}/place?limit=50")
    if links is None or items is None or not cats:
        return None
    cat_name = {c['id']: (c.get('name_ru') or '').strip() for c in cats.get('data') or []}
    item = {i['id']: i for i in items}
    out = defaultdict(list)
    for l in links:
        it = item.get(l.get('place_item_id'))
        if not it:
            continue
        out[l['object_id']].append({
            'category': cat_name.get(it.get('place_id'), ''),
            'category_id': it.get('place_id'),
            'name': (it.get('name_ru') or it.get('name_uz') or '').strip(),
            'minutes': l.get('time'),
            'on_foot': bool(l.get('on_foot')),
        })
    return out


def _declared_min(places, cat_ids):
    mins = [p['minutes'] for p in places
            if p.get('category_id') in cat_ids and isinstance(p.get('minutes'), int)]
    return min(mins) if mins else None


def fetch_yangiuylar():
    districts = {}
    rows = get_all('district')
    if rows is None:
        print("  WARN: district dictionary unreachable — districts will come from addresses",
              file=sys.stderr)
    for x in rows or []:
        districts[x['id']] = (x.get('name_ru') or x.get('name_uz') or '').strip()

    objects = get_all('object')
    if objects is None:
        return None

    # The secondary endpoints enrich, they do not gate: on failure the fields stay empty.
    plannings = get_all('planning')
    if plannings is None:
        print("  WARN: /planning unreachable — room mix left empty", file=sys.stderr)
    by_obj = defaultdict(list)
    for p in plannings or []:
        by_obj[p.get('object_id')].append(p)
    nearby = fetch_nearby()
    if nearby is None:
        print("  WARN: /nearby-place unreachable — declared infrastructure left empty",
              file=sys.stderr)
        nearby = {}
    companies = fetch_companies({o.get('company_id') for o in objects if o.get('company_id')})

    today = date.today().isoformat()
    out = []
    for o in objects:
        if o.get('region_id') not in YU_REGION_IDS:
            continue
        # Coordinates are strings, and one entry uses a decimal comma ("41,28954").
        try:
            lat = float(str(o.get('latitude') or '').replace(',', '.'))
            lng = float(str(o.get('longitude') or '').replace(',', '.'))
        except ValueError:
            continue
        if not (40.0 < lat < 42.5 and 68.0 < lng < 71.0):
            continue
        year = year_of(o)
        quarter = o.get('completion_quarter')
        completion = (f"{quarter} кв {year}" if quarter and year
                      else (str(year) if year else ''))
        cdate = (o.get('completion_date') or '')[:10]
        # is_archive = the catalogue no longer sells it, i.e. handed over. A complex whose
        # completion date has passed but is still listed is also finished — the developer
        # just keeps selling the remainder.
        done = bool(o.get('is_archive')) or (bool(cdate) and cdate < today)
        status = 'done' if done else 'building'
        mix, price_min = room_mix(by_obj.get(o['id'], []))
        places = nearby.get(o['id'], [])
        address = (o.get('address') or '').strip()
        out.append({
            "yu_id": o['id'],
            "slug": o.get('slug') or '',
            "name": (o.get('name') or '').strip(),
            "developer": companies.get(o.get('company_id'), {}).get('name', ''),
            "developer_id": o.get('company_id'),
            "developer_phone": companies.get(o.get('company_id'), {}).get('phone', ''),
            # The complex's own sales line, as listed in the catalogue.
            "phone": norm_phone(o.get('phone')),
            "telegram": (o.get('telegram') or o.get('telegram_channel') or '').strip(),
            "district": districts.get(o.get('district_id')) or district_from_address(address),
            "address": address,
            "lat": lat, "lon": lng,
            "completion": completion,
            "completion_date": cdate,
            "year": year,
            "status": status,
            "recent": status == 'building' or (year is not None and year >= RECENT_FROM_YEAR),
            "apartments": o.get('number_of_apartments') or 0,
            "floors": o.get('number_of_storeys') or 0,
            "apartments_for_sale": o.get('apartments_for_sale'),
            "price_m2": o.get('price') or None,
            "price_min_m2": price_min,
            "room_mix": mix,
            "is_commercial": bool(o.get('is_commercial')),
            "verified": bool(o.get('verified')),
            "nearby": places,
            "declared_retail_min": _declared_min(places, RETAIL_PLACE_IDS),
            "declared_metro_min": _declared_min(places, {METRO_PLACE_ID}),
            "area": YU_REGION_IDS[o['region_id']],
            "source": "yangiuylar.uz",
            "url": f"https://yangiuylar.uz/novostroyka/{o['slug']}" if o.get('slug') else "",
            "coord_approx": False,
        })
    return out


def dedupe(items):
    """Same complex listed twice: close together and named alike. Keep the record with
    more fields filled in."""
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
        filled = lambda r: sum(1 for v in r.values() if v not in (None, '', 0, False, {}, []))
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
    print("  " + ", ".join(f"{k}: {v}" for k, v in Counter(i['area'] for i in items).items()))

    before = len(items)
    items = dedupe(items)
    print(f"  after dedupe: {len(items)} (dropped {before - len(items)})")

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
    } for i in sorted(items, key=lambda x: (x['area'], x['district'], x['name']))]

    out = {"type": "FeatureCollection",
           "attribution": "Каталог новостроек — yangiuylar.uz",
           "source": "yangiuylar.uz API, region_id 13 (Toshkent viloyati) + 12 (Toshkent shahri)",
           "fetched": date.today().isoformat(),
           "features": features}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    apts = sum(i['apartments'] for i in items)
    building = sum(1 for i in items if i['status'] == 'building')
    no_district = sum(1 for i in items if not i['district'])
    no_dev = sum(1 for i in items if not i['developer'])
    print(f"\nwrote {len(features)} complexes → {os.path.relpath(OUT_PATH)} "
          f"({os.path.getsize(OUT_PATH)//1024} KB)")
    print(f"  building: {building}, done: {len(items)-building}, recent: "
          f"{sum(1 for i in items if i['recent'])}, apartments total: {apts:,}")
    print(f"  without district: {no_district}, without developer: {no_dev}, "
          f"with room mix: {sum(1 for i in items if i['room_mix'])}, "
          f"with declared retail: {sum(1 for i in items if i['declared_retail_min'] is not None)}")


if __name__ == "__main__":
    main()
