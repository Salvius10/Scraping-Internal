"""Platform: how fresh the data is, how each source is doing, and the budget.

  GET /api/status

With a 12-hour refresh a reader must be able to see how old the data is,
otherwise stale is indistinguishable from quiet. A database read: free.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import scheduler
from ..config import settings
from ..db import get_session
from ..llm.budget import get_spend
from ..models import (
    BlockedItem, FundedStartup, ItemStatus, PipelineRun, SeenItem, SourceRun, StartupEvent,
    utcnow,
)
from ..pipeline import guardrails
from .events import _upcoming
from .sources import SourceHealth, all_health

router = APIRouter(prefix="/api", tags=["status"])


class LastRun(BaseModel):
    id: int
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    fetched: int
    admitted: int
    funding_new: int
    events_new: int
    blocked: int
    pending_left: int
    llm_usd: float
    stopped_reason: str | None
    error: str | None


class Spend(BaseModel):
    total_usd: float
    total_cap: float
    remaining_total: float
    last_24h_usd: float
    daily_cap: float
    exhausted: bool


class StatusOut(BaseModel):
    last_refresh: datetime | None      # when the last run finished
    hours_since_refresh: float | None
    refresh_interval_hours: int
    next_refresh: datetime | None      # None when the scheduler runs elsewhere
    last_run: LastRun | None
    guardrails: str
    guardrails_on: bool
    funding_count: int
    upcoming_events: int
    pending: int
    blocked: int
    registry_error: str | None
    sources: list[SourceHealth]
    spend: Spend


@router.get("/status", response_model=StatusOut)
def status(session: Session = Depends(get_session)) -> StatusOut:
    now = utcnow()
    run = session.scalars(select(PipelineRun).order_by(PipelineRun.started_at.desc(),
                                                       PipelineRun.id.desc()).limit(1)).first()
    finished = session.scalar(select(func.max(PipelineRun.finished_at)))
    sources, registry_error = all_health(session)
    spend = get_spend(session)
    return StatusOut(
        last_refresh=finished,
        hours_since_refresh=round((now - finished).total_seconds() / 3600, 1) if finished else None,
        refresh_interval_hours=settings.refresh_hours,
        next_refresh=scheduler.next_run_time(),
        last_run=LastRun(
            id=run.id, trigger=run.trigger, started_at=run.started_at,
            finished_at=run.finished_at, fetched=run.fetched, admitted=run.admitted,
            funding_new=run.funding_new, events_new=run.events_new,
            blocked=run.blocked_raw + run.blocked_input + run.blocked_output,
            pending_left=run.pending_left, llm_usd=run.llm_usd,
            stopped_reason=run.stopped_reason, error=run.error,
        ) if run else None,
        guardrails=guardrails.state(),
        guardrails_on=guardrails.enabled(),
        funding_count=session.scalar(select(func.count(FundedStartup.id))) or 0,
        upcoming_events=session.scalar(select(func.count(StartupEvent.id))
                                       .where(_upcoming(now))) or 0,
        pending=session.scalar(select(func.count(SeenItem.id)).where(
            SeenItem.status == ItemStatus.PENDING)) or 0,
        blocked=session.scalar(select(func.count(BlockedItem.id))) or 0,
        registry_error=registry_error,
        sources=sources,
        spend=Spend(
            total_usd=round(spend.total_usd, 6), total_cap=spend.total_cap,
            remaining_total=round(spend.remaining_total, 6),
            last_24h_usd=round(spend.last_24h_usd, 6), daily_cap=spend.daily_cap,
            exhausted=spend.exhausted,
        ),
    )
