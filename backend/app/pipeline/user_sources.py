"""Funding sources added on the platform's Sources panel.

Three kinds, read on every run alongside the sources in sources.yaml and
through exactly the same stages:

  linkedin  a profile or company page             Apify, ~$0.002 a post, daily
  website   any news or blog page                 its RSS feed when it has one (free,
                                                  every run), else the page through
                                                  Firecrawl (1 credit, daily)
  search    a phrase searched in news on the web  Firecrawl, ~2-4 credits, daily

How a website is read is settled when it is added: the link itself may be a
feed, or the page may declare one (<link rel="alternate">); a feed is free and
gives each story its own link and date, so it always wins. Every fetch of an
added link goes through `netguard` -- the link is untrusted.

A source is refused, in words, when it is already a source (built-in or
added), when the link is not public, or when `max_user_sources` are in use.
Removing one stops it being read; what it already found stays.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import feedparser
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .. import netguard
from ..config import settings
from ..db import session_scope
from ..models import UserSource
from .items import FUNDING_PAGE, clean, normalise_url
from .sources import Source
from .sources.linkedin import LinkedinSource, canonical_url
from .sources.pages import PageSource
from .sources.rss import RssSource
from .sources.web_search import WebSearchSource

log = logging.getLogger(__name__)

KINDS = ("linkedin", "website", "search")
MIN_PHRASE, MAX_PHRASE = 3, 200


class SourceInputError(ValueError):
    """A source that cannot be added, worded for the reader."""


# --- Reading a website: feed or page ---------------------------------------------------

class _FeedLinks(HTMLParser):
    """Collects <link rel="alternate" type="application/rss+xml|atom+xml" href>."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if ("alternate" in a.get("rel", "").lower().split()
                and a.get("type", "").lower() in ("application/rss+xml", "application/atom+xml")
                and a.get("href")):
            self.hrefs.append(a["href"].strip())


def _is_feed(page: netguard.Fetched) -> bool:
    head = page.body[:500].lstrip().lower()
    if not ("xml" in page.content_type or "rss" in page.content_type
            or head.startswith((b"<?xml", b"<rss", b"<feed", b"<rdf"))):
        return False
    parsed = feedparser.parse(page.body)
    return bool(parsed.entries)


def feed_candidates(page_url: str, html: str) -> list[str]:
    """The feeds a page declares, its own section's first: a category page on a
    WordPress site lists the site-wide feed before its own."""
    parser = _FeedLinks()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - broken markup means no declared feed
        return []
    links = list(dict.fromkeys(urljoin(page_url, h) for h in parser.hrefs))
    own = urlparse(page_url).path.rstrip("/")
    links.sort(key=lambda link: 0 if own and urlparse(link).path.startswith(own + "/") else 1)
    return [link for link in links if not link.rstrip("/").endswith("/comments/feed")]


def how_to_read(url: str) -> tuple[str, str]:
    """("rss", feed url) when the link is or declares a working feed, else
    ("scrape", url). Raises SourceInputError for a link that may not be read."""
    try:
        url = netguard.check_url(url)
    except netguard.UnsafeUrl as exc:
        raise SourceInputError(str(exc)) from None
    try:
        page = netguard.fetch(url)
    except netguard.UnsafeUrl as exc:
        # Refused plain HTTP, timed out or needs a browser: Firecrawl reads it.
        log.info("website %s: plain fetch failed (%s); will scrape", url, exc)
        return "scrape", url
    if _is_feed(page):
        return "rss", page.url
    for candidate in feed_candidates(page.url, page.text)[:3]:
        try:
            feed = netguard.fetch(candidate)
        except netguard.UnsafeUrl:
            continue
        if _is_feed(feed):
            return "rss", feed.url
    return "scrape", url


# --- Adding and removing ---------------------------------------------------------------

def _all_rows() -> list[UserSource]:
    with session_scope() as s:
        return list(s.scalars(select(UserSource).order_by(UserSource.id)).all())


def _builtin() -> tuple[Source, ...]:
    from .registry import RegistryError, load_sources
    try:
        return load_sources()
    except RegistryError:
        return ()


def _check_room() -> None:
    with session_scope() as s:
        count = s.scalar(select(func.count(UserSource.id))) or 0
    if count >= settings.max_user_sources:
        raise SourceInputError(f"There are already {count} added sources, the most allowed. "
                               f"Remove one first.")


