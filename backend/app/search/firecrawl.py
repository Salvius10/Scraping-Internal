"""Hosted Firecrawl: web search, and a scrape of one page on demand.

Two calls, both plain HTTP -- no SDK, so nothing new to install:

  POST /v2/search   query -> ranked results (url, title, description).
                    Pages are NOT fetched. Documented as 1 credit per 10
                    results; measured at 2 on the first live run.
  POST /v2/scrape   one url -> clean main-content markdown. 1 credit.

Firecrawl credits are billed by Firecrawl, not by our $7 LLM ledger. Both
calls are cached so a repeated search, or a result clicked twice, is paid
for once. Every failure is turned into a sentence a reader can act on.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ..config import settings

SEARCH_TTL = 15 * 60
SCRAPE_TTL = 60 * 60

# Steer a plain query toward startup news unless it is already about that.
_STARTUP_WORDS = re.compile(
    r"\b(startups?|funding|funded|raises?|raised|founders?|venture|vcs?|"
    r"unicorns?|ipo|seed|series\s+[a-h]|valuation|acqui\w*|m&a)\b",
    re.I,
)

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class FirecrawlError(RuntimeError):
    """A Firecrawl failure, worded for the reader."""


@dataclass
class SearchResult:
    n: int
    url: str
    title: str
    description: str
    domain: str


@dataclass
class Search:
    query: str
    searched: str
    results: list[SearchResult] = field(default_factory=list)
    credits_used: int = 0
    cached: bool = False


@dataclass
class Page:
    url: str
    title: str
    description: str
    markdown: str
    status_code: int | None = None
    credits_used: int = 0


def steer(query: str) -> str:
    """Bias a query toward startup news without overriding what was asked."""
    query = " ".join((query or "").split())
    if query and not _STARTUP_WORDS.search(query):
        query = f"{query} startup news"
    return query


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"(^|\s)#{1,6}\s+|\*\*|__|`|\|")


def plain(text: str, limit: int = 320) -> str:
    """Flatten Firecrawl's markdown snippets into one readable line.

    Seen live: descriptions like "# Find Top Startups ## Track new startups
    funded by [top inv..." -- headings, links and table pipes that would
    show up as literal symbols in the results list.
    """
    text = _MD_LINK.sub(r"\1", text or "")
    text = _MD_MARKS.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _client() -> httpx.Client:
    """The HTTP client for Firecrawl. A seam tests replace with a mock."""
    return httpx.Client(timeout=settings.firecrawl_timeout + 5)


def _post(path: str, body: dict) -> dict:
    if not settings.firecrawl_api_key and "api.firecrawl.dev" in settings.firecrawl_api_url:
        raise FirecrawlError(
            "Firecrawl is not set up yet. Add FIRECRAWL_API_KEY to .env and "
            "restart the server.")

    headers = {"Content-Type": "application/json"}
    if settings.firecrawl_api_key:
        headers["Authorization"] = f"Bearer {settings.firecrawl_api_key}"

    try:
        with _client() as client:
            resp = client.post(settings.firecrawl_api_url.rstrip("/") + path,
                               json=body, headers=headers)
    except httpx.TimeoutException:
        raise FirecrawlError("Firecrawl took too long to answer. Try again.") from None
    except httpx.HTTPError as exc:
        raise FirecrawlError(f"Could not reach Firecrawl ({type(exc).__name__}).") from None

    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code == 401:
        raise FirecrawlError("Firecrawl rejected the API key. Check FIRECRAWL_API_KEY in .env.")
    if resp.status_code == 402:
        raise FirecrawlError("Firecrawl credits are used up. Top up at firecrawl.dev.")
    if resp.status_code == 429:
        raise FirecrawlError("Firecrawl's rate limit was hit. Wait a moment and try again.")
    if resp.status_code >= 400 or not payload.get("success", False):
        detail = payload.get("error") or payload.get("message") or f"HTTP {resp.status_code}"
        raise FirecrawlError(f"Firecrawl could not do that: {str(detail)[:200]}")
    return payload


def _cached(key: str):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    return None


def _store(key: str, value: object, ttl: int) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, value)


def _key(*parts: str) -> str:
    return hashlib.sha1("\n".join(p.strip().lower() for p in parts).encode()).hexdigest()


def search(query: str, limit: int | None = None) -> Search:
    """Search the web. Returns results only; nothing is scraped."""
    query = " ".join((query or "").split())
    if len(query) < 2:
        raise FirecrawlError("Type something to search for.")
    if len(query) > 400:
        raise FirecrawlError("Keep the search under 400 characters.")

    searched = steer(query)
    limit = limit or settings.firecrawl_search_limit
    key = _key("search", searched, str(limit))
    hit = _cached(key)
    if hit is not None:
        return Search(**{**hit.__dict__, "query": query, "cached": True, "credits_used": 0})

    payload = _post("/v2/search", {
        "query": searched,
        "limit": limit,
        "sources": ["web"],
        "ignoreInvalidURLs": True,
        "timeout": int(settings.firecrawl_timeout * 1000),
    })

    web = (payload.get("data") or {}).get("web") or []
    results: list[SearchResult] = []
    for row in web:
        url = (row or {}).get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        meta = row.get("metadata") or {}
        results.append(SearchResult(
            n=len(results) + 1,
            url=url,
            title=plain(row.get("title") or meta.get("title") or domain_of(url), 200),
            description=plain(row.get("description") or meta.get("description") or ""),
            domain=domain_of(url),
        ))

    found = Search(query=query, searched=searched, results=results,
                   credits_used=int(payload.get("creditsUsed") or 0))
    _store(key, found, SEARCH_TTL)
    return found


def _text(value: object) -> str:
    """Firecrawl metadata fields can be a string or a list of strings."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), "")
    return value.strip() if isinstance(value, str) else ""


def scrape(url: str) -> tuple[Page, bool]:
    """Fetch one page's main content as markdown. Returns (page, cached)."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise FirecrawlError("Only http and https links can be scraped.")

    key = _key("scrape", url)
    hit = _cached(key)
    if hit is not None:
        return hit, True

    payload = _post("/v2/scrape", {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "blockAds": True,
        "timeout": int(settings.firecrawl_timeout * 1000),
    })
    data = payload.get("data") or {}
    meta = data.get("metadata") or {}
    markdown = (data.get("markdown") or "").strip()
    if not markdown:
        raise FirecrawlError("That page came back empty. It may block automated readers.")

    page = Page(
        url=meta.get("url") or meta.get("sourceURL") or url,
        title=_text(meta.get("title")) or domain_of(url),
        description=_text(meta.get("description")),
        markdown=markdown,
        status_code=meta.get("statusCode"),
        credits_used=int(payload.get("creditsUsed") or 1),
    )
    _store(key, page, SCRAPE_TTL)
    return page, False


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
