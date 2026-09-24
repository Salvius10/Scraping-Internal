"""User-directed extraction tests. Offline: no Bedrock, no real network.

The URL is untrusted input, so most of these pin the fetch guard; the rest
pin the spend rails (cap before any call, cache, one run at a time) and the
shapes ScrapeGraphAI hands back.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel

import app.extract as ex
import app.ingest.scraper_sgai as sg
from app.config import settings
from app.llm.budget import BudgetExceeded, record_call
from app.main import app

PUBLIC_IP = "93.184.216.34"
_REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Every hostname resolves to a public IP unless a test says otherwise."""
    def fake_getaddrinfo(host, port, *a, **kw):
        ip = {"internal.test": "10.1.2.3", "rebind.test": "127.0.0.1"}.get(
            host, host if host.replace(".", "").isdigit() else PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ex.clear_cache()
    yield
    ex.clear_cache()


def _serve(monkeypatch, handler) -> None:
    """Route fetch_page's HTTP through an in-memory transport."""
    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*a, **kw)

    monkeypatch.setattr(ex.httpx, "Client", client)


# --- URL guard -----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost.test:8000/", "http://10.0.0.8/",
    "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/",
    "http://internal.test/", "file:///etc/passwd", "ftp://example.test/",
    "http://user:pw@example.test/", "https://example.test:8443/",
])
def test_unsafe_urls_are_refused(url) -> None:
    with pytest.raises(ex.ExtractError):
        ex.check_url(url)


def test_bare_domains_get_https() -> None:
    assert ex.check_url("inc42.com/buzz/") == "https://inc42.com/buzz/"


def test_a_redirect_to_the_local_network_is_refused(monkeypatch) -> None:
    """A public page must not be able to bounce the server onto localhost."""
    def handler(request):
        return httpx.Response(302, headers={"location": "http://rebind.test/admin"})

    _serve(monkeypatch, handler)
    with pytest.raises(ex.ExtractError, match="private or local"):
        ex.fetch_page("https://news.test/story")


def test_public_redirects_are_followed(monkeypatch) -> None:
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, html="<p>moved here</p>")

    _serve(monkeypatch, handler)
    final, html = ex.fetch_page("https://news.test/old")
    assert final == "https://news.test/new" and "moved here" in html


def test_oversized_and_non_html_pages_are_refused(monkeypatch) -> None:
    _serve(monkeypatch, lambda r: httpx.Response(
        200, content=b"x" * (ex.MAX_BYTES + 1), headers={"content-type": "text/html"}))
    with pytest.raises(ex.ExtractError, match="3 MB"):
        ex.fetch_page("https://news.test/huge")

    _serve(monkeypatch, lambda r: httpx.Response(
        200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    with pytest.raises(ex.ExtractError, match="application/pdf"):
        ex.fetch_page("https://news.test/file.pdf")


# --- Cleaning ------------------------------------------------------------------------

def test_cleaning_strips_noise_and_absolutises_links() -> None:
    html = """<html><head><title>Deals</title><style>.a{}</style>
      <script>track()</script></head><body class="x" data-v="1">
      <a href="/deal/1" class="card" onclick="go()">Qzorbit raises $40M</a>
      <svg><path d="M0"/></svg><img src="/i.png" alt="logo" width="9"></body></html>"""
    cleaned, chars, truncated = ex.clean_html(html, "https://news.test/list")
    assert "track()" not in cleaned and "<svg" not in cleaned and ".a{}" not in cleaned
    assert "class=" not in cleaned and "onclick" not in cleaned
    assert 'href="https://news.test/deal/1"' in cleaned
    assert 'src="https://news.test/i.png"' in cleaned and 'alt="logo"' in cleaned
    assert cleaned.startswith("<h1>Deals</h1>")
    assert chars > 0 and truncated is False


def test_long_pages_are_capped() -> None:
    html = "<body>" + "<p>word </p>" * 40_000 + "</body>"
    cleaned, _, truncated = ex.clean_html(html, "https://news.test/")
    assert truncated and len(cleaned) <= ex.MAX_HTML_CHARS


# --- Result shapes ------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ({"content": [{"a": 1}]}, [{"a": 1}]),
    ({"content": '[{"a": 1}]'}, [{"a": 1}]),                # JSON left as a string
    ('```json\n{"content": {"total": 3}}\n```', {"total": 3}),
    ({"content": "The page lists three deals."}, "The page lists three deals."),
    ({"deals": [{"a": 1}], "count": 1}, {"deals": [{"a": 1}], "count": 1}),
])
def test_unwrap(raw, expected) -> None:
    assert ex.unwrap(raw) == expected


