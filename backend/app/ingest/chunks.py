"""Citation chunks -- the stored text an Intelligence answer points back to.

A citation is only honest if the passage it names is the text the model was
shown. Chunks are written at ingest, but `describe.py` later replaces thin
feed descriptions with the publisher's own, which left chunks quoting the old
text. Everything that changes a description calls `sync_chunk`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Article, Chunk


def chunk_body(headline: str, description: str | None) -> str:
    """The passage stored for one article: headline, then the description."""
    if description:
        return f"{headline}\n\n{description}"
    return headline


def sync_chunk(session: Session, article: Article) -> bool:
    """Make the article's first chunk match its current text. True if changed."""
    body = chunk_body(article.headline, article.description)
    chunk = session.scalars(
        select(Chunk)
        .where(Chunk.article_id == article.id, Chunk.chunk_index == 0)
        .limit(1)
    ).first()
    if chunk is None:
        session.add(Chunk(article_id=article.id, chunk_index=0, text=body))
        return True
    if chunk.text != body:
        chunk.text = body
        return True
    return False


def sync_all_chunks(session: Session) -> int:
    """Repair every drifted chunk. Free; returns the number rewritten.

    Runs on every refresh over the whole corpus, so it reads all first chunks
    in one query rather than one query per article.
    """
    first: dict[int, Chunk] = {}
    for c in session.scalars(
            select(Chunk).where(Chunk.chunk_index == 0).order_by(Chunk.id)).all():
        first.setdefault(c.article_id, c)
    changed = 0
    for article_id, headline, description in session.execute(
            select(Article.id, Article.headline, Article.description)).all():
        body = chunk_body(headline, description)
        chunk = first.get(article_id)
        if chunk is None:
            session.add(Chunk(article_id=article_id, chunk_index=0, text=body))
            changed += 1
        elif chunk.text != body:
            chunk.text = body
            changed += 1
    return changed
