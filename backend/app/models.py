"""ORM models.

Two of these exist purely to keep the project honest:
  LlmCall   -- every model call is ledgered, so spend is a queryable fact.
  IngestRun -- per-source run stats, which is how we detect a listing window
               overflowing between 12h refreshes and silently dropping stories.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A DateTime that is always timezone-aware UTC on the Python side.

    SQLite has no timezone type: it stores the wall-clock digits and drops the
    offset, so every value read back used to be naive. The API serialised
    those as "2026-09-23T07:56:38" and browsers parsed them as *local* time --
    5h30m early in India. Normalising here fixes it for every column, every
    endpoint and every comparison at once.

    Storage is unchanged (naive UTC digits), so existing rows need no migration.
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


class Category(str, enum.Enum):
    """Fixed taxonomy.

    Deliberately a closed set: letting the model invent labels produces
    "Funding" / "Funding News" / "Fundraise" as separate categories and
    silently breaks every filter built on top of them.
    """

    FUNDING = "Funding"
    MA = "M&A"
    IPO = "IPO"
    PRODUCT_LAUNCH = "Product Launch"
    POLICY = "Policy/Regulation"
    HIRING = "Hiring/Layoffs"
    SHUTDOWN = "Shutdown"
    PARTNERSHIP = "Partnership"
    MARKET = "Market/Analysis"
    OTHER = "Other"


class Stage(str, enum.Enum):
    """The Insights tabs. Anything that is not one of the four named stages --
    Series C and later, debt, IPO anchor money, undisclosed -- is OTHER, so no
    funding story is hidden. The exact round name is kept separately."""

    PRE_SEED = "Pre-Seed"
    SEED = "Seed"
    SERIES_A = "Series A"
    SERIES_B = "Series B"
    OTHER = "Other"


class DescriptionOrigin(str, enum.Enum):
    META = "meta"            # taken from the page's own og:description
    FEED = "feed"            # taken from the RSS item
    GENERATED = "generated"  # written by gpt-oss as a fallback


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True, index=True)
    url_hash: Mapped[str] = mapped_column(String(40), index=True)

    source: Mapped[str] = mapped_column(String(60), index=True)
    headline: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    description_origin: Mapped[DescriptionOrigin | None] = mapped_column(
        Enum(DescriptionOrigin), default=None
    )
    published_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), index=True, default=None
    )

    # Filled by the batched gpt-oss enrichment pass (Phase 5).
    company: Mapped[str | None] = mapped_column(String(200), index=True, default=None)
    category: Mapped[Category | None] = mapped_column(
        Enum(Category), index=True, default=None
    )
    summary: Mapped[str | None] = mapped_column(Text, default=None)

    # Dedupe: story_key groups the same event across sources.
    story_key: Mapped[str | None] = mapped_column(String(40), index=True, default=None)
    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id"), index=True, default=None
    )

    content_hash: Mapped[str | None] = mapped_column(String(40), default=None)
    ingested_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow
    )
    enriched_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), default=None
    )

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )

    @property
    def is_canonical(self) -> bool:
        return self.canonical_id is None


Index("ix_articles_feed", Article.published_at.desc(), Article.canonical_id)


class Chunk(Base):
    """Backs FTS5 and, crucially, chunk-level citations.

    Answers cite a chunk id, so every claim resolves to specific stored text
    rather than a bare link at the bottom of the response.
    """

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)

    article: Mapped[Article] = relationship(back_populates="chunks")


class LlmCall(Base):
    """The spend ledger. Written on every model call, without exception."""

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


class IngestRun(Base):
    """Per-source run stats.

    items_new == items_seen means the feed window overflowed between refreshes
    and we have almost certainly lost stories.
    """

    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(60), index=True)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), default=None
    )
    strategy: Mapped[str] = mapped_column(String(20), default="rss")
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    @property
    def window_overflowed(self) -> bool:
        return self.items_seen > 0 and self.items_new == self.items_seen


class FundingRound(Base):
    """One funding round, pulled out of a Funding story by gpt-oss.

    One row per canonical story; the publish time, outlet and link come from
    the article itself. `extracted_at` is set even when a story turns out not to
    describe a round, so it is never paid for twice.
    """

    __tablename__ = "funding_rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), unique=True, index=True
    )
    company: Mapped[str | None] = mapped_column(String(200), default=None)
    stage: Mapped[Stage] = mapped_column(Enum(Stage), index=True, default=Stage.OTHER)
    round_label: Mapped[str | None] = mapped_column(String(80), default=None)
    amount: Mapped[str | None] = mapped_column(String(120), default=None)
    investors: Mapped[str | None] = mapped_column(Text, default=None)
    extracted_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    article: Mapped[Article] = relationship()


class WebRound(Base):
    """A funding round found by searching the web from Insights ("Search web").

    Kept apart from `articles`, so web finds never enter the news feed. Each
    row carries its own link, headline and publish time. `date_approx` is set
    when the time came from a phrase like "3 days ago" rather than a stamp.

    Every result the model reads gets a row, shown or not: `kept` is False for
    results that were not a startup round in the four stages, or repeated a
    round already known. That is what stops a second search paying to read
    the same result again.
    """

    __tablename__ = "web_rounds"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True, index=True)
    headline: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(200))
    snippet: Mapped[str | None] = mapped_column(Text, default=None)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True,
                                                          default=None)
    date_approx: Mapped[bool] = mapped_column(default=False)
    company: Mapped[str | None] = mapped_column(String(200), default=None)
    stage: Mapped[Stage] = mapped_column(Enum(Stage), index=True)
    round_label: Mapped[str | None] = mapped_column(String(80), default=None)
    amount: Mapped[str | None] = mapped_column(String(120), default=None)
    investors: Mapped[str | None] = mapped_column(Text, default=None)
    kept: Mapped[bool] = mapped_column(default=True, index=True)
    found_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class VcKind(str, enum.Enum):
    """What a VC firm's post is about. Only OTHER is hidden by default."""

    INVESTMENT = "Investment"      # the firm invested in, led or joined a round
    PORTFOLIO = "Portfolio news"   # a backed company's IPO, exit, milestone
    FUND = "Fund news"             # the firm raised or launched a fund/programme
    OTHER = "Other"                # essays, podcasts, events, hiring, unrelated