def test_rows_are_found_in_a_list_or_a_single_wrapped_list() -> None:
    assert ex.as_rows([{"a": 1}]) == [{"a": 1}]
    assert ex.as_rows({"deals": [{"a": 1}], "count": 1}) == [{"a": 1}]
    assert ex.as_rows({"x": [{"a": 1}], "y": [{"b": 2}]}) is None   # ambiguous
    assert ex.as_rows("text") is None


# --- Running: spend rails -------------------------------------------------------------

PAGE = "<html><body>" + "".join(
    f'<a href="/d/{i}">Qzorbit deal number {i} closes</a>' for i in range(5)) + "</body></html>"


class FakeLlm(FakeListChatModel):
    spent_usd: float = 0.0
    calls: int = 0

    def _call(self, messages, *a, **k):
        self.calls += 1
        self.spent_usd += 0.0004
        return super()._call(messages, *a, **k)


def _fake_model(monkeypatch, answer) -> FakeLlm:
    llm = FakeLlm(responses=[json.dumps(answer)] * 5)
    monkeypatch.setattr(sg, "make_llm", lambda feature="scrape": llm)
    monkeypatch.setattr(ex, "fetch_page", lambda url: ("https://news.test/deals", PAGE))
    return llm


def test_run_extracts_rows_and_reports_cost(monkeypatch, clean_ledger) -> None:
    llm = _fake_model(monkeypatch, {"content": [{"deal": "Qzorbit", "amount": "$40M"}]})
    result = ex.run("https://news.test/deals", "every deal with its amount")
    assert result.rows == [{"deal": "Qzorbit", "amount": "$40M"}]
    assert result.calls == 1 and result.cost_usd == pytest.approx(0.0004)
    assert result.final_url == "https://news.test/deals" and not result.cached
    assert llm.calls == 1


def test_same_question_is_served_from_cache_for_free(monkeypatch, clean_ledger) -> None:
    llm = _fake_model(monkeypatch, {"content": [{"deal": "Qzorbit"}]})
    ex.run("https://news.test/deals", "every deal")
    again = ex.run("https://NEWS.test/deals ", "Every   deal")
    assert again.cached and again.cost_usd == 0.0 and llm.calls == 1


def test_budget_is_checked_before_any_call(monkeypatch, clean_ledger) -> None:
    llm = _fake_model(monkeypatch, {"content": []})
    tokens_out = int(settings.total_usd_cap * 1.05
                     / settings.price_premium_out * 1_000_000) + 1
    record_call("test", settings.model_premium, 0, tokens_out)

    with pytest.raises(BudgetExceeded):
        ex.run("https://news.test/deals", "every deal")
    assert llm.calls == 0


def test_only_one_extraction_runs_at_a_time(monkeypatch) -> None:
    _fake_model(monkeypatch, {"content": []})
    assert ex._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(ex.ExtractError, match="Another extraction"):
            ex.run("https://news.test/deals", "every deal")
    finally:
        ex._run_lock.release()


def test_pages_without_text_are_refused_for_free(monkeypatch) -> None:
    llm = _fake_model(monkeypatch, {"content": []})
    monkeypatch.setattr(ex, "fetch_page",
                        lambda url: (url, "<body><div id='root'></div></body>"))
    with pytest.raises(ex.ExtractError, match="JavaScript"):
        ex.run("https://spa.test/", "everything")
    assert llm.calls == 0


@pytest.mark.parametrize("prompt", ["", "hi", "x" * 501])
def test_prompt_length_is_bounded(prompt) -> None:
    with pytest.raises(ex.ExtractError):
        ex.run("https://news.test/", prompt)


# --- Endpoint ---------------------------------------------------------------------------

def test_endpoint_returns_rows(monkeypatch, clean_ledger) -> None:
    _fake_model(monkeypatch, {"content": [{"deal": "Qzorbit"}]})
    with TestClient(app) as client:
        body = client.post("/api/extract", json={
            "url": "https://news.test/deals", "prompt": "every deal"}).json()
    assert body["error"] is None and body["rows"] == [{"deal": "Qzorbit"}]


def test_endpoint_reports_a_refused_url_as_an_error() -> None:
    with TestClient(app) as client:
        body = client.post("/api/extract", json={
            "url": "http://127.0.0.1/", "prompt": "everything"}).json()
    assert "private or local" in body["error"] and body["result"] is None
