"""Real-time search of our own six sources. Free: plain HTTP, no model call.

The 12-hour scheduler keeps the feed fresh enough; a question must not inherit
that staleness. Each source is reached the way `sources.yaml` records under
`live_search`, all in parallel, each under its own timeout -- a slow or
blocking site is dropped and *reported*, never allowed to stall the answer.

Results are cached for `live_search_ttl` seconds so a reader refining a
question does not re-fetch six sites for every keystroke of it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..ingest.rss import FeedItem, clean_text, parse_feed
from ..ingest.sources import Source, load_sources
from .corpus import keywords

log = logging.getLogger(__name__)

# Result links worth following. Section, tag, author and search pages are
# navigation, not stories; article slugs are long.
_NAV_PATH = re.compile(
    r"^/(?:tag|tags|topic|topics|category|categories|author|authors|search|"
    r"page|about|contact|privacy|terms|login|subscribe|newsletter)(?:/|$)",
    re.I,
)
_MIN_PATH = 25
_MIN_HEADLINE = 20
_MAX_CLIMB = 6
_HEADING = re.compile(r"title|heading|headline", re.I)
_DATE_CLASS = re.compile(r"date|time", re.I)

_cache: dict[str, tuple[float, list[FeedItem]]] = {}
_cache_lock = threading.Lock()


@dataclass
class SourceResult:
    """What one source returned, including why it returned nothing."""

    name: str
    label: str
    kind: str
    status: str = "ok"          # ok | empty | timeout | error | skipped
    items: list[FeedItem] = field(default_factory=list)
    error: str | None = None
    cached: bool = False


# --- Parsing ----------------------------------------------------------------

def _article_link(href: str, base: str, host: str) -> str | None:
    link = urljoin(base, href).split("#")[0]
    parsed = urlparse(link)
    if parsed.netloc != host:
        return None
    if len(parsed.path) < _MIN_PATH or _NAV_PATH.match(parsed.path):
        return None
    return link


def _card(anchor, link: str, links_under: dict[int, set[str]]):
    """Climb from a link to the largest element that is about only that link.

    Result pages wrap each story in a card holding the headline, a date and
    often an excerpt; the card ends where an ancestor starts holding a second
    story's link. `links_under` is precomputed in one pass -- rescanning every
    ancestor per anchor is quadratic and took seconds on a 160KB page.
    """
    node = anchor
    for _ in range(_MAX_CLIMB):
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if links_under.get(id(parent), set()) - {link}:
            break
        node = parent
    return node


def _headline(card, anchor) -> str | None:
    for el in card.find_all(True):
        is_heading = el.name in ("h1", "h2", "h3", "h4", "h5") or any(
            _HEADING.search(c) for c in (el.get("class") or [])
        )
        if is_heading:
            text = clean_text(el.get_text(" ", strip=True), limit=300)
            if text and len(text) >= _MIN_HEADLINE:
                return text
    img = card.find("img", alt=True)
    if img and len(img["alt"].strip()) >= _MIN_HEADLINE:
        return clean_text(img["alt"], limit=300)
    return clean_text(anchor.get_text(" ", strip=True), limit=300)


def _published(card):
    import dateparser

    tag = card.find("time", attrs={"datetime": True})
    candidates = [tag["datetime"]] if tag else []
    for el in card.find_all(True):
        if any(_DATE_CLASS.search(c) for c in (el.get("class") or [])):
            text = el.get_text(" ", strip=True)
            if text and len(text) <= 40:
                candidates.append(text)
    for raw in candidates:
        try:
            dt = dateparser.parse(raw, settings={
                "RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "Asia/Kolkata",
            })
        except Exception:  # noqa: BLE001 - a bad date must not lose the story
            dt = None
        if dt:
            from datetime import timezone
            return dt.astimezone(timezone.utc)
    return None


def _excerpt(card, headline: str) -> str | None:
    for p in card.find_all("p"):
        text = clean_text(p.get_text(" ", strip=True))
        if text and len(text) >= 60 and text != headline:
            return text
    return None


def parse_cards(html: str, base_url: str, source_name: str) -> list[FeedItem]:
    """Extract result cards from a search or tag page. Deterministic, free."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc

    anchors = []
    links_under: dict[int, set[str]] = {}
    for anchor in soup.find_all("a", href=True):
        link = _article_link(anchor["href"], base_url, host)
        if link is None:
            continue
        anchors.append((anchor, link))
        node = anchor
        for _ in range(_MAX_CLIMB + 1):
            node = node.parent
            if node is None:
                break
            links_under.setdefault(id(node), set()).add(link)

    items: list[FeedItem] = []
    seen: set[str] = set()
    for anchor, link in anchors:
        if link in seen:
            continue
        card = _card(anchor, link, links_under)
        headline = _headline(card, anchor)
        if not headline or len(headline) < _MIN_HEADLINE:
            continue
        seen.add(link)
        items.append(FeedItem(
            url=link,
            headline=headline,
            description=_excerpt(card, headline),
            published_at=_published(card),
            source=source_name,
            feed_categories=[],
        ))
    return items


