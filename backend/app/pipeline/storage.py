"""Dedup Stage 2 and Storage -- one row per deal and per event, across sources.

Dedup Stage 1 removed repeats of the same item. This removes the same story
told by different sources: one round reported by three outlets, a LinkedIn
post and a search result; one event listed on Luma and Eventbrite.

  funding  same company (case, punctuation and "Pvt Ltd" aside) at the same
           stage, reported at the same link, or published within
           `duplicate_days` of each other. Undated reports at different links
           are never merged -- they would match anything.
  events   same title (case and punctuation aside) on the same day in India,
           or the same event link.

A duplicate is folded into the stored row: details the row lacks are filled
in, investors are combined, and the report is added to `sources`. The row
keeps the most trusted report as its own source URL and date -- for funding
the news feed, then websites you added, then web search, then LinkedIn; for
events Luma, then Eventbrite, then other portals. Every row is tagged with a
source URL and date.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import FundedStartup, StartupEvent, utcnow
from .items import FUNDING_PAGE
from .structure import EventRow, FundingRow, Work
from .timeutil import day_bounds, ist_day

FUNDING_PRIORITY = {"rss": 0, "website": 1, "web_search": 2, "linkedin": 3}
EVENT_PRIORITY = {"luma": 0, "eventbrite": 1, "page": 2}
NEW, MERGED = "new", "merged"

_SUFFIXES = {"pvt", "private", "ltd", "limited", "inc", "llp", "llc", "corp", "co"}


def company_key(name: str) -> str:
    """A company name in one spelling: "Orbo Pvt. Ltd." and "orbo" match."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)[:200] or name.lower()[:200]


def title_key(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())[:300]


# Old and new names of the same city, so the city filter has one entry each.
_CITY_ALIASES = {
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru", "bombay": "Mumbai",
    "new delhi": "Delhi", "delhi": "Delhi", "delhi ncr": "Delhi", "gurgaon": "Gurugram",
    "madras": "Chennai", "calcutta": "Kolkata",
}


def canonical_city(city: str | None) -> str | None:
    if not city:
        return None
    name = " ".join(city.replace(",", " ").split())
    return _CITY_ALIASES.get(name.lower(), name[:1].upper() + name[1:])[:120] or None


def _report(work: Work, published: datetime | None) -> dict:
    return {"url": work.url, "source": work.source_label, "type": work.source_type,
            "published_at": published.isoformat() if published else None}


def _with_report(reports: list, report: dict) -> list:
    if any(r.get("url") == report["url"] for r in reports):
        return list(reports)
    return [*reports, report]


# --- Funding -------------------------------------------------------------------------

def _same_window(a: datetime | None, b: datetime | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= timedelta(days=settings.duplicate_days)


def _rank(priority: dict, source_type: str) -> int:
    return priority.get(source_type, len(priority))


def store_funding(session: Session, work: Work, row: FundingRow) -> tuple[str, int]:
    """Insert the round, or fold it into the stored copy of the same deal.

    A round read off a page carries its own story link and date; a round with
    no link of its own is the page's. The same company and stage reported at
    the same link is always one deal -- that is how a page read again, after
    it changed, folds into the rows it made last time even when undated.
    """
    key = company_key(row.company)
    url = row.url or work.url
    published = row.published_at or work.published_at
    # A page's title names the page, not this round.
    headline = (work.title if work.kind != FUNDING_PAGE
                else row.summary or f"{row.company} raises funding")
    report = _report(work, published) | {"url": url}
    candidates = session.scalars(select(FundedStartup).where(
        FundedStartup.company_key == key, FundedStartup.stage == row.stage)).all()
    match = next((c for c in candidates if c.source_url == url
                  or any(isinstance(s, dict) and s.get("url") == url for s in c.sources or [])),
                 None) or next((c for c in candidates if _same_window(c.published_at, published)),
                               None)

    if match is None:
        stored = FundedStartup(
            company=row.company, company_key=key, stage=row.stage, round_label=row.round_label,
            amount=row.amount, investors=list(row.investors), sector=row.sector,
            hq_city=canonical_city(row.hq_city), summary=row.summary, headline=headline,
            source_url=url, source_label=work.source_label, source_type=work.source_type,
            published_at=published, sources=[report],
        )
        session.add(stored)
        session.flush()
        return NEW, stored.id

    if _rank(FUNDING_PRIORITY, work.source_type) < _rank(FUNDING_PRIORITY, match.source_type):
        match.source_url, match.source_label = url, work.source_label
        match.source_type, match.published_at = work.source_type, published
        match.headline = headline
    elif match.published_at is None and published is not None:
        match.published_at = published
    for name in ("round_label", "amount", "sector", "summary"):
        if getattr(match, name) is None and getattr(row, name) is not None:
            setattr(match, name, getattr(row, name))
    if match.hq_city is None:
        match.hq_city = canonical_city(row.hq_city)
    known = {i.lower() for i in match.investors or []}
    extra = [i for i in row.investors if i.lower() not in known]
    if extra:
        match.investors = [*(match.investors or []), *extra]      # reassigned: JSON columns
    match.sources = _with_report(match.sources or [], report)
    match.updated_at = utcnow()
    return MERGED, match.id


# --- Events ---------------------------------------------------------------------------

def store_event(session: Session, work: Work, row: EventRow) -> tuple[str, int]:
    """Insert the event, or fold it into the stored copy of the same event."""
    key = title_key(row.title)
    city = canonical_city(row.city)
    day = ist_day(row.starts_at)
    since, until = day_bounds(day, day)
    match = session.scalars(select(StartupEvent).where(or_(
        StartupEvent.url == row.url,
        (StartupEvent.title_key == key) & (StartupEvent.starts_at >= since)
        & (StartupEvent.starts_at < until),
    )).order_by(StartupEvent.id)).first()
    report = _report(work, None) | {"url": row.url}

    if match is None:
        stored = StartupEvent(
            title=row.title, title_key=key, starts_at=row.starts_at, ends_at=row.ends_at,
            date_only=row.date_only, city=city, venue=row.venue, online=row.online,
            organiser=row.organiser, category=row.category, price=row.price,
            summary=row.summary, url=row.url, source_url=row.url,
            source_label=work.source_label, source_type=work.source_type, sources=[report],
        )
        session.add(stored)
        session.flush()
        return NEW, stored.id

    if _rank(EVENT_PRIORITY, work.source_type) < _rank(EVENT_PRIORITY, match.source_type):
        # A structured portal beats a page the model read: take its facts.
        match.url = match.source_url = row.url
        match.source_label, match.source_type = work.source_label, work.source_type
        match.starts_at, match.ends_at, match.date_only = row.starts_at, row.ends_at, row.date_only
    for name in ("ends_at", "venue", "online", "organiser", "price", "summary"):
        if getattr(match, name) is None and getattr(row, name) is not None:
            setattr(match, name, getattr(row, name))
    if match.city is None:
        match.city = city
    if match.category in (None, "Other") and row.category != "Other":
        match.category = row.category
    match.sources = _with_report(match.sources or [], report)
    match.updated_at = utcnow()
    return MERGED, match.id
