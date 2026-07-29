#!/usr/bin/env python3
"""Re-fetch joymee commercial rentals for Tashkent city (region=59) + oblast (region=69)."""
import urllib.request, urllib.parse, json, csv, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api.joymee.uz/api/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "ru",
    "X-Platform": "3",
    "Origin": "https://joymee.uz",
    "Referer": "https://joymee.uz/",
}

# Region ids were remapped when the API moved hosts; the old 1/11 now point at other countries.
REGION_CITY = 59    # Toshkent shahri
REGION_OBLAST = 69  # Toshkent viloyati

def get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries - 1: return None
            time.sleep(1.5 * (i + 1))

def page_url(region_id, page):
    params = urllib.parse.urlencode({"category": 6, "region": region_id, "page": page})
    return f"{BASE}/announcement/?{params}"

def fetch_page_ids(region_id, page):
    data = get(page_url(region_id, page))
    if data is None: return None
    return [item["id"] for item in (data.get("results") or [])]

def fetch_all_ids(region_id):
    """Fetch every page of the region concurrently, then union two passes.

    Two problems forced this shape. Following the `next` link meant one failed request
    silently truncated the rest of the region. And walking pages one at a time takes
    minutes, during which sellers bump listings to the top — the list slides under us, so
    pages overlap and whatever slid past a page boundary is never seen. A serial walk lost
    15-25% of the region that way, varying run to run.

    Fetching all pages at once collapses the walk to seconds, so there is far less time for
    the list to move; a second pass mops up whatever still slipped through.
    """
    first = get(page_url(region_id, 1))
    if not first:
        print(f"  region={region_id}: first page unavailable", file=sys.stderr)
        return []
    total_pages = first.get("total_pages") or 1
    claimed = first.get("filtered_count")
    ids = set(item["id"] for item in (first.get("results") or []))
    failed = set()

    for attempt in (1, 2):
        pages = range(2, total_pages + 1) if attempt == 1 else sorted(failed)
        if attempt == 2:
            pages = sorted(set(pages) | set(range(1, total_pages + 1)))
            failed = set()
        before = len(ids)
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(fetch_page_ids, region_id, p): p for p in pages}
            for fut in as_completed(futs):
                page_ids = fut.result()
                if page_ids is None:
                    failed.add(futs[fut])
                else:
                    ids.update(page_ids)
        print(f"  region={region_id} pass {attempt}: {len(ids)} unique "
              f"(+{len(ids)-before}), {len(failed)} pages unreadable")
        if claimed and len(ids) >= claimed:
            break

    if failed:
        print(f"  region={region_id}: WARN {len(failed)}/{total_pages} pages unreadable",
              file=sys.stderr)
    if claimed and len(ids) < claimed * 0.95:
        print(f"  region={region_id}: WARN got {len(ids)} of {claimed} claimed listings",
              file=sys.stderr)
    print(f"  region={region_id}: {len(ids)} unique from {total_pages} pages (claimed {claimed})")
    return sorted(ids)

def fetch_detail(ann_id):
    data = get(f"{BASE}/announcement/{ann_id}/")
    if not data: return None
    r = data.get("results", data)
    seller = r.get("seller") or {}
    region = r.get("region") or {}
    district = r.get("district") or {}
    location = r.get("location") or {}
    pricing = r.get("pricing") or {}
    detail = r.get("detail") or {}
    media = r.get("media") or []
    seller_name = " ".join(x for x in (seller.get("first_name"), seller.get("last_name")) if x).strip()

    def num(v):
        try: return float(v)
        except (TypeError, ValueError): return None

    return {
        "id": r.get("id"),
        "title": (r.get("title") or "").replace("\n"," ").strip(),
        "url": f"https://joymee.uz/ru/announcements/{r.get('slug') or r.get('id')}",
        "category_id": r.get("category"),
        "price": num(pricing.get("price")),
        "currency": pricing.get("currency"),
        "area_m2": num(detail.get("area_m2")),
        "floor_number": detail.get("floor_number"),
        "floors_count": detail.get("floors_count"),
        "room_qty": detail.get("room_qty"),
        "object_type": r.get("property_type"),
        "advertiser": r.get("advertiser_type"),
        "region_id": region.get("id"),
        "region_name": region.get("name"),
        "district_id": district.get("id"),
        "district_name": district.get("name"),
        "address_line": r.get("address_line"),
        "latitude": num(location.get("latitude")),
        "longitude": num(location.get("longitude")),
        "seller_name": seller_name or seller.get("username"),
        "phone_number": r.get("phone_number"),
        # Full description with newlines preserved (JS popup handles rendering)
        "description": (r.get("description") or "").strip(),
        "image_count": len(media),
        # No photo URLs: joymee now hands out pre-signed links that expire 10 minutes after
        # this call, so anything embedded in the published map is dead before a visitor sees
        # it. Leaving these empty makes build_map skip the gallery instead of rendering
        # broken image frames. The popup still carries description + phone, which is the
        # point of embedding at all (joymee.uz itself just redirects to an app landing).
        "first_image": None,
        "images": [],
        "created_at": r.get("ads_at"),
        "status": r.get("status"),
    }

def main():
    print(f"Step 1: IDs region={REGION_CITY}"); ids1 = fetch_all_ids(REGION_CITY); print(f"  → {len(ids1)}")
    print(f"Step 2: IDs region={REGION_OBLAST}"); ids2 = fetch_all_ids(REGION_OBLAST); print(f"  → {len(ids2)}")
    all_ids = list(dict.fromkeys(ids1 + ids2))
    print(f"Total unique: {len(all_ids)}")

    print("Step 3: details (4 workers)")
    rows = []; done = 0; t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_detail, aid): aid for aid in all_ids}
        for fut in as_completed(futs):
            row = fut.result(); done += 1
            if row: rows.append(row)
            if done % 200 == 0 or done == len(all_ids):
                rate = done / max(time.time()-t0, 0.1)
                print(f"  {done}/{len(all_ids)}  {rate:.1f}/s")

    rows.sort(key=lambda r: (r.get("region_id") or 0, r.get("district_id") or 0, r.get("id") or 0))
    json.dump(rows, open('/tmp/joymee_commercial.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f"\nWrote {len(rows)} → /tmp/joymee_commercial.json")
    with_coords = sum(1 for r in rows if r['latitude'])
    print(f"With coordinates: {with_coords}")

    # The API silently went dead once already and the empty map shipped unnoticed for 3 weeks.
    # Refuse to hand an empty scrape downstream instead of rebuilding the map without listings.
    if not rows or with_coords == 0:
        print("ERROR: scrape produced no usable listings — refusing to continue", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