def matches(item: FeedItem, terms: list[str]) -> bool:
    """True when a term starts a word in the headline or description.

    Search pages carry sidebars and trending lists, and a re-fetched feed is
    not query-aware at all, so every live result must mention the question.
    """
    tokens = [t for t in keywords(" ".join(terms)) if len(t) >= 3]
    if not tokens:
        return False
    haystack = f"{item.headline} {item.description or ''}".lower()
    return any(re.search(rf"\b{re.escape(t)}", haystack) for t in tokens)


# --- Fetching ---------------------------------------------------------------

def _fetch(source: Source, query: str) -> tuple[list[FeedItem], bool]:
    """Everything this source offers for `query`. Returns (items, cached)."""
    ls = source.live_search
    url = ls.build(query)

    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1], True

    with httpx.Client(
        timeout=settings.live_search_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        resp = client.get(url)

    if resp.status_code == 404:
        items: list[FeedItem] = []   # e.g. no VCCircle tag for this query
    else:
        resp.raise_for_status()
        if ls.kind == "search_html":
            items = parse_cards(resp.text, str(resp.url), source.name)
        else:
            items = parse_feed(resp.content, source.name)

    with _cache_lock:
        _cache[url] = (now + settings.live_search_ttl, items)
    return items, False


def _search_one(source: Source, query: str, terms: list[str]) -> SourceResult:
    ls = source.live_search
    result = SourceResult(name=source.name, label=source.label, kind=ls.kind)
    try:
        items, result.cached = _fetch(source, query)
    except Exception as exc:  # noqa: BLE001 - one site must not sink the rest
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"[:200]
        log.info("live search %s failed: %s", source.name, result.error)
        return result

    # A targeted search feed is already query-aware; everything else is
    # filtered to what the question is actually about -- the searched name
    # when there is one, so a feed re-fetch for "Zepto" does not keep every
    # story that merely says "market".
    if ls.kind != "search_feed":
        wanted = [query] if keywords(query) else terms
        items = [i for i in items if matches(i, wanted)]

    result.items = items
    result.status = "ok" if items else "empty"
    return result


def search_live(
    query: str, terms: list[str], sources: tuple[Source, ...] | None = None,
) -> list[SourceResult]:
    """Search every source in parallel. Never raises; never waits past timeout."""
    sources = sources if sources is not None else load_sources()
    results: dict[str, SourceResult] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, len(sources)))
    futures = {}

    for source in sources:
        if source.live_search is None:
            results[source.name] = SourceResult(
                name=source.name, label=source.label, kind="none",
                status="skipped",
            )
            continue
        futures[pool.submit(_search_one, source, query, terms)] = source

    done, _ = wait(futures, timeout=settings.live_search_timeout + 2)
    for future, source in futures.items():
        if future in done:
            results[source.name] = future.result()
        else:
            results[source.name] = SourceResult(
                name=source.name, label=source.label,
                kind=source.live_search.kind, status="timeout",
                error=f"no answer within {settings.live_search_timeout:.0f}s",
            )
    # Do not block the answer on a straggler; its thread finishes on its own.
    pool.shutdown(wait=False, cancel_futures=True)

    return [results[s.name] for s in sources]


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
