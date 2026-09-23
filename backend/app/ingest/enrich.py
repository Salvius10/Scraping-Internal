"""Fill company, category and fallback descriptions with one batched call.

This is the only routinely-paid step in ingest, and it is batched precisely so
it stays negligible: 30 articles per call works out around $0.00005 each, which
is roughly 50x cheaper than classifying them one at a time.

Two economies beyond batching:
  - Only canonical articles are enriched. Duplicates are hidden from the feed,
    so paying to classify them would buy nothing.
  - Summaries are requested only for articles whose publisher description was
    missing or too thin, because `describe.py` already supplied the rest free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import Article, Category, DescriptionOrigin, utcnow
from .describe import MIN_DESCRIPTION_CHARS

log = logging.getLogger(__name__)

BATCH_SIZE = 30
FEATURE = "enrich"

# Accept whatever casing or spacing the model returns, but never invent a
# category: anything unrecognised becomes Other rather than a new label.
_CATEGORY_BY_KEY = {c.value.lower().replace("/", "").replace(" ", ""): c
                    for c in Category}
_CATEGORY_LIST = ", ".join(c.value for c in Category)

SYSTEM_PROMPT = (
    "You classify Indian startup-ecosystem news. You reply with JSON only -- "
    "no prose, no markdown fences."
)

# Measured against tests/fixtures/labeled_articles.json. Without these hints
# the model sent executive appointments and regulatory settlements to "Other",
# which is the default whenever a headline does not obviously announce money.
CATEGORY_GUIDE = """Category guidance:
  Funding            a company or fund raising capital, at any stage
  M&A                acquisitions, stake purchases, spin-offs, mergers
  IPO                IPO filings, approvals, price bands, listings
  Product Launch     a new product, service or model going live
  Policy/Regulation  regulators, courts, legal settlements, new rules
  Hiring/Layoffs     appointments, promotions, executive exits, job cuts
  Shutdown           a business ceasing operations
  Partnership        two named organisations tying up
  Market/Analysis    results, valuations, share moves, trends, roundups
  Other              only when nothing above fits"""


def build_prompt_from_headlines(headlines: list[str]) -> str:
    """Prompt body for a list of headlines. Shared with the eval script."""
    lines = [
        "For each numbered article below, return one JSON object with keys:",
        '  "i"        the article number',
        '  "company"  the main organisation the article is about (the one it '
        "is reporting on), or null if the article is about the market in "
        "general",
        f'  "category" EXACTLY one of: {_CATEGORY_LIST}',
        '  "summary"  a single factual sentence (max 25 words), or null',
        "",
        CATEGORY_GUIDE,
        "",
        "Return a JSON array of these objects and nothing else.",
        "",
    ]
    lines += [f"[{n}] {h}" for n, h in enumerate(headlines, start=1)]
    return "\n".join(lines)


def _coerce_category(value: object) -> Category:
    if not isinstance(value, str):
        return Category.OTHER
    key = value.strip().lower().replace("/", "").replace(" ", "")
    return _CATEGORY_BY_KEY.get(key, Category.OTHER)


def _clean_company(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = " ".join(value.split()).strip(" .,")
    if not name or name.lower() in ("null", "none", "n/a", "unknown", "na"):
        return None
    return name[:200]


@dataclass
class EnrichResult:
    considered: int = 0
    updated: int = 0
    batches: int = 0
    cost_usd: float = 0.0
    stopped_reason: str | None = None


@dataclass
class _Pending:
    """A snapshot taken outside any transaction."""

    id: int
    headline: str
    description: str | None


def _load_pending(limit: int | None) -> list[_Pending]:
    stmt = (
        select(Article.id, Article.headline, Article.description)
        .where(Article.canonical_id.is_(None), Article.enriched_at.is_(None))
        .order_by(Article.published_at.desc().nullslast())
    )
    if limit:
        stmt = stmt.limit(limit)
    with session_scope() as s:
        return [_Pending(*row) for row in s.execute(stmt).all()]


def _apply_batch(entries: list[dict], by_number: dict[int, _Pending]) -> int:
    """Write one batch's results in a short transaction of its own."""
    updated = 0
    with session_scope() as s:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                number = int(entry.get("i"))
            except (TypeError, ValueError):
                continue

            pending = by_number.get(number)
            if pending is None:
                continue

            article = s.get(Article, pending.id)
            if article is None:
                continue

            article.company = _clean_company(entry.get("company"))
            article.category = _coerce_category(entry.get("category"))

            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                article.summary = " ".join(summary.split())[:400]
                thin = (not article.description
                        or len(article.description) < MIN_DESCRIPTION_CHARS)
                if thin:
                    article.description = article.summary
                    article.description_origin = DescriptionOrigin.GENERATED

            article.enriched_at = utcnow()
            updated += 1
    return updated


def enrich_pending(
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    session: Session | None = None,  # noqa: ARG001 - kept for call-site symmetry
) -> EnrichResult:
    """Classify unenriched canonical articles. Returns a run summary.

    Reads, model call, and writes are deliberately three separate steps. An
    earlier version held one transaction open across the whole loop, so the
    ledger write inside `invoke_json` -- which needs its own connection --
    deadlocked SQLite with "database is locked". Holding a write lock across a
    multi-second network call is wrong regardless of the database.
    """
    result = EnrichResult()

    pending = _load_pending(limit)
    result.considered = len(pending)
    if not pending:
        log.info("enrich: nothing pending")
        return result

    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        rows = list(enumerate(batch, start=1))
        by_number = {n: p for n, p in rows}

        try:
            parsed, call = invoke_json(
                FEATURE,
                build_prompt_from_headlines([
                    p.headline if not p.description
                    else f"{p.headline}\n    {p.description[:200]}"
                    for p in batch
                ]),
                system=SYSTEM_PROMPT,
                max_tokens=4096,
            )
        except BudgetExceeded as exc:
            # Stop cleanly: everything already enriched stays enriched.
            log.warning("enrich: %s", exc)
            result.stopped_reason = str(exc)
            break
        except (LlmUnavailable, ValueError) as exc:
            log.error("enrich: batch failed: %s", exc)
            result.stopped_reason = str(exc)
            break

        result.batches += 1
        result.cost_usd += call.cost_usd

        if not isinstance(parsed, list):
            log.warning("enrich: expected a JSON array, got %s",
                        type(parsed).__name__)
            continue

        result.updated += _apply_batch(parsed, by_number)

    log.info("enrich: %d/%d articles in %d batch(es), $%.6f",
             result.updated, result.considered, result.batches,
             result.cost_usd)
    return result
