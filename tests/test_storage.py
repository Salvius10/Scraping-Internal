"""Storage-layer tests: timezone-aware timestamps and citation chunks.

Offline -- no model calls, no cost.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import session_scope
from app.ingest.chunks import chunk_body, sync_all_chunks, sync_chunk
from app.main import app
from app.models import Article, Chunk, utcnow
from app.schemas import ArticleOut

IST = timezone(timedelta(hours=5, minutes=30))
SOURCE = "test_storage"


@pytest.fixture
def cleanup():
    yield
    with session_scope() as s:
        ids = s.scalars(select(Article.id).where(Article.source == SOURCE)).all()
        s.execute(delete(Chunk).where(Chunk.article_id.in_(ids)))
        s.execute(delete(Article).where(Article.id.in_(ids)))


def _article(s, url: str, published_at, description=None) -> Article:
    article = Article(
        url=url, url_hash=url, source=SOURCE, headline=f"Headline for {url}",
        description=description, published_at=published_at,
    )
    s.add(article)
    s.flush()
    return article


# --- Timestamps -------------------------------------------------------------

def test_timestamps_read_back_timezone_aware(cleanup) -> None:
    """SQLite drops offsets; the ORM must hand back aware UTC regardless."""
    published = datetime(2026, 9, 24, 9, 0, tzinfo=IST)
    with session_scope() as s:
        article_id = _article(s, "https://x.test/a1", published).id

    with session_scope() as s:
        stored = s.get(Article, article_id).published_at

    assert stored.tzinfo is not None
    assert stored == published                       # same instant
    assert stored.utcoffset() == timedelta(0)        # expressed in UTC


def test_api_serialises_an_offset(cleanup) -> None:
    """The bug: "2026-09-23T07:56:38" was read as local time by browsers."""
    with session_scope() as s:
        article = _article(s, "https://x.test/a2", datetime(2026, 9, 23, 7, 56, 38,
                                                            tzinfo=timezone.utc))
        payload = ArticleOut.model_validate(article).model_dump_json()
    assert '"published_at":"2026-09-23T07:56:38Z"' in payload \
        or '"published_at":"2026-09-23T07:56:38+00:00"' in payload


def test_activity_buckets_by_the_readers_day(cleanup) -> None:
    """20:00 UTC is already tomorrow in India."""
    evening = (utcnow() - timedelta(days=1)).replace(
        hour=20, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        _article(s, "https://x.test/a3", evening)

    with TestClient(app) as client:
        utc_days = {d["date"] for d in client.get("/api/activity").json()["days"]}
        ist_days = {d["date"] for d in
                    client.get("/api/activity?tz_offset=330").json()["days"]}

    assert evening.date().isoformat() in utc_days
    assert (evening.date() + timedelta(days=1)).isoformat() in ist_days


# --- Chunks -----------------------------------------------------------------

def test_chunk_body_layout() -> None:
    assert chunk_body("H", None) == "H"
    assert chunk_body("H", "D") == "H\n\nD"


def test_sync_rewrites_a_stale_chunk(cleanup) -> None:
    """describe.py improving a description must not leave citations quoting the old text."""
    with session_scope() as s:
        article = _article(s, "https://x.test/c1", None, description="old")
        s.add(Chunk(article_id=article.id, chunk_index=0,
                    text=chunk_body(article.headline, "old")))
        s.flush()
        article.description = "the publisher's much better description"
        assert sync_chunk(s, article) is True
        assert sync_chunk(s, article) is False       # idempotent
        chunk = s.scalars(select(Chunk).where(Chunk.article_id == article.id)).one()
        assert chunk.text.endswith("much better description")


def test_sync_all_creates_missing_chunks(cleanup) -> None:
    with session_scope() as s:
        article_id = _article(s, "https://x.test/c2", None, description="d").id
    with session_scope() as s:
        assert sync_all_chunks(s) >= 1
    with session_scope() as s:
        assert s.scalars(select(Chunk).where(Chunk.article_id == article_id)).one()
