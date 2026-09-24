"""Intelligence: a natural-language question, answered with citations.

Real-time, never waiting for the 12-hour scheduler. Cheapest paths first:

  1. Compile the question into a search plan    gpt-oss, ~$0.0002, cached
  2. Search our six sites live, in parallel     free (HTTP only)
  3. Write new finds through to the corpus      free; also warms the feed
  4. Rank the corpus with FTS5                  free
  5. Synthesise a cited answer                  gpt-oss ~$0.002, Sonnet opt-in

Every claim cites a numbered passage, and every passage is a stored chunk, so
a citation resolves to text we hold -- not a bare link. When the budget is
spent the matching stories are still returned; only the prose is withheld.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_session, session_scope
from ..ingest.describe import MIN_DESCRIPTION_CHARS, fetch_meta
from ..ingest.pipeline import store_item
from ..ingest.rss import FeedItem
from ..ingest.sources import load_sources
from ..llm.bedrock import LlmUnavailable, invoke, invoke_json
from ..llm.budget import BudgetExceeded, get_spend
from ..models import Article, QuestionCache
from ..search import corpus
from ..search.live import SourceResult, search_live

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["intelligence"])

FEATURE_PLAN = "intelligence_plan"
FEATURE_ANSWER = "intelligence"

MAX_EVIDENCE = 12       # passages shown to the model
HYDRATE_LIMIT = 8       # new live finds whose page we fetch for a description

PLAN_SYSTEM = (
    "You turn a reader's question about Indian startup news into a search "
    "plan. You reply with JSON only -- no prose, no markdown fences."
)

ANSWER_SYSTEM = (
    "You answer questions about the Indian startup ecosystem using ONLY the "
    "numbered news passages provided. Cite every factual claim with the "
    "passage number in square brackets, like [2] or [1][4]. If the passages "
    "do not answer the question, say so plainly and say what they do cover. "
    "Never use outside knowledge. Note dates when timing matters. No "
    "preamble, no sign-off."
)


# --- Request / response ------------------------------------------------------

class IntelligenceRequest(BaseModel):
    question: str
    premium: bool = False
    live: bool = True


class Citation(BaseModel):
    n: int
    article_id: int
    chunk_id: int | None
    headline: str
    url: str
    source: str
    published_at: datetime | None
    company: str | None
    category: str | None
    snippet: str
    cited: bool          # the answer actually refers to it
    discovered: bool     # found by this question's live search, not in the feed before


class SourceStatus(BaseModel):
    name: str
    label: str
    kind: str
    status: str
    found: int
    new: int
    cached: bool
    error: str | None = None


class IntelligenceResponse(BaseModel):
    question: str
    answer: str | None = None
    model: str | None = None
    search: str | None = None
    terms: list[str] = []
    since_days: int | None = None
    citations: list[Citation] = []
    sources: list[SourceStatus] = []
    cost_usd: float = 0.0
    plan_cached: bool = False
    error: str | None = None
    budget_remaining: float = 0.0


# --- 1. Search plan ----------------------------------------------------------

@dataclass
class Plan:
    search: str          # what to type into a site's search box
    terms: list[str]     # full-text search keywords, synonyms included
    since_days: int | None = None


def _question_hash(question: str) -> str:
    return hashlib.sha1(question.strip().lower().encode("utf-8")).hexdigest()


def _plan_prompt(question: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""Today is {today}.

Reply with a JSON object with these keys:

  "search"      string, 1-3 words to type into a news site's search box: the
                company, person, sector or event at the heart of the question.
                When the question names a company, use just that name.
  "terms"       array of 2-8 single lowercase keywords for full-text search,
                including useful synonyms (funding -> raises, round, funding).
  "since_days"  integer, only if the question limits time ("this week" is 7,
                "today" is 1, "this month" is 30). Omit it otherwise.

Question: {question}

Reply with the JSON object only."""


def fallback_plan(question: str) -> Plan:
    """Free plan used when the model is unavailable or the budget is spent."""
    tokens = corpus.keywords(question)
    # A capitalised word mid-sentence is usually the company being asked about.
    named = re.findall(r"(?<!^)(?<![.?!]\s)\b([A-Z][A-Za-z0-9&]+)", question)
    named = [w for w in named if w.lower() in tokens]
    # One name, not two: "Zepto IPO" misses VCCircle's /tag/zepto page and
    # narrows every site's own search to fewer results.
    search = named[0] if named else " ".join(tokens[:2])
    return Plan(search=search, terms=tokens[:8])


