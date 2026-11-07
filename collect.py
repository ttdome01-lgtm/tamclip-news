"""Collect news items into one dated JSON file for the news tool.

Run: python collect.py [output_dir]
Standard library only, so it also runs on a GitHub Actions runner.
A source that fails is reported and skipped. No output is written if every source fails.
The slow food-price source is reused from the newest
earlier file in output_dir, so each 15-minute run stays quick.
"""
import html
import json
import re
import ssl
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BANGKOK = timezone(timedelta(hours=7))
USER_AGENT = "TamClipNewsCollector/0.2"
MAX_ITEMS = 60
CATEGORY_CAP = 10  # so one busy category (alerts, earthquakes) cannot push the others out
SUMMARY_CHARS = 600
ALERT_URL = "https://www.tmd.go.th/uploads/CAP/"

# url, category, source. Add website feeds here after checking their terms.
SOURCES = [
    ("https://www.tmd.go.th/api/xml/CAP", "เตือนภัยอากาศ", "กรมอุตุนิยมวิทยา"),
] + [
    (f"https://www.tmd.go.th/api/xml/region-daily-forecast?regionid={i}", "พยากรณ์อากาศ", "กรมอุตุนิยมวิทยา")
    for i in range(1, 8)
] + [
    ("https://earthquake.tmd.go.th/feed/rss_inside.xml", "แผ่นดินไหว", "กรมอุตุนิยมวิทยา"),
]
EARTHQUAKE_MIN_SIZE = 3.0  # smaller quakes are kept only when they are inside Thailand
EARTHQUAKE_MAX_AGE = timedelta(hours=48)
AIR_URL = "http://air4thai.pcd.go.th/services/getNewAQI_JSON.php"
AIR_PAGE = "http://air4thai.pcd.go.th/webV3/"
AIR_TOP = 5

OIL_URL = "https://oil-price.bangchak.co.th/ApiOilPrice2/th"
OIL_PAGE = "https://www.bangchak.co.th/th/oilprice"
GOLD_URL = "https://www.goldtraders.or.th/api/GoldPrices/Latest?readjson=false"
GOLD_PAGE = "https://www.goldtraders.or.th/"
LOTTERY_URL = "https://www.glo.or.th/api/lottery/getLatestLottery"
LOTTERY_PAGE = "https://www.glo.or.th/"
FOOD_URL = "https://dataapi.moc.go.th/gis-product-prices?product_id={}&from_date={}&to_date={}"
FOOD_PAGE = "https://pricelist.dit.go.th/"
FOOD_RECHECK = timedelta(hours=3)  # the ministry publishes once a day and the API is slow
FOOD_PRODUCTS = [  # retail prices, short names for speech
    ("P11027", "ไข่ไก่ เบอร์ 2"),
    ("P11003", "หมูเนื้อแดง สะโพก"),
    ("P11005", "หมูสามชั้น"),
    ("P11010", "ไก่สดทั้งตัว"),
    ("P13011", "กะหล่ำปลี"),
    ("P13003", "ผักบุ้งจีน"),
    ("P13043", "มะนาว เบอร์ 1-2"),
    ("P13087", "พริกขี้หนูจินดา"),
    ("P13082", "ผักชี"),
    ("P13084", "ต้นหอม"),
]
FOOD_MOVE = 0.05  # a 5% weekly change is worth a headline
FOOD_MIN_ROWS = 6  # fewer than this: keep the last full list instead


def tls_context():
    """Normal certificate checks, plus the intermediate that Air4Thai's server leaves out."""
    context = ssl.create_default_context()
    extra = Path(__file__).parent / "air4thai_ca.pem"
    if extra.exists():
        context.load_verify_locations(cafile=str(extra))
    return context


def fetch(url, data=None, timeout=30):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
        return response.read()


