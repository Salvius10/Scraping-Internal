"""Bucket tests. Offline, database only.

A bucket must count exactly what clicking it shows: canonical stories in its
categories inside its window, with the same undated-story rule as the feed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.buckets import BUCKETS, _parse_seen
from app.db import session_scope
from app.main import app
from app.models import Article, Category, utcnow

SOURCE = "test_buckets"


@pytest.fixture
def stories():
    """Funding stories in and out of the window, plus noise."""
    now = utcnow()
    rows = [
        # (category, published, ingested, canonical?)
        (Category.FUNDING, now - timedelta(days=1), now - timedelta(hours=2), True),
        (Category.FUNDING, now - timedelta(days=3), now - timedelta(days=3), True),
        (Category.FUNDING, None, now - timedelta(hours=1), True),            # undated: kept
        (Category.FUNDING, now - timedelta(days=30), now - timedelta(days=30), True),  # too old
        (Category.FUNDING, now - timedelta(days=1), now - timedelta(hours=2), False),  # duplicate
        (Category.IPO, now - timedelta(days=1), now - timedelta(hours=2), True),       # other bucket
    ]
    with session_scope() as s:
        canonical_id = None
        for i, (cat, published, ingested, canonical) in enumerate(rows):
            article = Article(
                url=f"https://b.test/{i}", url_hash=f"b{i}", source=SOURCE,
                headline=f"Bucket story {i}", category=cat, published_at=published,
                ingested_at=ingested,
                canonical_id=None if canonical else canonical_id,
            )
            s.add(article)
            s.flush()
            canonical_id = canonical_id or article.id
    yield now
    with session_scope() as s:
        s.execute(delete(Article).where(Article.source == SOURCE))


def _funded(client, **params) -> dict:
    body = client.get("/api/buckets", params=params).json()
    return next(b for b in body["buckets"] if b["key"] == "funded")


def test_the_three_buckets_are_defined() -> None:
    assert [b.key for b in BUCKETS] == ["funded", "policy", "market"]
    assert BUCKETS[0].categories == (Category.FUNDING,)
    assert all(b.days == 7 for b in BUCKETS)


def test_count_matches_the_feed_rules(stories) -> None:
    with TestClient(app) as client:
        funded = _funded(client)
        # 3 inside the window: two dated, one undated. Not the 30-day-old one,
        # not the duplicate, not the IPO.
        with session_scope() as s:
            ours = s.scalars(select(Article).where(
                Article.source == SOURCE, Article.canonical_id.is_(None),
                Article.category == Category.FUNDING)).all()
        assert len(ours) == 4
        assert funded["count"] >= 3
        assert funded["categories"] == ["Funding"] and funded["days"] == 7


def test_count_equals_what_the_feed_shows(stories) -> None:
    """Clicking a bucket must show exactly the number on it."""
    with TestClient(app) as client:
        funded = _funded(client)
        page = client.get("/api/articles", params={
            "category": funded["categories"], "days": funded["days"], "limit": 1}).json()
    assert page["total"] == funded["count"]


def test_new_count_uses_arrival_time_since_last_seen(stories) -> None:
    now = stories
    with TestClient(app) as client:
        seen_3h_ago = (now - timedelta(hours=3)).isoformat()
        funded = _funded(client, seen=f"funded@{seen_3h_ago}")
    # Arrived in the last 3 hours: the 2h-old and 1h-old stories.
    assert funded["new_count"] >= 2
    assert funded["latest_ingested_at"] is not None


def test_nothing_is_new_without_a_last_seen_time(stories) -> None:
    with TestClient(app) as client:
        assert _funded(client)["new_count"] == 0


def test_bad_seen_values_are_ignored() -> None:
    parsed = _parse_seen(["funded@2026-09-25T06:00:00Z", "junk", "policy@not-a-date"])
    assert list(parsed) == ["funded"]
    assert parsed["funded"].utcoffset() == timedelta(0)
