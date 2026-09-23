"""Test setup.

Points the app at a throwaway database *before* anything imports settings, so
tests can never read or write the real ledger.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

_TMP_DB = Path(tempfile.gettempdir()) / "isn_test_news.db"
os.environ["DB_PATH"] = str(_TMP_DB)
os.environ["LLM_DRY_RUN"] = "false"

import pytest  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import LlmCall  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    for suffix in ("", "-wal", "-shm"):
        Path(str(_TMP_DB) + suffix).unlink(missing_ok=True)
    init_db()
    yield
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(_TMP_DB) + suffix).unlink(missing_ok=True)


@pytest.fixture
def clean_ledger():
    """Empty the spend ledger before and after a test."""
    from app.db import session_scope

    def _wipe():
        with session_scope() as s:
            s.query(LlmCall).delete()

    _wipe()
    yield
    _wipe()
