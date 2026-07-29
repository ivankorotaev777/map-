#!/usr/bin/env python3
"""Fetch Uzum per-hex population and cache it in data/uzum_population.json.

Population is NOT carried in the MVT tiles — those only expose h3/orders1d/type — so each
hex needs its own by_coordinates call. That is ~6k requests, far too many to repeat every
morning, hence the committed cache: a hex already answered is never asked again, and hexes
that answered "no data" are re-checked only every RETRY_NULL_DAYS days in case Uzum extends
coverage.
"""
import json, os, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import h3

ZONES_PATH = '/tmp/uzum_zones.json'
CACHE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'uzum_population.json')
ENDPOINT = "https://api-wms.uzum.uz/franchise/api/v1/map/hexes/by_coordinates"
HEADERS = {"User-Agent": "Mozilla/5.0",
           "Origin": "https://promo.uzum.uz",
           "Referer": "https://promo.uzum.uz/"}

RETRY_NULL_DAYS = 14      # how long a "hex not found" answer is trusted before re-asking
MAX_WORKERS = 8
TIMEOUT = 15
RETRIES = 2


def fetch_hex(cell):
    """Return (cell, record). population=None means Uzum has no data for this hex."""
    lat, lng = h3.cell_to_latlng(cell)
    url = f"{ENDPOINT}?latitude={lat}&longitude={lng}"
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read())
            return cell, {
                "population": d.get("population"),
                "population_level": d.get("population_level"),
                "pedestrian": d.get("pedestrian_traffic_level"),
                "checked": date.today().isoformat(),
            }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Outside Uzum's coverage — a valid answer, not a failure.
                return cell, {"population": None, "population_level": None,
                              "pedestrian": None, "checked": date.today().isoformat()}
            if attempt == RETRIES:
                return cell, None
            time.sleep(1.5 * (attempt + 1))
        except Exception:
            if attempt == RETRIES:
                return cell, None
            time.sleep(1.5 * (attempt + 1))
    return cell, None


def needs_fetch(cell, cache):
    rec = cache.get(cell)
    if rec is None:
        return True
    if rec.get("population") is not None:
        return False
    # Only "no data" answers go stale — Uzum may extend coverage later.
    try:
        checked = datetime.fromisoformat(rec["checked"]).date()
    except (KeyError, TypeError, ValueError):
        return True
    return date.today() - checked > timedelta(days=RETRY_NULL_DAYS)


def main():
    if not os.path.exists(ZONES_PATH):
        print(f"ERROR: {ZONES_PATH} missing — run fetch_uzum.py first", file=sys.stderr)
        sys.exit(1)
    zones = json.load(open(ZONES_PATH))
    cells = sorted(set(zones.get('recommended') or []) | set(zones.get('not_allowed') or []))
    if not cells:
        print("ERROR: no hexes in uzum_zones.json", file=sys.stderr)
        sys.exit(1)

    resolutions = sorted({h3.get_resolution(c) for c in cells})
    print(f"zone hexes: {len(cells)} at h3 resolution(s) {resolutions}")

    cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            cache = json.load(open(CACHE_PATH))
        except Exception as e:
            print(f"  WARN: cache unreadable ({e}), rebuilding from scratch", file=sys.stderr)
    print(f"cache: {len(cache)} hexes on disk")

    todo = [c for c in cells if needs_fetch(c, cache)]
    print(f"to fetch: {len(todo)} ({len(cells)-len(todo)} served from cache)")

    failed = 0
    if todo:
        done = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = [ex.submit(fetch_hex, c) for c in todo]
            for fut in as_completed(futs):
                cell, rec = fut.result()
                done += 1
                if rec is None:
                    failed += 1
                else:
                    cache[cell] = rec
                if done % 500 == 0 or done == len(todo):
                    rate = done / max(time.time() - t0, 0.1)
                    print(f"  {done}/{len(todo)}  {rate:.1f}/s  failed={failed}")

    # Drop hexes Uzum no longer returns as zones, so the cache cannot grow without bound.
    stale = set(cache) - set(cells)
    for c in stale:
        del cache[c]

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    known = [r["population"] for r in cache.values() if r.get("population") is not None]
    print(f"\nwrote {len(cache)} hexes → {os.path.relpath(CACHE_PATH)} "
          f"({os.path.getsize(CACHE_PATH)//1024} KB), dropped {len(stale)} stale")
    print(f"  with population: {len(known)}, no data: {len(cache)-len(known)}, failed: {failed}")
    if known:
        known.sort()
        def pct(p): return known[min(len(known)-1, int(len(known)*p))]
        print(f"  population per hex: min {known[0]:,} / median {pct(0.5):,} / "
              f"p90 {pct(0.9):,} / max {known[-1]:,}")

    # A total wipe means something upstream broke; better to fail than to publish a blank layer.
    if not known:
        print("ERROR: no hex has population — refusing to write an empty layer", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
