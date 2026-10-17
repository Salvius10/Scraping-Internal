"""Loads sources.yaml into Source objects, and refuses a broken registry.

A mistake in the registry (a duplicate key, a non-http URL, a LinkedIn link
that is not an account) fails the whole load with a message naming it, so it
is caught when the run starts rather than half-way through one.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import settings
from .items import web_url
from .sources import Source
from .sources.eventbrite import EventbriteSource
from .sources.linkedin import LinkedinSource, canonical_url
from .sources.luma import LumaSource
from .sources.pages import PageSource
from .sources.rss import RssSource
from .sources.web_search import WebSearchSource

_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")


class RegistryError(ValueError):
    """sources.yaml is not valid."""


def _section(data: dict, *path: str) -> dict:
    node: object = data
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise RegistryError(f"{'.'.join(path)} must be a mapping")
    return node


def _every(section: dict, where: str, default: float) -> float:
    value = section.get("every_hours", default)
    if not isinstance(value, (int, float)) or value < 0:
        raise RegistryError(f"{where}.every_hours must be a number of hours >= 0")
    return float(value)


def _count(section: dict, name: str, where: str, default: int, top: int) -> int:
    value = section.get(name, default)
    if not isinstance(value, int) or not 1 <= value <= top:
        raise RegistryError(f"{where}.{name} must be a whole number from 1 to {top}")
    return value


def _strings(section: dict, name: str, where: str) -> tuple[str, ...]:
    values = section.get(name) or []
    if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
        raise RegistryError(f"{where}.{name} must be a list of text")
    return tuple(v.strip() for v in values)


def _url(value: object, where: str) -> str:
    url = web_url(value)
    if url is None:
        raise RegistryError(f"{where}: {value!r} is not an http(s) URL")
    return url


def _key(value: object, where: str) -> str:
    if not isinstance(value, str) or not _KEY.match(value):
        raise RegistryError(f"{where}: key {value!r} must be lower-case letters, digits, - or _")
    return value


def parse(data: dict) -> list[Source]:
    if not isinstance(data, dict):
        raise RegistryError("sources.yaml must be a mapping")
    sources: list[Source] = []

    rss = _section(data, "funding", "rss")
    every = _every(rss, "funding.rss", 0)
    for n, feed in enumerate(rss.get("feeds") or []):
        where = f"funding.rss.feeds[{n}]"
        if not isinstance(feed, dict):
            raise RegistryError(f"{where} must be a mapping")
        key = _key(feed.get("key"), where)
        sources.append(RssSource(key=f"rss:{key}", type="rss", label=str(feed.get("label") or key),
                                 every_hours=every, paid=False, url=_url(feed.get("url"), where)))

    linkedin = _section(data, "funding", "linkedin")
    every = _every(linkedin, "funding.linkedin", 24)
    for n, account in enumerate(linkedin.get("accounts") or []):
        where = f"funding.linkedin.accounts[{n}]"
        if not isinstance(account, dict):
            raise RegistryError(f"{where} must be a mapping")
        try:
            url, slug = canonical_url(str(account.get("url") or ""))
        except ValueError as exc:
            raise RegistryError(f"{where}: {exc}") from None
        sources.append(LinkedinSource(
            key=f"linkedin:{slug}", type="linkedin", label=str(account.get("label") or slug),
            every_hours=every, paid=True, url=url, slug=slug))

    web = _section(data, "funding", "web_search")
    queries = _strings(web, "queries", "funding.web_search")
    if queries:
        sources.append(WebSearchSource(
            key="web_search", type="web_search", label="Web search",
            every_hours=_every(web, "funding.web_search", 24), paid=True, queries=queries,
            lookback_days=_count(web, "lookback_days", "funding.web_search", 2, 30),
            results_per_query=_count(web, "results_per_query", "funding.web_search", 20, 100)))

    luma = _section(data, "events", "luma")
    cities = _strings(luma, "cities", "events.luma")
    if cities:
        sources.append(LumaSource(
            key="luma", type="luma", label="Luma", every_hours=_every(luma, "events.luma", 24),
            paid=True, cities=cities, keyword=str(luma.get("keyword") or ""),
            max_items=_count(luma, "max_items", "events.luma", 60, 1000)))

    eventbrite = _section(data, "events", "eventbrite")
    locations = _strings(eventbrite, "locations", "events.eventbrite")
    if locations:
        sources.append(EventbriteSource(
            key="eventbrite", type="eventbrite", label="Eventbrite",
            every_hours=_every(eventbrite, "events.eventbrite", 24), paid=True,
            locations=locations, keyword=str(eventbrite.get("keyword") or ""),
            max_items=_count(eventbrite, "max_items", "events.eventbrite", 60, 1000)))

    pages = _section(data, "events", "pages")
    every = _every(pages, "events.pages", 24)
    for n, portal in enumerate(pages.get("portals") or []):
        where = f"events.pages.portals[{n}]"
        if not isinstance(portal, dict):
            raise RegistryError(f"{where} must be a mapping")
        key = _key(portal.get("key"), where)
        sources.append(PageSource(key=f"page:{key}", type="page",
                                  label=str(portal.get("label") or key), every_hours=every,
                                  paid=True, url=_url(portal.get("url"), where)))

    keys = [s.key for s in sources]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise RegistryError(f"duplicate source keys: {', '.join(duplicates)}")
    return sources


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> tuple[Source, ...]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(f"could not read {Path(path).name}: {exc}") from None
    return tuple(parse(data))


def load_sources(path: Path | None = None) -> tuple[Source, ...]:
    """Every source in the registry. Re-read when the file changes."""
    p = Path(path or settings.sources_file)
    try:
        mtime = p.stat().st_mtime
    except OSError as exc:
        raise RegistryError(f"could not read {p.name}: {exc}") from None
    return _load(str(p), mtime)


def all_sources() -> list[Source]:
    """The registry's sources, then those added on the Sources panel. A key in
    both is read once, as the registry's."""
    from .user_sources import sources as added

    builtin = list(load_sources())
    keys = {s.key for s in builtin}
    return builtin + [s for s in added() if s.key not in keys]
