#!/usr/bin/env python3
"""Build self-contained interactive Leaflet map. All listings with coords → on map.
Zone filtering is done client-side via the left panel."""
import json, os, math
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

# Expert picks — each expert gets their own layer of selected hexes.
EXPERTS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'expert_picks.json')
expert_picks = json.load(open(EXPERTS_PATH))
print(f"Expert picks: " + ", ".join(f"{e['name']}={len(e['hexes'])}" for e in expert_picks.values()))
print(f"Tashkent grid: {len(tashkent_grid['hexes'])} hexes, {len(tashkent_grid['metro_stations'])} metro stations")

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
    points.append({
        "id": r['id'],
        "lat": r['latitude'], "lng": r['longitude'],
        "title": r['title'], "district": r['district_name'],
        "address": r['address_line'],
        "price": r['price'], "currency": r['currency'], "price_usd": usd_total(r),
        "area": r['area_m2'], "phone": r['phone_number'],
        "img": r.get('first_image'),
        "desc": (r.get('description') or '')[:300],
        "zone": z, "tags": r.get('tags') or [], "primary": r.get('primary') or 'other',
        "created_at": r.get('created_at'),
        "url": f"https://joymee.uz/ru/announcements/{r['id']}",
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
  .popup-img { width:240px; height:140px; object-fit:cover; border-radius:6px; display:block; margin-bottom:6px; background:#eee; }
  .popup-row { font-size:13px; margin-bottom:3px; }
  .popup-row b { color:#555; }
  .leaflet-popup-content { width:260px !important; margin:10px 14px; }
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

  <div class="filter" style="background:#fff; padding:10px; border-radius:6px; border:1px solid #e7e7e7;">
    <label style="margin-bottom:6px;">Слои на карте</label>
    <label class="zone-check" style="background:#f5f5f5; border-bottom:1px solid #e7e7e7; margin-bottom:6px; padding-bottom:6px;">
      <input type="checkbox" id="layer-all"/>
      <span style="font-weight:600;">Выбрать все / снять все</span>
      <span style="margin-left:auto; color:#999;">6</span>
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
// Tashkent hex grid with scoring: { "T-XXXX": {h3, lat, lng, score, rank, zone, pop, d_pvz, n_listings, frac_first, d_metro, components} }
const HEX_GRID = __HEX_GRID__;
// Pre-computed hex polygon GeoJSON features (one per hex)
const HEX_POLYGONS = __HEX_POLYGONS__;
// Expert picks: {key: {name, color, emoji, hexes: [T-ID...]}}
const EXPERT_PICKS = __EXPERT_PICKS__;
</script>
<script>
const map = L.map('map', { preferCanvas: true }).setView([41.31, 69.27], 12);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap', maxZoom: 19,
}).addTo(map);

// Custom pane for the score heatmap — below overlayPane so Uzum zone overlays stay
// on top. Clicks are now handled via map.click + h3 latLngToCell, so heatmap layer
// doesn't need to intercept clicks.
map.createPane('heatmapPane');
map.getPane('heatmapPane').style.zIndex = 380;
map.getPane('heatmapPane').style.pointerEvents = 'auto';
// Each pane needs its own canvas renderer (preferCanvas only applies to default panes)
const heatmapRenderer = L.canvas({pane: 'heatmapPane'});

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
    </table>
  `;
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
  const img = p.img ? `<img class="popup-img" src="${p.img}" loading="lazy" onerror="this.style.display='none'"/>` : '';
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
  return `
    ${img}
    <h3>${escapeHtml(p.title)}</h3>
    <div class="popup-row"><b>Район:</b> ${escapeHtml(dist)} ${zoneTag}${ageStr}</div>
    ${tagsHtml}
    <div class="popup-row"><b>Цена:</b> ${price_str}</div>
    <div class="popup-row"><b>Площадь:</b> ${area}</div>
    <div class="popup-row"><b>Адрес:</b> ${escapeHtml(p.address||'')}</div>
    <div class="popup-row"><b>Тел:</b> <a class="tel" href="tel:${phoneClean}">${escapeHtml(p.phone||'')}</a></div>
    <div class="popup-row"><b>ID:</b> ${p.id}</div>
    <a class="url" href="${p.url}" target="_blank" rel="noopener">Открыть на joymee →</a>
  `;
}
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
      lyr.bindPopup(`<b>${info.emoji||''} Выбрано экспертом: ${info.name}</b><br><span style="font-size:11px;">${tid}</span>`);
    },
  });
  return layer;
}
Object.entries(EXPERT_PICKS).forEach(([key, info]) => {
  expertLayers[key] = buildExpertLayer(key, info);
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
    if (e.target.checked) expertLayers[key].addTo(map);
    else map.removeLayer(expertLayers[key]);
  });
});

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

# Hex grid data: scores + polygons
hex_polygons = []
for tid, info in tashkent_grid['hexes'].items():
    boundary = h3.cell_to_boundary(info['h3'])
    ring = [[lng, lat] for (lat, lng) in boundary]; ring.append(ring[0])
    hex_polygons.append({
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"tid": tid},
    })
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
