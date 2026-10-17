"""ORM models, one per box in the architecture that holds state.

  SeenItem          the seen-items index (URL + content hash), and the queue of
                    items waiting for the LLM stages
  GuardrailVerdict  one Bedrock guardrail answer per stage and text, so no text
                    is paid for twice
  BlockedItem       the blocked / rejected log
  FundedStartup     storage: the funded startups feed
  StartupEvent      storage: the India startup events feed
  PipelineRun       one scheduled or manual run, with what each stage did
  SourceRun         one fetch of one source within a run
  LlmCall           the spend ledger: every model and guardrail call

Tables from the previous design (articles, funding_rounds, linkedin_posts...)
are left in the database untouched; nothing here reads or writes them.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A DateTime that is always timezone-aware UTC on the Python side.

    SQLite has no timezone type: it stores the wall-clock digits and drops the
    offset, so every value read back used to be naive -- and browsers parsed
    the API's offset-less times as *local* time, 5h30m early in India.
    Storage is naive UTC digits; every value read back is aware UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # Naive values in this codebase are UTC by convention.
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Stage(str, enum.Enum):
    """Funding stage. Anything that is not one of the four early stages --
    Series C and later, debt, undisclosed -- is OTHER, so no round is hidden.
    The exact round name is kept separately."""

    PRE_SEED = "Pre-Seed"
    SEED = "Seed"
    SERIES_A = "Series A"
    SERIES_B = "Series B"
    OTHER = "Other"


class ItemStatus(str, enum.Enum):
    PENDING = "pending"        # admitted; waiting for Guardrail #2 and the LLM
    STORED = "stored"          # produced at least one stored row
    IRRELEVANT = "irrelevant"  # the LLM read it: not a round / not an India startup event
    BLOCKED = "blocked"        # stopped by Guardrail #2 or #3
    REJECTED = "rejected"      # nothing usable after max_item_attempts, or failed validation


class SeenItem(Base):
    """One item admitted by Dedup Stage 1, and the work queue after it.

    `url_hash` is unique: the same URL is never admitted twice. `content_hash`
    catches the same text under another URL (syndicated copies). The raw text
    is kept so a pending item can be retried on a later run without fetching
    it again.
    """

    __tablename__ = "seen_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    kind: Mapped[str] = mapped_column(String(20), index=True)   # funding | event | event_page
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    source_type: Mapped[str] = mapped_column(String(20))
    source_label: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[ItemStatus] = mapped_column(Enum(ItemStatus), index=True,
                                               default=ItemStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


Index("ix_seen_items_source_published", SeenItem.source_key, SeenItem.published_at)


class GuardrailVerdict(Base):
    """A guardrail's answer for one text at one stage.

    Keyed by a hash of the exact text screened, so an item re-fetched on every
    run (an RSS feed keeps its last 50 stories) is checked -- and paid for --
    once. Only definite answers are stored; an error is retried next time.
    """

    __tablename__ = "guardrail_verdicts"
    __table_args__ = (UniqueConstraint("stage", "text_hash", name="uq_guardrail_stage_text"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(String(10))       # raw | input | output
    text_hash: Mapped[str] = mapped_column(String(64))
    allowed: Mapped[bool] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class BlockedItem(Base):
    """The blocked / rejected log: every item stopped before storage, and why.

    stage: guardrail_raw | guardrail_input | guardrail_output | validation | model
    """

    __tablename__ = "blocked_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(String(20), index=True)
    source_key: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    seen_item_id: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class FundedStartup(Base):
    """One funding round, after LLM structuring and Dedup Stage 2.

    `source_url` and `published_at` are the most trusted report of the deal
    (news feed, then web search, then LinkedIn); `sources` lists every report
    that was merged into it: [{"url", "source", "type", "published_at"}].
    """

    __tablename__ = "funded_startups"

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    company_key: Mapped[str] = mapped_column(String(200), index=True)
    stage: Mapped[Stage] = mapped_column(Enum(Stage), index=True)
    round_label: Mapped[str | None] = mapped_column(String(80), default=None)
    amount: Mapped[str | None] = mapped_column(String(120), default=None)
    investors: Mapped[list] = mapped_column(JSON, default=list)
    sector: Mapped[str | None] = mapped_column(String(120), default=None)
    hq_city: Mapped[str | None] = mapped_column(String(120), default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    headline: Mapped[str] = mapped_column(Text)

    source_url: Mapped[str] = mapped_column(String(1000))
    source_label: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20), index=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True,
                                                          default=None)
    sources: Mapped[list] = mapped_column(JSON, default=list)

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class StartupEvent(Base):
    """One startup event in India, after LLM structuring and Dedup Stage 2.

    Times are UTC; `date_only` marks a day with no time given, stored as
    midnight IST and shown as a day. `source_url` is where it was found.
    """

    __tablename__ = "startup_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    title_key: Mapped[str] = mapped_column(String(300), index=True)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    date_only: Mapped[bool] = mapped_column(default=False)
    city: Mapped[str | None] = mapped_column(String(120), index=True, default=None)
    venue: Mapped[str | None] = mapped_column(String(300), default=None)
    online: Mapped[bool | None] = mapped_column(default=None)
    organiser: Mapped[str | None] = mapped_column(String(300), default=None)
    category: Mapped[str | None] = mapped_column(String(40), default=None)
    price: Mapped[str | None] = mapped_column(String(80), default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    url: Mapped[str] = mapped_column(String(1000))

    source_url: Mapped[str] = mapped_column(String(1000))
    source_label: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20), index=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PipelineRun(Base):
    """One run of the whole pipeline, scheduled or manual, and its stage counts."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))        # scheduled | manual | cli
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    blocked_raw: Mapped[int] = mapped_column(Integer, default=0)
    already_seen: Mapped[int] = mapped_column(Integer, default=0)
    admitted: Mapped[int] = mapped_column(Integer, default=0)
    blocked_input: Mapped[int] = mapped_column(Integer, default=0)
    structured: Mapped[int] = mapped_column(Integer, default=0)
    irrelevant: Mapped[int] = mapped_column(Integer, default=0)
    blocked_output: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    funding_new: Mapped[int] = mapped_column(Integer, default=0)
    funding_merged: Mapped[int] = mapped_column(Integer, default=0)
    events_new: Mapped[int] = mapped_column(Integer, default=0)
    events_merged: Mapped[int] = mapped_column(Integer, default=0)
    events_updated: Mapped[int] = mapped_column(Integer, default=0)
    pending_left: Mapped[int] = mapped_column(Integer, default=0)
    llm_usd: Mapped[float] = mapped_column(Float, default=0.0)
    apify_usd: Mapped[float] = mapped_column(Float, default=0.0)      # estimated
    firecrawl_credits: Mapped[int] = mapped_column(Integer, default=0)
    stopped_reason: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class SourceRun(Base):
    """One fetch of one source. `skipped` is set, with no fetch made, when the
    source was read within its interval; a failed fetch does not count as a read.

    `cursor` is the newest publish time in a read whose every item was dealt
    with (admitted, already seen, or blocked). LinkedIn asks only for posts
    newer than it, so an item held by an unreachable guardrail is read again.
    """

    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
                                        index=True)
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    source_type: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    skipped: Mapped[str | None] = mapped_column(String(200), default=None)
    items: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    apify_usd: Mapped[float] = mapped_column(Float, default=0.0)      # estimated
    firecrawl_credits: Mapped[int] = mapped_column(Integer, default=0)
    warning: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class UserSource(Base):
    """A funding source added on the platform's Sources panel.

    Read on every run alongside the sources in sources.yaml, through the same
    stages. kind:
      linkedin  `value` is the account in one spelling (linkedin.com/{in|company}/<slug>/)
      website   `value` is the page as given; `read_url` is what is read --
                its feed when it has one (`via` "rss", free), else the page
                itself through Firecrawl (`via` "scrape", 1 credit a read)
      search    `value` is the phrase, searched in news across the web
    Removing one stops it being read; what it already found stays.
    """

    __tablename__ = "user_sources"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_user_sources_kind_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(1000))
    label: Mapped[str] = mapped_column(String(200))
    via: Mapped[str | None] = mapped_column(String(20), default=None)
    read_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class LlmCall(Base):
    """The spend ledger. Written on every model and guardrail call, without exception."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                         index=True)
    feature: Mapped[str] = mapped_column(String(60), index=True)
    model: Mapped[str] = mapped_column(String(120))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str | None] = mapped_column(Text, default=None)
