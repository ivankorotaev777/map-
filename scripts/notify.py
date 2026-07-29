#!/usr/bin/env python3
"""Send a Telegram digest of listings matching default page filters.

Reads listing data + Uzum zones, applies the same filters that are pre-selected
when the map page opens, formats a short summary, sends via Telegram Bot API.

Env vars (set as GitHub Secrets):
  TG_BOT_TOKEN  — bot token from @BotFather
  TG_CHAT_ID    — chat ID (your DM or a group)
"""
import json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
import h3

def send(text):
    """Print the message, then push it to Telegram. Returns 'sent', 'skipped' or 'failed'."""
    print(f"--- Telegram message ({len(text)} chars) ---")
    print(text)
    print("--- end ---")

    token = (os.environ.get('TG_BOT_TOKEN') or '').strip()
    chat_id = (os.environ.get('TG_CHAT_ID') or '').strip()
    if not token or not chat_id:
        print("⚠️  TG_BOT_TOKEN or TG_CHAT_ID not set — printed only, skipping send", file=sys.stderr)
        return 'skipped'

    # Diagnostics that won't leak secrets
    print(f"DEBUG: token length = {len(token)} chars, starts with {token[:8]}…")
    print(f"DEBUG: chat_id = '{chat_id}'  (length={len(chat_id)})")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Read the error response body — it has the real reason
        body = e.read().decode('utf-8', errors='replace')
        print(f"❌ Telegram HTTP {e.code}: {body}", file=sys.stderr)
        return 'failed'
    except Exception as e:
        print(f"❌ Telegram send failed: {e}", file=sys.stderr)
        return 'failed'

    if resp.get('ok'):
        print("✅ Telegram message sent")
        return 'sent'
    print(f"⚠️  Telegram returned: {resp}", file=sys.stderr)
    return 'failed'

def now_tashkent():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5))).strftime('%d.%m.%Y %H:%M')

# ---- Defaults that match the JS in build_map.py ----
DEFAULT_TAGS = {
    'street_facing','retail_shop','beauty_service','education','showroom',
    'basement_floor','ground_floor','universal','pvz_explicit',
}
DEFAULT_INCLUDE_OTHER = True                  # also match listings with no tags ("Прочая категория")
ALLOWED_ZONES = {'recommended', 'unknown'}    # rec + white, NOT not_allowed
PRICE_MAX_USD = 600                           # only "До $600"
FRESH_MIN_DAYS, FRESH_MAX_DAYS = 0.0, 1.0     # only "За сутки"

# ---- Load data ----
# An upstream step may have died (the joymee API went dead once and shipped an empty map for
# three weeks). Alert loudly instead of crashing silently — this digest is the only thing
# watching the pipeline.
try:
    zones = json.load(open('/tmp/uzum_zones.json'))
    listings = json.load(open('/tmp/joymee_classified.json'))
except Exception as e:
    send(f"🚨 <b>PVZ-карта: сбор данных сломался</b> · {now_tashkent()}\n\n"
         f"Не удалось прочитать данные для сводки: <code>{type(e).__name__}: {e}</code>\n\n"
         f"Карта сегодня НЕ обновилась. Нужно проверить, что отдаёт joymee.")
    sys.exit(1)

listings = [r for r in listings if r.get('latitude') and r.get('longitude')]

if not listings:
    send(f"🚨 <b>PVZ-карта: объявлений не найдено</b> · {now_tashkent()}\n\n"
         f"Сбор отработал, но не вернул ни одного объявления с координатами. "
         f"Скорее всего joymee снова изменил доступ к данным.\n\n"
         f"Карта сегодня осталась без предложений аренды.")
    sys.exit(1)

H3_RES = h3.get_resolution(zones['recommended'][0])
rec_set = set(zones['recommended'])
forb_set = set(zones['not_allowed'])

def zone_of(r):
    try: cell = h3.latlng_to_cell(r['latitude'], r['longitude'], H3_RES)
    except: return 'unknown'
    if cell in rec_set: return 'recommended'
    if cell in forb_set: return 'not_allowed'
    return 'unknown'

def usd_total(r):
    p = r.get('price'); a = r.get('area_m2'); cur = r.get('currency')
    if p is None or cur != 2: return None
    try: p = float(p)
    except: return None
    try: a = float(a) if a else None
    except: a = None
    if a and p <= 60 and a >= 15: return p * a
    return p

# Find newest timestamp = our "now"
def parse_ts(r):
    try: return datetime.fromisoformat(r['created_at'].replace('Z','+00:00')).timestamp()
    except: return None
now_ts = max((parse_ts(r) or 0) for r in listings)

# ---- Apply filters ----
matches = []
for r in listings:
    ts = parse_ts(r)
    if ts is None: continue
    age_days = (now_ts - ts) / 86400.0
    if not (FRESH_MIN_DAYS <= age_days < FRESH_MAX_DAYS): continue
    z = zone_of(r)
    if z not in ALLOWED_ZONES: continue
    usd = usd_total(r)
    if usd is None or usd >= PRICE_MAX_USD: continue
    tags = set(r.get('tags') or [])
    # "any of selected tags" — DEFAULT_TAGS OR (empty tags AND we include "Прочая")
    has_match = bool(tags & DEFAULT_TAGS) or (DEFAULT_INCLUDE_OTHER and not tags)
    if not has_match: continue
    r['_zone'] = z
    r['_usd'] = usd
    matches.append(r)

# Stats by zone
by_zone = {'recommended': 0, 'unknown': 0}
for r in matches:
    by_zone[r['_zone']] += 1

# ---- Format Telegram message ----
lines = [
    f"🏠 <b>PVZ-карта обновлена</b> · {now_tashkent()}",
    "",
    f"<b>Новых за сутки</b> по дефолтным фильтрам: <b>{len(matches)}</b>",
    f"  🟣 в рекомендуемых: <b>{by_zone['recommended']}</b>",
    f"  ⚪ в белых: <b>{by_zone['unknown']}</b>",
]

if matches:
    # Sort by price ascending, show top 5
    matches.sort(key=lambda r: r['_usd'])
    lines.append("")
    lines.append("<b>Топ-5 (по цене):</b>")
    def safe_int(v):
        try: return int(float(v))
        except (TypeError, ValueError): return None
    for r in matches[:5]:
        district = (r.get('district_name') or '').replace(' tumani','').replace(' shahri','')
        zone_emoji = '🟣' if r['_zone'] == 'recommended' else '⚪'
        title = (r.get('title') or '').replace('\n', ' ').strip()
        if len(title) > 60: title = title[:57] + '…'
        area_n = safe_int(r.get('area_m2'))
        area = f"{area_n}м²" if area_n else '?'
        usd_n = safe_int(r['_usd']) or 0
        phone = (r.get('phone_number') or '').strip()
        # joymee.uz web listings are dead (they force mobile app) — show phone directly
        lines.append(f"{zone_emoji} <b>${usd_n}</b> / {area} / {district}")
        lines.append(f"   {title}")
        if phone: lines.append(f"   📞 <code>{phone}</code>")

lines.append("")
lines.append('📍 <a href="https://ivankorotaev777.github.io/map-/">Открыть карту</a>')

if send('\n'.join(lines)) == 'failed':
    sys.exit(1)
