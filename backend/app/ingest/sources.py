"""Loader for sources.yaml -- the single place sources are configured."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import settings


@dataclass(frozen=True)
class LiveSearch:
    """How the Intelligence section reaches this source in real time.

    kind:
      search_feed  -- free, targeted RSS of search results (best case)
      feed_refetch -- free, newest items, filtered to the question locally
      search_html  -- free, the site's own search page parsed as result cards
    """

    kind: str
    url: str

    def build(self, query: str) -> str:
        """Fill {q} (URL-encoded) and {slug} (tag-style) placeholders."""
        import re
        from urllib.parse import quote_plus
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        return self.url.replace("{q}", quote_plus(query)).replace("{slug}", slug)


@dataclass(frozen=True)
class Source:
    name: str
    label: str
    home: str
    strategy: str                      # "rss" | "scrape"
    feed_url: str | None = None
    listing_url: str | None = None
    feed_depth: int = 0
    live_search: LiveSearch | None = None
    extra: dict = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        """True when ingesting this source costs money (ScrapeGraphAI)."""
        return self.strategy == "scrape"


def _parse(raw: dict) -> Source:
    ls = raw.get("live_search") or None
    return Source(
        name=raw["name"],
        label=raw.get("label", raw["name"]),
        home=raw["home"],
        strategy=raw.get("strategy", "rss"),
        feed_url=raw.get("feed_url"),
        listing_url=raw.get("listing_url"),
        feed_depth=int(raw.get("feed_depth") or 0),
        live_search=LiveSearch(kind=ls["kind"], url=ls["url"]) if ls else None,
    )


@lru_cache(maxsize=1)
def load_sources(path: Path | None = None) -> tuple[Source, ...]:
    p = Path(path or settings.sources_file)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return tuple(_parse(r) for r in data.get("sources", []))


def rss_sources() -> tuple[Source, ...]:
    return tuple(s for s in load_sources() if s.strategy == "rss")


def scrape_sources() -> tuple[Source, ...]:
    return tuple(s for s in load_sources() if s.strategy == "scrape")


def get_source(name: str) -> Source | None:
    return next((s for s in load_sources() if s.name == name), None)


@lru_cache(maxsize=1)
def excluded_sources(path: Path | None = None) -> tuple[dict, ...]:
    """Sources deliberately left out, with the reason recorded."""
    p = Path(path or settings.sources_file)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return tuple(data.get("excluded", []))
