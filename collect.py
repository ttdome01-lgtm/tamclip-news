"""Collect news items into one dated JSON file for the news tool.

Run: python collect.py [output_dir]
Standard library only, so it also runs on a GitHub Actions runner.
A source that fails is reported and skipped. No output is written if every source fails.
"""
import html
import json
import re
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BANGKOK = timezone(timedelta(hours=7))
USER_AGENT = "TamClipNewsCollector/0.1"
MAX_ITEMS = 40
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


def tls_context():
    """Normal certificate checks, plus the intermediate that Air4Thai's server leaves out."""
    context = ssl.create_default_context()
    extra = Path(__file__).parent / "air4thai_ca.pem"
    if extra.exists():
        context.load_verify_locations(cafile=str(extra))
    return context


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30, context=tls_context()) as response:
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
    lines = " ".join(f"{area} {value:g} มคก./ลบ.ม." for _, value, area in top)
    return {
        "id": f"ฝุ่น PM2.5:{latest:%Y-%m-%dT%H:%M}",
        "category": "ฝุ่น PM2.5",
        "title": f"ฝุ่น PM2.5 สูงสุด เวลา {latest:%H:%M} น. {top[0][2]}",
        "summary": f"ค่าฝุ่น PM2.5 จากสถานีตรวจวัด เวลา {latest:%H:%M} น. สูงสุด {len(top)} จุด: {lines}",
        "source": "กรมควบคุมมลพิษ",
        "url": AIR_PAGE,
        "published": latest.replace(tzinfo=BANGKOK).isoformat(timespec="minutes"),
    }


def collect():
    items, errors = {}, []
    now = datetime.now(BANGKOK)
    for url, category, source in SOURCES:
        try:
            for item in parse_feed(fetch(url), category, source):
                if category == "แผ่นดินไหว":
                    item = tidy_earthquake(item, now)
                if item:
                    items.setdefault(item["id"], item)
        except Exception as error:  # one broken source must not stop the others
            errors.append({"url": url, "error": str(error)})
    try:
        item = air_item(fetch(AIR_URL))
        items[item["id"]] = item
    except Exception as error:
        errors.append({"url": AIR_URL, "error": str(error)})
    current = []
    for item in items.values():
        try:
            if item["url"].startswith(ALERT_URL) and alert_expired(item["url"], now):
                continue
        except Exception as error:  # expiry unknown: keep the alert and report it
            errors.append({"url": item["url"], "error": str(error)})
        current.append(item)
    ordered = sorted(current, key=lambda item: item["published"] or "", reverse=True)
    return ordered[:MAX_ITEMS], errors


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
    # esm.sh caches a served file for a year, so never rewrite this hour's file.
    path = out_dir / f"news-{datetime.now(BANGKOK):%Y-%m-%d-%H}.json"
    if path.exists():
        print(f"{path} already exists, nothing to do.")
        return 0
    items, errors = collect()
    for error in errors:
        print(f"SOURCE FAILED: {error['url']}: {error['error']}", file=sys.stderr)
    if not items:
        print("No items from any source, nothing written.", file=sys.stderr)
        return 1
    now = datetime.now(BANGKOK)
    out_dir.mkdir(parents=True, exist_ok=True)
    feed ={"updated": now.isoformat(timespec="minutes"), "errors": errors, "items": items}
    path.write_text(json.dumps(feed, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} items, {len(errors)} failed sources, wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
