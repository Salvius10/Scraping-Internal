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
    """Repair every drifted chunk. Free; returns the number rewritten."""
    return sum(
        sync_chunk(session, article)
        for article in session.scalars(select(Article)).all()
    )
