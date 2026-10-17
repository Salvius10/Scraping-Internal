"""Platform: the India startup events feed.

  GET /api/events?when=upcoming|past|all&city=&start=&end=&q=&limit=&offset=
        upcoming soonest first; past most recent first; with a count per city

An event is upcoming until it ends; one with no end time until its start, and
one given as a day only until that day is over in India.

Reads only: no fetch, no model, so it costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from ..db import LIKE_ESCAPE, get_session, like_pattern
from ..models import StartupEvent, utcnow
from .common import MAX_LIMIT, search_text, window
from .funding import SourceRef

router = APIRouter(prefix="/api", tags=["events"])

WHEN = ("upcoming", "past", "all")


@dataclass
class EventFilter:
    when: str = "upcoming"
    city: str | None = None
    start: date | None = None
    end: date | None = None
    q: str | None = None
    now: datetime = field(default_factory=utcnow)


def event_filter(
    when: str = Query("upcoming", description="upcoming | past | all"),
    city: str | None = Query(None, max_length=120),
    start: date | None = Query(None, description="starts on or after (IST day)"),
    end: date | None = Query(None, description="starts on or before (IST day)"),
    q: str | None = Query(None, max_length=200),
) -> EventFilter:
    if when not in WHEN:
        raise HTTPException(status_code=400, detail=f"Unknown when {when!r}.")
    return EventFilter(when=when, city=" ".join((city or "").split()) or None,
                       start=start, end=end, q=search_text(q))


def _upcoming(now: datetime):
    # Every branch is true or false, never NULL: NOT (NULL OR false) is NULL,
    # which would drop an event from both the upcoming and the past list.
    return or_(
        and_(StartupEvent.ends_at.is_not(None), StartupEvent.ends_at >= now),
        and_(StartupEvent.ends_at.is_(None), StartupEvent.date_only.is_(False),
             StartupEvent.starts_at >= now),
        and_(StartupEvent.ends_at.is_(None), StartupEvent.date_only.is_(True),
             StartupEvent.starts_at > now - timedelta(days=1)),
    )


def query(f: EventFilter, *, with_city: bool = True, with_when: bool = True) -> Select:
    stmt = select(StartupEvent)
    since, until = window(f.start, f.end)
    if since is not None:
        stmt = stmt.where(StartupEvent.starts_at >= since)
    if until is not None:
        stmt = stmt.where(StartupEvent.starts_at < until)
    if with_when and f.when == "upcoming":
        stmt = stmt.where(_upcoming(f.now))
    elif with_when and f.when == "past":
        stmt = stmt.where(~_upcoming(f.now))
    if with_city and f.city:
        stmt = stmt.where(func.lower(StartupEvent.city) == f.city.lower())
    if f.q:
        pattern = like_pattern(f.q)
        stmt = stmt.where(or_(*(column.ilike(pattern, escape=LIKE_ESCAPE) for column in (
            StartupEvent.title, StartupEvent.organiser, StartupEvent.summary,
            StartupEvent.venue, StartupEvent.category))))
    return stmt


def ordered(stmt: Select, when: str) -> Select:
    if when == "upcoming":
        return stmt.order_by(StartupEvent.starts_at.asc(), StartupEvent.id)
    return stmt.order_by(StartupEvent.starts_at.desc(), StartupEvent.id.desc())


class EventOut(BaseModel):
    id: int
    title: str
    starts_at: datetime
    ends_at: datetime | None
    date_only: bool
    city: str | None
    venue: str | None
    online: bool | None
    organiser: str | None
    category: str | None
    price: str | None
    summary: str | None
    url: str
    source_url: str
    source_label: str
    source_type: str
    sources: list[SourceRef]
    first_seen_at: datetime

    @classmethod
    def of(cls, row: StartupEvent) -> "EventOut":
        return cls(
            id=row.id, title=row.title, starts_at=row.starts_at, ends_at=row.ends_at,
            date_only=row.date_only, city=row.city, venue=row.venue, online=row.online,
            organiser=row.organiser, category=row.category, price=row.price,
            summary=row.summary, url=row.url, source_url=row.source_url,
            source_label=row.source_label, source_type=row.source_type,
            sources=[SourceRef(**s) for s in (row.sources or []) if isinstance(s, dict)
                     and s.get("url")],
            first_seen_at=row.first_seen_at,
        )


class CityCount(BaseModel):
    name: str
    count: int


class EventsPage(BaseModel):
    rows: list[EventOut]
    total: int
    limit: int
    offset: int
    cities: list[CityCount]
    upcoming: int
    past: int


@router.get("/events", response_model=EventsPage)
def events(
    f: EventFilter = Depends(event_filter),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> EventsPage:
    stmt = query(f)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(ordered(stmt, f.when).limit(limit).offset(offset)).all()

    by_city = query(f, with_city=False).subquery()
    cities = [CityCount(name=name, count=count) for name, count in session.execute(
        select(by_city.c.city, func.count()).where(by_city.c.city.is_not(None))
        .group_by(by_city.c.city).order_by(func.count().desc(), by_city.c.city)).all()]

    def count(when: str) -> int:
        sub = query(EventFilter(when=when, city=f.city, start=f.start, end=f.end, q=f.q,
                                now=f.now)).subquery()
        return session.scalar(select(func.count()).select_from(sub)) or 0

    return EventsPage(rows=[EventOut.of(r) for r in rows], total=total, limit=limit,
                      offset=offset, cities=cities, upcoming=count("upcoming"),
                      past=count("past"))
