"""RSS ingest -- the free path, covering 5 of our 6 sources.

The feed already contains headline, link, published date and a description
written by the publisher. Paying a model to re-derive any of that would be
spending money to get a worse answer.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import httpx

from ..config import settings
from .sources import Source

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class FeedItem:
    """A normalised feed entry, before dedupe and storage."""

    url: str
    headline: str
    description: str | None
    published_at: datetime | None
    source: str
    feed_categories: list[str]


def clean_text(raw: str | None, limit: int = 600) -> str | None:
    """Strip markup and entities from a feed description."""
    if not raw:
        return None
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0]
        text = cut + "…"
    return text


def _parse_date(entry) -> datetime | None:
    """feedparser's struct_time first; dateparser only as a fallback."""
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass

    for attr in ("published", "updated", "pubDate"):
        raw = getattr(entry, attr, None)
        if not raw:
            continue
        try:
            import dateparser
            dt = dateparser.parse(raw, settings={"RETURN_AS_TIMEZONE_AWARE": True})
            if dt:
                return dt.astimezone(timezone.utc)
        except Exception:  # noqa: BLE001 - a bad date must not kill the run
            continue
    return None


def fetch_feed(source: Source, client: httpx.Client | None = None) -> list[FeedItem]:
    """Fetch and normalise one RSS feed. Raises on transport failure."""
    if not source.feed_url:
        raise ValueError(f"{source.name} has no feed_url")

    owns_client = client is None
    client = client or httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    try:
        resp = client.get(source.feed_url)
        resp.raise_for_status()
    finally:
        if owns_client:
            client.close()

    items = parse_feed(resp.content, source.name)
    log.info("%s: %d items from %s", source.name, len(items), source.feed_url)
    return items


def parse_feed(content: bytes | str, source_name: str) -> list[FeedItem]:
    """Normalise raw RSS/Atom into FeedItems. Shared with live search."""
    parsed = feedparser.parse(content)

    if parsed.bozo and not parsed.entries:
        raise ValueError(
            f"{source_name}: unparseable feed ({parsed.get('bozo_exception')})"
        )

    items: list[FeedItem] = []
    for entry in parsed.entries:
        url = (getattr(entry, "link", "") or "").strip()
        headline = clean_text(getattr(entry, "title", None), limit=300)
        if not url or not headline:
            continue

        desc = clean_text(
            getattr(entry, "summary", None) or getattr(entry, "description", None)
        )
        cats = [
            t.get("term", "").strip()
            for t in getattr(entry, "tags", []) or []
            if t.get("term")
        ]

        items.append(FeedItem(
            url=url,
            headline=headline,
            description=desc,
            published_at=_parse_date(entry),
            source=source_name,
            feed_categories=cats,
        ))
    return items
