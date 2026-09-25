"""Intelligence: search the web for startup news, then read one page on demand.

  POST /api/intelligence/search  query -> Firecrawl web results. No page is
                                 fetched and no model is called.
  POST /api/intelligence/scrape  one url -> Firecrawl scrapes that page, then
                                 gpt-oss summarises it for the reader's query.

Nothing is scraped until the reader asks for that specific page, so a search
costs one Firecrawl credit per ten results and nothing else. A summary costs
about $0.001 of the LLM budget and is cached per page and query. When the
budget is spent the page content is still returned; only the summary is
withheld.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..llm.bedrock import LlmUnavailable, invoke
from ..llm.budget import BudgetExceeded, get_spend
from ..search import firecrawl

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])

FEATURE = "intelligence_summary"
MAX_PAGE_CHARS = 20_000      # sent to the model: ~5k tokens, ~$0.001 all in
MAX_SHOWN_CHARS = 30_000     # returned to the page for "Page content"
SUMMARY_TTL = 60 * 60

SYSTEM_PROMPT = (
    "You summarise one web page for someone tracking startups and venture "
    "funding. Use only the page text you are given. Lead with what the page "
    "is, then the key facts: companies, amounts, investors, dates, numbers. "
    "If the page does not relate to the reader's search, say so in one line. "
    "No preamble, no sign-off."
)

_summaries: dict[str, tuple[float, tuple[str, str, float]]] = {}
_summaries_lock = threading.Lock()


# --- Search -----------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    kind: Literal["news", "web"] = "news"


class ResultOut(BaseModel):
    n: int
    url: str
    title: str
    description: str
    domain: str
    published: str | None = None


class SearchResponse(BaseModel):
    query: str
    kind: str = "news"
    searched: str | None = None
    results: list[ResultOut] = []
    credits_used: int = 0
    cached: bool = False
    error: str | None = None


@router.post("/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    try:
        found = firecrawl.search(request.query, kind=request.kind)
    except firecrawl.FirecrawlError as exc:
        return SearchResponse(query=request.query, kind=request.kind, error=str(exc))

    return SearchResponse(
        query=found.query,
        kind=found.kind,
        searched=found.searched,
        results=[ResultOut(**r.__dict__) for r in found.results],
        credits_used=found.credits_used,
        cached=found.cached,
    )


# --- Scrape + summarise -------------------------------------------------------

class ScrapeRequest(BaseModel):
    url: str
    query: str | None = None


class ScrapeResponse(BaseModel):
    url: str
    title: str | None = None
    description: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    content: str | None = None
    content_truncated: bool = False
    model: str | None = None
    cost_usd: float = 0.0
    credits_used: int = 0
    cached: bool = False
    error: str | None = None
    budget_remaining: float = 0.0


def published_fallback(url: str) -> datetime | None:
    """Read the publish time from the page itself when Firecrawl gave none.

    Free: one guarded HTTP fetch (public addresses only, 3 MB cap), the same
    one Extract uses, then the page's article:published_time meta tag. Never
    raises -- a missing date is shown as "not given", not as an error.
    """
    try:
        from ..extract import fetch_page
        from ..ingest.describe import extract_meta
        _, html = fetch_page(url)
        return extract_meta(html).published_at
    except Exception:  # noqa: BLE001
        return None


def summary_prompt(page: firecrawl.Page, query: str | None) -> str:
    text = page.markdown[:MAX_PAGE_CHARS]
    cut = " (truncated)" if len(page.markdown) > MAX_PAGE_CHARS else ""
    reader = f"The reader searched for: {query}\n\n" if query else ""
    return (
        f"{reader}Page title: {page.title}\nPage URL: {page.url}\n\n"
        f"Page text{cut}:\n{text}\n\n"
        "Write the summary as 3 to 6 short bullet points."
    )


def _summary_key(url: str, query: str | None) -> str:
    raw = f"{url.strip()}\n{' '.join((query or '').lower().split())}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@router.post("/scrape", response_model=ScrapeResponse)
def scrape(request: ScrapeRequest) -> ScrapeResponse:
    response = ScrapeResponse(url=request.url)
    try:
        page, page_cached = firecrawl.scrape(request.url)
    except firecrawl.FirecrawlError as exc:
        response.error = str(exc)
        response.budget_remaining = round(get_spend().remaining_total, 6)
        return response

    response.url = page.url
    response.title = page.title
    response.description = page.description or None
    response.content = page.markdown[:MAX_SHOWN_CHARS]
    response.content_truncated = len(page.markdown) > MAX_SHOWN_CHARS
    response.credits_used = 0 if page_cached else page.credits_used
    if page.published_at is None and not page_cached:
        page.published_at = published_fallback(page.url)
    response.published_at = page.published_at

    key = _summary_key(request.url, request.query)
    with _summaries_lock:
        hit = _summaries.get(key)
        hit = hit[1] if hit and hit[0] > time.monotonic() else None

    if hit is not None:
        response.summary, response.model, _ = hit
        response.cached = True
    else:
        try:
            result = invoke(FEATURE, summary_prompt(page, request.query),
                            system=SYSTEM_PROMPT, max_tokens=900)
        except BudgetExceeded as exc:
            response.error = f"{exc}. The page content is shown without a summary."
        except LlmUnavailable as exc:
            log.error("intelligence summary: %s", exc)
            response.error = "The model is unreachable right now. The page content is shown below."
        else:
            response.summary = result.text or "No summary came back."
            response.model = result.model
            response.cost_usd = result.cost_usd
            with _summaries_lock:
                _summaries[key] = (time.monotonic() + SUMMARY_TTL,
                                   (response.summary, result.model, result.cost_usd))

    response.budget_remaining = round(get_spend().remaining_total, 6)
    return response


def clear_cache() -> None:
    with _summaries_lock:
        _summaries.clear()
