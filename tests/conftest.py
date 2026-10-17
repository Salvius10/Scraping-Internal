"""Test setup.

Points the app at a throwaway database *before* anything imports settings, so
tests can never read or write the real ledger. Every paid service is switched
off: no test reaches Bedrock, Apify or Firecrawl.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

_TMP_DB = Path(tempfile.gettempdir()) / "gps_test_pipeline.db"
os.environ["DB_PATH"] = str(_TMP_DB)
os.environ["LLM_DRY_RUN"] = "false"
# Starting the app in a test must never kick off a real, paid run.
os.environ["SCHEDULER_ENABLED"] = "false"
# Nor reach a paid service: tests that need a credential set a fake one.
os.environ["APIFY_API_TOKEN"] = ""
os.environ["FIRECRAWL_API_KEY"] = ""
os.environ["GUARDRAILS_ENABLED"] = "false"
os.environ["GUARDRAIL_ID"] = ""
os.environ["AWS_BEARER_TOKEN_BEDROCK"] = ""

import pytest  # noqa: E402

from app.db import engine, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    BlockedItem, FundedStartup, GuardrailVerdict, LlmCall, PipelineRun, SeenItem, SourceRun,
    StartupEvent, UserSource,
)


@pytest.fixture(scope="session", autouse=True)
def _database():
    for suffix in ("", "-wal", "-shm"):
        Path(str(_TMP_DB) + suffix).unlink(missing_ok=True)
    init_db()
    yield
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(_TMP_DB) + suffix).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _no_real_bedrock(monkeypatch):
    """A test that forgets to stub the model fails loudly instead of calling AWS."""
    from app.llm import bedrock

    def refuse():
        raise AssertionError("a test tried to build a real Bedrock client")

    monkeypatch.setattr(bedrock, "get_client", refuse)


def wipe() -> None:
    with session_scope() as s:
        for model in (SourceRun, PipelineRun, BlockedItem, GuardrailVerdict, SeenItem,
                      FundedStartup, StartupEvent, LlmCall, UserSource):
            s.query(model).delete()


@pytest.fixture
def clean_db():
    """Empty every pipeline table and the ledger before and after a test."""
    wipe()
    yield
    wipe()


@pytest.fixture
def clean_ledger():
    """Empty the spend ledger before and after a test."""
    def _wipe():
        with session_scope() as s:
            s.query(LlmCall).delete()

    _wipe()
    yield
    _wipe()
