"""Phase 1: probe candidate sources for free ingest and live-search paths.

Stdlib only, so it runs before anything is installed. For each domain it answers
three questions that decide the whole ingest design for that source:

  1. Can we fetch it at all with a normal browser User-Agent?
  2. Does it publish an RSS/Atom feed? (free scheduled ingest)
  3. Does it expose a search endpoint returning a feed? (free *real-time*
     Intelligence search -- verified working on Inc42 as `?s=<q>&feed=rss2`)

Writes scripts/discovery_report.json and prints a summary table.
"""

from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
TIMEOUT = 15
SEARCH_TERM = "funding"

# The 7 v1 candidates. AIBoomi is excluded -- its feed is valid but empty.
SITES = {
    "indianstartupnews": "https://indianstartupnews.com/",
    "inc42": "https://inc42.com/",
    "yourstory": "https://yourstory.com/",
    "sujatachronicle": "https://sujatachronicle.com/",
    "entrackr": "https://entrackr.com/",
    "vccircle": "https://www.vccircle.com/",
    "moneycontrol": "https://www.moneycontrol.com/",
}

FEED_PATHS = [
    "feed", "feed/", "rss", "rss/", "rss.xml", "atom.xml",
    "feeds/posts/default", "index.xml", "rss/news", "rss/latest",
]

# Search endpoints worth trying. The `feed` variant is the prize: a real-time,
# targeted, zero-cost result set for the Intelligence section.
SEARCH_PATHS = [
    ("wp_search_feed", "?s={q}&feed=rss2"),
    ("wp_search_html", "?s={q}"),
    ("path_search_html", "search?q={q}"),
    ("path_search_html2", "search/{q}"),
]


def fetch(url):
    """GET a URL. Never raises -- failure is data, not an exception."""
    req = urllib.request.Request(url, headers=HEADERS)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read(3_000_000)
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return {
                "ok": True,
                "status": r.status,
                "final_url": r.geturl(),
                "content_type": r.headers.get("Content-Type", ""),
                "body": raw.decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": "HTTP %d" % e.code}
    except urllib.error.URLError as e:
        return {"ok": False, "status": None, "error": "URL error: %s" % (e.reason,)}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "status": None, "error": "timeout"}
    except Exception as e:  # noqa: BLE001 - probing, report anything
        return {"ok": False, "status": None, "error": "%s: %s" % (type(e).__name__, e)}


def parse_feed(body):
    """Return item count + links if body is a parseable RSS/Atom feed, else None."""
    head = body.lstrip()[:400].lower()
    if "<rss" not in head and "<feed" not in head and "<?xml" not in head:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)

    links, titles, dates = [], [], []
    for it in items:
        link_el = it.find("link")
        link = ""
        if link_el is not None and link_el.text:
            link = link_el.text.strip()
        if not link:
            a = it.find("atom:link", ns)
            if a is not None:
                link = a.get("href", "")
        links.append(link)

        t = it.find("title")
        if t is None:
            t = it.find("atom:title", ns)
        titles.append(t.text.strip() if t is not None and t.text else "")

        d = it.find("pubDate")
        if d is None:
            d = it.find("atom:updated", ns)
        dates.append(d.text.strip() if d is not None and d.text else "")

    return {
        "items": len(items),
        "links": links,
        "first_title": titles[0] if titles else "",
        "newest_date": dates[0] if dates else "",
    }


def homepage_feed_links(body, base):
    """Extract feeds the site declares in <head>."""
    out = []
    for m in re.finditer(r"<link\b[^>]*>", body, re.I):
        tag = m.group(0)
        if not re.search(r"rel=[\"']?alternate", tag, re.I):
            continue
        if not re.search(r"type=[\"']?application/(rss|atom)\+xml", tag, re.I):
            continue
        href = re.search(r"href=[\"']([^\"']+)[\"']", tag, re.I)
        if href:
            out.append(urljoin(base, href.group(1)))
    return out


