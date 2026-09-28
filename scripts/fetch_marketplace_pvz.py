#!/usr/bin/env python3
"""Competitor pickup points in Tashkent city: Ozon, Wildberries, Yandex Market.

NOT part of the daily pipeline — competitors open points over weeks, and Yandex Maps rate-limits
bursts of requests. Run by hand (after scripts/fetch_ozon_site.js); build_map.py only reads the
committed data/marketplace_pvz.json.

Every brand is checked against two independent sources, so a point that exists only in one of
them is visible as such on the map instead of being silently trusted or dropped:
  * the marketplace's own list —
      Wildberries: the public pickup-point file its site loads (static-basket-01.wbbasket.ru);
      Ozon: its pickup map on ozon.uz, collected by scripts/fetch_ozon_site.js;
      Yandex Market: has no public list for Uzbekistan, so Yandex Maps (Yandex's own
      directory) is the only source there;
  * Yandex Maps — the brand's "chain" page (all branches of one company), read at many map
    centres because a single page only ever returns the ~25 branches nearest to its centre.
Only points inside the Tashkent city outline are kept.
"""
import json, math, os, re, sys, time, urllib.request, urllib.error

from shapely.geometry import Point, shape

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
OUT = os.path.join(DATA_DIR, 'marketplace_pvz.json')
OZON_SITE = os.path.join(DATA_DIR, 'ozon_site_points.json')
PLACES = os.path.join(DATA_DIR, 'places_region.geojson')
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Yandex Maps chain ids (from the /chain/<slug>/<id>/ links on a search for the brand).
YANDEX_CHAINS = {
    'ozon': ('ozon', '100688546620'),
    'wb':   ('wildberries', '113208944148'),
    'ym':   ('market_yandex_go', '190177870270'),
}
SAME_POINT_M = 120   # a site point and a Yandex point closer than this are the same desk
# Map centres for the Yandex sweep: a 7×7 grid over the city, ~4 km apart.
CENTRES = [(round(41.19 + i * 0.035, 3), round(69.13 + j * 0.05, 3))
           for i in range(7) for j in range(7)]


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'ru'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def haversine_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- Wildberries (own site)
def fetch_wb_site():
    """WB's site loads one file with every pickup point it has, versioned v1, v2, …"""
    best = None
    for v in range(10, 31):
        url = f"https://static-basket-01.wbbasket.ru/vol0/data/all-poo-fr-v{v}.json"
        try:
            urllib.request.urlopen(urllib.request.Request(url, method='HEAD',
                                   headers={'User-Agent': UA}), timeout=20)
            best = url
        except urllib.error.HTTPError:
            if best: break
    if not best:
        raise RuntimeError("WB pickup-point file not found")
    groups = json.loads(get(best, timeout=180))
    uz = next(g['items'] for g in groups if g.get('country') == 'uz')
    print(f"WB site: {best.rsplit('/', 1)[1]}, {len(uz)} points in Uzbekistan")
    return [{'id': str(p['id']), 'lat': p['coordinates'][0], 'lng': p['coordinates'][1],
             'address': p.get('address')} for p in uz]


# ---------------------------------------------------------------- Yandex Maps chain pages
def yandex_items(html):
    m = re.search(r'<script type="application/json" class="state-view">(.*?)</script>', html, re.S)
    if not m:
        return None   # rate-limit page ("limited") — caller waits and retries
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get('coordinates') and o.get('title') and (o.get('address') or o.get('fullAddress')):
                found[o.get('id') or o['title'] + str(o['coordinates'])] = o
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(json.loads(m.group(1)))
    return found


def fetch_yandex_chain(slug, cid):
    base = f"https://yandex.uz/maps/10335/tashkent/chain/{slug}/{cid}/"
    urls = [base, base + '?page=2', base + '?page=3']
    urls += [f"{base}?ll={lng}%2C{lat}&z=14" for lat, lng in CENTRES]
    found = {}
    for url in urls:
        for attempt in range(4):
            try:
                items = yandex_items(get(url).decode())
            except Exception as e:
                items = None; print(f"  ! {e}", file=sys.stderr)
            if items is not None: break
            time.sleep(30 * (attempt + 1))
        found.update(items or {})
        time.sleep(2.5)
    out = []
    for o in found.values():
        if o.get('status') not in (None, 'open'):
            continue   # closed / temporarily closed branches are not competition today
        lng, lat = o['coordinates']
        out.append({'id': str(o.get('id')), 'lat': lat, 'lng': lng,
                    'address': o.get('fullAddress') or o.get('address')})
    print(f"Yandex Maps {slug}: {len(out)} open branches")
    return out


# ---------------------------------------------------------------- merge
def merge(site, yandex):
    """→ [lat, lng, address, source]; source 3 = both, 1 = site only, 2 = Yandex only."""
    used, rows = set(), []
    for s in site:
        best, best_d = None, SAME_POINT_M
        for i, y in enumerate(yandex):
            if i in used: continue
            d = haversine_m(s['lat'], s['lng'], y['lat'], y['lng'])
            if d < best_d: best, best_d = i, d
        if best is not None: used.add(best)
        rows.append([round(s['lat'], 6), round(s['lng'], 6), s['address'], 3 if best is not None else 1])
    for i, y in enumerate(yandex):
        if i not in used:
            rows.append([round(y['lat'], 6), round(y['lng'], 6), y['address'], 2])
    return rows


def main():
    city = shape(json.load(open(PLACES))['city_outline'])
    in_city = lambda p: city.contains(Point(p['lng'], p['lat']))

    ozon_site = json.load(open(OZON_SITE))['points'] if os.path.exists(OZON_SITE) else []
    if not ozon_site:
        print("⚠️  no data/ozon_site_points.json — run scripts/fetch_ozon_site.js first", file=sys.stderr)
    sites = {'ozon': ozon_site, 'wb': fetch_wb_site(), 'ym': []}

    brands = {}
    for key, (slug, cid) in YANDEX_CHAINS.items():
        site = [p for p in sites[key] if in_city(p)]
        yx = [p for p in fetch_yandex_chain(slug, cid) if in_city(p)]
        rows = merge(site, yx)
        both = sum(r[3] == 3 for r in rows)
        print(f"  {key}: site {len(site)}, Yandex {len(yx)} → {len(rows)} points "
              f"(both {both}, site only {sum(r[3] == 1 for r in rows)}, Yandex only {sum(r[3] == 2 for r in rows)})")
        brands[key] = rows

    json.dump({'fetched': time.strftime('%Y-%m-%d'), 'brands': brands},
              open(OUT, 'w'), ensure_ascii=False, separators=(',', ':'))
    print(f"→ {OUT}")


if __name__ == '__main__':
    main()
