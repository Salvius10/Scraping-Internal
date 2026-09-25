"""Buckets: fixed views of the recent feed, each one a click away.

A bucket is a named set of categories over a recency window -- "Recently
funded" is Funding stories from the last 7 days. The definitions live here
and nowhere else; the page renders whatever this endpoint returns, so a new
bucket is one line.

Counts come from the database, which the 12-hour scheduler fills, so they
move as new stories land. `seen` lets the page ask how many stories arrived
in each bucket since the reader last opened it, for a "+N new" badge.
Reads only: no fetch, no model call, free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Article, Category, utcnow

router = APIRouter(prefix="/api", tags=["buckets"])


@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    note: str
    categories: tuple[Category, ...]
    days: int = 7


BUCKETS: tuple[Bucket, ...] = (
    Bucket("funded", "Recently funded", "Funding rounds from the last 7 days",
           (Category.FUNDING,)),
    Bucket("policy", "Policy & regulation", "Regulators, courts and new rules, last 7 days",
           (Category.POLICY,)),
    Bucket("market", "Market & analysis", "Results, valuations and trends, last 7 days",
           (Category.MARKET,)),
)


class BucketOut(BaseModel):
    key: str
    label: str
    note: str
    categories: list[str]
    days: int
    count: int
    new_count: int
    latest_ingested_at: datetime | None


class BucketsOut(BaseModel):
    buckets: list[BucketOut]


def _parse_seen(values: list[str]) -> dict[str, datetime]:
    """`seen=funded@2026-09-25T06:00:00Z` -> {"funded": datetime}. Bad input is ignored."""
    out: dict[str, datetime] = {}
    for value in values:
        key, _, stamp = value.partition("@")
        try:
            out[key] = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
    return out


@router.get("/buckets", response_model=BucketsOut)
def buckets(
    session: Session = Depends(get_session),
    seen: list[str] = Query(default_factory=list,
                            description="key@ISO time the reader last opened it"),
) -> BucketsOut:
    last_seen = _parse_seen(seen)
    out: list[BucketOut] = []

    for bucket in BUCKETS:
        since = utcnow() - timedelta(days=bucket.days)
        # Same rules as the feed: canonical stories only, undated ones kept.
        base = (
            select(func.count(Article.id))
            .where(Article.canonical_id.is_(None))
            .where(Article.category.in_(bucket.categories))
            .where((Article.published_at.is_(None)) | (Article.published_at >= since))
        )
        count = session.scalar(base) or 0

        new_count = 0
        if bucket.key in last_seen:
            new_count = session.scalar(
                base.where(Article.ingested_at > last_seen[bucket.key])) or 0

        latest = session.scalar(
            select(func.max(Article.ingested_at))
            .where(Article.canonical_id.is_(None))
            .where(Article.category.in_(bucket.categories))
        )

        out.append(BucketOut(
            key=bucket.key, label=bucket.label, note=bucket.note,
            categories=[c.value for c in bucket.categories], days=bucket.days,
            count=count, new_count=new_count, latest_ingested_at=latest,
        ))
    return BucketsOut(buckets=out)
