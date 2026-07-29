#!/usr/bin/env python3
"""Build self-contained interactive Leaflet map. All listings with coords → on map.
Zone filtering is done client-side via the left panel."""
import json, os, math, re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import h3

zones = json.load(open('/tmp/uzum_zones.json'))
listings_all = json.load(open('/tmp/joymee_classified.json'))
listings = [r for r in listings_all if r.get('latitude') and r.get('longitude')]

# Static Tashkent grid: hex IDs T-XXXX, population, dist to metro, metro stations
# This file is shipped in the repo (built once via scripts/build_grid.py)
GRID_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'tashkent_grid.json')
tashkent_grid = json.load(open(GRID_PATH))

# Samarkand grid — display-only overlay (no population, no scoring).
# User just wants Uzum zones + numbered hex labels in Samarkand.
SAMARKAND_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'samarkand_grid.json')
samarkand_grid = json.load(open(SAMARKAND_PATH)) if os.path.exists(SAMARKAND_PATH) else {'hexes': {}}

# Expert picks — each expert gets their own layer of selected hexes.
EXPERTS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'expert_picks.json')
expert_picks = json.load(open(EXPERTS_PATH))
print(f"Expert picks: " + ", ".join(f"{e['name']}={len(e['hexes'])}" for e in expert_picks.values()))
print(f"Tashkent grid: {len(tashkent_grid['hexes'])} hexes, {len(tashkent_grid['metro_stations'])} metro stations")
print(f"Samarkand grid: {len(samarkand_grid.get('hexes', {}))} hexes")

# Uzum per-hex population (cached by scripts/fetch_uzum_population.py — the API only answers
# one hex per request, so the cache is what keeps the daily job from making ~6k calls).
POP_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'uzum_population.json')
uzum_population = json.load(open(POP_PATH)) if os.path.exists(POP_PATH) else {}
if not uzum_population:
    print(f"  WARN: no Uzum population data ({POP_PATH} missing) — density layer will be empty")

# Points of attraction from OpenStreetMap (scripts/fetch_poi.py): markets, supermarkets,
# banks across the Tashkent *region*. Tashkent city is a separate OSM area and is excluded,
# so the big city wholesale bazaars are not in here.
POI_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'poi_region.geojson')
poi_data = json.load(open(POI_PATH)) if os.path.exists(POI_PATH) else {'features': []}
poi_features = poi_data.get('features', [])
if not poi_features:
    print(f"  WARN: no POI data ({POI_PATH} missing) — market/shop/bank layers will be empty")

# How far around a hex we count points of attraction. One place to change it.
POI_RADIUS_KM = 3.0
# New housing pulls customers from further out than a corner shop does.
ZHK_RADIUS_KM = 5.0

# Static, hand-refreshed datasets (scripts/fetch_housing_official.py, fetch_novostroyki.py).
# Deliberately NOT rebuilt by the daily workflow: the statistics office publishes quarterly
# and new complexes appear over months, so the daily job just reads what is committed.
def _load_static(name, default):
    p = os.path.join(os.path.dirname(__file__), '..', 'data', name)
    if not os.path.exists(p):
        print(f"  WARN: {name} missing — that layer will be empty")
        return default
    return json.load(open(p, encoding='utf-8'))

housing_stats = _load_static('housing_commissioned.json', {'districts': {}})
district_geo = _load_static('districts_region.geojson', {'features': []})
novostroyki = _load_static('novostroyki.geojson', {'features': []})

def _territory_key(name):
    """Same city-vs-district aware key as fetch_housing_official.py — five names exist as
    both a city and a district, so the suffix has to survive normalisation."""
    s = (name or '').lower().replace('ʻ', "'").replace('‘', "'").replace('’', "'").replace('`', "'")
    kind = 'shahar' if ('shahri' in s or 'shahar' in s or 'город' in s) else 'tuman'
    s = re.sub(r"[^a-z']", '', s)
    for w in ('tumani', 'shahri', 'shahar', 'tuman'):
        s = s.replace(w, '')
    return f"{s}|{kind}"

uzum_dp = json.load(open('/tmp/uzum_delivery_points.json'))
uzum_pvz_points = []
for f in uzum_dp.get('features', []):
    c = f.get('geometry', {}).get('coordinates')
    if not c or len(c) < 2: continue
    lng, lat = c[0], c[1]
    if 40.0 < lat < 42.5 and 68.0 < lng < 71.0:
        uzum_pvz_points.append([lat, lng])
print(f"existing Uzum PVZ in Tashkent area: {len(uzum_pvz_points)}")

# ---- Compute hex scoring (dynamic, based on today's data) ----
def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

# Pre-index listings by H3 cell for fast lookup
listings_by_h3 = defaultdict(list)
for r in listings:
    try:
        cell = h3.latlng_to_cell(r['latitude'], r['longitude'], tashkent_grid['h3_resolution'])
        listings_by_h3[cell].append(r)
    except: pass

rec_set = set(zones['recommended'])
forb_set = set(zones['not_allowed'])

# --- Points of attraction near each hex -------------------------------------------------
POI_RADIUS_M = POI_RADIUS_KM * 1000
_poi_index = [(f['geometry']['coordinates'][1], f['geometry']['coordinates'][0],
               f['properties']['type'], bool(f['properties'].get('chain')))
              for f in poi_features
              if f.get('geometry', {}).get('type') == 'Point']

def poi_stats(lat, lng):
    """Counts within POI_RADIUS_KM plus distance to the nearest of each kind, in km.

    Nearest deliberately scans every POI, not just those inside the radius — "the closest
    chain store is 7 km away" tells you something that a bare "0 nearby" does not.
    """
    counts = {'markets': 0, 'supermarkets': 0, 'chain': 0, 'banks': 0}
    nearest = {'market': None, 'chain': None, 'bank': None}
    for plat, plng, ptype, chain in _poi_index:
        d = haversine_m(lat, lng, plat, plng)
        if d <= POI_RADIUS_M:
            if ptype == 'market': counts['markets'] += 1
            elif ptype == 'supermarket':
                counts['supermarkets'] += 1
                if chain: counts['chain'] += 1
            elif ptype == 'bank': counts['banks'] += 1
        key = 'chain' if (ptype == 'supermarket' and chain) else ptype
        if key in nearest and (nearest[key] is None or d < nearest[key]):
            nearest[key] = d
    return counts, nearest

ZHK_RADIUS_M = ZHK_RADIUS_KM * 1000
_zhk_index = [(f['geometry']['coordinates'][1], f['geometry']['coordinates'][0],
               f['properties'].get('apartments') or 0)
              for f in novostroyki.get('features', [])
              if f.get('geometry', {}).get('type') == 'Point']

def zhk_fields(lat, lng):
    n = apts = 0
    for zlat, zlng, a in _zhk_index:
        if haversine_m(lat, lng, zlat, zlng) <= ZHK_RADIUS_M:
            n += 1
            apts += a
    return {'zhk_5km': n, 'zhk_apts_5km': apts}

def poi_fields(lat, lng):
    c, n = poi_stats(lat, lng)
    def km(v): return round(v / 1000, 2) if v is not None else None
    return {
        'markets_3km': c['markets'],
        'supermarkets_3km': c['supermarkets'],
        'chain_3km': c['chain'],
        'banks_3km': c['banks'],
        'nearest_market_km': km(n['market']),
        'nearest_chain_km': km(n['chain']),
        'nearest_bank_km': km(n['bank']),
    }

# Score = population density only.
# Grey (low) → Yellow (medium) → Bright green (high), normalized by P95.
print(f"\nComputing hex scores (population density)…")

raw_metrics = {}
for tid, info in tashkent_grid['hexes'].items():
    lat, lng = info['lat'], info['lng']
    h3_cell = info['h3']
    here = listings_by_h3.get(h3_cell, [])
    pop = info['population']
    n_listings = len(here)
    n_first = sum(1 for r in here if 'street_facing' in (r.get('tags') or []))
    frac_first = (n_first / n_listings) if n_listings else 0
    d_metro = info['dist_metro_m'] or 99999
    if uzum_pvz_points:
        d_pvz = min(haversine_m(lat, lng, p[0], p[1]) for p in uzum_pvz_points)
    else:
        d_pvz = 99999
    z = 'unknown'
    if h3_cell in rec_set: z = 'recommended'
    elif h3_cell in forb_set: z = 'not_allowed'
    raw_metrics[tid] = {
        'h3': h3_cell,                     # needed for click→cell→tid lookup in JS
        'lat': lat, 'lng': lng,
        'pop': pop, 'd_pvz': round(d_pvz),
        'n_listings': n_listings, 'n_first': n_first,
        'frac_first': round(frac_first, 3),
        'd_metro': d_metro,
        'zone': z,
        **poi_fields(lat, lng),
        **zhk_fields(lat, lng),
    }

# Two-metric scoring (50/50): population density × rental price per m².
#   - Higher population → more customers
#   - Higher rent → wealthier area (more spending power for online orders)
# Mahalla hexes (population=0 by detection) stay at score=0 regardless of price.
POP_FLOOR = 50

# --- Component 1: population percentile rank ---
populated = [(tid, m['pop']) for tid, m in raw_metrics.items() if m['pop'] >= POP_FLOOR]
populated.sort(key=lambda x: x[1])
n_pop_hex = len(populated)
pop_pct_by_tid = {}
for i, (tid, _) in enumerate(populated):
    pop_pct_by_tid[tid] = (i + 0.5) / n_pop_hex
print(f"  populated hexes (pop≥{POP_FLOOR}): {n_pop_hex}, empty hexes: {len(raw_metrics)-n_pop_hex}")

# --- Component 2: price percentile rank (loaded from /tmp/price_per_hex.json) ---
price_per_hex_path = '/tmp/price_per_hex.json'
price_data = {}
if os.path.exists(price_per_hex_path):
    price_data = json.load(open(price_per_hex_path))
    print(f"  price data: {len(price_data)} hexes")
else:
    print(f"  WARN: no price data ({price_per_hex_path} missing) — using population only")

# Price percentile rank
priced = [(tid, info['price_per_m2']) for tid, info in price_data.items()]
priced.sort(key=lambda x: x[1])
n_priced = len(priced)
price_pct_by_tid = {}
for i, (tid, _) in enumerate(priced):
    price_pct_by_tid[tid] = (i + 0.5) / n_priced if n_priced else 0

# Weights
W_POP = 0.5
W_PRICE = 0.5

hex_scores = {}
for tid, m in raw_metrics.items():
    n_pop = pop_pct_by_tid.get(tid, 0.0)
    n_price = price_pct_by_tid.get(tid, 0.0)
    has_price = tid in price_pct_by_tid

    if n_pop == 0:
        # Mahalla / empty / below pop floor → always 0
        score = 0.0
    elif not has_price:
        # No price data — score uses only population, weighted 50%
        # (max possible 0.5, so they appear in middle of color scale at most)
        score = W_POP * n_pop
    else:
        score = W_POP * n_pop + W_PRICE * n_price

    hex_scores[tid] = {
        **m,
        'score': round(score, 4),
        'price_per_m2': price_data.get(tid, {}).get('price_per_m2'),
        'price_sample_size': price_data.get(tid, {}).get('sample_size'),
        'components': {
            'population': round(n_pop, 3),
            'price':      round(n_price, 3) if has_price else None,
        }
    }

# Rank (1 = best)
ranked = sorted(hex_scores.items(), key=lambda x: -x[1]['score'])
for rank, (tid, info) in enumerate(ranked, 1):
    info['rank'] = rank
print(f"  hexes scored: {len(hex_scores)}, top score: {ranked[0][1]['score']}, bottom: {ranked[-1][1]['score']}")
print(f"  top 5: " + ", ".join(f"{tid}({h['score']})" for tid, h in ranked[:5]))

# ---- AI recommendation: spatially diversified top picks ----
# Goal: 30 hexes that are good per scoring AND avoid existing PVZ AND spread across the city.
AI_TARGET = 30
AI_SCORE_MIN = 0.5
AI_MIN_DIST_PVZ_M = 500       # don't recommend within 500m of existing PVZ
AI_SUPPRESSION_RADIUS_M = 600 # don't put two AI picks within 600m of each other

candidates = []
for tid, info in hex_scores.items():
    if info['zone'] == 'not_allowed': continue
    if info['score'] < AI_SCORE_MIN: continue
    if info['pop'] < 100: continue
    if info['d_pvz'] < AI_MIN_DIST_PVZ_M: continue
    if not info.get('price_per_m2'): continue  # need price data
    candidates.append((tid, info))

candidates.sort(key=lambda x: -x[1]['score'])
print(f"\nAI candidates after filters: {len(candidates)} (target picks: {AI_TARGET})")

ai_picks = []
suppressed_locations = []  # [(lat, lng), ...]
for tid, info in candidates:
    lat, lng = info['lat'], info['lng']
    # Check distance to already-picked
    too_close = False
    for (plat, plng) in suppressed_locations:
        if haversine_m(lat, lng, plat, plng) < AI_SUPPRESSION_RADIUS_M:
            too_close = True; break
    if too_close: continue
    ai_picks.append(tid)
    suppressed_locations.append((lat, lng))
    if len(ai_picks) >= AI_TARGET: break

print(f"AI picks (after diversification): {len(ai_picks)}")
print(f"  sample: {', '.join(ai_picks[:5])}")

# Inject as virtual expert
expert_picks['ai'] = {
    'name': 'AI рекомендация',
    'color': '#10b981',
    'emoji': '🤖',
    'hexes': ai_picks,
    'auto_generated': True,
}

TAG_META = [
    ("street_facing",    "1-я линия",            "🛣"),
    ("retail_shop",      "Магазин",              "🛒"),
    ("mall_in",          "Внутри ТЦ / БЦ",       "🏬"),
    ("cafe_restaurant",  "Кафе/ресторан",        "🍽"),
    ("warehouse_prod",   "Склад/производство",   "📦"),
    ("medical",          "Медицина",             "⚕"),
    ("beauty_service",   "Красота/салон",        "💅"),
    ("gym_fitness",      "Фитнес",               "🏋"),
    ("education",        "Учебный центр",        "🎓"),
    ("showroom",         "Шоурум/мебельный",     "🛋"),
    ("hotel_hostel",     "Гостиница/хостел",     "🏨"),
    ("office",           "Офис",                 "💼"),
    ("standalone_bldg",  "Отд. здание",          "🏢"),
    ("basement_floor",   "Подвал/цоколь",        "🕳"),
    ("ground_floor",     "1 этаж",               "🪟"),
    ("universal",        "Универсал",            "🔁"),
    ("pvz_explicit",     "Под ПВЗ (явно)",       "📮"),
    ("hookah",           "Кальянная",            "💨"),
]

def cells_to_geojson(cells, zone_type):
    out = []
    for cell in cells:
        boundary = h3.cell_to_boundary(cell)
        ring = [[lng, lat] for (lat, lng) in boundary]; ring.append(ring[0])
        out.append({"type":"Feature","geometry":{"type":"Polygon","coordinates":[ring]},
                    "properties":{"h3":cell,"type":zone_type}})
    return out

rec_features = cells_to_geojson(zones['recommended'], 'recommended')
forb_features = cells_to_geojson(zones['not_allowed'], 'not_allowed')
print(f"recommended polygons: {len(rec_features)}")
print(f"not_allowed polygons: {len(forb_features)}")

H3_RES = h3.get_resolution(zones['recommended'][0])
rec_set = set(zones['recommended']); forb_set = set(zones['not_allowed'])

def usd_total(r):
    p = r.get('price'); a = r.get('area_m2'); cur = r.get('currency')
    if p is None: return None
    try: p = float(p)
    except: return None
    if cur != 2: return None
    if a:
        try: a = float(a)
        except: a = None
    if a and p <= 60 and a >= 15: return p * a
    return p

def zone_of(r):
    try: cell = h3.latlng_to_cell(r['latitude'], r['longitude'], H3_RES)
    except: return 'unknown'
    if cell in rec_set: return 'recommended'
    if cell in forb_set: return 'not_allowed'
    return 'unknown'