def clean_text(raw):
    text = html.unescape(raw or "")
    text = re.sub(r"<\s*/?\s*[a-zA-Z][^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_iso(raw):
    """Return an ISO date in Bangkok time, or None when the feed date is unusable."""
    try:
        # The forecast feed writes "+07:00", which the parser drops, leaving a naive time.
        parsed = parsedate_to_datetime(re.sub(r"([+-]\d\d):(\d\d)\s*$", r"\1\2", raw or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BANGKOK)
    return parsed.astimezone(BANGKOK).isoformat(timespec="minutes")


def item_time(guid, raw):
    """Prefer the Bangkok timestamp in a weather alert id; those feeds' pubDate runs 7 hours early."""
    match = re.search(r"TMD(\d{14})", guid)
    if match:
        try:
            issued = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
            return issued.replace(tzinfo=BANGKOK).isoformat(timespec="minutes")
        except ValueError:
            pass
    return to_iso(raw)


def baht(value):
    """64800.0 -> '64,800', 4.5 -> '4.50', 63308.16 -> '63,308.16'."""
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def spoken_unit(unit):
    """'บาท/กก.' -> 'บาทต่อกิโลกรัม', easier for the voice to read."""
    return (unit or "บาท").replace("กก.", "กิโลกรัม").replace("/", "ต่อ").rstrip(".")


def satang(step):
    step = round(step, 2)
    return f"{step * 100:.0f} สตางค์" if step < 1 else f"{baht(step)} บาท"


def fuel_kind(name):
    for kind in ("ดีเซล", "แก๊สโซฮอล์", "เบนซิน", "NGV", "LPG"):
        if kind in name:
            return kind
    return name


def parse_feed(data, category, source):
    items = []
    for node in ET.fromstring(data).iter("item"):
        title = clean_text(node.findtext("title"))
        if not title:
            continue
        url = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        items.append({
            "id": f"{category}:{guid or url or title}",
            "category": category,
            "title": title,
            "summary": clean_text(node.findtext("description"))[:SUMMARY_CHARS],
            "source": source,
            "url": url,
            "published": item_time(guid, node.findtext("pubDate")),
        })
    return items


def alert_expired(url, now):
    """Read the expiry from the alert's own CAP file; the RSS item does not carry it."""
    match = re.search(rb"<(?:\w+:)?expires>\s*([^<\s]+)", fetch(url))
    return bool(match) and datetime.fromisoformat(match.group(1).decode()) <= now


def tidy_earthquake(item, now):
    """Return a shorter item, or None when the quake is old or too small to be news."""
    size = re.search(r"ขนาด\s*(\d+(?:\.\d+)?)", item["title"])
    in_thailand = re.search(r"\bจ\.", item["title"])
    if not size or (float(size.group(1)) < EARTHQUAKE_MIN_SIZE and not in_thailand):
        return None
    if not item["published"] or now - datetime.fromisoformat(item["published"]) > EARTHQUAKE_MAX_AGE:
        return None
    title = re.sub(r"\s*\[TMD\].*$", "", item["title"])
    return {**item, "title": re.sub(r"\s*\([\d.,\s-]+\)", "", title)}


def air_item(data):
    """One item: the highest PM2.5 readings at the latest hour across all stations."""
    readings = []
    for station in json.loads(data)["stations"]:
        last = station.get("AQILast") or {}
        try:
            value = float(last["PM25"]["value"])
            when = datetime.strptime(f"{last['date']} {last['time']}", "%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError):
            continue
        if value >= 0:
            readings.append((when, value, station.get("areaTH", "").strip()))
    if not readings:
        raise ValueError("no PM2.5 readings")
    latest = max(reading[0] for reading in readings)
    top = sorted((r for r in readings if r[0] == latest), key=lambda r: r[1], reverse=True)[:AIR_TOP]
    lines = "; ".join(f"{area} ค่า {value:g}" for _, value, area in top)  # road names can hold numbers too
    return {
        "id": f"ฝุ่น PM2.5:{latest:%Y-%m-%dT%H:%M}",
        "category": "ฝุ่น PM2.5",
        "title": f"ฝุ่น PM2.5 สูงสุด เวลา {latest:%H:%M} น. {top[0][2]}",
        "summary": f"ค่าฝุ่น PM2.5 จากสถานีตรวจวัด เวลา {latest:%H:%M} น. สูงสุด {len(top)} จุด หน่วยไมโครกรัมต่อลูกบาศก์เมตร: {lines}",
        "source": "กรมควบคุมมลพิษ",
        "url": AIR_PAGE,
        "published": latest.replace(tzinfo=BANGKOK).isoformat(timespec="minutes"),
    }


def thai_date(raw, clock="00:00"):
    """'05/11/2569' (Buddhist year) -> Bangkok datetime."""
    day, month, year = (int(part) for part in raw.split("/"))
    hour, minute = (int(part) for part in clock.split(":"))
    return datetime(year - 543 if year > 2400 else year, month, day, hour, minute, tzinfo=BANGKOK)


def oil_item(data):
    """One item: the latest retail price change, or today's prices when nothing changed."""
    board = json.loads(data)[0]
    oils = board["OilList"]
    oils = json.loads(oils) if isinstance(oils, str) else oils
    announced = thai_date(board["OilPriceDate"], board.get("OilPriceTime") or "00:00")
    effective = clean_text(board.get("OilRemark2"))  # e.g. "ราคามีผล ณ วันที่ 6 พ.ย. 69 เวลา 05.00 น."
    changes, prices = [], []
    for oil in oils:
        name, old, today, tomorrow = oil["OilName"], oil["PriceYesterday"], oil["PriceToday"], oil["PriceTomorrow"]
        prices.append(f"{name} {today:.2f} บาท")
        if tomorrow != today:
            changes.append((name, today, tomorrow))
        elif today != old:
            changes.append((name, old, today))
    if changes:
        ups = [c for c in changes if c[2] > c[1]]
        downs = [c for c in changes if c[2] < c[1]]
        if ups and downs:
            direction = "ปรับราคา"
        else:
            direction = "ขึ้นราคา" if ups else "ลดราคา"
        by_kind = {}  # "แก๊สโซฮอล์ ลด 1 บาท", the largest step of each fuel kind
        for name, old, new in changes:
            kind = fuel_kind(name)
            if kind not in by_kind or abs(new - old) > abs(by_kind[kind]):
                by_kind[kind] = new - old
        title = f"น้ำมัน{direction} " + " ".join(
            f"{kind}{'ขึ้น' if step > 0 else 'ลด'} {satang(abs(step))}" for kind, step in by_kind.items())
        lines = "; ".join(
            f"{name} จาก {old:.2f} เป็น {new:.2f} บาท ({'ขึ้น' if new > old else 'ลด'} {satang(abs(new - old))})"
            for name, old, new in changes)
        summary = f"บางจากประกาศปรับราคาน้ำมันขายปลีก {effective}: {lines}"
    else:
        title = f"ราคาน้ำมันวันนี้ คงที่ ดีเซล B20 ลิตรละ {next((o['PriceToday'] for o in oils if 'B20' in o['OilName']), oils[0]['PriceToday']):.2f} บาท"
        summary = f"ราคาน้ำมันขายปลีกบางจาก ไม่มีการปรับราคา: {'; '.join(prices)}"
    return {
        "id": f"ราคาน้ำมัน:{announced:%Y-%m-%dT%H:%M}:{len(changes)}",
        "category": "ราคาน้ำมัน",
        "title": title,
        "summary": (summary + ". " + clean_text(board.get("OilRemark")))[:SUMMARY_CHARS],
        "source": "บางจาก",
        "url": OIL_PAGE,
        "published": announced.isoformat(timespec="minutes"),
    }


def gold_item(data):
    """One item: the latest announced gold price from the Gold Traders Association."""
    gold = json.loads(data)
    when = datetime.fromisoformat(gold["asTime"]).replace(tzinfo=BANGKOK)
    change = gold.get("priceChangeFromPrevDayLast") or 0
    move = f"ขึ้น {baht(abs(change))} บาท" if change > 0 else f"ลง {baht(abs(change))} บาท" if change < 0 else "ทรงตัว"
    sell = gold["bL_SellPrice"]
    summary = (
        f"สมาคมค้าทองคำประกาศราคาทอง วันที่ {when.day}/{when.month}/{when.year + 543} เวลา {when:%H:%M} น. "
        f"ครั้งที่ {gold.get('seq', 1)} ของวัน เทียบกับราคาปิดเมื่อวาน{move}. "
        f"ทองคำแท่ง 96.5% รับซื้อบาทละ {baht(gold['bL_BuyPrice'])} บาท ขายออกบาทละ {baht(sell)} บาท. "
        f"ทองรูปพรรณ 96.5% รับซื้อบาทละ {baht(gold['oM965_BuyPrice'])} บาท ขายออกบาทละ {baht(gold['oM965_SellPrice'])} บาท. "
        f"ราคาทองโลก {gold.get('goldSpot')} ดอลลาร์ต่อออนซ์ ค่าเงิน {gold.get('bahtPerUSD')} บาทต่อดอลลาร์"
    )
    return {
        "id": f"ราคาทอง:{gold['asTime']}:{gold.get('seq')}",
        "category": "ราคาทอง",
        "title": f"ราคาทองวันนี้{move} ทองแท่งขายออกบาทละ {baht(sell)} บาท",
        "summary": summary[:SUMMARY_CHARS],
        "source": "สมาคมค้าทองคำ",
        "url": GOLD_PAGE,
        "published": when.isoformat(timespec="minutes"),
    }


def lottery_item(data):
    """One item: the latest government lottery draw."""
    draw = json.loads(data)["response"]
    prizes = draw["data"]

    def numbers(key):
        return " ".join(n["value"] for n in sorted(prizes[key]["number"], key=lambda n: n.get("round", 0)))

    day = datetime.fromisoformat(draw["date"]).replace(hour=16, tzinfo=BANGKOK)  # draws are announced in the afternoon
    date_text = f"{day.day}/{day.month}/{day.year + 543}"
    return {
        "id": f"ผลสลากกินแบ่ง:{draw['date']}",
        "category": "ผลสลากกินแบ่ง",
        "title": f"ผลสลากกินแบ่งรัฐบาล งวด {date_text} รางวัลที่ 1 {numbers('first')}",
        "summary": (f"ผลสลากกินแบ่งรัฐบาล งวดวันที่ {date_text}: รางวัลที่ 1 {numbers('first')}; "
                    f"เลขหน้า 3 ตัว {numbers('last3f')}; เลขท้าย 3 ตัว {numbers('last3b')}; "
                    f"เลขท้าย 2 ตัว {numbers('last2')}; รางวัลข้างเคียงรางวัลที่ 1 {numbers('near1')}. "
                    "โปรดตรวจสอบกับสำนักงานสลากกินแบ่งรัฐบาลอีกครั้ง")[:SUMMARY_CHARS],
        "source": "สำนักงานสลากกินแบ่งรัฐบาล",
        "url": LOTTERY_PAGE,
        "published": day.isoformat(timespec="minutes"),
    }


def food_price(product, now):
    """(name, unit, latest (date, low, high), week-earlier (date, low, high)) for one product."""
    pid, name = product
    start = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    url = FOOD_URL.format(pid, start, now.strftime("%Y-%m-%d"))
    try:
        data = json.loads(fetch(url, timeout=90))
    except Exception:  # the ministry's API often answers 500, then works a little later
        time.sleep(10)
        data = json.loads(fetch(url, timeout=90))
    points = [(p["date"][:10], p["price_min"], p["price_max"]) for p in data.get("price_list") or []
              if p.get("price_min") is not None and p.get("price_max") is not None]
    if not points:
        raise ValueError(f"no prices for {pid}")
    points.sort()
    latest = points[-1]
    week_ago = (datetime.fromisoformat(latest[0]) - timedelta(days=7)).strftime("%Y-%m-%d")
    earlier = min(points, key=lambda p: abs((datetime.fromisoformat(p[0]) - datetime.fromisoformat(week_ago)).days))
    return name, data.get("unit") or "บาท", latest, earlier


def food_item(now):
    """One item: retail prices of everyday food, and the biggest weekly move."""
    with ThreadPoolExecutor(max_workers=2) as pool:  # more parallel calls made the API answer 500
        results = list(pool.map(lambda p: _safe(food_price, p, now), FOOD_PRODUCTS))
    rows = [r for r in results if r]
    if len(rows) < FOOD_MIN_ROWS:
        raise ValueError(f"only {len(rows)} of {len(FOOD_PRODUCTS)} food prices")
    day = max(row[2][0] for row in rows)
    parts, moves = [], []
    for name, unit, (_, low, high), (_, old_low, old_high) in rows:
        price = f"{baht(low)}-{baht(high)}" if low != high else baht(low)
        unit = spoken_unit(unit)
        parts.append(f"{name} {price} {unit}")
        middle, old_middle = (low + high) / 2, (old_low + old_high) / 2
        if old_middle:
            moves.append(((middle - old_middle) / old_middle, name, middle - old_middle, unit))
    moves.sort(key=lambda m: abs(m[0]), reverse=True)
    published = datetime.fromisoformat(day).replace(hour=18, tzinfo=BANGKOK)
    date_text = f"{published.day}/{published.month}/{published.year + 543}"
    if moves and abs(moves[0][0]) >= FOOD_MOVE:
        share, name, step, unit = moves[0]
        title = f"{name}ราคา{'ขึ้น' if step > 0 else 'ลง'} {baht(abs(step))} {unit} ในหนึ่งสัปดาห์"
        changed = "; ".join(f"{n} {'ขึ้น' if s > 0 else 'ลง'} {baht(abs(s))} {u}" for r, n, s, u in moves if abs(r) >= FOOD_MOVE)
        week = f"เทียบกับสัปดาห์ก่อน: {changed}"
    else:
        title = "ราคาอาหารสดสัปดาห์นี้ ทรงตัว " + parts[0]
        week = "เทียบกับสัปดาห์ก่อน ราคาเปลี่ยนไม่เกิน 5%"
    return {
        "id": f"ราคาอาหารสด:{day}",
        "category": "ราคาอาหารสด",
        "title": title,
        "summary": f"ราคาขายปลีกจากกรมการค้าภายใน วันที่ {date_text}: {'; '.join(parts)}. {week}"[:SUMMARY_CHARS],
        "source": "กรมการค้าภายใน",
        "url": FOOD_PAGE,
        "published": published.isoformat(timespec="minutes"),
        "checked": now.isoformat(timespec="minutes"),
    }


def _safe(function, *args):
    try:
        return function(*args)
    except Exception as error:
        print(f"SKIPPED {args[0]}: {error}", file=sys.stderr)
        return None


def load_previous(out_dir):
    """Items from the newest earlier output file, by id."""
    files = sorted(out_dir.glob("news-*.json"))
    for path in reversed(files):
        try:
            return {item["id"]: item for item in json.loads(path.read_text(encoding="utf-8"))["items"]}
        except Exception:
            continue
    return {}


def collect(previous=None):
    previous = previous or {}
    items, errors = {}, []
    now = datetime.now(BANGKOK)

    def add(source_url, make):
        try:
            for item in make():
                if item:
                    items.setdefault(item["id"], item)
        except Exception as error:  # one broken source must not stop the others
            errors.append({"url": source_url, "error": str(error)})

    for url, category, source in SOURCES:
        add(url, lambda: [tidy_earthquake(item, now) if category == "แผ่นดินไหว" else item
                          for item in parse_feed(fetch(url), category, source)])
    add(AIR_URL, lambda: [air_item(fetch(AIR_URL))])
    add(OIL_URL, lambda: [oil_item(fetch(OIL_URL))])
    add(GOLD_URL, lambda: [gold_item(fetch(GOLD_URL))])
    add(LOTTERY_URL, lambda: [lottery_item(fetch(LOTTERY_URL, data=b""))])

    food = [item for item in previous.values() if item.get("category") == "ราคาอาหารสด"]
    fresh = [item for item in food if item.get("checked") and now - datetime.fromisoformat(item["checked"]) < FOOD_RECHECK]
    if fresh:
        add(FOOD_URL, lambda: fresh[:1])
    else:
        before = len(items)
        add(FOOD_URL, lambda: [food_item(now)])
        if len(items) == before and food:  # API down: keep the last known prices
            items.setdefault(food[0]["id"], food[0])

    current = []
    for item in items.values():
        try:
            if item["url"].startswith(ALERT_URL) and alert_expired(item["url"], now):
                continue
        except Exception as error:  # expiry unknown: keep the alert and report it
            errors.append({"url": item["url"], "error": str(error)})
        current.append(item)
    ordered = sorted(current, key=lambda item: item["published"] or "", reverse=True)
    kept, per_category = [], {}
    for item in ordered:
        count = per_category.get(item["category"], 0)
        if count < CATEGORY_CAP:
            per_category[item["category"]] = count + 1
            kept.append(item)
    return kept[:MAX_ITEMS], errors


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
    # esm.sh caches a served file for a year, so never rewrite a file: each 15-minute slot gets its own.
    start = datetime.now(BANGKOK)
    slot = start.replace(minute=start.minute - start.minute % 15)
    path = out_dir / f"news-{slot:%Y-%m-%d-%H-%M}.json"
    hour_path = out_dir / f"news-{start:%Y-%m-%d-%H}.json"  # older shared links read hourly files
    if path.exists():
        print(f"{path} already exists, nothing to do.")
        return 0
    items, errors = collect(load_previous(out_dir) if out_dir.exists() else {})
    for error in errors:
        print(f"SOURCE FAILED: {error['url']}: {error['error']}", file=sys.stderr)
    if not items:
        print("No items from any source, nothing written.", file=sys.stderr)
        return 1
    now = datetime.now(BANGKOK)
    out_dir.mkdir(parents=True, exist_ok=True)
    feed = {"updated": now.isoformat(timespec="minutes"), "errors": errors, "items": items}
    text = json.dumps(feed, ensure_ascii=False, indent=1)
    path.write_text(text, encoding="utf-8")
    if not hour_path.exists():
        hour_path.write_text(text, encoding="utf-8")
    print(f"{len(items)} items, {len(errors)} failed sources, wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
