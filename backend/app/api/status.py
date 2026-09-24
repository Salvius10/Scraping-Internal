"""Feed freshness and budget status.

With a 12-hour refresh, a user must be able to see how old the feed is --
otherwise stale news is indistinguishable from no news.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import scheduler
from ..config import settings
from ..db import get_session
from ..ingest.sources import load_sources
from ..llm.budget import get_spend
from ..models import Article, IngestRun, utcnow
from ..schemas import SourceHealth, SpendOut, StatusOut

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status", response_model=StatusOut)
def status(session: Session = Depends(get_session)) -> StatusOut:
    last_refresh = session.scalar(select(func.max(IngestRun.started_at)))
    hours_since = None
    if last_refresh is not None:
        hours_since = round(
            (utcnow() - last_refresh).total_seconds() / 3600.0, 1
        )

    sources: list[SourceHealth] = []
    for source in load_sources():
        run = session.scalars(
            select(IngestRun)
            .where(IngestRun.source == source.name)
            .order_by(IngestRun.started_at.desc())
            .limit(1)
        ).first()
        sources.append(SourceHealth(
            name=source.name,
            label=source.label,
            strategy=source.strategy,
            last_run=run.started_at if run else None,
            items_seen=run.items_seen if run else 0,
            items_new=run.items_new if run else 0,
            error=run.error if run else None,
            window_overflowed=bool(run and run.window_overflowed),
        ))

    spend = get_spend(session)

    return StatusOut(
        last_refresh=last_refresh,
        hours_since_refresh=hours_since,
        refresh_interval_hours=settings.refresh_hours,
        next_refresh=scheduler.next_run_time(),
        article_count=session.scalar(
            select(func.count(Article.id)).where(Article.canonical_id.is_(None))
        ) or 0,
        newest_published=session.scalar(select(func.max(Article.published_at))),
        recency_window_days=settings.recency_window_days,
        sources=sources,
        spend=SpendOut(
            total_usd=round(spend.total_usd, 6),
            total_cap=spend.total_cap,
            remaining_total=round(spend.remaining_total, 6),
            last_24h_usd=round(spend.last_24h_usd, 6),
            daily_cap=spend.daily_cap,
            calls=spend.calls,
            exhausted=spend.exhausted,
        ),
    )
