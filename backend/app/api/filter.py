"""Natural-language filtering.

The expensive way to do this is to hand the model a pile of articles and ask
which ones match. The cheap way -- and the correct one -- is to hand it only
the *schema* and the user's phrase, get back a JSON filter, and run that as
SQL. Roughly $0.0002 per phrase instead of a call that grows with the corpus,
it stays exact at 10,000 articles, and identical phrases are never recompiled.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..ingest.sources import load_sources
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import Category, FilterCache
from ..schemas import FilterSpec

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["filter"])

FEATURE = "filter"

SYSTEM_PROMPT = (
    "You translate a reader's phrase into a JSON filter for a news feed. "
    "You reply with JSON only -- no prose, no markdown fences."
)


class FilterRequest(BaseModel):
    phrase: str


class FilterResponse(BaseModel):
    filter: FilterSpec
    cached: bool
    cost_usd: float = 0.0
    explanation: str | None = None
    error: str | None = None


def _phrase_hash(phrase: str) -> str:
    return hashlib.sha1(phrase.strip().lower().encode("utf-8")).hexdigest()


def _build_prompt(phrase: str) -> str:
    categories = ", ".join(c.value for c in Category)
    sources = ", ".join(s.name for s in load_sources())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""Today is {today}.

Translate the reader's phrase into a JSON object with these keys. Omit a key
entirely when the phrase says nothing about it -- do not guess.

  "categories"  array, any of: {categories}
  "sources"     array, any of: {sources}
  "company"     string, a single company name mentioned in the phrase
  "query"       string, free-text words to search headlines for
  "since_days"  integer, how many days back the reader wants

Rules:
  - "this week" is 7, "today" is 1, "this month" is 30.
  - Put a topic that is not one of the categories into "query", not
    "categories". Fintech, EV and healthtech are topics, not categories.
  - Use "company" only for an actual named company.

Reader's phrase: {phrase}

Reply with the JSON object only."""


def _coerce(raw: object) -> FilterSpec:
    """Validate the model's JSON into a FilterSpec, dropping anything unknown."""
    if not isinstance(raw, dict):
        return FilterSpec()

    valid_categories = {c.value.lower(): c for c in Category}
    categories = []
    for value in raw.get("categories") or []:
        if isinstance(value, str):
            match = valid_categories.get(value.strip().lower())
            if match:
                categories.append(match)

    valid_sources = {s.name for s in load_sources()}
    sources = [
        v.strip() for v in (raw.get("sources") or [])
        if isinstance(v, str) and v.strip() in valid_sources
    ]

    company = raw.get("company")
    company = company.strip() if isinstance(company, str) and company.strip() else None

    query = raw.get("query")
    query = query.strip() if isinstance(query, str) and query.strip() else None

    since_days = raw.get("since_days")
    try:
        since_days = int(since_days) if since_days is not None else None
        if since_days is not None and not (1 <= since_days <= 365):
            since_days = None
    except (TypeError, ValueError):
        since_days = None

    return FilterSpec(
        categories=categories, sources=sources,
        company=company, query=query, since_days=since_days,
    )


def _describe(spec: FilterSpec) -> str:
    """Plain-language echo, so the reader can see what was understood."""
    parts: list[str] = []
    if spec.categories:
        parts.append(" or ".join(c.value for c in spec.categories))
    if spec.company:
        parts.append(f"about {spec.company}")
    if spec.query:
        parts.append(f"matching “{spec.query}”")
    if spec.sources:
        parts.append("from " + ", ".join(spec.sources))
    if spec.since_days:
        parts.append(
            "from today" if spec.since_days == 1
            else f"from the last {spec.since_days} days"
        )
    return "Showing stories " + ", ".join(parts) if parts else "Showing everything"


@router.post("/filter", response_model=FilterResponse)
def compile_filter(
    request: FilterRequest, session: Session = Depends(get_session)
) -> FilterResponse:
    phrase = request.phrase.strip()
    if not phrase:
        return FilterResponse(filter=FilterSpec(), cached=True,
                              explanation="Showing everything")

    key = _phrase_hash(phrase)
    cached = session.get(FilterCache, key)
    if cached is not None:
        spec = FilterSpec.model_validate_json(cached.filter_json)
        return FilterResponse(filter=spec, cached=True, explanation=_describe(spec))

    try:
        parsed, call = invoke_json(
            FEATURE, _build_prompt(phrase), system=SYSTEM_PROMPT, max_tokens=700
        )
    except BudgetExceeded as exc:
        return FilterResponse(filter=FilterSpec(), cached=False, error=str(exc))
    except (LlmUnavailable, ValueError) as exc:
        log.error("filter: %s", exc)
        return FilterResponse(
            filter=FilterSpec(), cached=False,
            error="Could not read that phrase. Try the filters on the left.",
        )

    spec = _coerce(parsed)
    session.add(FilterCache(
        phrase_hash=key, phrase=phrase,
        filter_json=spec.model_dump_json(),
    ))
    session.commit()

    return FilterResponse(
        filter=spec, cached=False, cost_usd=call.cost_usd,
        explanation=_describe(spec),
    )


@router.get("/filter/examples")
def examples() -> dict:
    """Seeded phrases, so the reader can see what the box accepts."""
    return {"examples": [
        "funding rounds this week",
        "IPO news from Entrackr",
        "anything about Zepto",
        "fintech stories from the last 3 days",
        "layoffs and shutdowns",
    ]}


def apply_filter(stmt, spec: FilterSpec):
    """Fold a FilterSpec into a SQLAlchemy select. Used by the feed endpoint."""
    from ..models import Article
    from sqlalchemy import or_

    if spec.categories:
        stmt = stmt.where(Article.category.in_(spec.categories))
    if spec.sources:
        stmt = stmt.where(Article.source.in_(spec.sources))
    if spec.company:
        stmt = stmt.where(Article.company.ilike(f"%{spec.company}%"))
    if spec.query:
        term = f"%{spec.query}%"
        stmt = stmt.where(or_(
            Article.headline.ilike(term),
            Article.description.ilike(term),
            Article.company.ilike(term),
        ))
    return stmt


__all__ = ["router", "apply_filter", "FilterRequest", "FilterResponse"]
