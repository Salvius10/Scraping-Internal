"""Manual "Refresh now" tests. Offline: the refreshes themselves are stubbed.

Protected: a manual refresh runs in the background and reports how it went; it
shares the scheduler's lock, so it never overlaps a scheduled run; a scope
refreshed within the cooldown is refused rather than paid for again; and the
"vcs" scope reads the VC firms only.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import scheduler
from app.ingest.vc_firms import VcResult
from app.main import app
from app.models import IngestRun, utcnow


def wait_until_done(timeout: float = 5.0) -> scheduler.ManualRun:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = scheduler.manual_status()
        if not run.running:
            return run
        time.sleep(0.02)
    raise AssertionError("manual refresh did not finish")


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    """Nothing refreshed recently, unless a test says otherwise."""
    monkeypatch.setattr(scheduler, "last_refresh", lambda: None)
    monkeypatch.setattr(scheduler, "_last_vc_read", lambda: None)
    yield
    wait_until_done()


def stub_feed(monkeypatch, gate: threading.Event | None = None, fail: bool = False):
    import app.ingest.pipeline as pipeline
    calls = []

    def fake_refresh(*args, **kwargs):
        calls.append("feed")
        if gate:
            gate.wait(5)
        if fail:
            raise RuntimeError("source exploded")
        run = IngestRun(source="entrackr", items_seen=5, items_new=3, items_duplicate=0)
        return pipeline.RefreshResult(runs=[run], vcs=VcResult())

    monkeypatch.setattr(pipeline, "refresh", fake_refresh)
    return calls


def test_feed_refresh_runs_in_background_and_reports(monkeypatch) -> None:
    calls = stub_feed(monkeypatch)
    assert scheduler.start_manual("feed") is None
    run = wait_until_done()
    assert calls == ["feed"]
    assert run.scope == "feed" and run.error is None
    assert "3 new stories" in run.summary and "0 new VC firm posts" in run.summary
    # The lock is released: the scheduled job can run afterwards.
    assert scheduler._run_lock.acquire(blocking=False)
    scheduler._run_lock.release()


def test_never_overlaps_a_running_refresh(monkeypatch) -> None:
    gate = threading.Event()
    calls = stub_feed(monkeypatch, gate)
    assert scheduler.start_manual("feed") is None
    assert "already running" in scheduler.start_manual("feed")
    assert "already running" in scheduler.start_manual("vcs")
    gate.set()
    wait_until_done()
    assert calls == ["feed"]


def test_refused_while_the_scheduled_job_runs(monkeypatch) -> None:
    calls = stub_feed(monkeypatch)
    assert scheduler._run_lock.acquire(blocking=False)     # the scheduler, mid-run
    try:
        assert "already running" in scheduler.start_manual("feed")
    finally:
        scheduler._run_lock.release()
    assert calls == []


def test_cooldown_refuses_a_recent_scope(monkeypatch) -> None:
    calls = stub_feed(monkeypatch)
    monkeypatch.setattr(scheduler, "last_refresh", lambda: utcnow() - timedelta(minutes=2))
    reason = scheduler.start_manual("feed")
    assert reason and "Try again in 8 minutes" in reason
    assert calls == []


def test_failure_is_reported_not_raised(monkeypatch) -> None:
    stub_feed(monkeypatch, fail=True)
    assert scheduler.start_manual("feed") is None
    run = wait_until_done()
    assert run.summary is None and "source exploded" in run.error


def test_vcs_scope_reads_firms_only(monkeypatch) -> None:
    import app.ingest.vc_firms as vf
    feed = stub_feed(monkeypatch)
    monkeypatch.setattr(vf, "refresh_firms", lambda: VcResult(skipped=4))
    assert scheduler.start_manual("vcs") is None
    run = wait_until_done()
    assert feed == []
    assert "Refreshed VC firms: 0 new posts" in run.summary
    assert "4 Firecrawl reads skipped" in run.summary


def test_api_start_and_status(monkeypatch) -> None:
    stub_feed(monkeypatch)
    client = TestClient(app)
    started = client.post("/api/refresh", json={"scope": "feed"}).json()
    assert started["started"] is True
    wait_until_done()
    status = client.get("/api/refresh").json()
    assert status["running"] is False and status["summary"].startswith("Refreshed")

    unknown = client.post("/api/refresh", json={"scope": "everything"}).json()
    assert unknown["started"] is False and "Unknown refresh" in unknown["reason"]
