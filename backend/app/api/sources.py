"""Platform: the Sources panel -- what is read, how it is doing, and adding more.

  GET    /api/sources?section=funding|events|all   every source with its health
  POST   /api/sources {kind, value, label?}        add a LinkedIn account, a website
                                                   or a search phrase (funding)
  DELETE /api/sources/{id}                         remove one that was added here

Built-in sources (sources.yaml) are listed but cannot be removed here. An
added source is read on the next run -- "Refresh now" starts one. A refused
add is an ordinary answer (`added: false` with the reason), not an error.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import SourceRun, UserSource
from ..pipeline import user_sources
from ..pipeline.registry import RegistryError, all_sources
from ..pipeline.sources import Source
from ..pipeline.sources.eventbrite import EventbriteSource
from ..pipeline.sources.linkedin import LinkedinSource
from ..pipeline.sources.luma import LumaSource
from ..pipeline.sources.pages import PageSource
from ..pipeline.sources.rss import RssSource
from ..pipeline.sources.web_search import WebSearchSource
from ..search import firecrawl

router = APIRouter(prefix="/api", tags=["sources"])

FUNDING_TYPES = ("rss", "website", "linkedin", "web_search")
SECTIONS = ("funding", "events", "all")


class SourceHealth(BaseModel):
    key: str
    label: str
    type: str
    detail: str                        # the link, phrase or cities it reads
    via: str | None                    # websites: "rss" (free) or "scrape" (Firecrawl)
    id: int | None                     # set for a source added here: removable
    every_hours: float
    paid: bool
    last_read: datetime | None         # last successful read
    last_items: int
    last_error: str | None             # the latest attempt, when it failed
    last_skipped: str | None
    warning: str | None


def _detail(source: Source) -> str:
    if isinstance(source, (RssSource, LinkedinSource, PageSource)):
        return source.url
    if isinstance(source, WebSearchSource):
        return "; ".join(source.queries)
    if isinstance(source, LumaSource):
        return ", ".join(source.cities) + (f" (keyword: {source.keyword})" if source.keyword else "")
    if isinstance(source, EventbriteSource):
        return f"{len(source.locations)} cities" + (
            f" (keyword: {source.keyword})" if source.keyword else "")
    return ""


def health(session: Session, source: Source) -> SourceHealth:
    latest = session.scalars(select(SourceRun).where(SourceRun.source_key == source.key)
                             .order_by(SourceRun.started_at.desc(), SourceRun.id.desc())
                             .limit(1)).first()
    good = session.scalars(select(SourceRun).where(
        SourceRun.source_key == source.key, SourceRun.error.is_(None),
        SourceRun.skipped.is_(None)).order_by(SourceRun.started_at.desc()).limit(1)).first()
    via = None
    if source.type == "website":
        via = "scrape" if isinstance(source, PageSource) else "rss"
    return SourceHealth(
        key=source.key, label=source.label, type=source.type, detail=_detail(source), via=via,
        id=source.user_id, every_hours=source.every_hours, paid=source.paid,
        last_read=good.started_at if good else None, last_items=good.items if good else 0,
        last_error=latest.error if latest else None,
        last_skipped=latest.skipped if latest else None,
        warning=good.warning if good else None,
    )


def all_health(session: Session, section: str = "all") -> tuple[list[SourceHealth], str | None]:
    try:
        sources = all_sources()
    except RegistryError as exc:
        return [], str(exc)
    if section == "funding":
        sources = [s for s in sources if s.type in FUNDING_TYPES]
    elif section == "events":
        sources = [s for s in sources if s.type not in FUNDING_TYPES]
    return [health(session, s) for s in sources], None


class SourcesOut(BaseModel):
    sources: list[SourceHealth]
    added: int                     # sources added here, all sections
    max_added: int
    apify_ready: bool
    firecrawl_ready: bool
    registry_error: str | None


@router.get("/sources", response_model=SourcesOut)
def list_sources(section: str = Query("funding"),
                 session: Session = Depends(get_session)) -> SourcesOut:
    if section not in SECTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown section {section!r}.")
    rows, error = all_health(session, section)
    added = len(session.scalars(select(UserSource.id)).all())
    return SourcesOut(sources=rows, added=added, max_added=settings.max_user_sources,
                      apify_ready=bool(settings.apify_api_token),
                      firecrawl_ready=firecrawl.ready(), registry_error=error)


class AddIn(BaseModel):
    kind: str = Field(max_length=20)                 # linkedin | website | search
    value: str = Field(max_length=2000)
    label: str | None = Field(default=None, max_length=200)


class AddOut(BaseModel):
    added: bool
    message: str
    source: SourceHealth | None = None


_COST = {
    "linkedin": "Read through Apify once a day, about $0.002 a post",
    "search": "Searched through Firecrawl once a day, about 2-4 credits",
    "rss": "It has a feed, so it is read free on every run",
    "scrape": "It has no feed, so Firecrawl reads the page once a day, 1 credit",
}


@router.post("/sources", response_model=AddOut)
def add_source(request: AddIn, session: Session = Depends(get_session)) -> AddOut:
    try:
        row = user_sources.add(request.kind, request.value, request.label)
    except user_sources.SourceInputError as exc:
        return AddOut(added=False, message=str(exc))
    source = user_sources.to_source(row)
    service = source.needs
    ready = not (service == "apify" and not settings.apify_api_token
                 or service == "firecrawl" and not firecrawl.ready())
    cost = _COST[row.via or row.kind]
    message = (f"Added {row.label}. {cost}. It is read on the next run; "
               f"Refresh now reads it straight away.")
    if not ready:
        key = "APIFY_API_TOKEN" if service == "apify" else "FIRECRAWL_API_KEY"
        message = (f"Added {row.label}, but it cannot be read until {key} is set on the "
                   f"server. {cost}.")
    return AddOut(added=True, message=message, source=health(session, source))


@router.delete("/sources/{source_id}")
def remove_source(source_id: int) -> dict:
    if not user_sources.remove(source_id):
        raise HTTPException(status_code=404, detail="No added source with that id.")
    return {"removed": True}
