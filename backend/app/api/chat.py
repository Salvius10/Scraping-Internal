"""Sidebar assistant: explain one story, or summarise what is on screen.

Both modes work from what is already in the database -- the headline, the
publisher's description, and the one-line summary written during enrichment.
Nothing here re-fetches an article or re-summarises text we have already paid
to summarise once.

`premium` routes to Sonnet 4.6 instead of gpt-oss. It is roughly 21x the cost,
so it is never the default and the caller has to ask for it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..llm.bedrock import LlmUnavailable, invoke
from ..llm.budget import BudgetExceeded, get_spend
from ..models import Article

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])

MAX_SUMMARY_ARTICLES = 40

SYSTEM_PROMPT = (
    "You help a reader work through Indian startup news. Be specific and "
    "factual. Use only what the reader gives you -- if something is not in "
    "the text, say so rather than filling the gap. No preamble, no sign-off."
)


class ChatRequest(BaseModel):
    mode: str = Field(description="explain | summarise | ask")
    article_id: int | None = None
    article_ids: list[int] = []
    question: str | None = None
    premium: bool = False


class ChatResponse(BaseModel):
    answer: str | None = None
    model: str | None = None
    cost_usd: float = 0.0
    error: str | None = None
    budget_remaining: float = 0.0


def _article_line(article: Article, index: int | None = None) -> str:
    prefix = f"[{index}] " if index is not None else ""
    parts = [f"{prefix}{article.headline}"]
    if article.company:
        parts.append(f"company: {article.company}")
    if article.category:
        parts.append(f"category: {article.category.value}")
    body = article.summary or article.description
    if body:
        parts.append(body[:280])
    return "\n    ".join(parts)


def _explain_prompt(article: Article, question: str | None) -> str:
    ask = question.strip() if question and question.strip() else (
        "Explain what happened and why it matters to someone tracking Indian "
        "startups. Three sentences at most."
    )
    return (
        f"Story:\n    {_article_line(article)}\n"
        f"    source: {article.source}\n\n{ask}"
    )


def _summarise_prompt(articles: list[Article], question: str | None) -> str:
    lines = [_article_line(a, i) for i, a in enumerate(articles, start=1)]
    ask = question.strip() if question and question.strip() else (
        "Summarise these stories in at most five bullet points. Group related "
        "ones. Lead with the largest deals. Cite the story numbers you used, "
        "like [3]."
    )
    return f"Stories currently on screen:\n" + "\n".join(lines) + f"\n\n{ask}"


@router.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest, session: Session = Depends(get_session)
) -> ChatResponse:
    remaining = get_spend(session).remaining_total

    if request.mode == "explain":
        if request.article_id is None:
            return ChatResponse(error="No story selected.",
                                budget_remaining=remaining)
        article = session.get(Article, request.article_id)
        if article is None:
            return ChatResponse(error="That story is no longer in the feed.",
                                budget_remaining=remaining)
        prompt = _explain_prompt(article, request.question)
        max_tokens = 500

    elif request.mode in ("summarise", "ask"):
        ids = request.article_ids[:MAX_SUMMARY_ARTICLES]
        if not ids:
            return ChatResponse(
                error="Nothing on screen to work with.",
                budget_remaining=remaining,
            )
        articles = list(session.scalars(
            select(Article).where(Article.id.in_(ids))
        ).all())
        # Preserve the order the reader sees.
        order = {article_id: i for i, article_id in enumerate(ids)}
        articles.sort(key=lambda a: order.get(a.id, 0))
        if not articles:
            return ChatResponse(error="Nothing on screen to work with.",
                                budget_remaining=remaining)
        prompt = _summarise_prompt(articles, request.question)
        max_tokens = 1200

    else:
        return ChatResponse(error=f"Unknown mode {request.mode!r}.",
                            budget_remaining=remaining)

    try:
        result = invoke(
            f"chat_{request.mode}", prompt,
            system=SYSTEM_PROMPT,
            premium=request.premium,
            max_tokens=max_tokens,
        )
    except BudgetExceeded as exc:
        return ChatResponse(error=str(exc), budget_remaining=0.0)
    except LlmUnavailable as exc:
        log.error("chat: %s", exc)
        return ChatResponse(
            error="The model is unreachable right now.",
            budget_remaining=remaining,
        )

    return ChatResponse(
        answer=result.text or "No answer came back.",
        model=result.model,
        cost_usd=result.cost_usd,
        budget_remaining=get_spend(session).remaining_total,
    )
