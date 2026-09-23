"""API response shapes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .models import Category


class ArticleOut(BaseModel):
    """The seven fields the feed must show, plus dedupe provenance."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    headline: str
    description: str | None
    source: str
    published_at: datetime | None
    company: str | None
    category: Category | None
    url: str

    # How many other outlets ran the same story. Surfacing this turns dedupe
    # from invisible plumbing into a signal about how big a story is.
    also_reported_by: list[str] = []


class FeedPage(BaseModel):
    articles: list[ArticleOut]
    total: int
    limit: int
    offset: int
    has_more: bool


class SourceHealth(BaseModel):
    name: str
    label: str
    strategy: str
    last_run: datetime | None
    items_seen: int
    items_new: int
    error: str | None
    window_overflowed: bool


class SpendOut(BaseModel):
    total_usd: float
    total_cap: float
    remaining_total: float
    last_24h_usd: float
    daily_cap: float
    calls: int
    exhausted: bool


class StatusOut(BaseModel):
    """Everything the header needs to tell the user how fresh the feed is."""

    last_refresh: datetime | None
    hours_since_refresh: float | None
    refresh_interval_hours: int
    article_count: int
    newest_published: datetime | None
    recency_window_days: int
    sources: list[SourceHealth]
    spend: SpendOut


class FilterSpec(BaseModel):
    """A compiled feed filter. Phase 7 produces these from natural language."""

    categories: list[Category] = []
    sources: list[str] = []
    company: str | None = None
    query: str | None = None
    since_days: int | None = None
