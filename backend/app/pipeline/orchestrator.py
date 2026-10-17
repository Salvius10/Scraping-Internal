"""Ingestion Orchestrator -- fans out fetch jobs to all sources.

Every source in the registry gets one job; jobs run in parallel
(`fetch_workers` at a time), each isolated: a source that fails, times out or
returns nonsense is recorded on its own SourceRun and never stops the others.
Fetching only reads -- nothing is written until every job is back, so the
stages after this one see one consistent batch.

A source is skipped, with the reason recorded and nothing paid, when:
  - it was read successfully within its `every_hours` (unless forced)
  - it is paid and the run is free-only
  - the service it needs (Apify, Firecrawl) has no credentials
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..config import settings
from ..db import session_scope
from ..models import SourceRun, utcnow
from ..search import firecrawl
from .items import RawItem
from .sources import FetchContext, FetchResult, Source, SourceError

log = logging.getLogger(__name__)

@dataclass
class Fetched:
    source: Source
    run: SourceRun
    items: list[RawItem] = field(default_factory=list)


def last_good_read(source_key: str) -> datetime | None:
    with session_scope() as s:
        return s.scalar(select(func.max(SourceRun.started_at)).where(
            SourceRun.source_key == source_key, SourceRun.error.is_(None),
            SourceRun.skipped.is_(None)))


def cursor(source_key: str) -> datetime | None:
    """The newest publish time from a read of this source that was fully dealt with."""
    with session_scope() as s:
        return s.scalar(select(func.max(SourceRun.cursor)).where(
            SourceRun.source_key == source_key, SourceRun.error.is_(None)))


def skip_reason(source: Source, now: datetime, *, force: bool, free_only: bool) -> str | None:
    if free_only and source.paid:
        return "paid source; this run reads free sources only"
    if source.needs == "apify" and not settings.apify_api_token:
        return "APIFY_API_TOKEN is not set"
    if source.needs == "firecrawl" and not firecrawl.ready():
        return "FIRECRAWL_API_KEY is not set"
    if not force and source.every_hours > 0:
        last = last_good_read(source.key)
        if last is not None and now - last < timedelta(hours=source.every_hours):
            return f"read {int((now - last).total_seconds() // 3600)}h ago; every {source.every_hours:g}h"
    return None


def _fetch_one(source: Source, ctx: FetchContext) -> tuple[FetchResult | Exception, datetime]:
    try:
        result = source.fetch(ctx)
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
        if not isinstance(exc, SourceError):
            log.exception("%s: fetch failed", source.key)
        return exc, utcnow()
    return result, utcnow()


def fetch_all(sources: list[Source], run_id: int, *, force: bool = False,
              free_only: bool = False, now: datetime | None = None) -> list[Fetched]:
    """Fetch every due source in parallel. One SourceRun per source, read or not."""
    now = now or utcnow()
    ctx = FetchContext(cursor=cursor, now=now)
    fetched: list[Fetched] = []
    due: list[Fetched] = []
    for source in sources:
        run = SourceRun(run_id=run_id, source_key=source.key, source_type=source.type,
                        started_at=now, items=0, apify_usd=0.0, firecrawl_credits=0)
        job = Fetched(source=source, run=run)
        reason = skip_reason(source, now, force=force, free_only=free_only)
        if reason:
            run.skipped, run.finished_at = reason, now
        else:
            due.append(job)
        fetched.append(job)

    if due:
        with ThreadPoolExecutor(max_workers=max(1, settings.fetch_workers)) as pool:
            outcomes = list(pool.map(lambda f: _fetch_one(f.source, ctx), due))
        for job, (outcome, finished) in zip(due, outcomes):
            job.run.finished_at = finished
            if isinstance(outcome, Exception):
                job.run.error = (str(outcome) if isinstance(outcome, SourceError)
                                 else f"{type(outcome).__name__}: {outcome}")[:500]
                job.run.apify_usd = getattr(outcome, "apify_usd", 0.0)
                job.run.firecrawl_credits = getattr(outcome, "credits", 0)
                continue
            job.items = outcome.items
            job.run.items = len(outcome.items)
            job.run.apify_usd = outcome.apify_usd
            job.run.firecrawl_credits = outcome.credits
            job.run.warning = outcome.warning
            dated = [i.published_at for i in outcome.items if i.published_at]
            job.run.cursor = max(dated) if dated else None

    for job in fetched:
        if job.run.error:
            log.warning("%s: %s", job.source.key, job.run.error)
    log.info("orchestrator: %d sources, %d read, %d skipped, %d failed, %d items",
             len(fetched), len(due), len(fetched) - len(due),
             sum(1 for f in due if f.run.error), sum(len(f.items) for f in fetched))
    return fetched


def record(fetched: list[Fetched]) -> None:
    """Write the SourceRuns once the guardrail has had its say on their items."""
    with session_scope() as s:
        s.add_all([f.run for f in fetched])
