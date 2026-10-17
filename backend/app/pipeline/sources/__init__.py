"""1 · Information sources.

Every source is a small adapter with one job: read one place and return
`RawItem`s. None of them touches the database, a guardrail or the LLM -- the
orchestrator fans out to them and the stages after it do the rest.

  funding   RssSource         a publisher's RSS feed                 free
            LinkedinSource    one profile or company page, via Apify  ~$0.002 a post
            WebSearchSource   funding news search, via Firecrawl      ~2 credits / 10 results
  events    LumaSource        Luma city feeds, via Apify              ~$0.002 an event
            EventbriteSource  Eventbrite city search, via Apify       ~$0.001 an event
            PageSource        any other events page, via Firecrawl    1 credit a page
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ..items import RawItem


class SourceError(RuntimeError):
    """A source could not be read; worded for the status page. Carries what
    the failed read still cost, when the provider charges for it."""

    def __init__(self, message: str, *, apify_usd: float = 0.0, credits: int = 0) -> None:
        super().__init__(message)
        self.apify_usd = apify_usd
        self.credits = credits


@dataclass
class FetchContext:
    """What a source may ask about earlier runs, without touching the database."""

    # The newest publish time already read from a source, or None. LinkedIn
    # uses it so only newer posts are paid for.
    cursor: Callable[[str], datetime | None] = lambda key: None
    now: datetime | None = None


@dataclass
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    apify_usd: float = 0.0          # estimated from the actor's published prices
    credits: int = 0                # Firecrawl
    warning: str | None = None      # read, but something was not right


_NEEDS = {"linkedin": "apify", "luma": "apify", "eventbrite": "apify",
          "web_search": "firecrawl", "page": "firecrawl"}


@dataclass(frozen=True)
class Source:
    key: str
    type: str          # rss | website | linkedin | web_search | luma | eventbrite | page
    label: str
    every_hours: float              # read at most this often; 0 = every run
    paid: bool
    # A source added on the Sources panel (its user_sources id), or None for
    # one from sources.yaml.
    user_id: int | None = None

    @property
    def needs(self) -> str | None:
        """The paid service this source cannot be read without: apify, firecrawl or None."""
        return _NEEDS.get(self.type)

    def fetch(self, ctx: FetchContext) -> FetchResult:  # pragma: no cover - interface
        raise NotImplementedError
