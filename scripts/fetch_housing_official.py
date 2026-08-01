#!/usr/bin/env python3
"""Official housing commissioned (m²) per district of Tashkent region + district outlines.

NOT part of the daily pipeline — the statistics office publishes quarterly. Run by hand
when the numbers should be refreshed; build_map.py only reads the committed files.

Two sources are needed because neither one alone is complete:
  * SIAT indicator 1905 carries 2010–2024 per district, but has no 2025 column even after
    its July 2026 refresh.
  * The Tashkent region statistics office publishes 2025 only inside a PDF bulletin.
Their region totals agree exactly (2025: 1 997,7 thousand m²), so they can be joined.

District outlines come from OSM and are simplified before shipping — full-detail admin
boundaries would add megabytes to a page that is already heavy.

Sources: Госкомстат (stat.uz / siat.stat.uz, toshvilstat.uz); границы — © OpenStreetMap (ODbL).
"""
import json, os, re, sys, time, urllib.request, urllib.parse, csv, io

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
OUT_STATS = os.path.join(DATA_DIR, 'housing_commissioned.json')
OUT_GEO = os.path.join(DATA_DIR, 'districts_region.geojson')

SIAT_CSV = "https://api.siat.stat.uz/media/uploads/sdmx/sdmx_data_1905.csv"
TOSHVIL_PDF = ("https://toshvilstat.uz/files/343/ch-n-2025-yanvar-dekabr/4964/"
               "Investitsiya-va-qurilish.pdf")
TERRITORY_CODES = ("1727", "1726")   # Toshkent viloyati + Toshkent shahri
YEARS = ["2022", "2023", "2024", "2025"]
SUM_YEARS = ["2023", "2024", "2025"]  # the "last 3 years" total

OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://lz4.overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
AREAS = (3600196251, 3602216724)     # region and city are separate admin areas
HEADERS = {"User-Agent": "pvz-map/1.0 (github.com/ivankorotaev777/map-)"}
SIMPLIFY_DEG = 0.002                 # ~200 m — plenty for a district-level choropleth


def norm(name, kind=None):
    """Key a territory as base name + city/district.

    Both halves matter. The PDF spaces the Chirchiq compounds differently from SIAT
    (Quyi Chirchiq vs Quyichirchiq) and varies the apostrophe, so the base has to be
    stripped down. And five names — Ohangaron, Bekobod, Yangiyo'l, Chirchiq, Toshkent —
    exist as BOTH a city and a district, so dropping the suffix silently merges them and
    loses three territories.
    """
    s = (name or '').lower()
    s = s.replace('ʻ', "'").replace('‘', "'").replace('’', "'").replace('`', "'")
    if kind is None:
        kind = 'shahar' if ('shahri' in s or 'shahar' in s) else 'tuman'
    s = re.sub(r"[^a-z']", '', s)
    for w in ('tumani', 'shahri', 'shahar', 'tuman'):
        s = s.replace(w, '')
    return f"{s}|{kind}"


def fetch_siat():
    """{normalised name: {year: value}} plus display names, 2010–2024."""
    try:
        req = urllib.request.Request(SIAT_CSV, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=90) as r:
            text = r.read().decode('utf-8-sig')
    except Exception as e:
        print(f"  SIAT unreachable: {type(e).__name__} {e}", file=sys.stderr)
        return None, None
    rows = list(csv.reader(io.StringIO(text)))
    hdr = rows[0]
    out, names = {}, {}
    for r in rows[1:]:
        code = r[0]
        # Districts only: the bare 1727/1726 rows are the territory totals, not places.
        if not any(code.startswith(c) and len(code) > len(c) for c in TERRITORY_CODES):
            continue
        key = norm(r[1])
        names[key] = r[2] or r[1]          # Russian name for the tooltip
        vals = {}
        for y in YEARS:
            if y in hdr:
                try: vals[y] = float(r[hdr.index(y)] or 0) or None
                except ValueError: vals[y] = None
        out[key] = vals
    print(f"  SIAT 1905: {len(out)} districts, years {[y for y in YEARS if y in hdr]}")
    return out, names


