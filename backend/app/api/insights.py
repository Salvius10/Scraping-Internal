"""Insights: startup funding rounds by stage, on screen and as Excel.

  GET /api/insights/rounds?stage=seed     rounds for one stage, newest first,
                                          plus the count for every stage
  GET /api/insights/rounds.xlsx?stage=... the same rows as an Excel file; with
                                          no stage, one sheet per stage

Both take optional `start` and `end` dates (YYYY-MM-DD, inclusive, Indian
days) on the publish time. With a range set, undated stories are left out --
they cannot be placed in it -- and the stage counts follow the range too.

  POST /api/insights/search-web {start, end}  Firecrawl web search for rounds
                                              in the range (ingest/web_rounds)

Rows are the feed's rounds plus any found by "Search web", merged by date.

Rows come from `funding_rounds`, which the scheduled refresh fills as new
Funding stories arrive (`ingest/rounds.py`). Reads only: no fetch, no model.
Only canonical stories still classed as Funding are shown, so a story later
merged as a duplicate or reclassified drops out on its own.
"""

from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..ingest.rounds import tidy_round
from ..ingest.web_rounds import WebSearchError, search_web
from ..ingest.sources import load_sources
from ..models import Article, Category, FundingRound, Stage, WebRound, utcnow

router = APIRouter(prefix="/api/insights", tags=["insights"])

IST = timezone(timedelta(hours=5, minutes=30))

# URL-friendly keys for the tabs, in display order.
STAGE_KEYS: dict[str, Stage] = {
    "pre-seed": Stage.PRE_SEED,
    "seed": Stage.SEED,
    "series-a": Stage.SERIES_A,
    "series-b": Stage.SERIES_B,
    "other": Stage.OTHER,
}
STAGE_TITLES = {
    Stage.PRE_SEED: "Pre-Seed", Stage.SEED: "Seed", Stage.SERIES_A: "Series A",
    Stage.SERIES_B: "Series B", Stage.OTHER: "Other rounds",
}


class RoundOut(BaseModel):
    id: str                      # "feed-12" or "web-3": unique across both
    company: str | None
    stage: str
    round: str | None
    amount: str | None
    investors: list[str]
    published_at: datetime | None
    source: str
    source_label: str
    headline: str
    url: str
    origin: str = "feed"         # "feed" (our news sites) or "web" (Search web)
    date_approx: bool = False    # time came from "3 days ago", not a stamp


class StageCount(BaseModel):
    key: str
    label: str
    count: int


class RoundsOut(BaseModel):
    stage: str
    stages: list[StageCount]
    rounds: list[RoundOut]
    pending: int                 # Funding stories not yet read for Insights


def _base():
    """Rounds whose story is still a canonical Funding story."""
    return (
        select(FundingRound, Article)
        .join(Article, Article.id == FundingRound.article_id)
        .where(Article.canonical_id.is_(None), Article.category == Category.FUNDING)
    )


class Window:
    """A publish-date range in Indian days, as UTC bounds: [since, until)."""

    def __init__(self, start: date | None = None, end: date | None = None) -> None:
        if start and end and start > end:
            raise HTTPException(status_code=400, detail="start is after end")
        self.start, self.end = start, end
        self.since = (datetime.combine(start, time.min, IST).astimezone(timezone.utc)
                      if start else None)
        self.until = (datetime.combine(end + timedelta(days=1), time.min, IST)
                      .astimezone(timezone.utc) if end else None)

    def apply(self, stmt, column=None):
        column = column if column is not None else Article.published_at
        if self.since:
            stmt = stmt.where(column >= self.since)
        if self.until:
            stmt = stmt.where(column < self.until)
        return stmt

    def label(self) -> str:
        if not (self.start or self.end):
            return "all-dates"
        return f"{self.start or 'start'}-to-{self.end or 'today'}"


def _stage(key: str | None) -> Stage | None:
    if key is None:
        return None
    try:
        return STAGE_KEYS[key]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown stage {key!r}") from None


def _rows(session: Session, stage: Stage, window: Window | None = None) -> list[RoundOut]:
    labels = {s.name: s.label for s in load_sources()}
    stmt = (window or Window()).apply(_base().where(FundingRound.stage == stage))
    stmt = stmt.order_by(Article.published_at.desc().nullslast(), Article.ingested_at.desc())
    out = []
    for fr, article in session.execute(stmt).all():
        out.append(RoundOut(
            id=f"feed-{fr.id}",
            company=fr.company,
            stage=STAGE_TITLES[fr.stage],
            round=tidy_round(fr.round_label),
            amount=fr.amount,
            investors=[n for n in (fr.investors or "").split("; ") if n],
            published_at=article.published_at,
            source=article.source,
            source_label=labels.get(article.source, article.source),
            headline=article.headline,
            url=article.url,
        ))

    web = (window or Window()).apply(
        select(WebRound).where(WebRound.stage == stage, WebRound.kept.is_(True)),
        WebRound.published_at)
    for wr in session.scalars(web).all():
        out.append(RoundOut(
            id=f"web-{wr.id}", company=wr.company, stage=STAGE_TITLES[wr.stage],
            round=wr.round_label, amount=wr.amount,
            investors=[n for n in (wr.investors or "").split("; ") if n],
            published_at=wr.published_at, source="web", source_label=wr.domain,
            headline=wr.headline, url=wr.url, origin="web", date_approx=wr.date_approx,
        ))

    # One list, newest first; undated rows last.
    out.sort(key=lambda r: (r.published_at is not None,
                            r.published_at.timestamp() if r.published_at else 0),
             reverse=True)
    return out


