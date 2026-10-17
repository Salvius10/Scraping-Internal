"""Platform: the funded startups feed.

  GET /api/funding?stage=&source=&start=&end=&q=&limit=&offset=
        newest first (undated last), with a count per stage for the same filters

Reads only: no fetch, no model, so it costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from ..db import LIKE_ESCAPE, get_session, like_pattern
from ..models import FundedStartup, Stage
from .common import MAX_LIMIT, search_text, window

router = APIRouter(prefix="/api", tags=["funding"])

STAGE_KEYS = {"pre-seed": Stage.PRE_SEED, "seed": Stage.SEED, "series-a": Stage.SERIES_A,
              "series-b": Stage.SERIES_B, "other": Stage.OTHER}
SOURCE_TYPES = {"rss": "News feed", "website": "Your websites", "web_search": "Web search",
                "linkedin": "LinkedIn"}


@dataclass
class FundingFilter:
    stage: str | None = None
    source: str | None = None
    start: date | None = None
    end: date | None = None
    q: str | None = None

    def check(self) -> None:
        if self.stage is not None and self.stage not in STAGE_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown stage {self.stage!r}.")
        if self.source is not None and self.source not in SOURCE_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown source {self.source!r}.")


def funding_filter(
    stage: str | None = Query(None, description="pre-seed | seed | series-a | series-b | other"),
    source: str | None = Query(None, description="rss | website | web_search | linkedin"),
    start: date | None = Query(None, description="published on or after (IST day)"),
    end: date | None = Query(None, description="published on or before (IST day)"),
    q: str | None = Query(None, max_length=200),
) -> FundingFilter:
    f = FundingFilter(stage=stage, source=source, start=start, end=end, q=search_text(q))
    f.check()
    return f


def query(f: FundingFilter, *, with_stage: bool = True) -> Select:
    stmt = select(FundedStartup)
    since, until = window(f.start, f.end)
    if since is not None:
        stmt = stmt.where(FundedStartup.published_at >= since)
    if until is not None:
        stmt = stmt.where(FundedStartup.published_at < until)
    if f.source:
        stmt = stmt.where(FundedStartup.source_type == f.source)
    if f.q:
        pattern = like_pattern(f.q)
        stmt = stmt.where(or_(*(column.ilike(pattern, escape=LIKE_ESCAPE) for column in (
            FundedStartup.company, FundedStartup.sector, FundedStartup.summary,
            FundedStartup.headline, FundedStartup.hq_city))))
    if with_stage and f.stage:
        stmt = stmt.where(FundedStartup.stage == STAGE_KEYS[f.stage])
    return stmt


def ordered(stmt: Select) -> Select:
    return stmt.order_by(FundedStartup.published_at.desc().nullslast(), FundedStartup.id.desc())


class SourceRef(BaseModel):
    url: str
    source: str
    type: str
    published_at: datetime | None = None


class FundingOut(BaseModel):
    id: int
    company: str
    stage: str
    round: str | None
    amount: str | None
    investors: list[str]
    sector: str | None
    hq_city: str | None
    summary: str | None
    headline: str
    source_url: str
    source_label: str
    source_type: str
    published_at: datetime | None
    sources: list[SourceRef]
    first_seen_at: datetime

    @classmethod
    def of(cls, row: FundedStartup) -> "FundingOut":
        return cls(
            id=row.id, company=row.company, stage=row.stage.value, round=row.round_label,
            amount=row.amount, investors=list(row.investors or []), sector=row.sector,
            hq_city=row.hq_city, summary=row.summary, headline=row.headline,
            source_url=row.source_url, source_label=row.source_label,
            source_type=row.source_type, published_at=row.published_at,
            sources=[SourceRef(**s) for s in (row.sources or []) if isinstance(s, dict)
                     and s.get("url")],
            first_seen_at=row.first_seen_at,
        )


class StageCount(BaseModel):
    key: str
    label: str
    count: int


class FundingPage(BaseModel):
    rows: list[FundingOut]
    total: int
    limit: int
    offset: int
    stages: list[StageCount]


@router.get("/funding", response_model=FundingPage)
def funding(
    f: FundingFilter = Depends(funding_filter),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> FundingPage:
    stmt = query(f)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(ordered(stmt).limit(limit).offset(offset)).all()

    base = query(f, with_stage=False).subquery()
    counts = dict(session.execute(
        select(base.c.stage, func.count()).group_by(base.c.stage)).all())
    stages = [StageCount(key=key, label=stage.value, count=counts.get(stage, 0))
              for key, stage in STAGE_KEYS.items()]
    return FundingPage(rows=[FundingOut.of(r) for r in rows], total=total, limit=limit,
                       offset=offset, stages=stages)
