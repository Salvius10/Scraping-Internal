"""Corpus retrieval over the FTS5 index. Free: no embeddings, no model call.

Returns *evidence* -- a stored chunk plus the article it belongs to -- because
an Intelligence answer cites chunks, so every claim resolves to specific text
we hold rather than a bare link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from ..models import Article, Chunk, utcnow

# Column weights for bm25(): headline, description, company, summary. A match
# on the company or headline says far more than one buried in a description.
_BM25 = "bm25(articles_fts, 3.0, 1.0, 4.0, 1.0)"

# Words that carry no search signal. Kept short on purpose: FTS5's porter
# stemmer already folds "raises"/"raised", so only true filler goes here.
_STOPWORDS = frozenset("""
    a an and are about any as at be by did do does for from has have how i in
    into is it its latest me news of on or recent recently show tell than that
    the their them there these this those to was were what when where which
    who whom why will with week weeks today month months year years last new
    anything everything story stories give list please india indian
""".split())


@dataclass
class Evidence:
    """One citable passage."""

    article_id: int
    chunk_id: int | None
    headline: str
    url: str
    source: str
    published_at: datetime | None
    company: str | None
    category: str | None
    text: str


def keywords(text_: str) -> list[str]:
    """Lowercase search tokens from free text, filler removed, order kept."""
    seen: list[str] = []
    for token in re.findall(r"[a-z0-9]+", (text_ or "").lower()):
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
    return seen


def fts_query(terms: list[str], must: list[str] | None = None) -> str | None:
    """An FTS5 MATCH expression that is safe to run whatever the input.

    Every token is double-quoted, so FTS5 operators and punctuation in a
    reader's question ("M&A", "AND", "Series-B") can never become syntax.
    `terms` are OR-ed; every token of `must` is required.
    """
    required = keywords(" ".join(must or []))
    optional = [t for t in keywords(" ".join(terms)) if t not in required]
    parts = [f'"{t}"' for t in required]
    if optional:
        parts.append("(" + " OR ".join(f'"{t}"' for t in optional) + ")")
    if not parts:
        return None
    if not required:
        return " OR ".join(f'"{t}"' for t in optional)
    return " AND ".join(parts)


def search(
    session: Session,
    terms: list[str],
    *,
    entity: str | None = None,
    limit: int = 12,
    since_days: int | None = None,
    include_ids: list[int] | None = None,
    reserve: int = 4,
) -> list[Evidence]:
    """Rank the corpus for `terms` and return canonical evidence.

    `entity` is what the question is about ("Zepto"). Stories naming it rank
    ahead of stories that merely share vocabulary: asked about Zepto's IPO,
    OR-ing "ipo, listing, stock, market" alone ranked other companies' IPOs
    first. Tiers, best first: entity plus a term, entity alone, any term.

    `include_ids` are articles the live search just found; up to `reserve` of
    them are guaranteed a place even if bm25 would rank them lower, because a
    site's own search engine already judged them relevant.
    """
    must = [entity] if entity and keywords(entity) else []
    tiers = [fts_query(terms, must), fts_query([], must)] if must else []
    tiers.append(fts_query([*terms, *(must or [])]))

    ranked: list[int] = []
    for query in dict.fromkeys(q for q in tiers if q):
        rows = session.execute(
            text(
                f"SELECT a.id, a.canonical_id FROM articles_fts "
                f"JOIN articles a ON a.id = articles_fts.rowid "
                f"WHERE articles_fts MATCH :q ORDER BY {_BM25} LIMIT :n"
            ),
            {"q": query, "n": limit * 4},
        ).all()
        for article_id, canonical_id in rows:
            # A duplicate's hit counts for the story it was folded into.
            target = canonical_id or article_id
            if target not in ranked:
                ranked.append(target)

    extra = [i for i in (include_ids or []) if i not in ranked][:reserve]

    stmt = select(Article).where(Article.id.in_(ranked + extra))
    if since_days:
        since = utcnow() - timedelta(days=since_days)
        stmt = stmt.where(
            or_(Article.published_at.is_(None), Article.published_at >= since)
        )
    articles = {a.id: a for a in session.scalars(stmt).all()}

    # Keep bm25 order, leave room for the live finds, then append them.
    chosen = [i for i in ranked if i in articles][: max(0, limit - len(extra))]
    chosen += [i for i in extra if i in articles and i not in chosen]

    chunks = {
        c.article_id: c
        for c in session.scalars(
            select(Chunk).where(Chunk.article_id.in_(chosen),
                                Chunk.chunk_index == 0)
        ).all()
    }

    evidence = []
    for article_id in chosen:
        article = articles[article_id]
        chunk = chunks.get(article_id)
        evidence.append(Evidence(
            article_id=article.id,
            chunk_id=chunk.id if chunk else None,
            headline=article.headline,
            url=article.url,
            source=article.source,
            published_at=article.published_at,
            company=article.company,
            category=article.category.value if article.category else None,
            text=chunk.text if chunk else article.headline,
        ))

    # Newest first: questions about this ecosystem are nearly always "what is
    # the latest", and a chronological list lets the model see sequence.
    evidence.sort(
        key=lambda e: e.published_at.timestamp() if e.published_at else 0.0,
        reverse=True,
    )
    return evidence