def fetch_2025_pdf():
    """2025 per district, from the region statistics office bulletin (page 9)."""
    try:
        import pypdf
    except ImportError:
        print("  pypdf not installed — skipping the 2025 PDF", file=sys.stderr)
        return {}
    try:
        req = urllib.request.Request(TOSHVIL_PDF, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
    except Exception as e:
        print(f"  toshvilstat PDF unreachable: {type(e).__name__} {e}", file=sys.stderr)
        return {}
    reader = pypdf.PdfReader(io.BytesIO(raw))
    # The bulletin holds several district tables; only the one under this heading is housing.
    page = next((p.extract_text() or '' for p in reader.pages
                 if 'Turar-joylar' in (p.extract_text() or '')), '')
    if not page:
        print("  housing table not found in the PDF", file=sys.stderr)
        return {}, None

    out, kind, region_total = {}, None, None
    for line in page.split('\n'):
        low = line.lower()
        if low.strip().startswith('shaharlar'):
            kind = 'shahar'; continue
        if low.strip().startswith('tumanlar'):
            kind = 'tuman'; continue
        if 'boʻyicha' in low or 'bo‘yicha' in low:
            m = re.search(r"([\d\s]+,\d)", line)
            if m:
                region_total = float(m.group(1).replace(' ', '').replace(',', '.'))
            continue
        if kind is None:
            continue
        # "Zangiota  149,9 147,2 - -" — first number is the total, second the rural split.
        m = re.match(r"\s*([A-Za-zʻ‘’'\- ]{4,40}?)\s+([\d\s]*\d,\d)\s", line)
        if not m:
            if re.match(r"\s*\d+\.", line):     # numbered heading = table is over
                break
            continue
        try:
            out[norm(m.group(1).strip(), kind)] = float(
                m.group(2).replace(' ', '').replace(',', '.'))
        except ValueError:
            pass
    print(f"  toshvilstat 2025: {len(out)} territories, region total {region_total}")
    return out, region_total


def fetch_boundaries():
    out = []
    for area_id in AREAS:
        q = (f"[out:json][timeout:180];area({area_id})->.a;"
             f'relation["boundary"="administrative"]["admin_level"~"^(6|7)$"](area.a);'
             f"out geom;")
        got = None
        for url in OVERPASS:
            try:
                data = urllib.parse.urlencode({"data": q}).encode()
                req = urllib.request.Request(url, data=data, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=240) as r:
                    got = json.loads(r.read()).get("elements", [])
                break
            except Exception as e:
                print(f"    {url.split('/')[2]}: {type(e).__name__}", file=sys.stderr)
                time.sleep(2)
        if got is None:
            return None          # a partial set would quietly blank half the choropleth
        print(f"    area {area_id}: {len(got)} relations")
        out.extend(got)
        time.sleep(2)
    return out


def rings_to_polygon(el):
    """Stitch an OSM relation's outer ways into rings, then simplify."""
    from shapely.geometry import LineString, MultiPolygon, Polygon, mapping
    from shapely.ops import linemerge, polygonize
    lines = []
    for m in el.get('members', []):
        if m.get('role') not in ('outer', '') or m.get('type') != 'way':
            continue
        geom = m.get('geometry') or []
        pts = [(p['lon'], p['lat']) for p in geom if p]
        if len(pts) >= 2:
            lines.append(LineString(pts))
    if not lines:
        return None
    polys = [p for p in polygonize(linemerge(lines)) if p.is_valid and p.area > 0]
    if not polys:
        return None
    geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
    geom = geom.simplify(SIMPLIFY_DEG, preserve_topology=True)
    if geom.is_empty:
        return None
    return mapping(geom)


def main():
    print("Housing commissioned, official…")
    siat, names = fetch_siat()
    if siat is None:
        print("WARN: SIAT unavailable — keeping the previous stats file", file=sys.stderr)
        if not os.path.exists(OUT_STATS):
            print("ERROR: no previous stats file", file=sys.stderr)
            sys.exit(1)
    else:
        pdf2025, region_total = fetch_2025_pdf()
        matched = 0
        for key, vals in siat.items():
            if key in pdf2025:
                vals['2025'] = pdf2025[key]
                matched += 1
        print(f"  2025 matched onto {matched}/{len(siat)} districts")
        # If the parsed districts do not add up to the published region figure, the table
        # was read wrong — better no 2025 than a wrong 2025 painted across the map.
        if region_total:
            got = round(sum(pdf2025.values()), 1)
            if abs(got - region_total) > 1.0:
                print(f"  WARN: parsed 2025 sums to {got} but the bulletin says "
                      f"{region_total} — dropping 2025", file=sys.stderr)
                for vals in siat.values():
                    vals.pop('2025', None)
            else:
                print(f"  2025 check: districts sum to {got} vs published {region_total} ✓")

        stats = {}
        for key, vals in siat.items():
            years = {y: vals.get(y) for y in YEARS if vals.get(y) is not None}
            stats[names[key]] = {
                **{y: round(v, 1) for y, v in years.items()},
                "sum3": round(sum(years.get(y) or 0 for y in SUM_YEARS), 1),
                "_key": key,
            }
        with open(OUT_STATS, 'w', encoding='utf-8') as f:
            json.dump({"unit": "тыс. м²",
                       "attribution": "Госкомстат: siat.stat.uz (2010–2024), toshvilstat.uz (2025)",
                       "years": YEARS, "sum_years": SUM_YEARS,
                       "districts": stats}, f, ensure_ascii=False, indent=1)
        tot = {y: round(sum(d.get(y) or 0 for d in stats.values()), 1) for y in YEARS}
        print(f"  wrote {len(stats)} districts → {os.path.relpath(OUT_STATS)}; "
              f"region totals {tot}")

    print("District outlines from OSM…")
    els = fetch_boundaries()
    if els is None:
        print("WARN: Overpass unavailable — keeping the previous outlines", file=sys.stderr)
        if not os.path.exists(OUT_GEO):
            print("ERROR: no previous outlines file", file=sys.stderr)
            sys.exit(1)
        return
    feats = []
    for el in els:
        tags = el.get('tags') or {}
        geom = rings_to_polygon(el)
        if not geom:
            continue
        feats.append({"type": "Feature", "geometry": geom, "properties": {
            "name": tags.get('name:ru') or tags.get('name') or '',
            "name_uz": tags.get('name') or '',
            "admin_level": tags.get('admin_level'),
            "osm_id": el.get('id'),
        }})
    if len(feats) < 25 and os.path.exists(OUT_GEO):
        print(f"WARN: only {len(feats)} outlines — keeping the previous file", file=sys.stderr)
        return
    with open(OUT_GEO, 'w', encoding='utf-8') as f:
        json.dump({"type": "FeatureCollection",
                   "attribution": "© OpenStreetMap contributors (ODbL)",
                   "features": feats}, f, ensure_ascii=False)
    print(f"  wrote {len(feats)} outlines → {os.path.relpath(OUT_GEO)} "
          f"({os.path.getsize(OUT_GEO)//1024} KB)")


if __name__ == "__main__":
    main()