def probe(name, home):
    res = {"name": name, "home": home, "reachable": False,
           "feeds": [], "search": {}, "notes": []}

    root_resp = fetch(home)
    res["reachable"] = root_resp["ok"]
    res["home_status"] = root_resp.get("status")
    if not root_resp["ok"]:
        res["notes"].append("homepage unreachable: %s" % root_resp.get("error"))

    candidates = list(FEED_PATHS)
    if root_resp["ok"]:
        for link in homepage_feed_links(root_resp["body"], root_resp["final_url"]):
            if link not in candidates:
                candidates.insert(0, link)
                res["notes"].append("declared feed in <head>: %s" % link)

    seen_urls = set()
    baseline_links = set()
    for path in candidates:
        url = path if path.startswith("http") else urljoin(home, path)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        r = fetch(url)
        if not r["ok"]:
            continue
        parsed = parse_feed(r["body"])
        if parsed and parsed["items"] > 0:
            res["feeds"].append({
                "url": r["final_url"],
                "items": parsed["items"],
                "newest": parsed["newest_date"],
                "first_title": parsed["first_title"][:90],
            })
            if not baseline_links:
                baseline_links = set(l for l in parsed["links"] if l)
        elif parsed:
            res["notes"].append("%s -> valid feed but ZERO items" % url)

    # Search probes -- the real-time Intelligence path.
    for label, tmpl in SEARCH_PATHS:
        url = urljoin(home, tmpl.format(q=SEARCH_TERM))
        r = fetch(url)
        if not r["ok"]:
            res["search"][label] = {"ok": False, "error": r.get("error")}
            continue
        parsed = parse_feed(r["body"])
        if parsed and parsed["items"] > 0:
            got = set(l for l in parsed["links"] if l)
            differs = bool(baseline_links) and got != baseline_links
            res["search"][label] = {
                "ok": True,
                "kind": "feed",
                "items": parsed["items"],
                "differs_from_main_feed": differs,
                "first_title": parsed["first_title"][:90],
            }
        else:
            body = r["body"]
            hits = len(re.findall(
                r"<article\b|class=[\"'][^\"']*(post|card|story|result)", body, re.I))
            res["search"][label] = {
                "ok": True, "kind": "html", "bytes": len(body),
                "result_markers": hits,
            }
    return res


def live_search_verdict(r):
    """Classify the cheapest real-time search path available for a source."""
    for _k, v in r["search"].items():
        if v.get("kind") == "feed" and v.get("differs_from_main_feed"):
            return "FEED (free)"
    if any(v.get("kind") == "feed" for v in r["search"].values()):
        return "feed, unfiltered"
    if any(v.get("ok") and v.get("result_markers", 0) > 3
           for v in r["search"].values()):
        return "HTML (needs parse)"
    return "none found"


def main():
    print("Probing %d domains (browser UA, %ds timeout)...\n" % (len(SITES), TIMEOUT))
    with ThreadPoolExecutor(max_workers=7) as ex:
        results = list(ex.map(lambda kv: probe(kv[0], kv[1]), SITES.items()))

    hdr = "%-20s %-6s %-6s %-42s %-20s" % (
        "SOURCE", "REACH", "FEEDS", "BEST FEED", "LIVE SEARCH")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        feeds = r["feeds"]
        best = max(feeds, key=lambda f: f["items"])["url"] if feeds else "-"
        if len(best) > 41:
            best = best[:40] + "…"
        print("%-20s %-6s %-6d %-42s %-20s" % (
            r["name"], "yes" if r["reachable"] else "NO",
            len(feeds), best, live_search_verdict(r)))

    for r in results:
        if r["notes"]:
            print("\n[%s]" % r["name"])
            for n in r["notes"]:
                print("  - %s" % n)

    out = Path(__file__).parent / "discovery_report.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\nFull report -> %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