class VcPost(Base):
    """One post read for a VC firm: from its own site, or a news search.

    Written when first seen; `classified_at` is set once gpt-oss has read the
    title, so no post is paid for twice. The same link can belong to two
    firms (a co-led round), hence uniqueness on (firm, url).
    """

    __tablename__ = "vc_posts"
    __table_args__ = (UniqueConstraint("firm", "url", name="uq_vc_posts_firm_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    firm: Mapped[str] = mapped_column(String(60), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text, default=None)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True,
                                                          default=None)
    date_approx: Mapped[bool] = mapped_column(default=False)
    # rss | html | sitemap | scrape | map | search, or "added" for a pasted source
    via: Mapped[str] = mapped_column(String(20))
    found_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    # Filled by the batched gpt-oss pass.
    classified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    kind: Mapped[VcKind | None] = mapped_column(Enum(VcKind), index=True, default=None)
    headline: Mapped[str | None] = mapped_column(Text, default=None)
    company: Mapped[str | None] = mapped_column(String(200), default=None)
    round_label: Mapped[str | None] = mapped_column(String(80), default=None)
    amount: Mapped[str | None] = mapped_column(String(120), default=None)


class VcRead(Base):
    """One read of one firm. Shows health on the page, and spaces out the
    Firecrawl reads, which cost credits."""

    __tablename__ = "vc_reads"

    id: Mapped[int] = mapped_column(primary_key=True)
    firm: Mapped[str] = mapped_column(String(60), index=True)
    via: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 index=True)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    credits_used: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class PastedSource(Base):
    """A website the reader pasted on Insights > Startup firms or VC firms.

    Each section keeps its own: a "startups" source yields funding rounds for
    the stage tabs (`PastedRound`), a "vcs" source yields posts sorted like a
    firm's own news (`VcPost` rows with firm "pasted-<id>"). `via` is how it is
    read, cheapest that works when it was added: "rss" (its feed, or one the
    page links to), "html" (post links on the page, plain HTTP) or "scrape"
    (Firecrawl). `read_url` is what is fetched -- the feed when one was found.
    `firm` ties a VC source to a tracked firm, so its posts show under it.
    """

    __tablename__ = "pasted_sources"
    __table_args__ = (UniqueConstraint("section", "url", name="uq_pasted_sources_section_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    section: Mapped[str] = mapped_column(String(20), index=True)   # startups | vcs
    url: Mapped[str] = mapped_column(String(1000))
    read_url: Mapped[str] = mapped_column(String(1000))
    via: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(200))
    firm: Mapped[str | None] = mapped_column(String(60), default=None)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PastedSourceRead(Base):
    """One read of one pasted source: health on the page, and the spacing of
    Firecrawl reads, which cost credits."""

    __tablename__ = "pasted_source_reads"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 index=True)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    credits_used: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class PastedRound(Base):
    """A post from a pasted Startup firms source, and the round in it.

    Written when first seen; gpt-oss then reads it once (`extracted_at`). Like
    `WebRound`, every post read gets a row: `kept` is False for posts that do
    not announce a raise, or repeat a round already known, so none is paid for
    twice and only rounds reach the tabs.
    """

    __tablename__ = "pasted_rounds"
    __table_args__ = (UniqueConstraint("source_id", "url", name="uq_pasted_rounds_source_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    url: Mapped[str] = mapped_column(String(1000))
    headline: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text, default=None)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True,
                                                          default=None)
    date_approx: Mapped[bool] = mapped_column(default=False)
    found_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    # Filled by the batched gpt-oss pass.
    extracted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    company: Mapped[str | None] = mapped_column(String(200), default=None)
    stage: Mapped[Stage | None] = mapped_column(Enum(Stage), index=True, default=None)
    round_label: Mapped[str | None] = mapped_column(String(80), default=None)
    amount: Mapped[str | None] = mapped_column(String(120), default=None)
    investors: Mapped[str | None] = mapped_column(Text, default=None)
    kept: Mapped[bool] = mapped_column(default=False, index=True)


