"""Production hardening that is not specific to one stage.

Protected: the SPA route never serves a file outside dist/; every response
carries the security headers and a 500 never leaks a traceback; budget
reservations hold headroom under concurrency; LIKE search takes wildcards
literally; the cross-process lock excludes a second holder; caches stay bounded.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.cache import TTLCache
from app.config import settings
from app.db import like_pattern
from app.llm import budget
from app.proclock import ProcessLock

client = TestClient(main.app, raise_server_exceptions=False)


# --- Static files ------------------------------------------------------------------

def test_spa_never_serves_a_file_outside_dist(tmp_path, monkeypatch) -> None:
    dist = (tmp_path / "dist").resolve()
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "robots.txt").write_text("ok")
    (tmp_path / "secret.env").write_text("KEY=1")
    monkeypatch.setattr(main, "FRONTEND_DIST", dist)

    assert main._dist_file("robots.txt") == dist / "robots.txt"
    for path in ("../secret.env", "..\\secret.env", "assets/../../secret.env",
                 str(tmp_path / "secret.env"), "", "assets", "missing.js"):
        assert main._dist_file(path) is None, path


@pytest.mark.skipif(not main.FRONTEND_DIST.is_dir(), reason="frontend not built")
def test_encoded_traversal_gets_the_app_not_the_file() -> None:
    for path in ("/..%2F..%2F.env.example", "/%2e%2e/%2e%2e/.env.example",
                 "/..%5C..%5C.env.example"):
        response = client.get(path)
        assert "AWS_" not in response.text, path


@pytest.mark.skipif(not main.FRONTEND_DIST.is_dir(), reason="frontend not built")
def test_unknown_api_route_is_404_not_the_app() -> None:
    response = client.get("/api/no-such-route")
    assert response.status_code == 404
    assert "<html" not in response.text.lower()


def test_responses_carry_security_headers() -> None:
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_an_unhandled_error_is_a_plain_500(monkeypatch) -> None:
    from app.api import status

    def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(status, "get_spend", boom)
    response = client.get("/api/status")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}


# --- Budget ------------------------------------------------------------------------

def test_a_reservation_holds_headroom_until_released(monkeypatch, clean_ledger) -> None:
    monkeypatch.setattr(settings, "total_usd_cap", 0.010)
    monkeypatch.setattr(settings, "daily_usd_cap", 0.010)

    with budget.reserve(0.006):
        assert budget.in_flight_usd() == pytest.approx(0.006)
        with pytest.raises(budget.BudgetExceeded, match="calls under way"):
            budget.ensure_budget(0.006)
    assert budget.in_flight_usd() == 0
    budget.ensure_budget(0.006)


def test_a_failed_call_releases_its_reservation(clean_ledger) -> None:
    with pytest.raises(RuntimeError):
        with budget.reserve(0.001):
            raise RuntimeError("bedrock down")
    assert budget.in_flight_usd() == 0


def test_a_call_priced_by_the_caller_is_ledgered_at_that_price(clean_ledger) -> None:
    cost = budget.record_call("guardrail_raw", "guardrail:x:1", 0, 0, cost_usd=0.00045)
    assert cost == 0.00045
    assert budget.get_spend().total_usd == pytest.approx(0.00045)


# --- Search -----------------------------------------------------------------------

def test_like_pattern_takes_wildcards_literally() -> None:
    assert like_pattern("100%") == "%100\\%%"
    assert like_pattern("e_com") == "%e\\_com%"


# --- Refresh locking ------------------------------------------------------------------

def test_process_lock_excludes_a_second_holder(tmp_path) -> None:
    path = tmp_path / "x.lock"
    first, second = ProcessLock(path), ProcessLock(path)
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


# --- Caches ------------------------------------------------------------------------

def test_ttl_cache_expires_and_stays_bounded() -> None:
    cache: TTLCache[int] = TTLCache(max_entries=3)
    cache.set("old", 1, ttl=0.01)
    time.sleep(0.02)
    assert cache.get("old") is None
    for n in range(5):
        cache.set(str(n), n, ttl=60)
    assert len(cache) == 3 and cache.get("0") is None and cache.get("4") == 4


def test_ttl_cache_is_safe_across_threads() -> None:
    cache: TTLCache[int] = TTLCache(max_entries=50)

    def work(base: int) -> None:
        for n in range(200):
            cache.set(f"{base}-{n}", n, ttl=60)
            cache.get(f"{base}-{n // 2}")

    threads = [threading.Thread(target=work, args=(b,)) for b in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(cache) <= 50