def _counts(session: Session, window: Window | None = None) -> list[StageCount]:
    stmt = (
        select(FundingRound.stage, func.count(FundingRound.id))
        .join(Article, Article.id == FundingRound.article_id)
        .where(Article.canonical_id.is_(None), Article.category == Category.FUNDING)
    )
    rows = dict(session.execute(
        (window or Window()).apply(stmt).group_by(FundingRound.stage)
    ).all())
    web = dict(session.execute(
        (window or Window()).apply(
            select(WebRound.stage, func.count(WebRound.id))
            .where(WebRound.kept.is_(True)), WebRound.published_at)
        .group_by(WebRound.stage)
    ).all())
    return [StageCount(key=key, label=STAGE_TITLES[stage],
                       count=rows.get(stage, 0) + web.get(stage, 0))
            for key, stage in STAGE_KEYS.items()]


def _pending(session: Session) -> int:
    return session.scalar(
        select(func.count(Article.id))
        .where(Article.canonical_id.is_(None), Article.category == Category.FUNDING,
               Article.id.not_in(select(FundingRound.article_id)))
    ) or 0


@router.get("/rounds", response_model=RoundsOut)
def rounds(
    stage: str = Query("pre-seed", description="pre-seed | seed | series-a | series-b | other"),
    start: date | None = Query(None, description="published on or after (YYYY-MM-DD, IST)"),
    end: date | None = Query(None, description="published on or before (YYYY-MM-DD, IST)"),
    session: Session = Depends(get_session),
) -> RoundsOut:
    chosen = _stage(stage)
    window = Window(start, end)
    return RoundsOut(
        stage=stage, stages=_counts(session, window),
        rounds=_rows(session, chosen, window), pending=_pending(session),
    )


# --- Excel ------------------------------------------------------------------------

COLUMNS = [
    ("Company", 28), ("Stage", 12), ("Round", 16), ("Investors", 44),
    ("Amount", 18), ("Published (IST)", 19), ("Source", 18), ("Found via", 12),
    ("Headline", 60), ("Link", 50),
]


def _sheet(workbook, title: str, rows: list[RoundOut]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    ws = workbook.create_sheet(title=title[:31])
    ws.append([name for name, _ in COLUMNS])
    header_fill = PatternFill("solid", fgColor="1A00D9")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    for r in rows:
        published = (r.published_at.astimezone(IST).replace(tzinfo=None)
                     if r.published_at else None)
        ws.append([
            r.company or "", r.stage, r.round or "", ", ".join(r.investors),
            r.amount or "Undisclosed", published, r.source_label,
            "Web search" if r.origin == "web" else "News feed", r.headline, r.url,
        ])
        row = ws.max_row
        ws.cell(row=row, column=6).number_format = (
            "dd mmm yyyy" if r.date_approx else "dd mmm yyyy hh:mm")
        link = ws.cell(row=row, column=10)
        link.hyperlink = r.url
        link.font = Font(color="1A00D9", underline="single")

    for index, (_, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=index).column_letter].width = width
    ws.auto_filter.ref = ws.dimensions


@router.get("/rounds.xlsx")
def rounds_excel(
    stage: str | None = Query(None, description="one stage, or omit for all stages"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    from openpyxl import Workbook

    chosen = _stage(stage)
    window = Window(start, end)
    workbook = Workbook()
    workbook.remove(workbook.active)
    stages = [chosen] if chosen else list(STAGE_KEYS.values())
    for s in stages:
        _sheet(workbook, STAGE_TITLES[s], _rows(session, s, window))

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    day = utcnow().astimezone(IST).strftime("%Y-%m-%d")
    name = f"gps-funding-{stage or 'all-stages'}-{window.label()}-{day}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- Search web -------------------------------------------------------------------

class SearchWebRequest(BaseModel):
    start: date
    end: date | None = None


class SearchWebResponse(BaseModel):
    start: date | None = None
    end: date | None = None
    results_seen: int = 0
    added: dict[str, int] = {}
    total_added: int = 0
    duplicates: int = 0
    outside_range: int = 0
    not_startup_rounds: int = 0
    credits_used: int = 0
    cost_usd: float = 0.0
    failed_queries: list[str] = []
    error: str | None = None


@router.post("/search-web", response_model=SearchWebResponse)
def search_web_endpoint(request: SearchWebRequest) -> SearchWebResponse:
    """Search the web for startup rounds in the range and add the new ones."""
    try:
        summary = search_web(request.start, request.end)
    except WebSearchError as exc:
        return SearchWebResponse(start=request.start, end=request.end, error=str(exc))

    return SearchWebResponse(
        start=summary.start, end=summary.end, results_seen=summary.results_seen,
        added=summary.added, total_added=summary.total_added,
        duplicates=summary.duplicates, outside_range=summary.outside_range,
        not_startup_rounds=summary.not_startup_rounds,
        credits_used=summary.credits_used, cost_usd=round(summary.cost_usd, 6),
        failed_queries=summary.failed_queries, error=summary.stopped_reason,
    )
