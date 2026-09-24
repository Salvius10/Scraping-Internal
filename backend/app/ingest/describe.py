"""Fetch each article's own description and publish date. Costs nothing.

Publishers already write a summary for social previews (`og:description`) and
stamp the publish time in `article:published_time`. Both are free, exact, and
written by the outlet -- strictly better than asking a model to invent them.

Only articles the free path cannot supply go on to `enrich.py`, which pays.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Article, DescriptionOrigin
from .chunks import sync_chunk

log = logging.getLogger(__name__)

# Below this, a description is boilerplate ("Read more", a site tagline) and
# worth replacing with a generated one.
MIN_DESCRIPTION_CHARS = 40

_DESCRIPTION_META = (
    ("property", "og:description"),
    ("name", "og:description"),
    ("name", "twitter:description"),
    ("property", "twitter:description"),
    ("name", "description"),
)

_DATE_META = (
    ("property", "article:published_time"),
    ("name", "article:published_time"),
    ("property", "og:article:published_time"),
    ("name", "publish-date"),
    ("name", "pubdate"),
    ("itemprop", "datePublished"),
)


@dataclass
class PageMeta:
    description: str | None = None
    published_at: datetime | None = None


def _meta_content(soup: BeautifulSoup, pairs) -> str | None:
    for attr, value in pairs:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            text = " ".join(tag["content"].split()).strip()
            if text:
                return text
    return None


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        import dateparser
        dt = dateparser.parse(raw, settings={"RETURN_AS_TIMEZONE_AWARE": True})
        return dt.astimezone(timezone.utc) if dt else None
    except Exception:  # noqa: BLE001 - a bad date must not kill the pass
        return None


def extract_meta(html: str) -> PageMeta:
    """Pull description and publish time out of a page's <head>."""
    soup = BeautifulSoup(html, "html.parser")

    description = _meta_content(soup, _DESCRIPTION_META)
    if description and len(description) > 600:
        description = description[:600].rsplit(" ", 1)[0] + "…"

    published = _parse_dt(_meta_content(soup, _DATE_META))
    if published is None:
        # Fall back to a <time datetime="..."> element.
        tag = soup.find("time", attrs={"datetime": True})
        if tag:
            published = _parse_dt(tag["datetime"])

    return PageMeta(description=description, published_at=published)


def fetch_meta(url: str, client: httpx.Client) -> PageMeta:
    """Fetch one article and read its metadata. Never raises."""
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return extract_meta(resp.text)
    except Exception as exc:  # noqa: BLE001 - one bad article must not stop the pass
        log.debug("meta fetch failed for %s: %s", url, exc)
        return PageMeta()


def needs_meta(article: Article) -> bool:
    """True when the free path could still improve this article."""
    thin = (not article.description
            or len(article.description) < MIN_DESCRIPTION_CHARS)
    return thin or article.published_at is None


def describe_pending(limit: int | None = None, session: Session | None = None) -> int:
    """Fill missing descriptions and dates from article pages. Returns count updated.

    Scoped to canonical articles: duplicates are hidden from the feed, so
    fetching their pages would be wasted requests.
    """
    def _run(s: Session) -> int:
        stmt = (
            select(Article)
            .where(
                Article.canonical_id.is_(None),
                or_(
                    Article.description.is_(None),
                    Article.published_at.is_(None),
                ),
            )
            .order_by(Article.ingested_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        pending = list(s.scalars(stmt).all())

        if not pending:
            log.info("describe: nothing pending")
            return 0

        updated = 0
        last_host: str | None = None
        with httpx.Client(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            for article in pending:
                from urllib.parse import urlparse
                host = urlparse(article.url).netloc
                if host == last_host:
                    time.sleep(settings.per_domain_delay)
                last_host = host

                meta = fetch_meta(article.url, client)
                changed = False

                if meta.description and (
                    not article.description
                    or len(article.description) < len(meta.description)
                ):
                    article.description = meta.description
                    article.description_origin = DescriptionOrigin.META
                    sync_chunk(s, article)
                    changed = True

                if article.published_at is None and meta.published_at:
                    article.published_at = meta.published_at
                    changed = True

                if changed:
                    updated += 1

        log.info("describe: updated %d of %d articles (free)", updated, len(pending))
        return updated

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
