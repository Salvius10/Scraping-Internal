"""The news feed endpoint.

Reads the database only -- ingest runs on its own schedule. Nothing here ever
fetches a page or calls a model, so browsing the feed is free and instant no
matter how much of the budget is gone.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session
from ..models import Article, Category, utcnow
from ..schemas import ArticleOut, FeedPage

router = APIRouter(prefix="/api", tags=["feed"])


def _duplicate_sources(session: Session, ids: list[int]) -> dict[int, list[str]]:
    """Which other outlets covered each canonical story."""
    if not ids:
        return {}
    rows = session.execute(
        select(Article.canonical_id, Article.source)
        .where(Article.canonical_id.in_(ids))
    ).all()
    out: dict[int, list[str]] = {}
    for canonical_id, source in rows:
        bucket = out.setdefault(canonical_id, [])
        if source not in bucket:
            bucket.append(source)
    return out


@router.get("/articles", response_model=FeedPage)
def list_articles(
    session: Session = Depends(get_session),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    days: int | None = Query(None, ge=1, le=365,
                             description="recency window; defaults to config"),
    category: list[Category] | None = Query(None),
    source: list[str] | None = Query(None),
    company: str | None = Query(None),
    q: str | None = Query(None, description="free-text search over the corpus"),
) -> FeedPage:
    """Newest first, duplicates folded away."""
    window_days = days or settings.recency_window_days
    since = utcnow() - timedelta(days=window_days)

    stmt = select(Article).where(Article.canonical_id.is_(None))

    # Articles with no date are kept rather than hidden: a scraped listing may
    # not carry one, and dropping them would silently lose a source.
    stmt = stmt.where(
        or_(Article.published_at.is_(None), Article.published_at >= since)
    )

    if category:
        stmt = stmt.where(Article.category.in_(category))
    if source:
        stmt = stmt.where(Article.source.in_(source))
    if company:
        stmt = stmt.where(Article.company.ilike(f"%{company}%"))
    if q:
        term = f"%{q}%"
        stmt = stmt.where(or_(
            Article.headline.ilike(term),
            Article.description.ilike(term),
            Article.company.ilike(term),
        ))

    total = session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0

    rows = session.scalars(
        stmt.order_by(Article.published_at.desc().nullslast(),
                      Article.ingested_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    dupes = _duplicate_sources(session, [a.id for a in rows])
    articles = []
    for row in rows:
        item = ArticleOut.model_validate(row)
        item.also_reported_by = dupes.get(row.id, [])
        articles.append(item)

    return FeedPage(
        articles=articles,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(articles) < total,
    )


@router.get("/articles/{article_id}", response_model=ArticleOut)
def get_article(
    article_id: int, session: Session = Depends(get_session)
) -> ArticleOut:
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    item = ArticleOut.model_validate(article)
    item.also_reported_by = _duplicate_sources(
        session, [article.id]
    ).get(article.id, [])
    return item


@router.get("/activity")
def activity(
    session: Session = Depends(get_session),
    days: int | None = Query(None, ge=1, le=365),
    tz_offset: int = Query(0, ge=-720, le=840,
                           description="reader's UTC offset in minutes"),
) -> dict:
    """Stories per day across the window, for the masthead activity bar.

    Days are the *reader's* days. Timestamps are stored in UTC, so without the
    offset a story published at 02:00 IST was counted on the previous day's
    bar while the tape below grouped it under the right one.
    """
    window_days = days or settings.recency_window_days
    since = utcnow() - timedelta(days=window_days)

    rows = session.execute(
        select(
            func.date(Article.published_at, f"{tz_offset:+d} minutes").label("day"),
            func.count(Article.id),
        )
        .where(
            Article.canonical_id.is_(None),
            Article.published_at.is_not(None),
            Article.published_at >= since,
        )
        .group_by("day")
        .order_by("day")
    ).all()
    return {"days": [{"date": d, "count": n} for d, n in rows if d]}


@router.get("/facets")
def facets(session: Session = Depends(get_session)) -> dict:
    """Counts per category and per source, for populating filter controls."""
    canonical = Article.canonical_id.is_(None)

    categories = [
        {"value": c.value if c else None, "count": n}
        for c, n in session.execute(
            select(Article.category, func.count(Article.id))
            .where(canonical)
            .group_by(Article.category)
            .order_by(func.count(Article.id).desc())
        ).all()
    ]
    sources = [
        {"value": s, "count": n}
        for s, n in session.execute(
            select(Article.source, func.count(Article.id))
            .where(canonical)
            .group_by(Article.source)
            .order_by(func.count(Article.id).desc())
        ).all()
    ]
    return {"categories": categories, "sources": sources}