def coerce_plan(raw: object, question: str) -> Plan:
    """Validate the model's plan. Anything malformed falls back to free."""
    if not isinstance(raw, dict):
        return fallback_plan(question)

    search = raw.get("search")
    search = " ".join(search.split())[:60] if isinstance(search, str) else ""

    terms: list[str] = []
    for value in raw.get("terms") or []:
        if isinstance(value, str):
            for token in corpus.keywords(value):
                if token not in terms and len(token) <= 30:
                    terms.append(token)
    terms = terms[:8]

    since_days = raw.get("since_days")
    try:
        since_days = int(since_days) if since_days is not None else None
        if since_days is not None and not (1 <= since_days <= 365):
            since_days = None
    except (TypeError, ValueError):
        since_days = None

    if not search and not terms:
        return fallback_plan(question)
    return Plan(
        search=search or " ".join(terms[:2]),
        terms=terms or corpus.keywords(search),
        since_days=since_days,
    )


def compile_plan(session: Session, question: str) -> tuple[Plan, float, bool]:
    """(plan, cost, cached). Never raises: failure means the free plan."""
    key = _question_hash(question)
    cached = session.get(QuestionCache, key)
    if cached is not None:
        data = json.loads(cached.plan_json)
        return Plan(**data), 0.0, True

    try:
        parsed, call = invoke_json(
            FEATURE_PLAN, _plan_prompt(question), system=PLAN_SYSTEM,
            max_tokens=500,
        )
    except (BudgetExceeded, LlmUnavailable, ValueError) as exc:
        log.info("intelligence plan fell back to keywords: %s", exc)
        return fallback_plan(question), 0.0, False

    plan = coerce_plan(parsed, question)
    session.add(QuestionCache(
        question_hash=key, question=question,
        plan_json=json.dumps(plan.__dict__),
    ))
    session.commit()
    return plan, call.cost_usd, False


# --- 2 + 3. Live search, hydrate, write through -------------------------------

