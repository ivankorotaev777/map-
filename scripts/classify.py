#!/usr/bin/env python3
"""Multi-label classifier for joymee commercial listings."""
import json, re
from collections import Counter

TAGS = [
    ("street_facing",   "1-я линия",            "🛣",
        r"\b(1\s*-?\s*(линия|liniya)|перв(ая|ой)\s+лини|вдоль\s+дороги|вдоль\s+трасс|на\s+трасс|1-?liniya|yo[‘'`´]l\s+yuzi|yo[‘'`´]l\s+bo[‘'`´]y|na\s+1-?lini|tashqi|fasad|фасад|выходит\s+на\s+улицу|первая\s+линия|оживл[её]нн|na\s+pervoy\s+linii)\b"),
    ("retail_shop",     "Магазин",              "🛒",
        r"\b(магазин|magazin|do['‘`´]?kon|do['‘`´]?kani|shop|retail|торгов[ао]|prodaj|продаж[нм]|витрин|under\s+shop|pod\s+magazin|jenskiy\s+magazin)\b"),
    ("mall_in",         "Внутри ТЦ / БЦ",       "🏬",
        r"\b(тр[цк]|тц|trc|trk|tts|mall|молл|корзинк|korzink|magnit|nest\s+one|nestone|biznes\s+sentr|business\s+center|бизнес[\s-]?центр|бц|alfraganus|seoul\s+moon|seoul\s+mun|seul\s+mun|seoul\s+mon|trilliant|infinity|akay\s+city|parkwood|royal\s+house|celebrity|piramit|katartal|tashkent\s+city)\b"),
    ("cafe_restaurant", "Кафе/ресторан",        "🍽",
        r"\b(каф[еэ]|kaf[eyio]|ресторан|restoran|пицце|pizzeri|fastfood|фастфуд|столовая|oshxon|ошхон|чайхан|choyxon|бар(\b|у)|кальян|kalyan|bistro|kofeyn|кофейн|пекарн|burgerkin|kfc|baker|кондитерск)\b"),
    ("warehouse_prod",  "Склад/производство",   "📦",
        r"\b(склад|sklad|ombor|цех(\b|и)|sex(\b|i)|завод|zavod|производств|ishlab\s+chiqar|производственн|логистическ|logistic|industrial|промышленн)\b"),
    ("medical",         "Медицина",             "⚕",
        r"\b(клиник|klinik|аптек|aptek|dorixon|стомат|stomatol|медцентр|med\s+center|medical|больниц|госпиталь|tibbiy|tibbi[yt]ot|laborator|поликлиник)\b"),
    ("beauty_service",  "Красота/салон",        "💅",
        r"\b(салон|salon|барбершоп|barbershop|парикмахер|sartaroshxon|массаж|massaj|спа\b|spa\b|nail\s+(bar|studio)|маникюр|manikyur|космет|beauty|brow|epil|epilyat|тату\b|tattoo)\b"),
    ("gym_fitness",     "Фитнес",               "🏋",
        r"\b(фитнес|fitnes|fitness|тренажёр|trenajor|gym|спортзал|sport[\s-]?zal|йога|yoga|боевые\s+искусств|единоборств|танц(а|евальн)|tantsa)\b"),
    ("education",       "Учебный центр",        "🎓",
        r"\b(учебн(ый|ого|ом|ое|ы[мх])|обуч|школа|maktab|курс(ы|ов)|kurs(\s|lar)|детский\s+сад|bog['‘`´]?ch|репетит|repetit|repetitor|тренинг|trening|educat|university|tutor)\b"),
    ("showroom",        "Шоурум/мебельный",     "🛋",
        r"\b(шоурум|showroom|шоу[\s-]?рум|showrum|автосалон|avtosalon|мебельный|mebel(ny|niy)|interior|интерьер)\b"),
    ("hotel_hostel",    "Гостиница/хостел",     "🏨",
        r"\b(отель|hotel|hostel|хостел|гостиниц|mehmonxon|гостев[ыо]й\s+дом|guest\s+house|aparthotel)\b"),
    ("office",          "Офис",                 "💼",
        r"\b(офис|ofis|ofes|ofice|office|кабинет|kabinet|biznes[\s-]?sentr|business[\s-]?center|coworking|коворкин|it[\s-]?park|айти[\s-]?парк)\b"),
    ("standalone_bldg", "Отд. здание",          "🏢",
        r"\b(отдельно\s+стоящ|отдельное?\s+здани|отдельно[е]?\s+бино|alohida\s+bino|standalone|bino\s+ijaraga)\b"),
    ("basement_floor",  "Подвал/цоколь",        "🕳",
        r"\b(подвал|podval|цокол|cokol|tsokol|basement|tagjoy)\b"),
    ("ground_floor",    "1 этаж",               "🪟",
        r"\b(1[\s-]?этаж|перв(ый|ого|ое|ой)\s+этаж|1[\s-]?etaj|1[\s-]?qavat|ground\s+floor)\b"),
    ("universal",       "Универсал",            "🔁",
        r"\b(под\s+любой|под\s+бизнес|универс|любой\s+вид|любой\s+бизнес|pod\s+biznes|biznes\s+uchun|для\s+любого|under\s+any|fits\s+any)\b"),
    ("pvz_explicit",    "Под ПВЗ (явно)",       "📮",
        r"\b(пвз|pvz|пункт\s+выдач|topshirish\s+punkt|pickup\s+point|пункт\s+приёма)\b"),
    ("hookah",          "Кальянная",            "💨",
        r"\b(кальян|kalyan|hookah)\b"),
]
COMPILED = [(t[0], t[1], t[2], re.compile(t[3], re.IGNORECASE | re.UNICODE)) for t in TAGS]

def classify(text):
    return [tid for tid, _, _, pat in COMPILED if pat.search(text)]

def primary_tag(tags, area=None):
    priority = ["pvz_explicit","hookah","gym_fitness","education","medical","beauty_service",
                "warehouse_prod","cafe_restaurant","hotel_hostel","showroom",
                "mall_in","retail_shop","standalone_bldg","office",
                "street_facing","universal","basement_floor","ground_floor"]
    for p in priority:
        if p in tags: return p
    if area:
        try:
            if float(area) >= 500: return "warehouse_prod_inferred"
        except: pass
    return "other"

def main():
    rows = json.load(open('/tmp/joymee_commercial.json'))
    for r in rows:
        text = (r.get('title','') + ' \n ' + r.get('description','')).lower()
        r['tags'] = classify(text)
        r['primary'] = primary_tag(r['tags'], r.get('area_m2'))
    json.dump(rows, open('/tmp/joymee_classified.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f"Classified {len(rows)} rows")
    tag_counts = Counter()
    for r in rows:
        for t in r['tags']: tag_counts[t] += 1
    for t in COMPILED:
        print(f"  {t[2]} {t[1]:24s}  {tag_counts.get(t[0], 0)}")

if __name__ == "__main__":
    main()
