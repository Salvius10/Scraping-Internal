"""Scheduler tests. Offline: the refresh itself is replaced by a stub.

The two properties that matter on a $7 budget: a restart must not trigger a
paid run when the feed is fresh, and two runs must never overlap.
"""

from __future__ import annotations

from datetime import timedelta

from app import scheduler
from app.config import settings
from app.models import utcnow

H = timedelta(hours=settings.refresh_hours)


def test_never_refreshed_runs_now() -> None:
    now = utcnow()
    assert scheduler.next_due(None, now) == now


def test_overdue_feed_runs_now() -> None:
    now = utcnow()
    assert scheduler.next_due(now - H - timedelta(hours=3), now) == now


def test_fresh_feed_waits_for_the_interval() -> None:
    """A server restart two hours after a refresh must not pay for another."""
    now = utcnow()
    last = now - timedelta(hours=2)
    assert scheduler.next_due(last, now) == last + H


def test_freshness_allows_a_slightly_early_fire() -> None:
    now = utcnow()
    assert scheduler.is_fresh(now - timedelta(hours=1), now)
    assert not scheduler.is_fresh(now - H + timedelta(minutes=5), now)
    assert not scheduler.is_fresh(None, now)


def _stub_refresh(monkeypatch, calls: list) -> None:
    import app.ingest.pipeline as pipeline

    def fake_refresh(*args, **kwargs):
        calls.append(1)
        return pipeline.RefreshResult()

    monkeypatch.setattr(pipeline, "refresh", fake_refresh)


def test_job_skips_when_something_else_just_refreshed(monkeypatch) -> None:
    calls: list = []
    _stub_refresh(monkeypatch, calls)
    monkeypatch.setattr(scheduler, "last_refresh", lambda: utcnow())
    scheduler.refresh_job()
    assert calls == []


def test_job_runs_when_stale(monkeypatch) -> None:
    calls: list = []
    _stub_refresh(monkeypatch, calls)
    monkeypatch.setattr(scheduler, "last_refresh", lambda: utcnow() - H * 2)
    scheduler.refresh_job()
    assert calls == [1]


def test_job_never_overlaps(monkeypatch) -> None:
    calls: list = []
    _stub_refresh(monkeypatch, calls)
    monkeypatch.setattr(scheduler, "last_refresh", lambda: None)
    assert scheduler._run_lock.acquire(blocking=False)
    try:
        scheduler.refresh_job()     # a run is "in progress" -- must skip
    finally:
        scheduler._run_lock.release()
    assert calls == []


def test_job_survives_a_failing_refresh(monkeypatch) -> None:
    import app.ingest.pipeline as pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("source exploded")

    monkeypatch.setattr(pipeline, "refresh", boom)
    monkeypatch.setattr(scheduler, "last_refresh", lambda: None)
    scheduler.refresh_job()                       # must not raise
    assert scheduler._run_lock.acquire(blocking=False)
    scheduler._run_lock.release()


def test_background_scheduler_starts_with_the_job(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "last_refresh", lambda: utcnow())
    scheduler.start()
    try:
        due = scheduler.next_run_time()
        assert due is not None
        assert due > utcnow() + H - timedelta(minutes=1)
    finally:
        scheduler.shutdown()
    assert scheduler.next_run_time() is None


def test_tests_never_start_the_scheduler() -> None:
    """Starting the app under test must never kick off a real, paid ingest."""
    assert settings.scheduler_enabled is False
