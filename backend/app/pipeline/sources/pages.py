"""Any web page, via Firecrawl: an events portal, or a news page you added.

For a page with no actor and no feed, Firecrawl scrapes it (1 credit) and the
whole page becomes one item; the LLM stage lists what is on it -- the events
(`event_page`) or the funding rounds (`funding_page`). The page's identity in
the seen-items index includes its text, so an unchanged page is dropped
before any model call and a changed one is read again.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...search import firecrawl
from ..items import EVENT_PAGE, TEXT_CHARS, RawItem, normalise_url, sha256
from . import FetchContext, FetchResult, Source, SourceError


@dataclass(frozen=True)
class PageSource(Source):
    url: str = ""
    item_kind: str = EVENT_PAGE

    @property
    def needs(self) -> str | None:
        return "firecrawl"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        try:
            page, cached = firecrawl.scrape(self.url)
        except firecrawl.FirecrawlError as exc:
            raise SourceError(f"{self.label}: {exc}") from None
        text = page.markdown[:TEXT_CHARS]
        item = RawItem(
            kind=self.item_kind, source_key=self.key, source_type=self.type,
            source_label=self.label, url=self.url, title=page.title or self.label, text=text,
            identity=f"{normalise_url(self.url)}#{sha256(text)}",
        )
        return FetchResult(items=[item], credits=0 if cached else page.credits_used)
