"""Intelligence via Firecrawl. Offline: Firecrawl and the model are mocked.

What is protected: a search never scrapes and never calls the model; a
scrape happens only for the page asked for; Firecrawl failures become
readable errors; credits and budget are never spent twice for the same
thing; and a spent LLM budget still returns the page content.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import intelligence as intel
from app.config import settings
from app.llm.bedrock import LlmResult
from app.llm.budget import BudgetExceeded, get_spend
from app.main import app
from app.search import firecrawl as fc

_REAL_CLIENT = httpx.Client

SEARCH_OK = {
    "success": True,
    "creditsUsed": 1,
    "data": {"web": [
        {"url": "https://www.fundnews.test/qzorbit-raises-40m", "title": "Qzorbit raises $40M",
         "description": "The logistics startup closed a Series B.", "position": 1},
        {"url": "https://blog.example.test/list", "title": "",
         "description": "", "metadata": {"title": "Funded startups list"}},
        {"url": "javascript:alert(1)", "title": "bad"},
    ]},
}

SCRAPE_OK = {
    "success": True,
    "creditsUsed": 1,
    "data": {
        "markdown": "# Qzorbit raises $40M\n\nLed by Acme Ventures. " + "Detail. " * 50,
        "metadata": {"title": ["Qzorbit raises $40M"], "description": "Series B news",
                     "url": "https://www.fundnews.test/qzorbit-raises-40m", "statusCode": 200},
    },
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(settings, "firecrawl_api_key", "fc-test")
    monkeypatch.setattr(settings, "firecrawl_api_url", "https://api.firecrawl.dev")
    fc.clear_cache()
    intel.clear_cache()
    yield
    fc.clear_cache()
    intel.clear_cache()


def serve(monkeypatch, handler) -> list[httpx.Request]:
    """Route Firecrawl calls through an in-memory transport; record requests."""
    seen: list[httpx.Request] = []

    def recording(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(fc, "_client",
                        lambda: _REAL_CLIENT(transport=httpx.MockTransport(recording)))
    return seen


def ok(payload):
    return lambda request: httpx.Response(200, json=payload)


# --- Steering -------------------------------------------------------------------

@pytest.mark.parametrize("query, expected", [
    ("Zepto", "Zepto startup news"),
    ("quick commerce in India", "quick commerce in India startup news"),
    ("Zepto funding", "Zepto funding"),
    ("startups that raised Series B", "startups that raised Series B"),
    ("  Ola   IPO ", "Ola IPO"),
])
def test_queries_are_steered_toward_startup_news(query, expected) -> None:
    assert fc.steer(query) == expected


# --- Search ---------------------------------------------------------------------------

def test_search_returns_results_without_scraping(monkeypatch) -> None:
    seen = serve(monkeypatch, ok(SEARCH_OK))
    found = fc.search("Qzorbit")

    [request] = seen
    body = json.loads(request.content)
    assert request.url.path == "/v2/search"
    assert request.headers["authorization"] == "Bearer fc-test"
    assert body["query"] == "Qzorbit startup news"
    assert "scrapeOptions" not in body          # results only, no page fetched
    assert [r.n for r in found.results] == [1, 2]            # bad URL dropped
    assert found.results[0].domain == "fundnews.test"
    assert found.results[1].title == "Funded startups list"  # metadata fallback
    assert found.credits_used == 1


def test_repeat_search_is_cached(monkeypatch) -> None:
    seen = serve(monkeypatch, ok(SEARCH_OK))
    fc.search("Qzorbit")
    again = fc.search("  qzorbit ")
    assert len(seen) == 1 and again.cached and again.credits_used == 0


@pytest.mark.parametrize("status, fragment", [
    (401, "rejected the API key"),
    (402, "credits are used up"),
    (429, "rate limit"),
    (500, "could not do that"),
])
def test_firecrawl_errors_are_readable(monkeypatch, status, fragment) -> None:
    serve(monkeypatch, lambda r: httpx.Response(status, json={"success": False, "error": "x"}))
    with pytest.raises(fc.FirecrawlError, match=fragment):
        fc.search("Qzorbit")


def test_missing_key_is_explained(monkeypatch) -> None:
    monkeypatch.setattr(settings, "firecrawl_api_key", None)
    seen = serve(monkeypatch, ok(SEARCH_OK))
    with pytest.raises(fc.FirecrawlError, match="FIRECRAWL_API_KEY"):
        fc.search("Qzorbit")
    assert seen == []


def test_self_hosted_firecrawl_needs_no_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "firecrawl_api_key", None)
    monkeypatch.setattr(settings, "firecrawl_api_url", "http://localhost:3002")
    seen = serve(monkeypatch, ok(SEARCH_OK))
    fc.search("Qzorbit")
    assert "authorization" not in seen[0].headers


def test_timeouts_are_readable(monkeypatch) -> None:
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    serve(monkeypatch, slow)
    with pytest.raises(fc.FirecrawlError, match="too long"):
        fc.search("Qzorbit")


@pytest.mark.parametrize("query", ["", " ", "x" * 401])
def test_query_length_is_bounded(monkeypatch, query) -> None:
    seen = serve(monkeypatch, ok(SEARCH_OK))
    with pytest.raises(fc.FirecrawlError):
        fc.search(query)
    assert seen == []


# --- Scrape ------------------------------------------------------------------------------

def test_scrape_fetches_one_page_as_markdown(monkeypatch) -> None:
    seen = serve(monkeypatch, ok(SCRAPE_OK))
    page, cached = fc.scrape("https://www.fundnews.test/qzorbit-raises-40m")

    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v2/scrape"
    assert body["formats"] == ["markdown"] and body["onlyMainContent"] is True
    assert page.title == "Qzorbit raises $40M"          # list-valued metadata handled
    assert page.markdown.startswith("# Qzorbit") and not cached


def test_rescrape_is_cached(monkeypatch) -> None:
    seen = serve(monkeypatch, ok(SCRAPE_OK))
    fc.scrape("https://www.fundnews.test/qzorbit-raises-40m")
    _, cached = fc.scrape("https://www.fundnews.test/qzorbit-raises-40m")
    assert cached and len(seen) == 1


def test_empty_pages_and_bad_schemes_are_refused(monkeypatch) -> None:
    serve(monkeypatch, ok({"success": True, "data": {"markdown": "", "metadata": {}}}))
    with pytest.raises(fc.FirecrawlError, match="empty"):
        fc.scrape("https://blank.test/")
    with pytest.raises(fc.FirecrawlError, match="http"):
        fc.scrape("file:///etc/passwd")


# --- Endpoints -----------------------------------------------------------------------------

def _stub_model(monkeypatch, outcome):
    calls = []

    def fake_invoke(feature, prompt, **kw):
        calls.append((feature, prompt))
        if isinstance(outcome, Exception):
            raise outcome
        return LlmResult(outcome, settings.model_cheap, 1500, 200, 0.00035)

    monkeypatch.setattr(intel, "invoke", fake_invoke)
    return calls


def test_search_endpoint_never_calls_the_model(monkeypatch) -> None:
    serve(monkeypatch, ok(SEARCH_OK))
    calls = _stub_model(monkeypatch, "unused")
    with TestClient(app) as client:
        body = client.post("/api/intelligence/search", json={"query": "Qzorbit"}).json()
    assert body["error"] is None and len(body["results"]) == 2
    assert body["searched"] == "Qzorbit startup news"
    assert calls == []


def test_search_endpoint_reports_firecrawl_errors(monkeypatch) -> None:
    serve(monkeypatch, lambda r: httpx.Response(402, json={"success": False}))
    with TestClient(app) as client:
        body = client.post("/api/intelligence/search", json={"query": "Qzorbit"}).json()
    assert "credits are used up" in body["error"] and body["results"] == []


def test_scrape_endpoint_summarises_for_the_query(monkeypatch, clean_ledger) -> None:
    serve(monkeypatch, ok(SCRAPE_OK))
    calls = _stub_model(monkeypatch, "- Qzorbit raised $40M, led by Acme Ventures.")
    with TestClient(app) as client:
        body = client.post("/api/intelligence/scrape", json={
            "url": "https://www.fundnews.test/qzorbit-raises-40m",
            "query": "Qzorbit funding"}).json()

    assert body["summary"].startswith("- Qzorbit raised")
    assert body["title"] == "Qzorbit raises $40M"
    assert body["content"].startswith("# Qzorbit") and body["credits_used"] == 1
    assert body["cost_usd"] == pytest.approx(0.00035)
    [(feature, prompt)] = calls
    assert feature == intel.FEATURE and "Qzorbit funding" in prompt


def test_same_page_and_query_is_never_paid_twice(monkeypatch) -> None:
    seen = serve(monkeypatch, ok(SCRAPE_OK))
    calls = _stub_model(monkeypatch, "- summary")
    payload = {"url": "https://www.fundnews.test/qzorbit-raises-40m", "query": "Qzorbit"}
    with TestClient(app) as client:
        client.post("/api/intelligence/scrape", json=payload)
        again = client.post("/api/intelligence/scrape", json=payload).json()
    assert len(seen) == 1 and len(calls) == 1
    assert again["cached"] and again["credits_used"] == 0 and again["cost_usd"] == 0


def test_spent_budget_still_returns_the_page(monkeypatch) -> None:
    serve(monkeypatch, ok(SCRAPE_OK))
    _stub_model(monkeypatch, BudgetExceeded("daily cap reached", get_spend()))
    with TestClient(app) as client:
        body = client.post("/api/intelligence/scrape", json={
            "url": "https://www.fundnews.test/qzorbit-raises-40m"}).json()
    assert body["summary"] is None and "daily cap reached" in body["error"]
    assert body["content"].startswith("# Qzorbit")


def test_long_pages_are_capped_before_the_model() -> None:
    page = fc.Page(url="https://x.test", title="T", description="",
                   markdown="word " * 20_000)
    prompt = intel.summary_prompt(page, None)
    assert len(prompt) < intel.MAX_PAGE_CHARS + 500 and "(truncated)" in prompt


def test_markdown_snippets_become_plain_text() -> None:
    """Seen live: '# Find Top Startups ## Track new startups funded by [top inv...'."""
    raw = "# Find Top Startups\n## Track new startups funded by [top investors](https://x.test) | **Series A**"
    assert fc.plain(raw) == "Find Top Startups Track new startups funded by top investors Series A"
    assert fc.plain("word " * 200).endswith("…") and len(fc.plain("word " * 200)) <= 321
