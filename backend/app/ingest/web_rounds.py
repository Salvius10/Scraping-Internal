"""Insights "Search web": find Indian startup rounds across the web via Firecrawl.

The feed only knows what our six news sites published. This searches the
whole web, on demand, for the date range the reader picked:

  1. Firecrawl news search, one query per stage (Pre-Seed, Seed, Series A,
     Series B), limited to the range with Google's custom-date filter.
     Results only -- no page is scraped. ~2 credits per 10 results.
  2. gpt-oss reads each result's title and snippet, 20 per call, and pulls
     out company, round, stage, amount and investors (~$0.00005 a result).
  3. Rounds outside the four stages, outside the range, or already known --
     from the feed or an earlier web search -- are skipped. The rest are
     saved to `web_rounds` and appear in the Insights tabs, never in the feed.

A result URL is read by the model once; searching the same range again only
re-pays Firecrawl, and only after its 15-minute cache.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select

from ..db import session_scope
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import Article, Category, FundingRound, Stage, WebRound, utcnow
from ..search import firecrawl
from .rounds import PROMPT_HEAD, SYSTEM_PROMPT, _Pending, build_prompt, \
    clean_investors, clean_text, coerce_stage, tidy_round

log = logging.getLogger(__name__)

FEATURE = "web_rounds"
BATCH_SIZE = 20
RESULTS_PER_QUERY = 20
IST = timezone(timedelta(hours=5, minutes=30))
DUPLICATE_DAYS = 14          # same company + stage this close = same round

WANTED = (Stage.PRE_SEED, Stage.SEED, Stage.SERIES_A, Stage.SERIES_B)

QUERIES = {
    Stage.PRE_SEED: "Indian startup raises pre-seed funding round",
    Stage.SEED: "Indian startup raises seed funding round",
    Stage.SERIES_A: "Indian startup raises Series A funding",
    Stage.SERIES_B: "Indian startup raises Series B funding",
}

WEB_NOTE = (
    "These are web search results, not all of them Indian startups. Use stage "
    "Other for anything that is not an Indian startup announcing its own "
    "funding round.\n"
)

_RELATIVE = re.compile(r"\b(ago|hour|hours|minute|minutes|day|days|week|weeks|"
                       r"month|months|yesterday|today)\b", re.I)


class WebSearchError(RuntimeError):
    """Worded for the reader."""


@dataclass
class WebSearchSummary:
    start: date
    end: date
    results_seen: int = 0
    added: dict[str, int] = field(default_factory=dict)
    duplicates: int = 0
    outside_range: int = 0
    not_startup_rounds: int = 0
    credits_used: int = 0
    cost_usd: float = 0.0
    failed_queries: list[str] = field(default_factory=list)
    stopped_reason: str | None = None

    @property
    def total_added(self) -> int:
        return sum(self.added.values())


def tbs_for(start: date, end: date) -> str:
    """Google's custom date range, which Firecrawl search passes through."""
    fmt = lambda d: f"{d.month}/{d.day}/{d.year}"  # noqa: E731
    return f"cdr:1,cd_min:{fmt(start)},cd_max:{fmt(end)}"


def parse_published(text: str | None, now: datetime | None = None) -> tuple[datetime | None, bool]:
    """(UTC time, approximate?) from Firecrawl's date: "3 hours ago" or "Sep 23, 2026"."""
    if not text:
        return None, False
    import dateparser

    now = now or utcnow()
    try:
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


def _in_range(when: datetime, start: date, end: date) -> bool:
    since = datetime.combine(start, time.min, IST)
    until = datetime.combine(end + timedelta(days=1), time.min, IST)
    return since <= when < until


def _search_all(tbs: str, summary: WebSearchSummary) -> list[firecrawl.SearchResult]:
    """Run the four stage queries in parallel. A failed query is reported, not fatal."""
    def one(stage: Stage):
        try:
            return stage, firecrawl.search(QUERIES[stage], limit=RESULTS_PER_QUERY,
                                           kind="news", tbs=tbs, steered=False)
        except firecrawl.FirecrawlError as exc:
            return stage, exc

    results: list[firecrawl.SearchResult] = []
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=len(QUERIES)) as pool:
        for stage, outcome in pool.map(one, QUERIES):
            if isinstance(outcome, Exception):
                summary.failed_queries.append(f"{stage.value}: {outcome}")
                continue
            summary.credits_used += outcome.credits_used
            for r in outcome.results:
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)
    return results