# ALL listings with coords → on the map. Zone filtering done client-side.
points = []
zone_counter = {'recommended':0,'not_allowed':0,'unknown':0}
for r in listings:
    z = zone_of(r)
    zone_counter[z] += 1
    # joymee.uz web listings are broken (redirect to app landing) — so we embed the listing
    # info directly in the popup: full description + seller contact. Photos are no longer
    # embeddable (joymee serves 10-minute pre-signed URLs), so imgs stays empty and the
    # gallery block is skipped rather than rendered broken.
    points.append({
        "id": r['id'],
        "lat": r['latitude'], "lng": r['longitude'],
        "title": r['title'], "district": r['district_name'],
        "address": r['address_line'],
        "price": r['price'], "currency": r['currency'], "price_usd": usd_total(r),
        "area": r['area_m2'],
        "floor": r.get('floor_number'), "floors_count": r.get('floors_count'),
        "phone": r['phone_number'],
        "seller_name": r.get('seller_name'),
        "desc": (r.get('description') or ''),             # full description
        "zone": z, "tags": r.get('tags') or [], "primary": r.get('primary') or 'other',
        "created_at": r.get('created_at'),
        # What is around this particular address — the thing you actually decide on.
        # [markets, supermarkets, chain, banks, nearestMarketKm, nearestChainKm, nearestBankKm]
        "poi": (lambda c, n: [c['markets'], c['supermarkets'], c['chain'], c['banks'],
                              round(n['market']/1000, 2) if n['market'] is not None else None,
                              round(n['chain']/1000, 2) if n['chain'] is not None else None,
                              round(n['bank']/1000, 2) if n['bank'] is not None else None]
                )(*poi_stats(r['latitude'], r['longitude'])),
    })
print(f"\nlistings with coords: {len(points)}")
print(f"  recommended: {zone_counter['recommended']}")
print(f"  not_allowed: {zone_counter['not_allowed']}")
print(f"  unknown/white: {zone_counter['unknown']}")

districts = sorted(set(p['district'] for p in points if p['district']))

