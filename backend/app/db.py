"""Engine and session factory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
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
    cur.execute("PRAGMA journal_mode=WAL")      # readers are not blocked by a run
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    # Wait rather than fail if another connection holds a brief write lock --
    # the ledger writes on its own connection while a run is storing rows.
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# Columns added to a table after it was first created. create_all() makes a
# missing table but never alters an existing one, so each is added here, once.
_ADDED_COLUMNS = [
    ("enriched_contacts", "company_page", "VARCHAR(1000)"),
    ("enriched_contacts", "company_site", "VARCHAR(1000)"),
    ("enriched_contacts", "maybe_other", "BOOLEAN NOT NULL DEFAULT 0"),
    ("enrichment_companies", "cleared_at", "DATETIME"),
]


def init_db() -> None:
    """Create any missing tables, and add any missing column listed above.
    Idempotent; nothing existing is changed or dropped."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            have = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if have and column not in have:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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


LIKE_ESCAPE = "\\"


def like_pattern(text: str) -> str:
    """A LIKE pattern matching `text` anywhere, with its own % and _ taken literally.

    Use with `column.ilike(like_pattern(q), escape=LIKE_ESCAPE)`. Without the
    escaping, a reader searching "100%" or "e_commerce" gets wildcard matches.
    """
    escaped = (text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
               .replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_"))
    return f"%{escaped}%"


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
