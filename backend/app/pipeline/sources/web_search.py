"""Web search for funding news, via Firecrawl.

One Firecrawl news search per query in the registry, limited to the last
`lookback_days` with Google's custom-date filter. Results only -- no page is
fetched; the LLM reads each result's title and snippet. Results already read
on an earlier run are dropped by the seen-items index before any model call,
so a lookback that overlaps the previous run costs Firecrawl credits only.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ...search import firecrawl
from ..items import FUNDING, RawItem, web_url
from ..timeutil import IST
from . import FetchContext, FetchResult, Source, SourceError

_RELATIVE = re.compile(r"\b(ago|hours?|minutes?|days?|weeks?|months?|yesterday|today)\b", re.I)


def tbs_for(start, end) -> str:
    """Google's custom date range, which Firecrawl search passes through."""
    fmt = lambda d: f"{d.month}/{d.day}/{d.year}"  # noqa: E731
    return f"cdr:1,cd_min:{fmt(start)},cd_max:{fmt(end)}"


def parse_published(text: str | None, now: datetime) -> tuple[datetime | None, bool]:
    """(UTC time, approximate?) from Firecrawl's date: "3 hours ago" or "Sep 23, 2026"."""
    if not text:
        return None, False
    try:
        import dateparser
        parsed = dateparser.parse(text, settings={
            "RELATIVE_BASE": now.astimezone(IST).replace(tzinfo=None),
            "TIMEZONE": "Asia/Kolkata", "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "past",
        })
    except Exception:  # noqa: BLE001 - a bad date must not fail the search
        return None, False
    if parsed is None:
        return None, False
    return parsed.astimezone(timezone.utc), bool(_RELATIVE.search(text))


@dataclass(frozen=True)
class WebSearchSource(Source):
    queries: tuple[str, ...] = ()
    lookback_days: int = 2
    results_per_query: int = 20

    def fetch(self, ctx: FetchContext) -> FetchResult:
        now = ctx.now or datetime.now(timezone.utc)
        today = now.astimezone(IST).date()
        tbs = tbs_for(today - timedelta(days=self.lookback_days), today)

        def one(query: str):
            try:
                return query, firecrawl.search(query, limit=self.results_per_query, kind="news",
                                               tbs=tbs, steered=False)
            except firecrawl.FirecrawlError as exc:
                return query, exc

        result = FetchResult()
        failed: list[str] = []
        seen: set[str] = set()
        with ThreadPoolExecutor(max_workers=min(4, len(self.queries) or 1)) as pool:
            for query, outcome in pool.map(one, self.queries):
                if isinstance(outcome, Exception):
                    failed.append(f"{query!r}: {outcome}")
                    continue
                result.credits += outcome.credits_used
                for hit in outcome.results:
                    url = web_url(hit.url)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    published, approx = parse_published(hit.published, now)
                    result.items.append(RawItem(
                        kind=FUNDING, source_key=self.key, source_type=self.type,
                        source_label=hit.domain or self.label, url=url, title=hit.title,
                        text=hit.description, published_at=published,
                        payload={"date_approx": approx, "query": query},
                    ))
        if failed and len(failed) == len(self.queries):
            raise SourceError("Every web search failed. " + failed[0], credits=result.credits)
        if failed:
            result.warning = f"{len(failed)} of {len(self.queries)} searches failed: {failed[0]}"
        return result