html_doc = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<title>Joymee × Uzum PVZ — карта вариантов</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
<style>
  html,body { margin:0; padding:0; height:100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color:#222; }
  #map { position:absolute; top:0; bottom:0; left:340px; right:0; }
  #panel { position:absolute; left:0; top:0; bottom:0; width:340px;
    background:#fafafa; border-right:1px solid #ddd; padding:14px 16px; overflow-y:auto; box-sizing:border-box; }
  h1 { font-size:16px; margin:0 0 4px; }
  .sub { color:#666; font-size:12px; margin-bottom:14px; }
  .filter { margin-bottom:14px; }
  .filter label { display:block; font-size:11px; font-weight:600; color:#555; text-transform:uppercase; letter-spacing:.4px; margin-bottom:4px; }
  .filter input, .filter select { width:100%; box-sizing:border-box; padding:6px 8px; border:1px solid #ccc; border-radius:4px; font-size:13px; background:#fff; }
  .range { display:flex; gap:6px; align-items:center; }
  .range input { width:100%; }
  .legend-item { display:flex; align-items:center; gap:8px; font-size:13px; margin-bottom:4px; }
  .swatch { width:14px; height:14px; border-radius:50%; border:1.5px solid #fff; box-shadow:0 0 0 1px rgba(0,0,0,.2); }
  .hex-swatch { width:14px; height:14px; border:1px solid #888; }
  .stat { font-size:12px; color:#666; margin-bottom:10px; padding:8px; background:#fff; border-radius:6px; border:1px solid #e7e7e7; }
  .stat b { color:#222; font-size:14px; }
  .popup-img { width:100%; height:180px; object-fit:cover; border-radius:6px; display:block; margin-bottom:6px; background:#eee; }
  .popup-row { font-size:13px; margin-bottom:3px; }
  .popup-row b { color:#555; }
  .leaflet-popup-content { width:320px !important; margin:10px 14px; max-height:70vh; overflow-y:auto; }
  /* Photo gallery */
  .popup-gallery { position:relative; margin-bottom:8px; }
  .popup-gallery .gal-prev, .popup-gallery .gal-next {
    position:absolute; top:50%; transform:translateY(-50%);
    background:rgba(0,0,0,.55); color:#fff; border:0; width:32px; height:32px;
    border-radius:50%; cursor:pointer; font-size:20px; line-height:32px; padding:0;
    display:flex; align-items:center; justify-content:center;
  }
  .popup-gallery .gal-prev { left:6px; }
  .popup-gallery .gal-next { right:6px; }
  .popup-gallery .gal-count {
    position:absolute; bottom:10px; right:10px;
    background:rgba(0,0,0,.6); color:#fff; padding:2px 8px; border-radius:10px;
    font-size:11px; font-weight:600;
  }
  /* Description block */
  .popup-desc { font-size:12px; color:#444; background:#f9fafb; padding:6px 8px; border-radius:4px; margin:6px 0; border-left:3px solid #e5e7eb; }
  .popup-desc .desc-body { max-height:180px; overflow-y:auto; }
  .popup-desc .desc-toggle { display:inline-block; margin-top:4px; color:#2563eb; font-size:11px; text-decoration:none; cursor:pointer; }
  .popup-desc .desc-toggle:hover { text-decoration:underline; }
  .leaflet-popup-content h3 { margin:6px 0 6px; font-size:14px; line-height:1.3; }
  .leaflet-popup-content a.tel { color:#0066cc; text-decoration:none; font-weight:600; }
  .leaflet-popup-content a.url { display:inline-block; margin-top:6px; padding:5px 10px; background:#7000ff; color:#fff; border-radius:4px; text-decoration:none; font-size:12px; }
  .zone-tag { display:inline-block; padding:1px 6px; border-radius:3px; font-size:11px; font-weight:600; }
  .zone-recommended { background:rgba(112,0,255,.15); color:#5a00cc; }
  .zone-not_allowed { background:rgba(139,142,153,.2); color:#666; }
  .zone-unknown     { background:rgba(0,0,0,.06); color:#777; }
  details { margin-top:10px; }
  details summary { cursor:pointer; font-size:12px; color:#555; }
  .counts { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
  .counts span { background:#fff; border:1px solid #ddd; padding:2px 6px; border-radius:3px; font-size:11px; }
  .tag-chips { display:flex; flex-wrap:wrap; gap:4px; max-height:170px; overflow-y:auto; padding:4px; background:#fff; border:1px solid #ddd; border-radius:4px; }
  .tag-chip { display:inline-flex; align-items:center; gap:3px; font-size:11px; padding:3px 7px; border-radius:11px;
    background:#f0f0f0; border:1px solid #ddd; cursor:pointer; user-select:none; transition:all .15s ease; }
  .tag-chip:hover { background:#e5e5e5; }
  .tag-chip.active { background:#7000ff; color:#fff; border-color:#5a00cc; font-weight:600; }
  .tag-chip .count { font-size:9px; opacity:.7; }
  .popup-tags { display:flex; flex-wrap:wrap; gap:3px; margin-top:4px; }
  .popup-tag { display:inline-flex; align-items:center; gap:2px; font-size:10px; padding:1px 5px; border-radius:8px;
    background:#f0f0f0; color:#444; border:1px solid #ddd; }
  .popup-tag.primary { background:#7000ff; color:#fff; border-color:#5a00cc; font-weight:600; }
  .zone-check { display:flex; align-items:center; gap:8px; font-size:13px; font-weight:400; text-transform:none; letter-spacing:0; margin-bottom:4px; cursor:pointer; padding:4px 6px; border-radius:4px; }
  .zone-check:hover { background:#f0f0f0; }
  .zone-check input { width:auto; }
</style>
</head>
<body>
<div id="panel">
  <div style="display:flex; align-items:flex-start; gap:8px; margin-bottom:4px;">
    <div style="flex:1; min-width:0;">
      <h1>Joymee × Uzum PVZ</h1>
      <div class="sub" style="margin-bottom:0;">Все объявления коммерции (Ташкент + область)</div>
    </div>
    <div style="text-align:right; font-size:10px; color:#999; line-height:1.3; white-space:nowrap; padding-top:2px;">
      <div style="font-weight:600; color:#666;">Обновлено</div>
      <div>__BUILT_DATE__</div>
      <div>__BUILT_TIME__</div>
    </div>
  </div>

  <div class="stat" id="stat">…</div>

  <div class="filter" style="background:#fff; padding:10px; border-radius:6px; border:1px solid #e7e7e7;">
    <label style="margin-bottom:6px;">Зона Узума (показывать только)</label>
    <label class="zone-check" style="background:rgba(112,0,255,.08);">
      <input type="checkbox" id="zf-rec" checked/>
      <span style="color:#5a00cc; font-weight:600;">🟣 Рекомендуемые</span>
      <span id="cnt-rec" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check">
      <input type="checkbox" id="zf-unknown" checked/>
      <span>⚪ Белые (нет данных)</span>
      <span id="cnt-unknown" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check" style="background:rgba(139,142,153,.08);">
      <input type="checkbox" id="zf-forb"/>
      <span style="color:#666;">⛔ Запрещённые</span>
      <span id="cnt-forb" style="margin-left:auto; color:#999;">…</span>
    </label>
  </div>

  <div class="filter" style="background:#fff; padding:10px; border-radius:6px; border:1px solid #e7e7e7;">
    <label style="margin-bottom:6px;">Свежесть объявления (любая из выбранных)</label>
    <label class="zone-check" style="background:#f5f5f5; border-bottom:1px solid #e7e7e7; margin-bottom:6px; padding-bottom:6px;">
      <input type="checkbox" id="fresh-all" checked/>
      <span style="font-weight:600;">Выбрать все / снять все</span>
      <span style="margin-left:auto; color:#999;" id="fresh-all-count">…</span>
    </label>
    <label class="zone-check" style="background:rgba(34,197,94,.08);">
      <input type="checkbox" class="fresh-bucket" data-min="0" data-max="1" checked/>
      <span style="color:#16a34a; font-weight:600;">🟢 За сутки</span>
      <span class="fresh-count" data-min="0" data-max="1" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check" style="background:rgba(34,197,94,.05);">
      <input type="checkbox" class="fresh-bucket" data-min="1" data-max="3"/>
      <span style="color:#16a34a;">🟢 1–3 дня</span>
      <span class="fresh-count" data-min="1" data-max="3" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check" style="background:rgba(234,179,8,.06);">
      <input type="checkbox" class="fresh-bucket" data-min="3" data-max="5"/>
      <span style="color:#a16207;">🟡 3–5 дней</span>
      <span class="fresh-count" data-min="3" data-max="5" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check" style="background:rgba(234,179,8,.05);">
      <input type="checkbox" class="fresh-bucket" data-min="5" data-max="7"/>
      <span style="color:#a16207;">🟡 5–7 дней</span>
      <span class="fresh-count" data-min="5" data-max="7" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check">
      <input type="checkbox" class="fresh-bucket" data-min="7" data-max="14"/>
      <span>⚪ 1–2 недели</span>
      <span class="fresh-count" data-min="7" data-max="14" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check">
      <input type="checkbox" class="fresh-bucket" data-min="14" data-max="30"/>
      <span>⚪ 2–4 недели</span>
      <span class="fresh-count" data-min="14" data-max="30" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check">
      <input type="checkbox" class="fresh-bucket" data-min="30" data-max="99999"/>
      <span style="color:#999;">⚪ Старше месяца</span>
      <span class="fresh-count" data-min="30" data-max="99999" style="margin-left:auto; color:#999;">…</span>
    </label>
  </div>

  <div class="filter" style="background:#fff; padding:10px; border-radius:6px; border:1px solid #e7e7e7;">
    <label style="margin-bottom:6px;">Цена USD/мес (любая из выбранных)</label>
    <label class="zone-check" style="background:#f5f5f5; border-bottom:1px solid #e7e7e7; margin-bottom:6px; padding-bottom:6px;">
      <input type="checkbox" id="price-all" checked/>
      <span style="font-weight:600;">Выбрать все / снять все</span>
      <span style="margin-left:auto; color:#999;" id="price-all-count">…</span>
    </label>
    <label class="zone-check" style="background:rgba(34,197,94,.08);">
      <input type="checkbox" class="price-bucket" data-min="0" data-max="600" checked/>
      <span style="color:#16a34a; font-weight:600;">🟢 До $600</span>
      <span class="price-count" data-min="0" data-max="600" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check" style="background:rgba(234,179,8,.08);">
      <input type="checkbox" class="price-bucket" data-min="600" data-max="1000"/>
      <span style="color:#a16207; font-weight:600;">🟡 $600 – $1000</span>
      <span class="price-count" data-min="600" data-max="1000" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check" style="background:rgba(249,115,22,.08);">
      <input type="checkbox" class="price-bucket" data-min="1000" data-max="99999999"/>
      <span style="color:#c2410c; font-weight:600;">🟠 $1000 и выше</span>
      <span class="price-count" data-min="1000" data-max="99999999" style="margin-left:auto; color:#999;">…</span>
    </label>
    <label class="zone-check" style="background:#fafafa;">
      <input type="checkbox" class="price-bucket" data-min="-1" data-max="0"/>
      <span style="color:#999;">⚫ Без USD цены</span>
      <span class="price-count" data-min="-1" data-max="0" style="margin-left:auto; color:#999;">…</span>
    </label>
    <details style="margin-top:8px;">
      <summary style="font-size:11px; color:#888;">Точный диапазон</summary>
      <div class="range" style="margin-top:6px;">
        <input id="pmin" type="number" placeholder="от"/>
        <input id="pmax" type="number" placeholder="до"/>
      </div>
    </details>
  </div>

  <div class="filter">
    <label>Площадь, м²</label>
    <div class="range">
      <input id="amin" type="number" placeholder="от"/>
      <input id="amax" type="number" placeholder="до"/>
    </div>
  </div>

  <div class="filter">
    <label>Тип помещения <span id="tag-mode-label" style="font-weight:400; text-transform:none; color:#999;">(любой из выбранных)</span></label>
    <div id="tag-chips" class="tag-chips"></div>
    <div style="margin-top:6px;">
      <label style="display:inline; font-size:11px; text-transform:none; letter-spacing:0;">
        <input type="checkbox" id="tag-and" style="vertical-align:middle;"/>
        требовать все выбранные (AND)
      </label>
    </div>
  </div>

  <div class="filter" style="background:#fff; padding:10px; border-radius:6px; border:1px solid #e7e7e7; margin-bottom:10px;">
    <label style="margin-bottom:6px;">Поиск по карте</label>
    <input id="map-search" type="text" autocomplete="off" placeholder="Посёлок или координаты 41.31, 69.24"
           style="width:100%; box-sizing:border-box; padding:6px 8px; border:1px solid #d4d4d4; border-radius:4px; font-size:13px;"/>
    <div id="map-search-results" style="margin-top:4px; max-height:190px; overflow-y:auto; font-size:13px;"></div>
    <div style="font-size:11px; color:#999; margin-top:4px;">
      Можно вставить координаты прямо из Google Maps.
      <a id="shortlist-link" href="data/pvz_shortlist.csv" download style="color:#2563eb;">Скачать список мест (CSV)</a>
    </div>
  </div>
  <div class="filter" style="background:#fff; padding:10px; border-radius:6px; border:1px solid #e7e7e7;">
    <label style="margin-bottom:6px;">Слои на карте</label>
    <label class="zone-check" style="background:#f5f5f5; border-bottom:1px solid #e7e7e7; margin-bottom:6px; padding-bottom:6px;">
      <input type="checkbox" id="layer-all"/>
      <span style="font-weight:600;">Выбрать все / снять все</span>
      <span style="margin-left:auto; color:#999;">13</span>
    </label>
    <label class="zone-check" style="background:rgba(112,0,255,.08);">
      <input type="checkbox" class="layer-toggle" id="layer-rec" checked/>
      <span style="color:#5a00cc; font-weight:600;">🟣 Гексы рекомендуемых</span>
      <span style="margin-left:auto; color:#999;" id="hex-rec-count"></span>
    </label>
    <label class="zone-check" style="background:rgba(139,142,153,.08);">
      <input type="checkbox" class="layer-toggle" id="layer-forb" checked/>
      <span style="color:#666;">⬜ Гексы запрещённых</span>
      <span style="margin-left:auto; color:#999;" id="hex-forb-count"></span>
    </label>
    <label class="zone-check" style="background:rgba(112,0,255,.05);">
      <input type="checkbox" class="layer-toggle" id="layer-pvz" checked/>
      <span style="color:#7000ff; font-weight:600;">● Существующие ПВЗ Узум</span>
      <span style="margin-left:auto; color:#999;" id="pvz-count">(…)</span>
    </label>
    <label class="zone-check" style="background:rgba(34,197,94,.06);">
      <input type="checkbox" class="layer-toggle" id="layer-joymee" checked/>
      <span style="color:#16a34a; font-weight:600;">🟢🟡 Объявления joymee</span>
    </label>
    <label class="zone-check" style="background:linear-gradient(to right, rgba(220,38,38,.10), rgba(234,179,8,.10), rgba(34,197,94,.10));">
      <input type="checkbox" class="layer-toggle" id="layer-heatmap"/>
      <span style="font-weight:600;">🌡 Скоринг гексов (heatmap)</span>
      <span style="margin-left:auto; color:#999;" id="grid-count"></span>
    </label>
    <label class="zone-check" style="background:linear-gradient(to right, rgba(239,243,255,.9), rgba(107,174,214,.5), rgba(8,48,107,.25));">
      <input type="checkbox" class="layer-toggle" id="layer-uzumpop"/>
      <span style="font-weight:600;">👥 Плотность населения (Uzum)</span>
      <span style="margin-left:auto; color:#999;" id="uzumpop-count"></span>
    </label>
    <label class="zone-check" style="background:rgba(249,115,22,.08);">
      <input type="checkbox" class="layer-toggle" id="layer-market" checked/>
      <span style="color:#c2410c; font-weight:600;">🟠 Рынки / базары</span>
      <span style="margin-left:auto; color:#999;" id="poi-market-count"></span>
    </label>
    <label class="zone-check" style="background:rgba(59,130,246,.08);">
      <input type="checkbox" class="layer-toggle" id="layer-shop" checked/>
      <span style="color:#1e3a8a; font-weight:600;">🔵 Супермаркеты</span>
      <span style="margin-left:auto; color:#999;" id="poi-shop-count"></span>
    </label>
    <label class="zone-check" style="background:rgba(34,197,94,.08);">
      <input type="checkbox" class="layer-toggle" id="layer-bank"/>
      <span style="color:#166534; font-weight:600;">🟢 Банки</span>
      <span style="margin-left:auto; color:#999;" id="poi-bank-count"></span>
    </label>
    <label class="zone-check" style="background:linear-gradient(to right, rgba(16,185,129,.18), rgba(132,204,22,.14), rgba(253,224,71,.14));">
      <input type="checkbox" class="layer-toggle" id="layer-pvzpick" checked/>
      <span style="color:#065f46; font-weight:700;">⭐ Рекомендуемые для ПВЗ</span>
      <span style="margin-left:auto; color:#999;" id="pvzpick-count"></span>
    </label>
    <label class="zone-check" style="background:rgba(251,146,60,.10);">
      <input type="checkbox" class="layer-toggle" id="layer-zhk" checked/>
      <span style="color:#7c2d12; font-weight:600;">🏗 Новостройки (ЖК)</span>
      <span style="margin-left:auto; color:#999;" id="zhk-count"></span>
    </label>
    <label class="zone-check" style="background:linear-gradient(to right, rgba(247,244,249,.9), rgba(223,101,176,.35), rgba(152,0,67,.25));">
      <input type="checkbox" class="layer-toggle" id="layer-housing"/>
      <span style="color:#980043; font-weight:600;">🏘 Ввод жилья (Госкомстат)</span>
      <span style="margin-left:auto; color:#999;" id="housing-count"></span>
    </label>
    <label class="zone-check" style="background:rgba(0,0,0,.04);">
      <input type="checkbox" class="layer-toggle" id="layer-labels"/>
      <span style="font-weight:600;">🔢 Номера гексов (T-XXXX)</span>
      <span style="margin-left:auto; color:#999; font-size:11px;">видны при зуме ≥14</span>
    </label>
  </div>

  <div class="filter" style="background:#fff; padding:10px; border-radius:6px; border:1px solid #e7e7e7;" id="experts-panel">
    <label style="margin-bottom:6px;">Выборы экспертов</label>
    <div id="experts-list"></div>
    <div style="font-size:11px; color:#999; margin-top:6px;">
      Чтобы выбрать гексы как эксперт — открой ссылку <code>?pick=karima</code> / <code>?pick=ivan</code> / <code>?pick=oleg</code>
    </div>
  </div>

  <details open>
    <summary>Легенда</summary>
    <div class="legend-item" style="margin-top:8px;"><div class="hex-swatch" style="background:rgba(112,0,255,.30)"></div>Рекомендуемая зона Узума</div>
    <div class="legend-item"><div class="hex-swatch" style="background:rgba(139,142,153,.30)"></div>Запрещённая зона</div>
    <div class="legend-item"><div class="swatch" style="background:#7000ff"></div>Существующий ПВЗ Узум</div>
    <div class="legend-item"><div class="swatch" style="background:#22c55e"></div>joymee: цена &lt; $600/мес</div>
    <div class="legend-item"><div class="swatch" style="background:#eab308"></div>joymee: цена ≥ $600/мес</div>
    <div class="legend-item"><div class="swatch" style="background:#999"></div>joymee: без USD цены</div>
    <div class="legend-item" style="margin-top:8px;"><div class="swatch" style="background:#f97316; border:1px solid #c2410c;"></div>Рынок / базар</div>
    <div class="legend-item"><div class="swatch" style="background:#3b82f6; border:1px solid #1e3a8a;"></div>Супермаркет <span style="color:#999;">(крупнее = сетевой)</span></div>
    <div class="legend-item"><div class="swatch" style="background:#22c55e; border:1px solid #166534;"></div>Банк</div>
    <div style="font-size:11px; color:#999; margin-top:2px;">Точки притяжения — только область, без города Ташкента. Источник OpenStreetMap.</div>
    <div style="margin-top:8px; font-weight:600; font-size:12px;">Рекомендуемые для ПВЗ</div>
    <div class="legend-item"><div class="hex-swatch" style="background:#10b981;"></div>Лучшие (топ-30)</div>
    <div class="legend-item"><div class="hex-swatch" style="background:#84cc16;"></div>Хорошие (31–100)</div>
    <div class="legend-item"><div class="hex-swatch" style="background:#fde047;"></div>Стоит посмотреть (101+)</div>
    <div class="legend-item" style="margin-top:8px;"><div class="swatch" style="background:#fb923c; border:1px solid #7c2d12;"></div>Новостройка строится <span style="color:#999;">(размер = число квартир)</span></div>
    <div class="legend-item"><div class="swatch" style="background:#a3a3a3; border:1px solid #525252;"></div>Новостройка сдана</div>
    <div style="margin-top:10px; font-weight:600; font-size:12px;">Введено жилья за 3 года, тыс. м² (Госкомстат)</div>
    <div id="housing-legend" style="margin-top:4px;"></div>
    <div style="margin-top:10px; font-weight:600; font-size:12px;">Плотность населения (Uzum), чел. в гексе</div>
    <div id="uzumpop-legend" style="margin-top:4px;"></div>
  </details>
</div>
<div id="map"></div>

<!-- Floating pick-mode panel — only shown when URL has ?pick=expertkey -->
<div id="pick-panel" style="display:none; position:absolute; bottom:20px; right:20px; z-index:1000;
  background:#fff; border:2px solid #333; border-radius:8px; padding:14px 16px; min-width:280px;
  box-shadow:0 4px 16px rgba(0,0,0,.2); font-family: -apple-system, BlinkMacSystemFont, sans-serif;">
  <div style="font-size:14px; font-weight:600; margin-bottom:8px;">
    Вы выбираете как: <span id="pick-name">…</span> <span id="pick-emoji"></span>
  </div>
  <label style="display:flex; align-items:center; gap:8px; font-size:13px; margin-bottom:8px; cursor:pointer;">
    <input type="checkbox" id="pick-mode-on"/>
    <span><b>Режим выбора:</b> кликайте по гексам</span>
  </label>
  <div style="font-size:12px; color:#666; margin-bottom:8px;">
    Выбрано локально: <b id="pick-local-count">0</b> гексов
  </div>
  <div style="display:flex; gap:6px; flex-wrap:wrap;">
    <button id="pick-copy" style="padding:6px 10px; font-size:12px; border:1px solid #333; background:#f5f5f5; border-radius:4px; cursor:pointer;">📋 Скопировать список</button>
    <button id="pick-clear" style="padding:6px 10px; font-size:12px; border:1px solid #999; background:#fff; color:#666; border-radius:4px; cursor:pointer;">Очистить</button>
  </div>
  <div style="font-size:11px; color:#999; margin-top:8px;">
    После копирования отправьте список в чат для сохранения в общую карту.
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script src="https://unpkg.com/h3-js@4.1.0/dist/h3-js.umd.js"></script>
<script>
const POINTS = __POINTS__;
const ZONES_RECOMMENDED = __REC__;
const ZONES_NOT_ALLOWED = __FORB__;
const DISTRICTS = __DISTRICTS__;
const TAG_META = __TAGS__;
const UZUM_PVZ = __UZUM_PVZ__;
// Uzum per-hex population: { h3: [population|null, popLevel|null, pedLevel|null] }
// Levels are 0=LOW, 1=MIDDLE, 2=HIGH. Geometry comes from the zone polygons above.
const UZUM_POP = __UZUM_POP__;
// Percentile break points of the observed population values (see build_map.py)
const UZUM_POP_BREAKS = __UZUM_POP_BREAKS__;
// Points of attraction from OpenStreetMap: [lat, lng, type, chain, name, extra]
// type: 0=market, 1=supermarket, 2=bank. Tashkent city itself is NOT covered.
const POI = __POI__;
const POI_RADIUS_KM = __POI_RADIUS_KM__;
// Nearby-POI counts for Uzum zone hexes (the region — where the OSM set actually reaches):
// { h3: [markets, supermarkets, chain, banks, nearestMarketKm, nearestChainKm, nearestBankKm] }
// Hexes with nothing nearby are simply absent.
const UZUM_POI = __UZUM_POI__;
// Official housing commissioned per district (Госкомстат), thousand m², joined onto OSM
// district outlines. properties: {name, years:{YYYY: value}, sum3}
const DISTRICTS_HOUSING = __DISTRICTS_HOUSING__;
const HOUSING_BREAKS = __HOUSING_BREAKS__;
const HOUSING_YEARS = __HOUSING_YEARS__;
// Residential complexes: [lat, lng, name, district, completion, status, apartments, floors, priceM2, url]
const ZHK = __ZHK__;
const ZHK_RADIUS_KM = __ZHK_RADIUS_KM__;
// Shortlist of hexes recommended for a new PVZ:
// [h3, score, rank, population, peopleRank, trafficRank|null, growthRank, knownSignals,
//  district, zhkCount, zhkApartments, rentListings, bestRentUsd]
const PVZ_PICKS = __PVZ_PICKS__;
// [name, lat, lng, kind] for every settlement in the region — what the search box matches.
const SEARCH_INDEX = __SEARCH_INDEX__;
// Tashkent hex grid with scoring: { "T-XXXX": {h3, lat, lng, score, rank, zone, pop, d_pvz, n_listings, frac_first, d_metro, components} }
const HEX_GRID = __HEX_GRID__;
// Pre-computed hex polygon GeoJSON features (one per hex)
const HEX_POLYGONS = __HEX_POLYGONS__;
// Expert picks: {key: {name, color, emoji, hexes: [T-ID...]}}
const EXPERT_PICKS = __EXPERT_PICKS__;
</script>
<script>
const map = L.map('map', { preferCanvas: true }).setView([41.31, 69.27], 12);

// Small "Cities" control top-right for quick navigation
const CityNav = L.Control.extend({
  options: { position: 'topright' },
  onAdd: () => {
    const div = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
    div.style.background = '#fff';
    div.style.padding = '4px 6px';
    div.style.fontFamily = 'sans-serif';
    div.style.fontSize = '12px';
    div.style.lineHeight = '1.4';
    div.style.boxShadow = '0 1px 3px rgba(0,0,0,.15)';
    div.innerHTML = `
      <div style="font-weight:600; margin-bottom:2px; color:#666;">Города</div>
      <a href="#" data-city="tashkent" style="display:block; color:#2563eb; text-decoration:none; padding:2px 4px;">🏙 Ташкент</a>
      <a href="#" data-city="samarkand" style="display:block; color:#7c3aed; text-decoration:none; padding:2px 4px;">🏛 Самарканд</a>
    `;
    L.DomEvent.disableClickPropagation(div);
    div.querySelectorAll('a[data-city]').forEach(a => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        if (a.dataset.city === 'tashkent') map.setView([41.31, 69.27], 12);
        else map.setView([39.65, 66.96], 13);
      });
    });
    return div;
  },
});
new CityNav().addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  // Basemap tiles and the market/supermarket/bank points both come from OSM (ODbL).
  attribution: 'Карта и точки притяжения — © OpenStreetMap contributors (ODbL)', maxZoom: 19,
}).addTo(map);

// Custom pane for the score heatmap — below overlayPane so Uzum zone overlays stay
// on top. Clicks are now handled via map.click + h3 latLngToCell, so heatmap layer
// doesn't need to intercept clicks.
map.createPane('heatmapPane');
map.getPane('heatmapPane').style.zIndex = 380;
map.getPane('heatmapPane').style.pointerEvents = 'auto';
// Each pane needs its own canvas renderer (preferCanvas only applies to default panes)
const heatmapRenderer = L.canvas({pane: 'heatmapPane'});

// === Uzum population density layer =====================================================
// Reuses the zone polygons (no duplicated geometry) and colours them by Uzum's own
// population figure for that hex. Sits below the zone overlays and the score heatmap.
map.createPane('uzumPopPane');
map.getPane('uzumPopPane').style.zIndex = 360;
map.getPane('uzumPopPane').style.pointerEvents = 'auto';
const uzumPopRenderer = L.canvas({pane: 'uzumPopPane'});

// Sequential single-hue blues (ColorBrewer 5-class Blues + a darker top step).
// One hue, light to dark — readable for colour-blind users, unlike a rainbow ramp.
const POP_COLORS = ['#eff3ff', '#c6dbef', '#9ecae1', '#6baed6', '#3182bd', '#08519c', '#08306b'];
function popColor(pop) {
  if (pop === null || pop === undefined) return null;   // no data → no fill
  let i = 0;
  while (i < UZUM_POP_BREAKS.length && pop > UZUM_POP_BREAKS[i]) i++;
  return POP_COLORS[Math.min(i, POP_COLORS.length - 1)];
}
const LEVEL_LABELS = ['низкий', 'средний', 'высокий'];
function levelLabel(code) {
  return (code === null || code === undefined) ? 'нет данных' : (LEVEL_LABELS[code] || '—');
}
// Nearby markets / shops / banks for a zone hex. Shared shape with the scored-grid rows so
// a hex reads the same whether it is inside the city grid or out in the region.
// One-line "what's around" for a listing card.
function poiNearRow(p) {
  if (!p) return '';
  const [m, s, ch, b, nm, nc, nb] = p;
  if (!(m + s + b)) {
    const closest = [nm, nc, nb].filter(v => v !== null && v !== undefined);
    const hint = closest.length ? ` <small style="color:#999;">ближайшее в ${Math.min(...closest)} км</small>` : '';
    return `<div class="popup-row" style="color:#999;"><b>Рядом (${POI_RADIUS_KM} км):</b> по OSM ничего не отмечено${hint}</div>`;
  }
  const parts = [];
  if (m) parts.push(`рынков <b>${m}</b>`);
  if (s) parts.push(`супермаркетов <b>${s}</b>${ch ? ` <span style="color:#1e3a8a;">(сетевых ${ch})</span>` : ''}`);
  if (b) parts.push(`банков <b>${b}</b>`);
  return `<div class="popup-row"><b>Рядом (${POI_RADIUS_KM} км):</b> ${parts.join(', ')}</div>`;
}

function uzumPoiRows(cell) {
  const p = UZUM_POI[cell];
  const km = v => (v === null || v === undefined) ? '—' : `${v} км`;
  if (!p) {
    return `<tr style="color:#999;"><td colspan="2" style="padding-top:8px; font-size:11px;">`
      + `В радиусе ${POI_RADIUS_KM} км рынков, магазинов и банков по данным OSM нет`
      + `<br>(OSM не размечает всё подряд — «нет» здесь значит «не отмечено»)</td></tr>`;
  }
  return `
      <tr style="color:#999;"><td colspan="2" style="padding-top:8px; font-size:11px;">Рядом (${POI_RADIUS_KM} км), OSM:</td></tr>
      <tr><td>Рынки</td><td><b>${p[0]}</b> <small style="color:#999;">ближайший ${km(p[4])}</small></td></tr>
      <tr><td>Супермаркеты</td><td><b>${p[1]}</b> <small style="color:#999;">из них сетевых ${p[2]}, ближайший сетевой ${km(p[5])}</small></td></tr>
      <tr><td>Банки</td><td><b>${p[3]}</b> <small style="color:#999;">ближайший ${km(p[6])}</small></td></tr>`;
}

function uzumPopRows(cell) {
  const p = UZUM_POP[cell];
  if (!p) return '';
  const popStr = (p[0] === null || p[0] === undefined)
    ? '<span style="color:#999;">нет данных</span>'
    : `<b>${p[0].toLocaleString('ru-RU')}</b> чел.`;
  return `
      <tr style="color:#999;"><td colspan="2" style="padding-top:8px; font-size:11px;">Данные Узума по гексу:</td></tr>
      <tr><td>Население (Uzum)</td><td>${popStr}</td></tr>
      <tr><td>Уровень населения</td><td><b>${levelLabel(p[1])}</b></td></tr>
      <tr><td>Пешеходный трафик</td><td><b>${levelLabel(p[2])}</b></td></tr>`;
}

// Scored-grid lookup by h3 (the pick-mode code builds its own copy inside a block scope).
const H3_TO_TID = {};
for (const tid in HEX_GRID) H3_TO_TID[HEX_GRID[tid].h3] = tid;

const uzumPopFeatures = ZONES_RECOMMENDED.concat(ZONES_NOT_ALLOWED)
  .filter(f => UZUM_POP[f.properties.h3]);
const uzumPopLayer = L.geoJSON({type:'FeatureCollection', features: uzumPopFeatures}, {
  pane: 'uzumPopPane',
  renderer: uzumPopRenderer,
  style: f => {
    const p = UZUM_POP[f.properties.h3];
    const color = popColor(p && p[0]);
    if (!color) {
      // "No data" reads as a faint outline rather than a fill, so it can never be mistaken
      // for a genuinely low-population hex.
      return {color:'#bbb', weight:0.6, dashArray:'2,3', fill:false};
    }
    return {color:'#ffffff', weight:0.3, fillColor:color, fillOpacity:0.6};
  },
  onEachFeature: (feat, layer) => {
    const cell = feat.properties.h3;
    layer.bindPopup(() => {
      const tid = H3_TO_TID[cell];
      const h = tid ? HEX_GRID[tid] : null;
      // Hexes inside the scored grid get the full existing popup (which already carries the
      // Uzum rows); the rest get just the Uzum block, from the same builder.
      if (tid && h) return hexPopupHtml(tid, h);
      return `<h3 style="margin:0 0 6px;">Гекс Узума</h3>
              <table style="font-size:12px; border-collapse:collapse; width:100%;">`
             + uzumPopRows(cell) + uzumPoiRows(cell) + `</table>`;
    }, {maxWidth: 320});
  },
});

document.getElementById('uzumpop-count').textContent = `(${uzumPopFeatures.length})`;

// Legend is generated from the actual break points, so the labels can never drift from
// the colours on screen.
(function buildPopLegend() {
  const box = document.getElementById('uzumpop-legend');
  if (!box) return;
  if (!UZUM_POP_BREAKS.length) { box.innerHTML = '<div style="color:#999;">нет данных</div>'; return; }
  const fmt = n => n.toLocaleString('ru-RU');
  const rows = [];
  for (let i = 0; i <= UZUM_POP_BREAKS.length; i++) {
    const lo = i === 0 ? 0 : UZUM_POP_BREAKS[i-1] + 1;
    const hi = i < UZUM_POP_BREAKS.length ? UZUM_POP_BREAKS[i] : null;
    rows.push(`<div class="legend-item"><div class="hex-swatch" style="background:${POP_COLORS[i]}; opacity:.85;"></div>`
      + (hi === null ? `${fmt(lo)}+ чел.` : `${fmt(lo)}–${fmt(hi)} чел.`) + `</div>`);
  }
  rows.push('<div class="legend-item"><div class="hex-swatch" style="background:transparent; border:1px dashed #bbb;"></div>нет данных Узума</div>');
  box.innerHTML = rows.join('');
})();

// === Recommended hexes for a new PVZ ===================================================
// Sits above the other fills — this is the answer, not background. Three tiers rather than
// a smooth ramp: the point is a shortlist you can work through, not another heat map.
map.createPane('pvzPickPane');
map.getPane('pvzPickPane').style.zIndex = 430;
map.getPane('pvzPickPane').style.pointerEvents = 'auto';
const pvzPickRenderer = L.canvas({pane: 'pvzPickPane'});
const PICK_TIERS = [
  {upto: 30,  color: '#065f46', fill: '#10b981', label: 'Лучшие (топ-30)'},
  {upto: 100, color: '#166534', fill: '#84cc16', label: 'Хорошие (31–100)'},
  {upto: 1e9, color: '#713f12', fill: '#fde047', label: 'Стоит посмотреть (101+)'},
];
function pickTier(rank) { return PICK_TIERS.find(t => rank <= t.upto) || PICK_TIERS[2]; }

function pvzPickPopup(p) {
  const [cell, score, rank, pop, pr, tr, gr, known, district, zhk, apts, rent, rentUsd,
         place, placeKm, cityKm, band] = p;
  const bar = v => {
    if (v === null || v === undefined) return '<span style="color:#999;">нет данных</span>';
    const f = Math.round(v * 10);
    return `<tt>${'▓'.repeat(f)}${'░'.repeat(10-f)}</tt>`;
  };
  const conf = known === 3
    ? '<span style="color:#166534;">оценка по всем трём признакам</span>'
    : `<span style="color:#b45309;">оценка по ${known} признакам из 3 — про магазины рядом данных нет</span>`;
  const rentRow = rent
    ? `<tr><td>Аренда рядом</td><td><b>${rent}</b> предложений${rentUsd ? `, от $${rentUsd}` : ''}</td></tr>`
    : `<tr><td>Аренда рядом</td><td><span style="color:#999;">предложений нет — помещение искать самому</span></td></tr>`;
  return `
    <h3 style="margin:0 0 4px;">Место #${rank} для ПВЗ</h3>
    <div style="font-size:12px; color:#666; margin-bottom:2px;">
      ${place ? `<b>${place}</b>${placeKm ? ` (${placeKm} км)` : ''} · ` : ''}${district || 'Ташкентская область'}
    </div>
    <div style="font-size:12px; color:#666; margin-bottom:6px;">
      От границы Ташкента <b>${cityKm !== null && cityKm !== undefined ? cityKm + ' км' : '—'}</b>
      · <code style="background:#f3f4f6; padding:1px 4px; border-radius:3px;">${h3.cellToLatLng(cell)[0].toFixed(6)}, ${h3.cellToLatLng(cell)[1].toFixed(6)}</code>
    </div>
    <div style="margin-bottom:8px; font-size:13px;">
      <span style="background:${pickTier(rank).fill}; padding:2px 8px; border-radius:3px; font-weight:600;">
        балл ${(score*100).toFixed(0)}%</span>
    </div>
    <table style="font-size:12px; border-collapse:collapse; width:100%;">
      <tr><td><b>Люди рядом</b> <small style="color:#999;">(45%)</small></td>
          <td>${bar(pr)} <b>${pop.toLocaleString('ru-RU')}</b> чел.</td></tr>
      <tr><td><b>Живой поток</b> <small style="color:#999;">(30%)</small></td>
          <td>${bar(tr)}</td></tr>
      <tr><td><b>Рост</b> <small style="color:#999;">(25%)</small></td>
          <td>${bar(gr)} ${zhk ? `<small style="color:#999;">${zhk} ЖК, ${apts.toLocaleString('ru-RU')} кв.</small>` : ''}</td></tr>
      ${rentRow}
    </table>
    <div style="font-size:11px; margin-top:6px;">${conf}</div>`;
}

const pvzPickLayer = L.geoJSON({type:'FeatureCollection', features: PVZ_PICKS.map(p => {
  const ring = h3.cellToBoundary(p[0], true);   // [lng,lat] pairs
  return {type:'Feature', geometry:{type:'Polygon', coordinates:[ring.concat([ring[0]])]},
          properties:{pick: p}};
})}, {
  pane: 'pvzPickPane', renderer: pvzPickRenderer,
  style: f => {
    const t = pickTier(f.properties.pick[2]);
    return {color: t.color, weight: 1.2, fillColor: t.fill, fillOpacity: 0.65};
  },
});
pvzPickLayer.addTo(map);

// Clicks are handled on the map, not on the polygons. Leaflet's canvas renderers do not
// pass a click down to the pane below, so the POI and ЖК canvases (which sit on top) were
// swallowing every click that did not land exactly on one of their dots — the hex popups
// simply never opened. Resolving the hex from the clicked coordinate sidesteps the whole
// pane-stacking problem, and it is how pick-mode in this file already works.
const PICK_BY_H3 = {};
PVZ_PICKS.forEach(p => { PICK_BY_H3[p[0]] = p; });
map.on('click', e => {
  if (typeof h3 === 'undefined' || !h3.latLngToCell) return;
  const cell = h3.latLngToCell(e.latlng.lat, e.latlng.lng, 9);
  let html = null;
  if (map.hasLayer(pvzPickLayer) && PICK_BY_H3[cell]) {
    html = pvzPickPopup(PICK_BY_H3[cell]);
  } else if (UZUM_POP[cell]) {
    const tid = H3_TO_TID[cell];
    html = (tid && HEX_GRID[tid])
      ? hexPopupHtml(tid, HEX_GRID[tid])
      : `<h3 style="margin:0 0 6px;">Гекс Узума</h3>
         <table style="font-size:12px; border-collapse:collapse; width:100%;">`
        + uzumPopRows(cell) + uzumPoiRows(cell) + `</table>`;
  }
  if (html) L.popup({maxWidth: 340}).setLatLng(e.latlng).setContent(html).openOn(map);
});
document.getElementById('pvzpick-count').textContent = `(${PVZ_PICKS.length})`;
document.getElementById('layer-pvzpick').addEventListener('change', e => {
  if (e.target.checked) pvzPickLayer.addTo(map); else map.removeLayer(pvzPickLayer);
});

// === Search: settlement name or a pasted coordinate pair ===============================
(function initSearch() {
  const input = document.getElementById('map-search');
  const box = document.getElementById('map-search-results');
  if (!input || !box) return;
  let marker = null;

  function goTo(lat, lng, label) {
    map.setView([lat, lng], 15);
    if (marker) map.removeLayer(marker);
    marker = L.marker([lat, lng]).addTo(map)
      .bindPopup(`<b>${label}</b><br><code>${lat.toFixed(6)}, ${lng.toFixed(6)}</code>`)
      .openPopup();
    box.innerHTML = '';
  }

  // "41.31, 69.24" / "41.31 69.24" / "41,31 69,24" — whatever comes off a phone or Maps.
  function parseCoords(q) {
    const m = q.trim().match(/^(-?\d{1,3}[.,]\d+)[\s,;]+(-?\d{1,3}[.,]\d+)$/);
    if (!m) return null;
    const a = parseFloat(m[1].replace(',', '.')), b = parseFloat(m[2].replace(',', '.'));
    if (isNaN(a) || isNaN(b)) return null;
    // Uzbekistan sits at ~41 N, ~69 E; if the pair arrives the other way round, swap it
    // rather than silently dropping the user in the Indian Ocean.
    if (Math.abs(a) <= 90 && Math.abs(b) <= 180 && !(a > 60 && b < 50)) return [a, b];
    return [b, a];
  }

  function render(q) {
    const coords = parseCoords(q);
    if (coords) {
      box.innerHTML = `<div class="search-hit" data-lat="${coords[0]}" data-lng="${coords[1]}"
        data-label="Точка ${coords[0].toFixed(5)}, ${coords[1].toFixed(5)}"
        style="padding:5px 6px; cursor:pointer; border-radius:3px;">
        📍 Перейти к <b>${coords[0].toFixed(5)}, ${coords[1].toFixed(5)}</b></div>`;
      return;
    }
    const needle = q.trim().toLowerCase();
    if (needle.length < 2) { box.innerHTML = ''; return; }
    const hits = SEARCH_INDEX
      .filter(p => p[0].toLowerCase().includes(needle))
      .sort((a, b) => a[0].toLowerCase().indexOf(needle) - b[0].toLowerCase().indexOf(needle)
                      || a[0].length - b[0].length)
      .slice(0, 30);
    box.innerHTML = hits.length
      ? hits.map(p => `<div class="search-hit" data-lat="${p[1]}" data-lng="${p[2]}" data-label="${p[0]}"
          style="padding:5px 6px; cursor:pointer; border-radius:3px;">
          ${p[0]} <span style="color:#999; font-size:11px;">${p[3] || ''}</span></div>`).join('')
      : '<div style="padding:5px 6px; color:#999;">ничего не нашлось</div>';
  }

  input.addEventListener('input', () => render(input.value));
  input.addEventListener('keydown', ev => {
    if (ev.key !== 'Enter') return;
    const first = box.querySelector('.search-hit');
    if (first) first.click();
  });
  box.addEventListener('click', ev => {
    const el = ev.target.closest('.search-hit');
    if (!el) return;
    goTo(parseFloat(el.dataset.lat), parseFloat(el.dataset.lng), el.dataset.label);
  });
  box.addEventListener('mouseover', ev => {
    const el = ev.target.closest('.search-hit');
    if (el) el.style.background = '#eef2ff';
  });
  box.addEventListener('mouseout', ev => {
    const el = ev.target.closest('.search-hit');
    if (el) el.style.background = '';
  });
})();

// === Housing commissioned, official (Госкомстат) =======================================
// District choropleth. Sits at the very bottom — it is background context, not something
// to click through. Breaks are percentiles of the observed values: a handful of districts
// build several times what the quiet ones do, so an even split would flatten the picture.
map.createPane('housingPane');
map.getPane('housingPane').style.zIndex = 350;
map.getPane('housingPane').style.pointerEvents = 'auto';
const housingRenderer = L.canvas({pane: 'housingPane'});
const HOUSING_COLORS = ['#f7f4f9', '#e7e1ef', '#c994c7', '#df65b0', '#dd1c77', '#980043'];
function housingColor(v) {
  if (v === null || v === undefined) return null;
  let i = 0;
  while (i < HOUSING_BREAKS.length && v > HOUSING_BREAKS[i]) i++;
  return HOUSING_COLORS[Math.min(i, HOUSING_COLORS.length - 1)];
}
const housingLayer = L.geoJSON({type:'FeatureCollection', features: DISTRICTS_HOUSING}, {
  pane: 'housingPane', renderer: housingRenderer,
  style: f => {
    const c = housingColor(f.properties.sum3);
    return c ? {color:'#7a5673', weight:0.8, fillColor:c, fillOpacity:0.55}
             : {color:'#bbb', weight:0.6, dashArray:'2,3', fill:false};
  },
  onEachFeature: (feat, layer) => {
    const p = feat.properties;
    layer.bindPopup(() => {
      const rows = HOUSING_YEARS.filter(y => p.years[y] !== undefined && p.years[y] !== null)
        .map(y => `<tr><td>${y}</td><td><b>${p.years[y].toLocaleString('ru-RU')}</b> тыс. м²</td></tr>`)
        .join('');
      return `<h3 style="margin:0 0 6px;">${p.name}</h3>
        <div style="font-size:12px; color:#666; margin-bottom:4px;">Введено жилья, Госкомстат</div>
        <table style="font-size:12px; border-collapse:collapse; width:100%;">${rows}
          <tr style="border-top:1px solid #ddd;"><td><b>За 3 года</b></td>
              <td><b>${(p.sum3||0).toLocaleString('ru-RU')}</b> тыс. м²</td></tr>
        </table>`;
    }, {maxWidth: 300});
  },
});
document.getElementById('housing-count').textContent = `(${DISTRICTS_HOUSING.length})`;
document.getElementById('layer-housing').addEventListener('change', e => {
  if (e.target.checked) { housingLayer.addTo(map); housingLayer.bringToBack(); }
  else map.removeLayer(housingLayer);
});

(function buildHousingLegend() {
  const box = document.getElementById('housing-legend');
  if (!box) return;
  if (!HOUSING_BREAKS.length) { box.innerHTML = '<div style="color:#999;">нет данных</div>'; return; }
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const rows = [];
  for (let i = 0; i <= HOUSING_BREAKS.length; i++) {
    const lo = i === 0 ? 0 : HOUSING_BREAKS[i-1];
    const hi = i < HOUSING_BREAKS.length ? HOUSING_BREAKS[i] : null;
    rows.push(`<div class="legend-item"><div class="hex-swatch" style="background:${HOUSING_COLORS[i]}; opacity:.85;"></div>`
      + (hi === null ? `${fmt(lo)}+ тыс. м²` : `${fmt(lo)}–${fmt(hi)} тыс. м²`) + `</div>`);
  }
  box.innerHTML = rows.join('');
})();

// === Residential complexes (ЖК) ========================================================
map.createPane('zhkPane');
map.getPane('zhkPane').style.zIndex = 445;
const zhkRenderer = L.canvas({pane: 'zhkPane'});
const zhkLayer = L.layerGroup();
ZHK.forEach(z => {
  const [lat, lng, name, district, completion, status, apts, floors, price, url] = z;
  // Radius by apartment count — a 700-flat complex is a different animal from a 40-flat one.
  const r = apts ? Math.max(5, Math.min(16, 4 + Math.sqrt(apts) / 3.2)) : 5;
  const building = status === 'building';
  const priceStr = price ? `${(price/1000000).toFixed(1)} млн сум/м²` : '—';
  L.circleMarker([lat, lng], {
    renderer: zhkRenderer, pane: 'zhkPane', radius: r,
    color: building ? '#7c2d12' : '#525252', weight: 1.5,
    fillColor: building ? '#fb923c' : '#a3a3a3', fillOpacity: 0.55,
  }).bindTooltip(
    `<b>${name}</b><br><span style="color:#666;">${district}</span><br>`
    + `Сдача: <b>${completion || '—'}</b> · ${building ? 'строится' : 'сдан'}<br>`
    + `Квартир: <b>${apts || '—'}</b> · этажей ${floors || '—'}<br>`
    + `Цена: ${priceStr}`, {direction:'top'}
  ).addTo(zhkLayer);
});
zhkLayer.addTo(map);
document.getElementById('zhk-count').textContent = `(${ZHK.length})`;
document.getElementById('layer-zhk').addEventListener('change', e => {
  if (e.target.checked) zhkLayer.addTo(map); else map.removeLayer(zhkLayer);
});

// === Points of attraction: markets / supermarkets / banks ==============================
// Canvas renderer in its own pane — these sit above the hex fills but below the joymee
// listing markers, so they read as context rather than competing with the listings.
map.createPane('poiPane');
map.getPane('poiPane').style.zIndex = 440;
const poiRenderer = L.canvas({pane: 'poiPane'});

const POI_STYLE = [
  {label:'Рынок / базар',  color:'#c2410c', fill:'#f97316', r:5},   // 0 market
  {label:'Супермаркет',    color:'#1e3a8a', fill:'#3b82f6', r:4},   // 1 supermarket
  {label:'Банк',           color:'#166534', fill:'#22c55e', r:3.5}, // 2 bank
];
function poiTooltip(p) {
  const [lat, lng, code, chain, name, extra] = p;
  const st = POI_STYLE[code];
  const title = name || '(без названия)';
  let detail = '';
  if (code === 1) detail = chain ? `<br><b style="color:#1e3a8a;">сетевой${extra ? ': ' + extra : ''}</b>`
                                 : '<br><span style="color:#888;">не сетевой</span>';
  if (code === 0 && extra && extra !== 'general') detail = `<br><span style="color:#888;">профиль: ${extra}</span>`;
  return `<b>${title}</b><br><span style="color:#666;">${st.label}</span>${detail}`;
}
function buildPoiLayer(code) {
  const layer = L.layerGroup();
  const st = POI_STYLE[code];
  POI.forEach(p => {
    if (p[2] !== code) return;
    // Chain supermarkets get a visibly bigger marker — they are the strongest demand
    // signal here, since the chains run their own geo-analysis before opening.
    const r = (code === 1 && p[3]) ? st.r + 2.5 : st.r;
    L.circleMarker([p[0], p[1]], {
      renderer: poiRenderer, pane: 'poiPane',
      radius: r, color: st.color, weight: (code === 1 && p[3]) ? 2 : 1,
      fillColor: st.fill, fillOpacity: 0.85,
    }).bindTooltip(poiTooltip(p), {direction:'top'}).addTo(layer);
  });
  return layer;
}
const marketLayer = buildPoiLayer(0);
const supermarketLayer = buildPoiLayer(1);
// 145 bank branches would drown the map, so this one starts off and clusters when shown.
const bankLayer = (typeof L.markerClusterGroup === 'function')
  ? L.markerClusterGroup({maxClusterRadius: 45, disableClusteringAtZoom: 14})
  : L.layerGroup();
POI.forEach(p => {
  if (p[2] !== 2) return;
  const st = POI_STYLE[2];
  bankLayer.addLayer(L.circleMarker([p[0], p[1]], {
    renderer: poiRenderer, pane: 'poiPane',
    radius: st.r, color: st.color, weight: 1, fillColor: st.fill, fillOpacity: 0.85,
  }).bindTooltip(poiTooltip(p), {direction:'top'}));
});

marketLayer.addTo(map);
supermarketLayer.addTo(map);
document.getElementById('poi-market-count').textContent = `(${POI.filter(p=>p[2]===0).length})`;
document.getElementById('poi-shop-count').textContent = `(${POI.filter(p=>p[2]===1).length})`;
document.getElementById('poi-bank-count').textContent = `(${POI.filter(p=>p[2]===2).length})`;

document.getElementById('layer-market').addEventListener('change', e => {
  if (e.target.checked) marketLayer.addTo(map); else map.removeLayer(marketLayer);
});
document.getElementById('layer-shop').addEventListener('change', e => {
  if (e.target.checked) supermarketLayer.addTo(map); else map.removeLayer(supermarketLayer);
});
document.getElementById('layer-bank').addEventListener('change', e => {
  if (e.target.checked) bankLayer.addTo(map); else map.removeLayer(bankLayer);
});

const recLayer = L.geoJSON({type:'FeatureCollection', features: ZONES_RECOMMENDED}, {
  style: () => ({color:'#7000ff', weight:0.5, fillColor:'#7000ff', fillOpacity:0.22}),
  interactive: false,
}).addTo(map);
const forbLayer = L.geoJSON({type:'FeatureCollection', features: ZONES_NOT_ALLOWED}, {
  style: () => ({color:'#888', weight:0.4, fillColor:'#8b8e99', fillOpacity:0.22}),
  interactive: false,
}).addTo(map);
document.getElementById('hex-rec-count').textContent = `(${ZONES_RECOMMENDED.length})`;
document.getElementById('hex-forb-count').textContent = `(${ZONES_NOT_ALLOWED.length})`;

map.createPane('pvzPane');
map.getPane('pvzPane').style.zIndex = 450;
map.getPane('pvzPane').style.pointerEvents = 'none';
const pvzLayer = L.layerGroup();
UZUM_PVZ.forEach(([lat, lng]) => {
  L.circleMarker([lat, lng], {
    radius: 4, color: '#fff', weight: 1, fillColor: '#7000ff',
    fillOpacity: 0.95, pane: 'pvzPane', interactive: false,
  }).addTo(pvzLayer);
});
pvzLayer.addTo(map);
document.getElementById('pvz-count').textContent = `(${UZUM_PVZ.length})`;

document.getElementById('layer-rec').addEventListener('change', e => {
  if (e.target.checked) recLayer.addTo(map); else map.removeLayer(recLayer);
});
document.getElementById('layer-forb').addEventListener('change', e => {
  if (e.target.checked) forbLayer.addTo(map); else map.removeLayer(forbLayer);
});
document.getElementById('layer-pvz').addEventListener('change', e => {
  if (e.target.checked) pvzLayer.addTo(map); else map.removeLayer(pvzLayer);
});

// === Hex heatmap layer (density-colored polygons of all Tashkent hexes) ===
// Score = population density. Gradient: grey (low) → yellow (mid) → bright green (high).
function scoreColor(s) {
  s = Math.max(0, Math.min(1, s));
  // Three-stop interpolation: grey [180,180,180] → yellow [250,220,40] → green [50,200,60]
  let r, g, b;
  if (s < 0.5) {
    const t = s * 2;  // 0 → 1
    r = Math.round(180 + (250 - 180) * t);
    g = Math.round(180 + (220 - 180) * t);
    b = Math.round(180 + ( 40 - 180) * t);
  } else {
    const t = (s - 0.5) * 2;  // 0 → 1
    r = Math.round(250 + ( 50 - 250) * t);
    g = Math.round(220 + (200 - 220) * t);
    b = Math.round( 40 + ( 60 -  40) * t);
  }
  return `rgb(${r},${g},${b})`;
}
// Compute max score with a plain loop (Math.max with spread can crash on 5000+ args in some browsers)
let MAX_SCORE = 0;
for (const k in HEX_GRID) {
  if (HEX_GRID[k].score > MAX_SCORE) MAX_SCORE = HEX_GRID[k].score;
}
if (MAX_SCORE <= 0) MAX_SCORE = 1;
const HEX_COUNT = Object.keys(HEX_GRID).length;
console.log('Hex grid loaded:', HEX_COUNT, 'hexes, max score:', MAX_SCORE);

function bar(pct) {
  const filled = Math.round(Math.max(0, Math.min(1, pct)) * 10);
  return '▓'.repeat(filled) + '░'.repeat(10 - filled);
}
function hexPopupHtml(tid, h) {
  const zoneLabel = h.zone === 'recommended' ? '🟣 Рекомендуемая' :
                    h.zone === 'not_allowed' ? '⛔ Запрещённая' : '⚪ Белая';
  // Samarkand hexes are display-only (no scoring / joymee / population)
  if (h.city === 'Samarkand') {
    return `
      <h3 style="margin:0 0 6px;">${tid} <span style="font-weight:400; color:#888; font-size:12px;">${zoneLabel}</span></h3>
      <div style="font-size:13px; margin-bottom:6px;">
        <span style="background:#e0e7ff; color:#3730a3; padding:2px 8px; border-radius:3px; font-weight:600; font-size:11px;">🏙 Самарканд</span>
      </div>
      <div style="font-size:12px; color:#666;">Данные скоринга по Самарканду не собираются.<br>Показаны только зоны Узума и номер гекса.</div>
    `;
  }
  const c = h.components;
  return `
    <h3 style="margin:0 0 6px;">${tid} <span style="font-weight:400; color:#888; font-size:12px;">${zoneLabel}</span></h3>
    <div style="margin-bottom:8px; font-size:13px;">
      <b>Ранг #${h.rank}</b> из ${HEX_COUNT} •
      <span style="background:${scoreColor(h.score / MAX_SCORE)}; padding:1px 6px; border-radius:3px; color:#222; font-weight:600;">скор ${(h.score*100).toFixed(0)}%</span>
    </div>
    <table style="font-size:12px; border-collapse:collapse; width:100%;">
      <tr><td><b>Население</b> <small style="color:#999;">(50%)</small></td>
          <td><tt>${bar(c.population)}</tt> <b>${h.pop.toFixed(0)} чел/гекс</b></td></tr>
      <tr><td><b>Аренда</b> <small style="color:#999;">(50%)</small></td>
          <td>${c.price !== null && c.price !== undefined ? `<tt>${bar(c.price)}</tt> <b>$${h.price_per_m2}/м²</b> <small style="color:#999;">(${h.price_sample_size} ann.)</small>` : '<span style="color:#999;">нет данных</span>'}</td></tr>
      <tr style="color:#999;"><td colspan="2" style="padding-top:8px; font-size:11px;">Справочно:</td></tr>
      <tr><td>Объявления joymee</td><td><b>${h.n_listings} шт</b></td></tr>
      <tr><td>Доля «1-я линия»</td><td><b>${(h.frac_first*100).toFixed(0)}%</b></td></tr>
      <tr><td>До метро</td><td><b>${(h.d_metro/1000).toFixed(2)} км</b></td></tr>
      <tr><td>До ближайшего ПВЗ</td><td><b>${(h.d_pvz/1000).toFixed(2)} км</b></td></tr>
      ${uzumPopRows(h.h3)}
      ${poiRows(h)}
    </table>
  `;
}

// Points of attraction near this hex. The dataset covers the region only, so inside the
// city these read 0 — say so rather than letting a zero look like "nothing is here".
function poiRows(h) {
  const zhkRow = (h.zhk_5km !== undefined && h.zhk_5km > 0)
    ? `<tr><td>Новостройки (${ZHK_RADIUS_KM} км)</td><td><b>${h.zhk_5km}</b> ЖК`
      + `<small style="color:#999;">, ${h.zhk_apts_5km.toLocaleString('ru-RU')} квартир</small></td></tr>`
    : '';
  if (h.markets_3km === undefined) return zhkRow;
  const total = h.markets_3km + h.supermarkets_3km + h.banks_3km;
  const km = v => (v === null || v === undefined) ? '—' : `${v} км`;
  if (!total) {
    return `<tr style="color:#999;"><td colspan="2" style="padding-top:8px; font-size:11px;">`
      + `В радиусе ${POI_RADIUS_KM} км по данным OSM ничего не отмечено`
      + `<br>(набор собран по области, без города — внутри города здесь всегда 0)</td></tr>` + zhkRow;
  }
  return `
      <tr style="color:#999;"><td colspan="2" style="padding-top:8px; font-size:11px;">Рядом (${POI_RADIUS_KM} км), OSM:</td></tr>
      <tr><td>Рынки</td><td><b>${h.markets_3km}</b> <small style="color:#999;">ближайший ${km(h.nearest_market_km)}</small></td></tr>
      <tr><td>Супермаркеты</td><td><b>${h.supermarkets_3km}</b> <small style="color:#999;">из них сетевых ${h.chain_3km}, ближайший сетевой ${km(h.nearest_chain_km)}</small></td></tr>
      <tr><td>Банки</td><td><b>${h.banks_3km}</b> <small style="color:#999;">ближайший ${km(h.nearest_bank_km)}</small></td></tr>` + zhkRow;
}

const heatmapLayer = L.geoJSON({type:'FeatureCollection', features: HEX_POLYGONS}, {
  pane: 'heatmapPane',
  renderer: heatmapRenderer,
  style: f => {
    const h = HEX_GRID[f.properties.tid];
    return {
      color: 'transparent', weight: 0,        // no border — let colors speak
      fillColor: scoreColor(h ? h.score / MAX_SCORE : 0),
      fillOpacity: 0.75,                       // bright, saturated heatmap
    };
  },
  onEachFeature: (feat, layer) => {
    const tid = feat.properties.tid;
    const h = HEX_GRID[tid];
    if (!h) return;
    layer.bindPopup(() => hexPopupHtml(tid, h), {maxWidth: 320});
  },
});

// === Hex labels layer (T-XXXX text on each hex, only visible at zoom >= 14) ===
const labelLayer = L.layerGroup();
const labelMarkers = [];
Object.entries(HEX_GRID).forEach(([tid, h]) => {
  const icon = L.divIcon({
    html: `<div style="font-size:10px; color:#222; font-weight:600; text-shadow:0 0 3px #fff, 0 0 2px #fff; white-space:nowrap; pointer-events:none;">${tid}</div>`,
    className: 'hex-label',
    iconSize: [50, 12],
    iconAnchor: [25, 6],
  });
  const m = L.marker([h.lat, h.lng], {icon, interactive: false, pane: 'tooltipPane'});
  labelMarkers.push(m);
});
function updateLabelVisibility() {
  if (!map.hasLayer(labelLayer)) return;
  const z = map.getZoom();
  if (z >= 14) {
    labelMarkers.forEach(m => { if (!labelLayer.hasLayer(m)) m.addTo(labelLayer); });
  } else {
    labelLayer.clearLayers();
  }
}
map.on('zoomend', updateLabelVisibility);

document.getElementById('layer-heatmap').addEventListener('change', e => {
  if (e.target.checked) heatmapLayer.addTo(map); else map.removeLayer(heatmapLayer);
});
document.getElementById('layer-uzumpop').addEventListener('change', e => {
  if (e.target.checked) uzumPopLayer.addTo(map); else map.removeLayer(uzumPopLayer);
});
document.getElementById('layer-labels').addEventListener('change', e => {
  if (e.target.checked) { labelLayer.addTo(map); updateLabelVisibility(); }
  else { map.removeLayer(labelLayer); }
});

// Master "select all / deselect all" for layers
const layerAll = document.getElementById('layer-all');
const layerToggles = document.querySelectorAll('.layer-toggle');
layerAll.addEventListener('change', e => {
  layerToggles.forEach(cb => {
    if (cb.checked !== e.target.checked) {
      cb.checked = e.target.checked;
      cb.dispatchEvent(new Event('change'));
    }
  });
});
function syncLayerMaster() {
  const checked = [...layerToggles].filter(cb => cb.checked).length;
  if (checked === layerToggles.length) { layerAll.checked = true; layerAll.indeterminate = false; }
  else if (checked === 0) { layerAll.checked = false; layerAll.indeterminate = false; }
  else { layerAll.indeterminate = true; }
}
layerToggles.forEach(cb => cb.addEventListener('change', syncLayerMaster));

const TAG_BY_ID = {};
TAG_META.forEach(t => TAG_BY_ID[t[0]] = {name: t[1], icon: t[2]});
TAG_BY_ID['__other__'] = {name: 'Прочая категория', icon: '❔'};

const tagCounts = {};
let otherCount = 0;
POINTS.forEach(p => {
  if (!p.tags || p.tags.length === 0) { otherCount++; return; }
  p.tags.forEach(t => { tagCounts[t] = (tagCounts[t]||0)+1; });
});
const tagBox = document.getElementById('tag-chips');
const selectedTags = new Set();
function makeChip(id, name, icon, count) {
  const el = document.createElement('span');
  el.className = 'tag-chip'; el.dataset.tag = id;
  el.innerHTML = `${icon} ${name} <span class="count">${count}</span>`;
  el.addEventListener('click', () => {
    if (selectedTags.has(id)) { selectedTags.delete(id); el.classList.remove('active'); }
    else { selectedTags.add(id); el.classList.add('active'); }
    render();
  });
  return el;
}
// Tags pre-selected when the page opens (PVZ-relevant set + "Прочая категория")
const DEFAULT_ACTIVE_TAGS = new Set([
  'street_facing','retail_shop','beauty_service','education','showroom',
  'basement_floor','ground_floor','universal','pvz_explicit',
  '__other__',
]);
TAG_META.forEach(([id, name, icon]) => {
  const c = tagCounts[id] || 0;
  if (c === 0) return;
  const chip = makeChip(id, name, icon, c);
  if (DEFAULT_ACTIVE_TAGS.has(id)) {
    selectedTags.add(id);
    chip.classList.add('active');
  }
  tagBox.appendChild(chip);
});
if (otherCount > 0) {
  const otherChip = makeChip('__other__', 'Прочая категория', '❔', otherCount);
  if (DEFAULT_ACTIVE_TAGS.has('__other__')) {
    selectedTags.add('__other__');
    otherChip.classList.add('active');
  }
  tagBox.appendChild(otherChip);
}

// Update zone-count badges (totals, not filtered)
const totalsByZone = {recommended:0, not_allowed:0, unknown:0};
POINTS.forEach(p => totalsByZone[p.zone]++);
document.getElementById('cnt-rec').textContent = totalsByZone.recommended;
document.getElementById('cnt-unknown').textContent = totalsByZone.unknown;
document.getElementById('cnt-forb').textContent = totalsByZone.not_allowed;

const cluster = L.markerClusterGroup({
  showCoverageOnHover: false, maxClusterRadius: 35, spiderfyOnMaxZoom: true,
});
map.addLayer(cluster);

document.getElementById('layer-joymee').addEventListener('change', e => {
  if (e.target.checked) map.addLayer(cluster); else map.removeLayer(cluster);
});

function colorFor(p) {
  if (p.price_usd == null) return '#999';
  return p.price_usd < 600 ? '#22c55e' : '#eab308';
}

function popupHtml(p) {
  let price_str = '?';
  if (p.price_usd != null) {
    price_str = '$' + Math.round(p.price_usd).toLocaleString('ru-RU') + '/мес';
    if (p.price <= 60 && p.area) price_str += ' (~$' + p.price + '/м²)';
  } else if (p.price && p.currency === 1) {
    price_str = Number(p.price).toLocaleString('ru-RU') + ' UZS';
  } else if (p.price) {
    price_str = p.price + ' (cur=' + p.currency + ')';
  }
  const area = p.area ? Math.round(p.area) + ' м²' : '—';
  const dist = (p.district || '').replace(' tumani','').replace(' shahri','');
  const zoneTag = `<span class="zone-tag zone-${p.zone}">${p.zone === 'recommended' ? '✅ recommended' : p.zone === 'not_allowed' ? '⛔ not_allowed' : '⚪ unknown'}</span>`;
  const phoneClean = (p.phone||'').replace(/[^+0-9]/g,'');
  const tagsHtml = (p.tags && p.tags.length) ? `<div class="popup-tags">${
    p.tags.map(tid => {
      const meta = TAG_BY_ID[tid]; if (!meta) return '';
      const isPrim = tid === p.primary;
      return `<span class="popup-tag${isPrim ? ' primary':''}">${meta.icon} ${meta.name}</span>`;
    }).join('')
  }</div>` : '<div class="popup-tags"><span class="popup-tag primary">❔ Прочая категория</span></div>';
  // Age badge
  let ageStr = '';
  if (p._ts) {
    const days = Math.floor((NOW_TS - p._ts) / 86400000);
    const hours = Math.floor((NOW_TS - p._ts) / 3600000);
    let label, color;
    if (hours < 24) { label = `${hours}ч назад`; color = '#22c55e'; }
    else if (days < 3) { label = `${days} д. назад`; color = '#22c55e'; }
    else if (days < 7) { label = `${days} д. назад`; color = '#eab308'; }
    else if (days < 30) { label = `${days} д. назад`; color = '#999'; }
    else { label = `${Math.round(days/30)} мес. назад`; color = '#999'; }
    ageStr = ` <span style="background:${color}1f;color:${color};padding:1px 6px;border-radius:3px;font-size:11px;font-weight:600;">${label}</span>`;
  }
  // No photo gallery: joymee serves 10-minute pre-signed image URLs, so nothing we embed
  // is still alive when a visitor opens the map. The old gallery markup also carried a
  // literal </scr'+'ipt> inside this template string, which ended the page's script element
  // early and left the map blank — do not reintroduce inline script tags here.
  // Full description with expandable / collapsible box
  const descId = `desc-${p.id}`;
  const description = (p.desc || '').trim();
  const shortDesc = description.length > 250 ? description.slice(0, 250) + '…' : description;
  const descHtml = description ? `
    <div class="popup-desc" id="${descId}">
      <div class="desc-body">${escapeHtml(shortDesc).replace(/\\n/g, '<br>')}</div>
      ${description.length > 250 ? `<a href="#" class="desc-toggle" onclick="descToggle('${descId}', event, ${JSON.stringify(description).replace(/'/g, '&#39;')})">показать полностью</a>` : ''}
    </div>
  ` : '';
  // Floor info
  const floorStr = (p.floor != null && p.floors_count) ? `${p.floor}/${p.floors_count}` : (p.floor != null ? String(p.floor) : '');
  // Seller
  const sellerStr = p.seller_name ? `<div class="popup-row"><b>Продавец:</b> ${escapeHtml(p.seller_name)}</div>` : '';
  // What is around this address. Chain stores are called out because the chains do their
  // own geo-analysis before opening — someone already bet money that people come here.
  const joymeeNote = `<div style="font-size:10px; color:#999; margin-top:6px; padding:4px 6px; background:#fef3c7; border-radius:3px;">💡 joymee.uz веб-версия отключена. Вся информация здесь.</div>`;
  return `
    <h3>${escapeHtml(p.title)}</h3>
    <div class="popup-row"><b>Район:</b> ${escapeHtml(dist)} ${zoneTag}${ageStr}</div>
    ${tagsHtml}
    <div class="popup-row"><b>Цена:</b> ${price_str}</div>
    <div class="popup-row"><b>Площадь:</b> ${area}${floorStr ? ` · <b>Этаж:</b> ${floorStr}` : ''}</div>
    <div class="popup-row"><b>Адрес:</b> ${escapeHtml(p.address||'')}</div>
    ${poiNearRow(p.poi)}
    ${descHtml}
    ${sellerStr}
    <div class="popup-row"><b>Тел:</b> <a class="tel" href="tel:${phoneClean}">${escapeHtml(p.phone||'')}</a></div>
    <div class="popup-row" style="color:#999; font-size:11px;"><b>ID:</b> ${p.id}</div>
    ${joymeeNote}
  `;
}
window.descToggle = function(descId, evt, fullText) {
  evt.preventDefault();
  const el = document.getElementById(descId);
  if (!el) return;
  const body = el.querySelector('.desc-body');
  const toggle = el.querySelector('.desc-toggle');
  const expanded = el.dataset.expanded === '1';
  if (expanded) {
    body.innerHTML = escapeHtml(fullText.slice(0, 250) + '…').replace(/\\n/g, '<br>');
    toggle.textContent = 'показать полностью';
    el.dataset.expanded = '0';
  } else {
    body.innerHTML = escapeHtml(fullText).replace(/\\n/g, '<br>');
    toggle.textContent = 'свернуть';
    el.dataset.expanded = '1';
  }
};
function escapeHtml(s) {
  return String(s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

let tagAndMode = false;
const tagAndCheckbox = document.getElementById('tag-and');
const tagModeLabel = document.getElementById('tag-mode-label');
tagAndCheckbox.addEventListener('change', e => {
  tagAndMode = e.target.checked;
  tagModeLabel.textContent = tagAndMode ? '(все выбранные)' : '(любой из выбранных)';
  render();
});

// Pre-compute timestamps + age-in-days for every point
POINTS.forEach(p => {
  if (p.created_at) {
    const t = Date.parse(p.created_at);
    p._ts = isNaN(t) ? null : t;
  } else p._ts = null;
});
// "Now" = newest listing time (safer than client clock for cross-tz)
const NOW_TS = Math.max(...POINTS.map(p => p._ts || 0));
POINTS.forEach(p => {
  p._age_days = p._ts ? (NOW_TS - p._ts) / 86400000 : 999999;
});

// Update count badges next to each freshness checkbox
function updateFreshCounts() {
  document.querySelectorAll('.fresh-count').forEach(el => {
    const min = parseFloat(el.dataset.min), max = parseFloat(el.dataset.max);
    const n = POINTS.filter(p => p._age_days >= min && p._age_days < max).length;
    el.textContent = n;
  });
  document.getElementById('fresh-all-count').textContent = POINTS.length;
}
updateFreshCounts();

// Master "select all / deselect all" toggle for freshness
const freshAll = document.getElementById('fresh-all');
const freshBuckets = document.querySelectorAll('.fresh-bucket');
freshAll.addEventListener('change', e => {
  freshBuckets.forEach(cb => { cb.checked = e.target.checked; });
  render();
});
// When any bucket changes, sync master state (checked / unchecked / indeterminate)
function syncFreshMaster() {
  const checked = [...freshBuckets].filter(cb => cb.checked).length;
  if (checked === freshBuckets.length) { freshAll.checked = true; freshAll.indeterminate = false; }
  else if (checked === 0) { freshAll.checked = false; freshAll.indeterminate = false; }
  else { freshAll.indeterminate = true; }
}
freshBuckets.forEach(cb => cb.addEventListener('change', syncFreshMaster));
syncFreshMaster();  // sync master state from the initial HTML checked attributes

// Price buckets: counts, master toggle
function priceInBucket(p, min, max) {
  if (min === -1 && max === 0) return p.price_usd == null;  // "no USD price" bucket
  if (p.price_usd == null) return false;
  return p.price_usd >= min && p.price_usd < max;
}
function updatePriceCounts() {
  document.querySelectorAll('.price-count').forEach(el => {
    const min = parseFloat(el.dataset.min), max = parseFloat(el.dataset.max);
    el.textContent = POINTS.filter(p => priceInBucket(p, min, max)).length;
  });
  document.getElementById('price-all-count').textContent = POINTS.length;
}
updatePriceCounts();

const priceAll = document.getElementById('price-all');
const priceBuckets = document.querySelectorAll('.price-bucket');
priceAll.addEventListener('change', e => {
  priceBuckets.forEach(cb => { cb.checked = e.target.checked; });
  render();
});
function syncPriceMaster() {
  const checked = [...priceBuckets].filter(cb => cb.checked).length;
  if (checked === priceBuckets.length) { priceAll.checked = true; priceAll.indeterminate = false; }
  else if (checked === 0) { priceAll.checked = false; priceAll.indeterminate = false; }
  else { priceAll.indeterminate = true; }
}
priceBuckets.forEach(cb => cb.addEventListener('change', syncPriceMaster));
priceBuckets.forEach(cb => cb.addEventListener('change', render));
syncPriceMaster();  // sync master state from the initial HTML checked attributes

// Single point-passes-filters function. opts.skipFresh / skipPrice — exclude that group from the check.
function buildFilterCtx() {
  const pmin = parseFloat(document.getElementById('pmin').value);
  const pmax = parseFloat(document.getElementById('pmax').value);
  const amin = parseFloat(document.getElementById('amin').value);
  const amax = parseFloat(document.getElementById('amax').value);
  const allowZones = new Set();
  if (document.getElementById('zf-rec').checked) allowZones.add('recommended');
  if (document.getElementById('zf-unknown').checked) allowZones.add('unknown');
  if (document.getElementById('zf-forb').checked) allowZones.add('not_allowed');
  const freshBucketsArr = [];
  document.querySelectorAll('.fresh-bucket:checked').forEach(cb => {
    freshBucketsArr.push([parseFloat(cb.dataset.min), parseFloat(cb.dataset.max)]);
  });
  const allFreshSelected = document.querySelectorAll('.fresh-bucket').length === freshBucketsArr.length;
  const priceBucketsArr = [];
  document.querySelectorAll('.price-bucket:checked').forEach(cb => {
    priceBucketsArr.push([parseFloat(cb.dataset.min), parseFloat(cb.dataset.max)]);
  });
  const allPriceSelected = document.querySelectorAll('.price-bucket').length === priceBucketsArr.length;
  return {pmin, pmax, amin, amax, allowZones, freshBucketsArr, allFreshSelected, priceBucketsArr, allPriceSelected};
}

function passesFilters(p, ctx, opts={}) {
  if (!ctx.allowZones.has(p.zone)) return false;
  if (!opts.skipFresh && !ctx.allFreshSelected) {
    let inBucket = false;
    for (const [mn, mx] of ctx.freshBucketsArr) {
      if (p._age_days >= mn && p._age_days < mx) { inBucket = true; break; }
    }
    if (!inBucket) return false;
  }
  if (!opts.skipPrice && !ctx.allPriceSelected) {
    let inBucket = false;
    for (const [mn, mx] of ctx.priceBucketsArr) {
      if (mn === -1 && mx === 0) {
        if (p.price_usd == null) { inBucket = true; break; }
      } else if (p.price_usd != null && p.price_usd >= mn && p.price_usd < mx) {
        inBucket = true; break;
      }
    }
    if (!inBucket) return false;
  }
  if (!isNaN(ctx.pmin) && (p.price_usd == null || p.price_usd < ctx.pmin)) return false;
  if (!isNaN(ctx.pmax) && (p.price_usd == null || p.price_usd > ctx.pmax)) return false;
  if (!isNaN(ctx.amin) && (!p.area || p.area < ctx.amin)) return false;
  if (!isNaN(ctx.amax) && (!p.area || p.area > ctx.amax)) return false;
  if (selectedTags.size) {
    const tags = new Set(p.tags || []);
    const isOther = !p.tags || p.tags.length === 0;
    const effective = new Set(tags);
    if (isOther) effective.add('__other__');
    if (tagAndMode) {
      for (const t of selectedTags) if (!effective.has(t)) return false;
    } else {
      let any = false;
      for (const t of selectedTags) if (effective.has(t)) { any = true; break; }
      if (!any) return false;
    }
  }
  return true;
}

function recomputeBucketCounts(ctx) {
  // Freshness counts: how many points in each freshness bucket pass all OTHER filters
  document.querySelectorAll('.fresh-count').forEach(el => {
    const min = parseFloat(el.dataset.min), max = parseFloat(el.dataset.max);
    const n = POINTS.filter(p => passesFilters(p, ctx, {skipFresh:true}) && p._age_days >= min && p._age_days < max).length;
    el.textContent = n;
  });
  document.getElementById('fresh-all-count').textContent = POINTS.filter(p => passesFilters(p, ctx, {skipFresh:true})).length;
  // Price counts: how many points in each price bucket pass all OTHER filters
  document.querySelectorAll('.price-count').forEach(el => {
    const min = parseFloat(el.dataset.min), max = parseFloat(el.dataset.max);
    const n = POINTS.filter(p => {
      if (!passesFilters(p, ctx, {skipPrice:true})) return false;
      if (min === -1 && max === 0) return p.price_usd == null;
      return p.price_usd != null && p.price_usd >= min && p.price_usd < max;
    }).length;
    el.textContent = n;
  });
  document.getElementById('price-all-count').textContent = POINTS.filter(p => passesFilters(p, ctx, {skipPrice:true})).length;
}

// Keep references to currently-rendered markers, indexed by zone, for click-to-focus
const markersByZone = {recommended:[], unknown:[], not_allowed:[]};

function render() {
  const ctx = buildFilterCtx();
  recomputeBucketCounts(ctx);

  cluster.clearLayers();
  markersByZone.recommended = [];
  markersByZone.unknown = [];
  markersByZone.not_allowed = [];
  const counts = {recommended:0, not_allowed:0, unknown:0};
  let shown = 0;

  POINTS.forEach(p => {
    if (!passesFilters(p, ctx)) return;
    const color = colorFor(p);
    const marker = L.circleMarker([p.lat, p.lng], {
      radius: 6, color:'#fff', weight:1.5, fillColor: color, fillOpacity: 0.95,
    });
    marker.bindPopup(popupHtml(p), {maxWidth: 280});
    cluster.addLayer(marker);
    markersByZone[p.zone].push(marker);
    counts[p.zone]++;
    shown++;
  });

  const z = (zone, label, color) => `<a href="#" class="zone-focus" data-zone="${zone}" style="color:${color}; cursor:pointer; text-decoration:none; padding:0 2px;">${label} ${counts[zone]}</a>`;
  document.getElementById('stat').innerHTML =
    `Показано <b>${shown}</b> из ${POINTS.length} • ${z('recommended','🟣','#5a00cc')} · ${z('unknown','⚪','#888')} · ${z('not_allowed','⛔','#666')}`;
  document.querySelectorAll('.zone-focus').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      focusZone(a.dataset.zone);
    });
  });
}

function focusZone(zone) {
  // Switch zone-filter to show ONLY this zone
  document.getElementById('zf-rec').checked = (zone === 'recommended');
  document.getElementById('zf-unknown').checked = (zone === 'unknown');
  document.getElementById('zf-forb').checked = (zone === 'not_allowed');
  // Make sure BOTH hex overlays stay visible (so user keeps context)
  const layerRecCb = document.getElementById('layer-rec');
  const layerForbCb = document.getElementById('layer-forb');
  if (!layerRecCb.checked) { layerRecCb.checked = true; layerRecCb.dispatchEvent(new Event('change')); }
  if (!layerForbCb.checked) { layerForbCb.checked = true; layerForbCb.dispatchEvent(new Event('change')); }
  render();
  const markers = markersByZone[zone];
  if (!markers.length) return;
  if (markers.length === 1) {
    const m = markers[0];
    map.setView(m.getLatLng(), 17);
    setTimeout(() => {
      cluster.zoomToShowLayer(m, () => m.openPopup());
    }, 100);
  } else {
    const group = L.featureGroup(markers);
    map.fitBounds(group.getBounds(), {padding:[40,40], maxZoom: 16});
  }
}

['pmin','pmax','amin','amax','zf-rec','zf-unknown','zf-forb'].forEach(id => {
  document.getElementById(id).addEventListener('input', render);
  document.getElementById(id).addEventListener('change', render);
});
document.querySelectorAll('.fresh-bucket').forEach(cb => cb.addEventListener('change', render));

// ============================================================
// Expert layers + pick mode
// ============================================================

// Map T-ID → polygon GeoJSON (for fast lookup when rendering expert layers)
const POLY_BY_TID = {};
HEX_POLYGONS.forEach(p => { POLY_BY_TID[p.properties.tid] = p; });

// Compute consensus: hexes picked by 2+ experts
const expertsByTid = {};
Object.entries(EXPERT_PICKS).forEach(([key, info]) => {
  (info.hexes || []).forEach(tid => {
    if (!expertsByTid[tid]) expertsByTid[tid] = [];
    expertsByTid[tid].push(key);
  });
});
const consensusTids = Object.keys(expertsByTid).filter(tid => expertsByTid[tid].length >= 2);
console.log(`Consensus hexes (≥2 experts): ${consensusTids.length}`);

// Create one Leaflet layer per expert (server-committed picks)
const expertLayers = {};
function buildExpertLayer(key, info) {
  const features = (info.hexes || [])
    .map(tid => POLY_BY_TID[tid])
    .filter(Boolean);
  const layer = L.geoJSON({type:'FeatureCollection', features}, {
    style: () => ({color: info.color, weight: 2.5, fillColor: info.color, fillOpacity: 0.35}),
    onEachFeature: (feat, lyr) => {
      const tid = feat.properties.tid;
      const allExperts = (expertsByTid[tid] || []).map(k => `${EXPERT_PICKS[k].emoji||''} ${EXPERT_PICKS[k].name}`).join(', ');
      const isConsensus = (expertsByTid[tid] || []).length >= 2;
      lyr.bindPopup(`
        <b>${info.emoji||''} Выбрано экспертом: ${info.name}</b><br>
        <span style="font-size:11px;">${tid}</span>
        ${isConsensus ? `<br><br><b style="color:#d97706;">🌟 КОНСЕНСУС:</b><br>${allExperts}` : ''}
      `);
    },
  });
  return layer;
}
Object.entries(EXPERT_PICKS).forEach(([key, info]) => {
  expertLayers[key] = buildExpertLayer(key, info);
});

// Create a separate "consensus" highlight layer — sits on top with gold dashed border
const consensusFeatures = consensusTids.map(tid => POLY_BY_TID[tid]).filter(Boolean);
const consensusLayer = L.geoJSON({type:'FeatureCollection', features: consensusFeatures}, {
  style: () => ({color: '#d97706', weight: 4, dashArray: '8,5', fillColor: '#fbbf24', fillOpacity: 0.15}),
  interactive: false,  // expert layer below handles the popup
});

// UI: list expert toggles in the "Выборы экспертов" panel
const expertsListEl = document.getElementById('experts-list');
Object.entries(EXPERT_PICKS).forEach(([key, info]) => {
  const wrap = document.createElement('label');
  wrap.className = 'zone-check';
  wrap.style.background = info.color + '14';  // ~8% opacity hex tint
  wrap.style.borderLeft = `4px solid ${info.color}`;
  wrap.innerHTML = `
    <input type="checkbox" data-expert="${key}" checked/>
    <span style="font-weight:600; color:${info.color};">${info.emoji||''} ${info.name}</span>
    <span style="margin-left:auto; color:#999;">${(info.hexes||[]).length}</span>
  `;
  expertsListEl.appendChild(wrap);
  // Default: add layer to map
  expertLayers[key].addTo(map);
  wrap.querySelector('input').addEventListener('change', e => {
    if (e.target.checked) {
      expertLayers[key].addTo(map);
      // Re-add consensus on top so it stays visible
      if (document.getElementById('consensus-toggle')?.checked && map.hasLayer(consensusLayer)) {
        consensusLayer.bringToFront();
      }
    }
    else map.removeLayer(expertLayers[key]);
  });
});

// Add the consensus toggle row (only if there ARE consensus picks)
if (consensusTids.length > 0) {
  const wrap = document.createElement('label');
  wrap.className = 'zone-check';
  wrap.style.background = 'linear-gradient(to right, #fef3c7, #fde68a)';
  wrap.style.borderLeft = '4px solid #d97706';
  wrap.style.marginTop = '6px';
  wrap.innerHTML = `
    <input type="checkbox" id="consensus-toggle" checked/>
    <span style="font-weight:600; color:#d97706;">🌟 Консенсус ≥2 экспертов</span>
    <span style="margin-left:auto; color:#999;">${consensusTids.length}</span>
  `;
  expertsListEl.appendChild(wrap);
  consensusLayer.addTo(map);
  consensusLayer.bringToFront();
  wrap.querySelector('input').addEventListener('change', e => {
    if (e.target.checked) {
      consensusLayer.addTo(map);
      consensusLayer.bringToFront();
    } else {
      map.removeLayer(consensusLayer);
    }
  });
}

// ===== Pick mode (only when URL ?pick=expertkey) =====
const urlParams = new URLSearchParams(window.location.search);
const pickKey = urlParams.get('pick');
const pickInfo = pickKey ? EXPERT_PICKS[pickKey] : null;

if (pickInfo) {
  // Show floating panel
  document.getElementById('pick-panel').style.display = 'block';
  document.getElementById('pick-name').textContent = pickInfo.name;
  document.getElementById('pick-name').style.color = pickInfo.color;
  document.getElementById('pick-emoji').textContent = pickInfo.emoji || '';

  const storageKey = `picks-${pickKey}`;
  let localPicks = new Set(JSON.parse(localStorage.getItem(storageKey) || '[]'));

  // Build H3 cell → T-ID lookup table (so click latlng → hex without needing heatmap layer)
  const TID_BY_H3 = {};
  for (const tid in HEX_GRID) TID_BY_H3[HEX_GRID[tid].h3] = tid;

  // Visual layer for LOCAL picks (uses its OWN pane above heatmap so it stays visible
  // regardless of which other layers are on/off)
  map.createPane('pickLayerPane');
  map.getPane('pickLayerPane').style.zIndex = 460;
  map.getPane('pickLayerPane').style.pointerEvents = 'none';  // never intercepts clicks
  const localPickLayer = L.layerGroup().addTo(map);
  function rerenderLocalPicks() {
    localPickLayer.clearLayers();
    localPicks.forEach(tid => {
      const p = POLY_BY_TID[tid]; if (!p) return;
      L.geoJSON(p, {
        pane: 'pickLayerPane',
        style: () => ({
          color: pickInfo.color, weight: 3, dashArray: '5,4',
          fillColor: pickInfo.color, fillOpacity: 0.45,
        }),
        interactive: false,
      }).addTo(localPickLayer);
    });
    document.getElementById('pick-local-count').textContent = localPicks.size;
  }
  rerenderLocalPicks();

  let pickModeOn = false;
  const pickModeCb = document.getElementById('pick-mode-on');

  // When pick mode is on, suppress heatmap popups (so map.click can fire instead
  // of feature.popup-on-click consuming the event)
  function setHeatmapPopups(enabled) {
    heatmapLayer.eachLayer(layer => {
      const tid = layer.feature.properties.tid;
      if (enabled) {
        const h = HEX_GRID[tid];
        if (h) layer.bindPopup(() => hexPopupHtml(tid, h), {maxWidth: 320});
      } else {
        layer.unbindPopup();
      }
    });
  }

  pickModeCb.addEventListener('change', e => {
    pickModeOn = e.target.checked;
    setHeatmapPopups(!pickModeOn);
  });

  // Single click handler on map — works whether heatmap layer is on or off.
  // Fires only when no interactive feature (joymee marker, expert hex) consumed the click.
  map.on('click', e => {
    if (!pickModeOn) return;
    if (typeof h3 === 'undefined' || !h3.latLngToCell) {
      console.warn('h3-js not loaded yet'); return;
    }
    const cell = h3.latLngToCell(e.latlng.lat, e.latlng.lng, 9);
    const tid = TID_BY_H3[cell];
    if (!tid) return;  // outside our Tashkent grid
    if (localPicks.has(tid)) localPicks.delete(tid);
    else localPicks.add(tid);
    localStorage.setItem(storageKey, JSON.stringify([...localPicks]));
    rerenderLocalPicks();
  });

  document.getElementById('pick-copy').addEventListener('click', () => {
    const arr = [...localPicks].sort();
    const text = arr.join(', ');
    navigator.clipboard.writeText(text).then(() => {
      document.getElementById('pick-copy').textContent = '✅ Скопировано!';
      setTimeout(() => { document.getElementById('pick-copy').textContent = '📋 Скопировать список'; }, 2000);
    });
  });

  document.getElementById('pick-clear').addEventListener('click', () => {
    if (!confirm('Очистить всю локальную выборку?')) return;
    localPicks = new Set();
    localStorage.setItem(storageKey, '[]');
    rerenderLocalPicks();
  });
}

render();
</script>
</body>
</html>"""

html_doc = html_doc.replace('__POINTS__', json.dumps(points, ensure_ascii=False))
html_doc = html_doc.replace('__REC__', json.dumps(rec_features))
html_doc = html_doc.replace('__FORB__', json.dumps(forb_features))
html_doc = html_doc.replace('__DISTRICTS__', json.dumps(districts, ensure_ascii=False))
html_doc = html_doc.replace('__TAGS__', json.dumps(TAG_META, ensure_ascii=False))
html_doc = html_doc.replace('__UZUM_PVZ__', json.dumps(uzum_pvz_points))

# --- Uzum population layer -------------------------------------------------------------
# Compact payload: h3 -> [population, population_level, pedestrian_level]. Levels travel as
# 0/1/2 rather than LOW/MIDDLE/HIGH, and the geometry is NOT duplicated — the layer reuses
# the zone polygons already inlined above, keyed by their h3 property.
LEVEL_CODES = {"LOW": 0, "MIDDLE": 1, "HIGH": 2}
uzum_pop_compact = {}
for cell, rec in uzum_population.items():
    pop = rec.get('population')
    uzum_pop_compact[cell] = [
        pop,
        LEVEL_CODES.get(rec.get('population_level')),
        LEVEL_CODES.get(rec.get('pedestrian')),
    ]

# Population per hex is heavily right-skewed (a few dense hexes, a long thin tail), so an
# even split of the min..max range would paint almost everything the same colour. Break on
# percentiles of the observed values instead.
pop_values = sorted(v[0] for v in uzum_pop_compact.values() if v[0] is not None)
if pop_values:
    def _pct(p):
        return pop_values[min(len(pop_values) - 1, int(len(pop_values) * p))]
    pop_breaks = sorted({_pct(p) for p in (0.10, 0.25, 0.50, 0.75, 0.90, 0.97)})
    print(f"Uzum population: {len(pop_values)} hexes with data, "
          f"{len(uzum_pop_compact)-len(pop_values)} without; "
          f"breaks {pop_breaks}, max {pop_values[-1]:,}")
else:
    pop_breaks = []
    print("Uzum population: no values — density layer will render empty")

html_doc = html_doc.replace('__UZUM_POP__', json.dumps(uzum_pop_compact, separators=(',', ':')))
html_doc = html_doc.replace('__UZUM_POP_BREAKS__', json.dumps(pop_breaks))

# --- POI payload: positional arrays rather than objects, ~300 points but no reason to ship
# the key names 300 times over. [lat, lng, typeCode, chain, name, extra]
POI_TYPE_CODES = {'market': 0, 'supermarket': 1, 'bank': 2}
poi_compact = []
for f in poi_features:
    if f.get('geometry', {}).get('type') != 'Point':
        continue
    lng, lat = f['geometry']['coordinates']
    p = f['properties']
    code = POI_TYPE_CODES.get(p.get('type'))
    if code is None:
        continue
    extra = p.get('brand') if p.get('type') == 'supermarket' else p.get('profile')
    poi_compact.append([round(lat, 6), round(lng, 6), code,
                        1 if p.get('chain') else 0, p.get('name') or '', extra or ''])
poi_counts = {t: sum(1 for x in poi_compact if x[2] == c) for t, c in POI_TYPE_CODES.items()}
print(f"POI: {len(poi_compact)} points {poi_counts}, "
      f"chain supermarkets {sum(1 for x in poi_compact if x[3])}")

html_doc = html_doc.replace('__POI__', json.dumps(poi_compact, ensure_ascii=False, separators=(',', ':')))
html_doc = html_doc.replace('__POI_RADIUS_KM__', json.dumps(POI_RADIUS_KM))

# --- POI counts for the Uzum zone hexes --------------------------------------------------
# The scored T-XXXX grid stops at the city limits, so without this the region — the only
# place these OSM points actually cover — would have the counts computed nowhere.
# Only hexes with something nearby are shipped; the rest are absent and render as "нет".
uzum_poi_compact = {}
for cell in uzum_pop_compact:
    try:
        clat, clng = h3.cell_to_latlng(cell)
    except Exception:
        continue
    c, n = poi_stats(clat, clng)
    if not (c['markets'] or c['supermarkets'] or c['banks']):
        continue
    def _km(v): return round(v / 1000, 2) if v is not None else None
    uzum_poi_compact[cell] = [c['markets'], c['supermarkets'], c['chain'], c['banks'],
                              _km(n['market']), _km(n['chain']), _km(n['bank'])]
print(f"POI near Uzum hexes: {len(uzum_poi_compact)} of {len(uzum_pop_compact)} have something "
      f"within {POI_RADIUS_KM} km")
html_doc = html_doc.replace('__UZUM_POI__', json.dumps(uzum_poi_compact, separators=(',', ':')))


# --- Housing: official per-district totals joined onto the OSM district outlines ---------
housing_by_key = {v['_key']: (name, v) for name, v in housing_stats.get('districts', {}).items()}
HOUSING_YEARS = housing_stats.get('years', [])
district_features, unmatched = [], []
for f in district_geo.get('features', []):
    key = _territory_key(f['properties'].get('name_uz') or f['properties'].get('name'))
    hit = housing_by_key.get(key)
    if not hit:
        unmatched.append(f['properties'].get('name_uz'))
        continue
    ru_name, vals = hit
    district_features.append({
        "type": "Feature", "geometry": f["geometry"],
        "properties": {"name": ru_name,
                       "years": {y: vals.get(y) for y in HOUSING_YEARS if vals.get(y) is not None},
                       "sum3": vals.get('sum3')},
    })
if unmatched:
    print(f"  WARN: {len(unmatched)} district outlines had no housing figures: {unmatched[:5]}")
housing_vals = sorted(f['properties']['sum3'] for f in district_features
                      if f['properties'].get('sum3'))
if housing_vals:
    def _hpct(p): return housing_vals[min(len(housing_vals)-1, int(len(housing_vals)*p))]
    housing_breaks = sorted({round(_hpct(p), 1) for p in (0.2, 0.4, 0.6, 0.8, 0.92)})
else:
    housing_breaks = []
print(f"Housing: {len(district_features)} districts joined, "
      f"3-year total {sum(housing_vals):.0f} тыс. м², breaks {housing_breaks}")

# --- Residential complexes ---------------------------------------------------------------
zhk_compact = []
for f in novostroyki.get('features', []):
    if f.get('geometry', {}).get('type') != 'Point':
        continue
    lng, lat = f['geometry']['coordinates']
    p = f['properties']
    zhk_compact.append([round(lat, 6), round(lng, 6), p.get('name') or '',
                        p.get('district') or '', p.get('completion') or '',
                        p.get('status') or '', p.get('apartments') or 0,
                        p.get('floors') or 0, p.get('price_m2') or 0, p.get('url') or ''])
print(f"ЖК: {len(zhk_compact)} complexes, "
      f"{sum(z[6] for z in zhk_compact):,} apartments")

html_doc = html_doc.replace('__DISTRICTS_HOUSING__', json.dumps(district_features, ensure_ascii=False, separators=(',', ':')))
html_doc = html_doc.replace('__HOUSING_BREAKS__', json.dumps(housing_breaks))
html_doc = html_doc.replace('__HOUSING_YEARS__', json.dumps(HOUSING_YEARS))
html_doc = html_doc.replace('__ZHK__', json.dumps(zhk_compact, ensure_ascii=False, separators=(',', ':')))
html_doc = html_doc.replace('__ZHK_RADIUS_KM__', json.dumps(ZHK_RADIUS_KM))

places_data = _load_static('places_region.geojson', {'features': [], 'city_outline': None})
_places = [(f['geometry']['coordinates'][1], f['geometry']['coordinates'][0],
            f['properties'].get('name') or '', f['properties'].get('kind') or '')
           for f in places_data.get('features', [])
           if f.get('geometry', {}).get('type') == 'Point']
_city_shape = None
if places_data.get('city_outline'):
    from shapely.geometry import shape as _shp
    _city_shape = _shp(places_data['city_outline'])

def _nearest_place(lat, lng):
    best, bestd = None, None
    for plat, plng, name, kind in _places:
        d = haversine_m(lat, lng, plat, plng)
        if bestd is None or d < bestd:
            best, bestd = (name, kind), d
    return (best[0] if best else ''), (best[1] if best else ''), bestd

def _km_from_city(lat, lng):
    if _city_shape is None:
        return None
    from shapely.geometry import Point as _P
    from shapely.ops import nearest_points
    pt = _P(lng, lat)
    if _city_shape.contains(pt):
        return 0.0
    a, b = nearest_points(pt, _city_shape.boundary)
    return round(haversine_m(a.y, a.x, b.y, b.x) / 1000, 1)


# --- PVZ recommendation score -----------------------------------------------------------
# Hard filters first, score second. A forbidden zone must never be outranked by a big
# population — a veto is not "minus ten points". Distance to an existing Uzum PVZ is
# deliberately NOT scored: Uzum's own not_allowed zones already encode it.
PVZ_MIN_POP = 500          # median hex holds ~1 850; this drops empty land, not villages
PVZ_TOP_N = 200            # highlight a shortlist — painting half the region is not advice
PVZ_MIN_SEPARATION_M = 1500   # keep only the best hex per neighbourhood, see the thinning below
PVZ_PER_BAND = 60             # how many places to shortlist inside each distance band
BAND_EDGES = (30, 60)         # km from the Tashkent city limits
BAND_NAMES = (f'до {BAND_EDGES[0]} км', f'{BAND_EDGES[0]}–{BAND_EDGES[1]} км',
              f'более {BAND_EDGES[1]} км')

def _band(km):
    if km is None: return '?'
    if km < BAND_EDGES[0]: return BAND_NAMES[0]
    if km < BAND_EDGES[1]: return BAND_NAMES[1]
    return BAND_NAMES[2]
W_PEOPLE, W_TRAFFIC, W_GROWTH = 0.45, 0.30, 0.25
CHAIN_WEIGHT = 2.0         # a chain store already had someone else's money bet on the spot

district_growth = {}       # normalised territory key -> 3-year commissioned housing
for _name, _v in housing_stats.get('districts', {}).items():
    district_growth[_v['_key']] = _v.get('sum3') or 0
_growth_vals = sorted(v for v in district_growth.values() if v)

def _district_of(lat, lng):
    """Which district polygon covers this point (point-in-polygon over 22 outlines)."""
    from shapely.geometry import Point, shape
    pt = Point(lng, lat)
    for f in district_features:
        if shape(f['geometry']).contains(pt):
            return f['properties']['name']
    return None

# Pre-build the district shapes once — 6.6k hexes against 22 polygons is fine, rebuilding
# the geometry each time is not.
from shapely.geometry import Point as _Pt, shape as _shape
_district_shapes = [(f['properties']['name'], _shape(f['geometry']), f['properties'].get('sum3') or 0)
                    for f in district_features]

def _growth_at(lat, lng):
    pt = _Pt(lng, lat)
    for name, geom, sum3 in _district_shapes:
        if geom.contains(pt):
            return name, sum3
    return None, None

candidates = []
for cell, pop_rec in uzum_pop_compact.items():
    pop = pop_rec[0]
    if pop is None or pop < PVZ_MIN_POP:
        continue
    if cell in forb_set:                       # Uzum forbids it — no score can override that
        continue
    try:
        clat, clng = h3.cell_to_latlng(cell)
    except Exception:
        continue
    poi = uzum_poi_compact.get(cell)
    # Missing OSM data is NOT zero. A village with no mapped shops would otherwise sink for
    # being unmapped rather than for being bad, so traffic stays unknown and its weight is
    # redistributed over the components we do have.
    traffic = None
    if poi:
        traffic = poi[0] + (poi[1] - poi[2]) + poi[2] * CHAIN_WEIGHT + poi[3] * 0.5
    dname, dsum3 = _growth_at(clat, clng)
    # Outside the 22 district outlines means Tashkent city, where none of the supporting
    # data reaches — no OSM points, no commissioned-housing figure. Scoring a city hex on
    # population alone would put it next to region hexes judged on three signals, which is
    # not the same measurement at all.
    if dname is None:
        continue
    zhk = zhk_fields(clat, clng)
    growth = (dsum3 or 0) / 100.0 + zhk['zhk_apts_5km'] / 100.0
    candidates.append({'h3': cell, 'lat': clat, 'lng': clng, 'pop': pop,
                       'traffic': traffic, 'growth': growth, 'district': dname,
                       'zhk': zhk['zhk_5km'], 'zhk_apts': zhk['zhk_apts_5km'],
                       'zone': 'recommended' if cell in rec_set else 'unknown'})

def _pct_ranks(values):
    """Rank among peers, not raw magnitude — one 54 000-person hex would otherwise flatten
    everything else to zero."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for pos, i in enumerate(order):
        ranks[i] = (pos + 0.5) / len(values) if values else 0
    return ranks

if candidates:
    pop_r = _pct_ranks([c['pop'] for c in candidates])
    growth_r = _pct_ranks([c['growth'] for c in candidates])
    known_traffic = [i for i, c in enumerate(candidates) if c['traffic'] is not None]
    tr_r = _pct_ranks([candidates[i]['traffic'] for i in known_traffic])
    traffic_rank = {i: tr_r[k] for k, i in enumerate(known_traffic)}

    for i, c in enumerate(candidates):
        parts = [(W_PEOPLE, pop_r[i]), (W_GROWTH, growth_r[i])]
        if i in traffic_rank:
            parts.append((W_TRAFFIC, traffic_rank[i]))
        total_w = sum(w for w, _ in parts)
        c['score'] = round(sum(w * v for w, v in parts) / total_w, 4)
        c['components'] = {'people': round(pop_r[i], 3),
                           'traffic': round(traffic_rank[i], 3) if i in traffic_rank else None,
                           'growth': round(growth_r[i], 3)}
        c['known'] = len(parts)

    candidates.sort(key=lambda c: -c['score'])

    # Thin the shortlist out. Hexes are ~0.1 km², so the neighbours of a good hex score
    # almost the same and march into the list behind it — the first cut gave 30 "best
    # places" that were really 8, the top one repeated eleven times. Keeping the best of
    # each neighbourhood turns the list back into a set of choices.
    # Select inside each distance band separately. Ranked against everyone at once, the
    # far ring never appears: it has fewer mapped shops and less new housing, so its hexes
    # lose to the suburbs even though 1 032 of them hold 5.7 million people between them.
    # Three usable groups beat one long list that is really all the same ring.
    for c in candidates:
        c['city_km'] = _km_from_city(c['lat'], c['lng'])
        c['band'] = _band(c['city_km'])
    picked = []
    for band in BAND_NAMES:
        band_hexes = [c for c in candidates if c['band'] == band]
        chosen = []
        for c in band_hexes:
            if all(haversine_m(c['lat'], c['lng'], p['lat'], p['lng']) >= PVZ_MIN_SEPARATION_M
                   for p in chosen):
                chosen.append(c)
            if len(chosen) >= PVZ_PER_BAND:
                break
        for rank, c in enumerate(chosen, 1):
            c['rank'] = rank          # rank within the band — that is the useful comparison
        print(f"  {band}: {len(band_hexes)} candidates -> {len(chosen)} picked")
        picked.extend(chosen)
    candidates = picked

    # Is there anything to actually rent nearby? Flagged, never scored — a good spot with no
    # listing is still a good spot, the premises just have to be found another way.
    for c in candidates[:PVZ_TOP_N]:
        near = [p for p in points
                if haversine_m(c['lat'], c['lng'], p['lat'], p['lng']) <= 2000]
        c['rent'] = len(near)
        c['rent_best'] = min((p['price_usd'] for p in near if p.get('price_usd')), default=None)

shortlist = candidates[:PVZ_TOP_N]
pvz_compact = [[c['h3'], c['score'], c['rank'], c['pop'],
                c['components']['people'], c['components']['traffic'], c['components']['growth'],
                c['known'], c['district'] or '', c['zhk'], c['zhk_apts'],
                c.get('rent', 0), round(c['rent_best']) if c.get('rent_best') else 0]
               for c in shortlist]
print(f"PVZ score: {len(candidates)} candidate hexes (of {len(uzum_pop_compact)}), "
      f"top {len(shortlist)} shortlisted; "
      f"{sum(1 for c in candidates if c['known'] == 3)} scored on all three signals")
if shortlist:
    b = shortlist[0]
    print(f"  best: {b['district']} pop {b['pop']:,} score {b['score']} "
          f"(people {b['components']['people']}, traffic {b['components']['traffic']}, "
          f"growth {b['components']['growth']})")

# --- Describe each pick in words a person can act on -------------------------------------
# Coordinates alone are not a briefing. Distance is measured from the CITY LIMITS, not the
# centre: what matters operationally is how far out of town someone has to drive.
for c in shortlist:
    name, kind, dist_m = _nearest_place(c['lat'], c['lng'])
    c['place'] = name
    c['place_kind'] = kind
    c['place_km'] = round(dist_m / 1000, 1) if dist_m is not None else None

import collections as _coll
print("PVZ shortlist by distance from the city limits: "
      + ", ".join(f"{b}: {n}" for b, n in _coll.Counter(c['band'] for c in shortlist).items()))

# A file the team can be handed directly — the map is for looking, this is for working.
CSV_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'pvz_shortlist.csv')
import csv as _csv
_KIND_RU = {'city': 'город', 'town': 'город', 'village': 'село', 'hamlet': 'посёлок',
            'suburb': 'район города', 'neighbourhood': 'махалля'}
with open(CSV_PATH, 'w', encoding='utf-8-sig', newline='') as _f:
    w = _csv.writer(_f, delimiter=';')
    w.writerow(['Группа', 'Место', 'Балл, %', 'Широта', 'Долгота', 'Координаты для карт',
                'Ближайший пункт', 'Тип пункта', 'До пункта, км', 'От границы Ташкента, км',
                'Район', 'Население гекса, чел', 'Рынков 3км', 'Супермаркетов 3км',
                'Из них сетевых', 'Банков 3км', 'Новостроек 5км', 'Квартир в них',
                'Ввод жилья в районе за 3 года, тыс. м²', 'Объявлений аренды 2км',
                'Дешевейшее, $/мес', 'Зона Uzum', 'Признаков в оценке', 'Ссылка на карту'])
    for c in sorted(shortlist, key=lambda x: (x['city_km'] if x['city_km'] is not None else 1e9,
                                              x['rank'])):
        poi = uzum_poi_compact.get(c['h3']) or [0, 0, 0, 0, None, None, None]
        _dg = next((s for n, g, s in _district_shapes if n == c['district']), None)
        w.writerow([c['band'], c['rank'], round(c['score'] * 100),
                    round(c['lat'], 6), round(c['lng'], 6),
                    f"{c['lat']:.6f}, {c['lng']:.6f}",
                    c['place'], _KIND_RU.get(c['place_kind'], c['place_kind']), c['place_km'],
                    c['city_km'], c['district'] or '', c['pop'],
                    poi[0], poi[1], poi[2], poi[3], c['zhk'], c['zhk_apts'],
                    _dg if _dg else '', c.get('rent', 0),
                    round(c['rent_best']) if c.get('rent_best') else '',
                    'рекомендуемая' if c['zone'] == 'recommended' else 'белая',
                    f"{c['known']} из 3",
                    f"https://www.google.com/maps?q={c['lat']:.6f},{c['lng']:.6f}"])
print(f"  wrote {os.path.relpath(CSV_PATH)} ({os.path.getsize(CSV_PATH)//1024} KB)")

pvz_compact = [row + [c['place'], c['place_km'], c['city_km'], c['band']]
               for row, c in zip(pvz_compact, shortlist)]

# Everything the search box can match: settlements plus the shortlist's own places.
search_index = [[p[2], round(p[0], 6), round(p[1], 6), _KIND_RU.get(p[3], p[3])]
                for p in _places if p[2]]
html_doc = html_doc.replace('__SEARCH_INDEX__', json.dumps(search_index, ensure_ascii=False, separators=(',', ':')))
html_doc = html_doc.replace('__PVZ_PICKS__', json.dumps(pvz_compact, ensure_ascii=False, separators=(',', ':')))

# Hex grid data: scores + polygons (Tashkent)
hex_polygons = []
for tid, info in tashkent_grid['hexes'].items():
    boundary = h3.cell_to_boundary(info['h3'])
    ring = [[lng, lat] for (lat, lng) in boundary]; ring.append(ring[0])
    hex_polygons.append({
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"tid": tid},
    })

# Samarkand hexes: display-only (no scoring). Added to HEX_GRID as score=0 grey hexes
# but with labels (C-XXXX). They participate in the labels layer and popup.
for tid, info in samarkand_grid.get('hexes', {}).items():
    boundary = h3.cell_to_boundary(info['h3'])
    ring = [[lng, lat] for (lat, lng) in boundary]; ring.append(ring[0])
    hex_polygons.append({
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"tid": tid},
    })
    # Add to hex_scores with minimal placeholder data so JS side is happy
    hex_scores[tid] = {
        'h3': info['h3'], 'lat': info['lat'], 'lng': info['lng'],
        'city': 'Samarkand',
        'pop': 0, 'd_pvz': 0, 'n_listings': 0, 'n_first': 0,
        'frac_first': 0, 'd_metro': 99999, 'zone': 'unknown',
        'score': 0.0, 'rank': None,
        'price_per_m2': None, 'price_sample_size': None,
        'components': {'population': 0, 'price': None},
    }

html_doc = html_doc.replace('__HEX_GRID__', json.dumps(hex_scores, ensure_ascii=False))
html_doc = html_doc.replace('__HEX_POLYGONS__', json.dumps(hex_polygons))
html_doc = html_doc.replace('__EXPERT_PICKS__', json.dumps(expert_picks, ensure_ascii=False))

# Build timestamp in Tashkent time (UTC+5)
tashkent = timezone(timedelta(hours=5))
built_at = datetime.now(timezone.utc).astimezone(tashkent)
html_doc = html_doc.replace('__BUILT_DATE__', built_at.strftime('%d.%m.%Y'))
html_doc = html_doc.replace('__BUILT_TIME__', built_at.strftime('%H:%M ') + 'Ташкент')

OUT = '/tmp/joymee_uzum_map.html'
with open(OUT, 'w', encoding='utf-8') as f: f.write(html_doc)
import os
print(f"\n✅ Written: {OUT}  ({os.path.getsize(OUT)/1024:.0f} KB)")