def _known_urls(urls: list[str]) -> set[str]:
    with session_scope() as s:
        feed = s.scalars(select(Article.url).where(Article.url.in_(urls))).all()
        web = s.scalars(select(WebRound.url).where(WebRound.url.in_(urls))).all()
    return set(feed) | set(web)


def _already_known(company: str, stage: Stage, when: datetime) -> bool:
    """The same company at the same stage within two weeks is the same round."""
    lo, hi = when - timedelta(days=DUPLICATE_DAYS), when + timedelta(days=DUPLICATE_DAYS)
    name = company.strip().lower()
    with session_scope() as s:
        feed = s.scalar(
            select(func.count(FundingRound.id))
            .join(Article, Article.id == FundingRound.article_id)
            .where(func.lower(FundingRound.company) == name, FundingRound.stage == stage,
                   Article.canonical_id.is_(None), Article.category == Category.FUNDING,
                   Article.published_at >= lo, Article.published_at <= hi)
        )
        web = s.scalar(
            select(func.count(WebRound.id))
            .where(WebRound.kept.is_(True),
                   func.lower(WebRound.company) == name, WebRound.stage == stage,
                   WebRound.published_at >= lo, WebRound.published_at <= hi)
        )
    return bool(feed or web)


def search_web(start: date, end: date | None = None) -> WebSearchSummary:
    """Search the web for startup rounds published in [start, end]. Never raises
    for a partial failure; raises WebSearchError when nothing could be searched."""
    today = utcnow().astimezone(IST).date()
    end = min(end or today, today)
    if start > end:
        raise WebSearchError("The From date is after the To date.")

    summary = WebSearchSummary(start=start, end=end)
    found = _search_all(tbs_for(start, end), summary)
    if not found and len(summary.failed_queries) == len(QUERIES):
        raise WebSearchError(summary.failed_queries[0].split(": ", 1)[-1])
    summary.results_seen = len(found)

    known = _known_urls([r.url for r in found])
    fresh: list[tuple[firecrawl.SearchResult, datetime, bool]] = []
    for r in found:
        if r.url in known:
            summary.duplicates += 1
            continue
        when, approx = parse_published(r.published)
        if when is None or not _in_range(when, start, end):
            summary.outside_range += 1
            continue
        fresh.append((r, when, approx))

    batch_seen: set[tuple[str, Stage]] = set()
    for offset in range(0, len(fresh), BATCH_SIZE):
        batch = fresh[offset:offset + BATCH_SIZE]
        pending = [_Pending(i, r.title, r.description, None) for i, (r, _, _) in enumerate(batch)]
        prompt = build_prompt(pending).replace(PROMPT_HEAD, PROMPT_HEAD + "\n" + WEB_NOTE, 1)
        try:
            parsed, call = invoke_json(FEATURE, prompt, system=SYSTEM_PROMPT, max_tokens=4096)
        except BudgetExceeded as exc:
            summary.stopped_reason = str(exc)
            break
        except (LlmUnavailable, ValueError) as exc:
            log.error("web rounds: batch failed: %s", exc)
            summary.stopped_reason = str(exc)
            break
        summary.cost_usd += call.cost_usd
        if not isinstance(parsed, list):
            continue

        with session_scope() as s:
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                try:
                    r, when, approx = batch[int(entry.get("i")) - 1]
                except (TypeError, ValueError, IndexError):
                    continue
                round_label = tidy_round(clean_text(entry.get("round"), 80))
                stage = coerce_stage(entry.get("stage"), round_label)
                company = clean_text(entry.get("company"), 200)

                kept = True
                if stage not in WANTED or not company:
                    summary.not_startup_rounds += 1
                    kept = False
                else:
                    key = (company.lower(), stage)
                    if key in batch_seen or _already_known(company, stage, when):
                        summary.duplicates += 1
                        kept = False
                    batch_seen.add(key)

                # Recorded either way, so this result is never read again.
                s.add(WebRound(
                    url=r.url, headline=r.title, domain=r.domain,
                    snippet=r.description or None, published_at=when,
                    date_approx=approx, company=company, stage=stage,
                    round_label=round_label, amount=clean_text(entry.get("amount"), 120),
                    investors=clean_investors(entry.get("investors")), kept=kept,
                ))
                if kept:
                    summary.added[stage.value] = summary.added.get(stage.value, 0) + 1

    log.info("web rounds %s..%s: %d results, %d added, %d duplicates, $%.6f, %d credits",
             start, end, summary.results_seen, summary.total_added, summary.duplicates,
             summary.cost_usd, summary.credits_used)
    return summary
