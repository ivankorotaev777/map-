#!/usr/bin/env python3
"""Re-fetch joymee commercial rentals for Tashkent city (region=1) + oblast (region=11)."""
import urllib.request, urllib.parse, json, csv, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api.joymi.uz/api/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://joymee.uz",
    "Referer": "https://joymee.uz/",
}

def get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries - 1: return None
            time.sleep(1.5 * (i + 1))

def fetch_all_ids(region_id):
    ids = []; page = 1
    while True:
        params = urllib.parse.urlencode({"category": 6, "region": region_id, "page": page})
        data = get(f"{BASE}/announcement/list-combined/?{params}")
        if not data: break
        results = data.get("results", []) or []
        for item in results: ids.append(item["id"])
        total_pages = data.get("total_pages", 0)
        if not data.get("has_next") or page >= total_pages: break
        page += 1
        if page % 50 == 0: print(f"  region={region_id} page={page}/{total_pages} cum={len(ids)}")
        time.sleep(0.2)
    return ids

def fetch_detail(ann_id):
    data = get(f"{BASE}/announcement/plain/{ann_id}/")
    if not data: return None
    r = data.get("results", data)
    seller = r.get("seller") or {}
    region = r.get("region") or {}
    district = r.get("district") or {}
    images = r.get("images") or []
    return {
        "id": r.get("id"),
        "title": (r.get("title") or "").replace("\n"," ").strip(),
        "url": f"https://joymee.uz/ru/announcements/{r.get('id')}",
        "category_id": r.get("category"),
        "price": r.get("price"),
        "currency": r.get("currency"),
        "area_m2": r.get("area_m2"),
        "floor_number": r.get("floor_number"),
        "floors_count": r.get("floors_count"),
        "room_qty": r.get("room_qty"),
        "object_type": r.get("object_type"),
        "advertiser": r.get("advertiser"),
        "region_id": region.get("id"),
        "region_name": region.get("name"),
        "district_id": district.get("id"),
        "district_name": district.get("name"),
        "address_line": r.get("address_line"),
        "latitude": r.get("latitude"),
        "longitude": r.get("longitude"),
        "seller_name": seller.get("profile_name"),
        "phone_number": r.get("phone_number"),
        "description": (r.get("description") or "").replace("\n"," ").strip()[:500],
        "image_count": len(images),
        "first_image": (images[0].get("file") if images and isinstance(images[0], dict) else None),
        "created_at": r.get("created_at"),
        "status": r.get("status"),
    }

def main():
    print("Step 1: IDs region=1"); ids1 = fetch_all_ids(1); print(f"  → {len(ids1)}")
    print("Step 2: IDs region=11"); ids2 = fetch_all_ids(11); print(f"  → {len(ids2)}")
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

if __name__ == "__main__":
    main()
