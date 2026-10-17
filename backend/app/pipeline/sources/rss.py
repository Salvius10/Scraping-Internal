"""RSS feeds of startup news publishers -- the free funding source.

The feed already carries headline, link, publish time and a publisher-written
description, so nothing here costs money. Whether a story announces a funding
round is the LLM stage's call, not this one's.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser

from ... import netguard
from ..items import FUNDING, RawItem, clean, web_url
from . import FetchContext, FetchResult, Source, SourceError

log = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")


def strip_markup(raw: object, limit: int = 1500) -> str:
    """Feed descriptions are HTML: drop tags, decode entities, collapse space."""
    if not isinstance(raw, str):
        return ""
    return clean(html.unescape(_TAG.sub(" ", raw)), limit)


def _published(entry) -> datetime | None:
    """feedparser's parsed struct first; dateparser only as a fallback."""
    for attr in ("published_parsed", "updated_parsed"):
        st = entry.get(attr)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    for attr in ("published", "updated"):
        raw = entry.get(attr)
        if not raw:
            continue
        try:
            import dateparser
            parsed = dateparser.parse(raw, settings={"RETURN_AS_TIMEZONE_AWARE": True})
        except Exception:  # noqa: BLE001 - a bad date must not fail the feed
            parsed = None
        if parsed:
            return parsed.astimezone(timezone.utc)
    return None


def parse_feed(content: bytes, source: "RssSource") -> list[RawItem]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise SourceError(f"{source.label}: the feed could not be read "
                          f"({type(parsed.get('bozo_exception')).__name__}).")
    items: list[RawItem] = []
    for entry in parsed.entries:
        url = web_url(entry.get("link"))
        title = strip_markup(entry.get("title"), 300)
        if not (url and title):
            continue
        items.append(RawItem(
            kind=FUNDING, source_key=source.key, source_type=source.type,
            source_label=source.label, url=url, title=title,
            text=strip_markup(entry.get("summary") or entry.get("description")),
            published_at=_published(entry),
        ))
    return items


@dataclass(frozen=True)
class RssSource(Source):
    url: str = ""

    def fetch(self, ctx: FetchContext) -> FetchResult:
        # Feeds added on the page are untrusted links, so every feed is fetched
        # through the guard: public addresses only, redirects re-checked, 3 MB.
        try:
            page = netguard.fetch(self.url)
        except netguard.UnsafeUrl as exc:
            raise SourceError(f"{self.label}: {exc}") from None
        items = parse_feed(page.body, self)
        log.info("%s: %d items", self.key, len(items))
        return FetchResult(items=items)
