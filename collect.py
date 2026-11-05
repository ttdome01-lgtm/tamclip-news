"""Collect news items into one dated JSON file for the news tool.

Run: python collect.py [output_dir]
Standard library only, so it also runs on a GitHub Actions runner.
A source that fails is reported and skipped. No output is written if every source fails.
"""
import html
import json
import re
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

# url, category, source. Add website feeds here after checking their terms.
SOURCES = [
    ("https://www.tmd.go.th/api/xml/CAP", "เตือนภัยอากาศ", "กรมอุตุนิยมวิทยา"),
] + [
    (f"https://www.tmd.go.th/api/xml/region-daily-forecast?regionid={i}", "พยากรณ์อากาศ", "กรมอุตุนิยมวิทยา")
    for i in range(1, 8)
]


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def clean_text(raw):
    text = html.unescape(raw or "")
    text = re.sub(r"<\s*/?\s*[a-zA-Z][^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_iso(raw):
    """Return an ISO date in Bangkok time, or None when the feed date is unusable."""
    try:
        return parsedate_to_datetime(raw).astimezone(BANGKOK).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return None


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
            "published": to_iso(node.findtext("pubDate")),
        })
    return items


def collect():
    items, errors = {}, []
    for url, category, source in SOURCES:
        try:
            for item in parse_feed(fetch(url), category, source):
                items.setdefault(item["id"], item)
        except Exception as error:  # one broken source must not stop the others
            errors.append({"url": url, "error": str(error)})
    ordered = sorted(items.values(), key=lambda item: item["published"] or "", reverse=True)
    return ordered[:MAX_ITEMS], errors


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
    items, errors = collect()
    for error in errors:
        print(f"SOURCE FAILED: {error['url']}: {error['error']}", file=sys.stderr)
    if not items:
        print("No items from any source, nothing written.", file=sys.stderr)
        return 1
    now = datetime.now(BANGKOK)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"news-{now:%Y-%m-%d-%H}.json"
    feed = {"updated": now.isoformat(timespec="minutes"), "errors": errors, "items": items}
    path.write_text(json.dumps(feed, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} items, {len(errors)} failed sources, wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
