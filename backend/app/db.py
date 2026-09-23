"""Engine, session factory, and the FTS5 index that powers free corpus search.

FTS5 is an external-content table over `articles`, kept in sync by triggers.
This is what makes Intelligence retrieval cost $0 -- no embeddings, no vector
store, no per-query model call just to find candidates.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

engine = create_engine(
    settings.db_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")      # concurrent reads during ingest
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    # Wait rather than fail if another connection holds a brief write
    # lock -- the ledger writes on its own connection during ingest.
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# External-content FTS5 index plus the triggers that keep it current.
_FTS_SQL = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
        headline, description, company, summary,
        content='articles', content_rowid='id', tokenize='porter unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, headline, description, company, summary)
        VALUES (new.id, new.headline, new.description, new.company, new.summary);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, headline, description, company, summary)
        VALUES ('delete', old.id, old.headline, old.description, old.company, old.summary);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, headline, description, company, summary)
        VALUES ('delete', old.id, old.headline, old.description, old.company, old.summary);
        INSERT INTO articles_fts(rowid, headline, description, company, summary)
        VALUES (new.id, new.headline, new.description, new.company, new.summary);
    END
    """,
]


def init_db() -> None:
    """Create tables, the FTS index, and its sync triggers. Idempotent."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in _FTS_SQL:
            conn.execute(text(stmt))


def rebuild_fts() -> None:
    """Repair the index if it ever drifts from `articles`."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')"))


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