def add(kind: str, value: str, label: str | None = None) -> UserSource:
    """Validate and save a source. It is read on the next run."""
    if kind not in KINDS:
        raise SourceInputError("Choose a LinkedIn account, a website or a search phrase.")
    label = clean(label or "", 200) or None
    value = (value or "").strip()
    if not value:
        raise SourceInputError("Paste a link or type a phrase to add.")
    _check_room()
    builtin = _builtin()
    added = _all_rows()
    via = read_url = None

    if kind == "linkedin":
        try:
            value, slug = canonical_url(value)
        except ValueError:
            raise SourceInputError("Paste a LinkedIn profile (linkedin.com/in/...) or company "
                                   "page (linkedin.com/company/...).") from None
        if any(s.key == f"linkedin:{slug}" for s in builtin) or any(
                r.kind == "linkedin" and r.value == value for r in added):
            raise SourceInputError("That LinkedIn account is already a source.")
        label = label or slug

    elif kind == "search":
        value = clean(value, MAX_PHRASE + 1)
        if not MIN_PHRASE <= len(value) <= MAX_PHRASE:
            raise SourceInputError(f"A search phrase is {MIN_PHRASE} to {MAX_PHRASE} characters.")
        known = {q.casefold() for s in builtin if isinstance(s, WebSearchSource)
                 for q in s.queries}
        known |= {r.value.casefold() for r in added if r.kind == "search"}
        if value.casefold() in known:
            raise SourceInputError("That phrase is already searched.")
        label = label or value

    else:
        try:
            value = netguard.check_url(value)
        except netguard.UnsafeUrl as exc:
            raise SourceInputError(str(exc)) from None
        key = normalise_url(value)
        if any(normalise_url(r.value) == key or normalise_url(r.read_url or "") == key
               for r in added if r.kind == "website") or any(
                isinstance(s, RssSource) and normalise_url(s.url) == key for s in builtin):
            raise SourceInputError("That website is already a source.")
        via, read_url = how_to_read(value)
        if via == "rss" and any(isinstance(s, RssSource) and normalise_url(s.url)
                                == normalise_url(read_url) for s in builtin):
            raise SourceInputError("That website's feed is already a built-in source.")
        label = label or (urlparse(value).hostname or value).removeprefix("www.")

    try:
        with session_scope() as s:
            row = UserSource(kind=kind, value=value[:1000], label=label[:200], via=via,
                             read_url=read_url)
            s.add(row)
    except IntegrityError:
        raise SourceInputError("That is already a source.") from None
    log.info("source added: %s %s (%s)", kind, value, via or "-")
    return row


def remove(source_id: int) -> bool:
    with session_scope() as s:
        row = s.get(UserSource, source_id)
        if row is None:
            return False
        s.delete(row)
    return True


# --- As pipeline sources ---------------------------------------------------------------

def to_source(row: UserSource) -> Source:
    every = float(settings.user_source_hours)
    if row.kind == "linkedin":
        _, slug = canonical_url(row.value)
        return LinkedinSource(key=f"linkedin:{slug}", type="linkedin", label=row.label,
                              every_hours=every, paid=True, user_id=row.id, url=row.value,
                              slug=slug)
    if row.kind == "search":
        return WebSearchSource(key=f"search:{row.id}", type="web_search", label=row.label,
                               every_hours=every, paid=True, user_id=row.id,
                               queries=(row.value,))
    if row.via == "rss":
        return RssSource(key=f"site:{row.id}", type="website", label=row.label, every_hours=0,
                         paid=False, user_id=row.id, url=row.read_url or row.value)
    return PageSource(key=f"site:{row.id}", type="website", label=row.label, every_hours=every,
                      paid=True, user_id=row.id, url=row.read_url or row.value,
                      item_kind=FUNDING_PAGE)


def sources() -> list[Source]:
    """Every added source, as the orchestrator reads them. A row that no longer
    validates is skipped and logged, never fatal to the run."""
    out: list[Source] = []
    for row in _all_rows():
        try:
            out.append(to_source(row))
        except ValueError as exc:
            log.error("added source %d is not valid and is skipped: %s", row.id, exc)
    return out