def _hydrate(items: list[FeedItem]) -> None:
    """Fill descriptions and dates for new finds from their own pages. Free."""
    thin = [i for i in items
            if not i.description or len(i.description) < MIN_DESCRIPTION_CHARS
            or i.published_at is None][:HYDRATE_LIMIT]
    if not thin:
        return

    def _one(item: FeedItem) -> None:
        with httpx.Client(
            timeout=settings.live_search_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            meta = fetch_meta(item.url, client)
        if meta.description and (not item.description
                                 or len(meta.description) > len(item.description)):
            item.description = meta.description
        if item.published_at is None:
            item.published_at = meta.published_at

    with ThreadPoolExecutor(max_workers=min(8, len(thin))) as pool:
        list(pool.map(_one, thin))


def write_through(results: list[SourceResult]) -> tuple[list[int], set[int], dict[str, int]]:
    """Persist live finds. Returns (canonical ids found, ids new to us, new per source).

    Everything a live search discovers joins the corpus, so the next question
    -- and the feed -- start from it rather than fetching it again.
    """
    items = [i for r in results for i in r.items]
    if not items:
        return [], set(), {}

    with session_scope() as s:
        known = set(s.scalars(
            select(Article.url).where(Article.url.in_([i.url for i in items]))
        ).all())

    fresh = [i for i in items if i.url not in known]
    _hydrate(fresh)

    found: list[int] = []
    new_ids: set[int] = set()
    new_per_source: dict[str, int] = {}
    with session_scope() as s:
        for item in items:
            outcome = store_item(s, item)
            s.flush()
            article = s.scalars(
                select(Article).where(Article.url == item.url).limit(1)
            ).first()
            if article is None:
                continue
            target = article.canonical_id or article.id
            if target not in found:
                found.append(target)
            if outcome == "new":
                new_ids.add(article.id)
                new_per_source[item.source] = new_per_source.get(item.source, 0) + 1
    return found, new_ids, new_per_source


# --- 5. Answer -----------------------------------------------------------------

_LABELS = {s.name: s.label for s in load_sources()}


def _passage(n: int, e: corpus.Evidence) -> str:
    date = e.published_at.strftime("%d %b %Y") if e.published_at else "undated"
    meta = " · ".join(filter(None, [_LABELS.get(e.source, e.source), date,
                                    e.company, e.category]))
    return f"[{n}] {meta}\n{e.text[:700]}"


def answer_prompt(question: str, evidence: list[corpus.Evidence]) -> str:
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    passages = "\n\n".join(_passage(n, e) for n, e in enumerate(evidence, 1))
    return (
        f"Today is {today}.\n\nPassages, newest first:\n\n{passages}\n\n"
        f"Question: {question}\n\n"
        "Answer in at most six short paragraphs or bullet points. Cite the "
        "passage numbers for every claim."
    )


_CITE = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")

# gpt-oss cites in its own house style -- 【9】 or 【9†L3-L5】 -- whatever the
# prompt asks for. Seen on the first live run: every marker was missed.
_ODD_CITE = re.compile(r"【\s*(\d+(?:\s*[,–-]\s*\d+)*)\s*(?:†[^】]*)?】")


def normalise_citations(answer: str) -> str:
    """Rewrite any bracket style the model used into plain [n]."""
    return _ODD_CITE.sub(lambda m: f"[{m.group(1)}]", answer or "")


def cited_numbers(answer: str) -> set[int]:
    """Passage numbers an answer refers to: [3], [1][4], [2, 5], [2-4]."""
    found: set[int] = set()
    for group in _CITE.findall(answer or ""):
        for part in re.split(r"\s*,\s*", group):
            bounds = re.split(r"\s*[–-]\s*", part)
            try:
                lo, hi = int(bounds[0]), int(bounds[-1])
            except ValueError:
                continue
            if hi - lo <= 20:
                found.update(range(lo, hi + 1))
    return found


# --- Endpoint ---------------------------------------------------------------

@router.post("/intelligence", response_model=IntelligenceResponse)
def intelligence(
    request: IntelligenceRequest, session: Session = Depends(get_session)
) -> IntelligenceResponse:
    question = " ".join(request.question.split())
    response = IntelligenceResponse(question=question)
    if not question:
        response.error = "Ask a question first."
        return response

    plan, plan_cost, response.plan_cached = compile_plan(session, question)
    response.search, response.terms = plan.search, plan.terms
    response.since_days = plan.since_days
    response.cost_usd = plan_cost

    live_ids: list[int] = []
    new_ids: set[int] = set()
    if request.live and plan.search:
        results = search_live(plan.search, plan.terms)
        try:
            live_ids, new_ids, new_per_source = write_through(results)
        except Exception as exc:  # noqa: BLE001 - never lose the corpus answer
            log.error("intelligence write-through failed: %s", exc)
            new_per_source = {}
        response.sources = [
            SourceStatus(
                name=r.name, label=r.label, kind=r.kind, status=r.status,
                found=len(r.items), new=new_per_source.get(r.name, 0),
                cached=r.cached, error=r.error,
            )
            for r in results
        ]
        session.expire_all()   # see what write-through just committed

    evidence = corpus.search(
        session, plan.terms, entity=plan.search, limit=MAX_EVIDENCE,
        since_days=plan.since_days, include_ids=live_ids,
    )

    response.citations = [
        Citation(
            n=n, article_id=e.article_id, chunk_id=e.chunk_id,
            headline=e.headline, url=e.url, source=e.source,
            published_at=e.published_at, company=e.company,
            category=e.category, snippet=e.text[:400], cited=False,
            discovered=e.article_id in new_ids,
        )
        for n, e in enumerate(evidence, 1)
    ]

    if not evidence:
        response.answer = (
            "Nothing in the six sources matches that question"
            + (" in the time window asked about." if plan.since_days else ".")
        )
        response.budget_remaining = get_spend(session).remaining_total
        return response

    try:
        result = invoke(
            FEATURE_ANSWER, answer_prompt(question, evidence),
            system=ANSWER_SYSTEM, premium=request.premium, max_tokens=1500,
        )
    except BudgetExceeded as exc:
        response.error = f"{exc}. The matching stories are listed below."
        return response
    except LlmUnavailable as exc:
        log.error("intelligence: %s", exc)
        response.error = ("The model is unreachable right now. The matching "
                          "stories are listed below.")
        response.budget_remaining = get_spend(session).remaining_total
        return response

    response.answer = normalise_citations(result.text) or "No answer came back."
    response.model = result.model
    response.cost_usd = round(plan_cost + result.cost_usd, 6)

    cited = cited_numbers(response.answer)
    for citation in response.citations:
        citation.cited = citation.n in cited

    response.budget_remaining = get_spend(session).remaining_total
    return response
