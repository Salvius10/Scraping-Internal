"""Production hardening. Offline: no model, no Firecrawl, no Apify.

Protected: the SPA route never serves a file outside dist/ (it once served
`/..%2F..%2F.env`); every response carries the security headers; the guarded
fetcher re-checks the address at connect time (DNS rebinding); concurrent
model calls cannot jointly overshoot a cap; LIKE search takes % and _
literally; feed links are http(s) only; Search web runs one at a time and
survives a model that repeats or zero-numbers a result; a refresh is locked
across processes; describe holds no write lock while it fetches pages; and the
in-process caches stay bounded.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import extract as ex
from app import main, scheduler
from app.cache import TTLCache
from app.config import settings
from app.db import like_pattern, session_scope
from app.ingest import describe, web_rounds as wr
from app.ingest.rss import parse_feed
from app.llm import budget
from app.llm.bedrock import LlmResult
from app.models import Article, WebRound
from app.proclock import ProcessLock
from app.search import firecrawl as fc

client = TestClient(main.app)


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
def test_app_page_is_revalidated_so_a_deploy_is_picked_up() -> None:
    for path in ("/", "/some/client/route"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


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


def test_oversized_request_bodies_are_refused() -> None:
    response = client.post("/api/filter", json={"phrase": "x" * 5000})
    assert response.status_code == 422


# --- Guarded fetching -----------------------------------------------------------------

def test_connect_time_check_refuses_a_rebound_address(monkeypatch) -> None:
    """A host that resolved public for check_url but private at connect is refused."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    with pytest.raises(ex.ExtractError, match="private or local"):
        ex._PublicOnlyBackend().connect_tcp("rebind.test", 443, timeout=1)


def test_connect_goes_to_the_checked_address(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    connected = []
    monkeypatch.setattr(ex.httpcore.SyncBackend, "connect_tcp",
                        lambda self, host, port, **kw: connected.append(host))
    ex._PublicOnlyBackend().connect_tcp("news.test", 443, timeout=1)
    assert connected == ["93.184.216.34"]


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


def test_a_failed_call_releases_its_reservation(monkeypatch, clean_ledger) -> None:
    with pytest.raises(RuntimeError):
        with budget.reserve(0.001):
            raise RuntimeError("bedrock down")
    assert budget.in_flight_usd() == 0


# --- Search and links -----------------------------------------------------------------

def test_like_pattern_takes_wildcards_literally() -> None:
    assert like_pattern("100%") == "%100\\%%"
    assert like_pattern("e_com") == "%e\\_com%"
    assert like_pattern("a\\b") == "%a\\\\b%"


def test_feed_search_for_percent_does_not_match_everything() -> None:
    with session_scope() as s:
        s.add(Article(url="https://h.test/a", url_hash="h1", source="entrackr",
                      headline="Hardening test plain story"))
        s.add(Article(url="https://h.test/b", url_hash="h2", source="entrackr",
                      headline="Hardening test grows 100% in a year"))
    try:
        hits = client.get("/api/articles", params={"q": "100%"}).json()["articles"]
        assert [a["url"] for a in hits] == ["https://h.test/b"]
    finally:
        with session_scope() as s:
            s.execute(delete(Article).where(Article.url.like("https://h.test/%")))


def test_feed_links_must_be_web_addresses() -> None:
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Good</title><link>https://ok.test/story</link></item>
      <item><title>Bad</title><link>javascript:alert(1)</link></item>
      <item><title>Data</title><link>data:text/html,hi</link></item>
    </channel></rss>"""
    assert [i.url for i in parse_feed(rss, "t")] == ["https://ok.test/story"]


# --- Search web ---------------------------------------------------------------------

def _stub_search(monkeypatch, answers) -> None:
    results = [fc.SearchResult(n=1, url="https://a.test/one", title="Onefold raises seed",
                               description="", domain="a.test", published="Sep 20, 2026")]

    def fake_search(query, limit=None, kind="news", tbs=None, steered=True):
        return fc.Search(query=query, searched=query, kind=kind, results=results,
                         credits_used=1)

    monkeypatch.setattr(wr.firecrawl, "search", fake_search)
    monkeypatch.setattr(wr, "invoke_json", lambda *a, **k: (
        answers, LlmResult("", settings.model_cheap, 10, 10, 0.0)))
    monkeypatch.setattr(wr, "utcnow", lambda: datetime(2026, 9, 25, 12, tzinfo=timezone.utc))


@pytest.fixture
def _web_rounds_clean():
    yield
    with session_scope() as s:
        s.execute(delete(WebRound))


def test_search_web_survives_a_repeated_answer(monkeypatch, _web_rounds_clean) -> None:
    seed = {"company": "Onefold", "round": "Seed", "stage": "Seed", "amount": None,
            "investors": []}
    _stub_search(monkeypatch, [{"i": 1, **seed}, {"i": 1, **seed}, {"i": 0, **seed}])
    summary = wr.search_web(date(2026, 9, 18), date(2026, 9, 25))
    assert summary.total_added == 1
    with session_scope() as s:
        assert len(s.scalars(select(WebRound)).all()) == 1


def test_search_web_runs_one_at_a_time() -> None:
    assert wr._search_lock.acquire(blocking=False)
    try:
        with pytest.raises(wr.WebSearchError, match="already running"):
            wr.search_web(date(2026, 9, 18), date(2026, 9, 25))
    finally:
        wr._search_lock.release()


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


def test_refresh_is_refused_while_another_process_holds_the_lock(monkeypatch) -> None:
    calls = []
    from app.ingest import pipeline
    monkeypatch.setattr(pipeline, "refresh", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(scheduler, "last_refresh", lambda: None)

    other = scheduler.refresh_lock()          # stands in for another process
    assert other.acquire()
    try:
        scheduler.refresh_job()
        assert calls == []
        assert "already running" in scheduler.start_manual("feed")
    finally:
        other.release()
    assert scheduler._run_lock.acquire(blocking=False)   # nothing left held
    scheduler._run_lock.release()


# --- Describe ----------------------------------------------------------------------

def test_describe_holds_no_write_lock_while_fetching(monkeypatch) -> None:
    with session_scope() as s:
        for n in (1, 2):
            s.add(Article(url=f"https://d.test/{n}", url_hash=f"d{n}",
                          source="vccircle", headline=f"Describe lock story {n}"))

    blocked = []

    def fake_fetch(url, client):
        # Another writer must get in at once while a page is being fetched.
        conn = sqlite3.connect(str(settings.db_path), timeout=0.2)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        except sqlite3.OperationalError:
            blocked.append(url)
        finally:
            conn.close()
        return describe.PageMeta(description="A publisher description long enough "
                                             "to replace the missing one.")

    monkeypatch.setattr(describe, "fetch_meta", fake_fetch)
    monkeypatch.setattr(settings, "per_domain_delay", 0)
    try:
        assert describe.describe_pending() >= 2
        assert blocked == []
    finally:
        with session_scope() as s:
            s.execute(delete(Article).where(Article.url.like("https://d.test/%")))


def test_double_encoded_meta_description_is_decoded() -> None:
    page = ('<meta property="og:description" '
            'content="Sweden&amp;#39;s EQT&amp;nbsp;invests &amp;amp; grows">')
    assert describe.extract_meta(page).description == "Sweden's EQT invests & grows"


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