class EventSource(Base):
    """A website the reader pasted on Insights > Events organised.

    The VC firms' own Luma calendars live in `vc_firms.yaml`; these are the
    extra sources added from the page. `via` is "luma" for a Luma calendar
    (read through Apify) and "page" for any other page (read through
    Firecrawl, then gpt-oss lists the events on it).
    """

    __tablename__ = "event_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    firm: Mapped[str | None] = mapped_column(String(60), default=None)
    via: Mapped[str] = mapped_column(String(20))
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Event(Base):
    """One event, from a firm's Luma calendar or a pasted website.

    `uid` identifies the event within its source, so a re-read updates the
    row (events get rescheduled) instead of adding a second one. Upcoming or
    past is worked out at read time from the dates, never stored.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    firm: Mapped[str | None] = mapped_column(String(60), index=True, default=None)
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    date_only: Mapped[bool] = mapped_column(default=False)   # no time was given
    timezone: Mapped[str | None] = mapped_column(String(60), default=None)
    venue: Mapped[str | None] = mapped_column(String(300), default=None)
    city: Mapped[str | None] = mapped_column(String(120), default=None)
    country: Mapped[str | None] = mapped_column(String(80), default=None)
    online: Mapped[bool | None] = mapped_column(default=None)
    host: Mapped[str | None] = mapped_column(String(300), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    via: Mapped[str] = mapped_column(String(20))       # apify | firecrawl
    found_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class EventRead(Base):
    """One read of one event source. Shows health on the page, spaces out the
    paid reads, and records what each one cost.

    kind: "upcoming" / "past" for a Luma calendar, "page" for a website.
    `content_hash` is the page text read last time, so an unchanged page is
    not paid for again with a model call.
    """

    __tablename__ = "event_reads"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 index=True)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    apify_usd: Mapped[float] = mapped_column(Float, default=0.0)   # estimated
    credits_used: Mapped[int] = mapped_column(Integer, default=0)  # Firecrawl
    llm_usd: Mapped[float] = mapped_column(Float, default=0.0)
    content_hash: Mapped[str | None] = mapped_column(String(40), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class FilterCache(Base):
    """Natural-language filter phrase -> compiled JSON filter.

    The same phrase is never sent to a model twice.
    """

    __tablename__ = "filter_cache"

    phrase_hash: Mapped[str] = mapped_column(String(40), primary_key=True)
    phrase: Mapped[str] = mapped_column(Text)
    filter_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow
    )
