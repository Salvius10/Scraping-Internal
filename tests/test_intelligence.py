"""Intelligence tests. Offline -- live sites and the model are both stubbed.

What is protected: the plan the model returns is untrusted; a reader's words
can never become FTS5 syntax; live results are relevant, deduplicated and
written through; one slow site cannot stall an answer; every citation points
at a stored chunk; and a spent budget still returns the matching stories.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import intelligence as intel
from app.config import settings
from app.db import session_scope
from app.ingest.rss import FeedItem
from app.ingest.sources import LiveSearch, Source
from app.llm.bedrock import LlmResult
from app.llm.budget import BudgetExceeded, get_spend
from app.main import app
from app.models import Article, Chunk, QuestionCache, utcnow
from app.search import corpus, live

SOURCE = "test_intel"


@pytest.fixture
def cleanup():
    yield
    with session_scope() as s:
        ids = s.scalars(select(Article.id).where(Article.source == SOURCE)).all()
        s.execute(delete(Chunk).where(Chunk.article_id.in_(ids)))
        s.execute(delete(Article).where(Article.id.in_(ids)))
        s.execute(delete(QuestionCache))
    live.clear_cache()


def _store(url: str, headline: str, description: str | None = None,
           published_at=None, canonical_id: int | None = None) -> int:
    with session_scope() as s:
        article = Article(
            url=url, url_hash=url, source=SOURCE, headline=headline,
            description=description, canonical_id=canonical_id,
            published_at=published_at or utcnow(),
        )
        s.add(article)
        s.flush()
        s.add(Chunk(article_id=article.id, chunk_index=0,
                    text=f"{headline}\n\n{description or ''}".strip()))
        return article.id


# --- Search plan --------------------------------------------------------------

def test_plan_keeps_valid_fields() -> None:
    plan = intel.coerce_plan(
        {"search": "Qzorbit", "terms": ["qzorbit", "funding", "raises"],
         "since_days": 7}, "How much did Qzorbit raise this week?")
    assert plan.search == "Qzorbit"
    assert plan.terms == ["qzorbit", "funding", "raises"]
    assert plan.since_days == 7


def test_plan_is_untrusted() -> None:
    plan = intel.coerce_plan(
        {"search": "  x " * 50, "terms": ["ok", 42, None, "a" * 40],
         "since_days": 9999}, "q")
    assert len(plan.search) <= 60
    assert plan.terms == ["ok"]
    assert plan.since_days is None


def test_garbage_plan_falls_back_to_free_keywords() -> None:
    plan = intel.coerce_plan("not a dict", "Why did Qzorbit pause its IPO?")
    assert plan.search == "Qzorbit"
    assert "ipo" in plan.terms and "qzorbit" in plan.terms


def test_plan_is_cached_and_never_paid_twice(monkeypatch, cleanup) -> None:
    calls = []

    def fake_invoke_json(feature, prompt, **kw):
        calls.append(feature)
        return ({"search": "Qzorbit", "terms": ["qzorbit"]},
                LlmResult("", settings.model_cheap, 10, 10, 0.0001))

    monkeypatch.setattr(intel, "invoke_json", fake_invoke_json)
    with session_scope() as s:
        first = intel.compile_plan(s, "News about Qzorbit?")
        second = intel.compile_plan(s, "  news about QZORBIT?  ")
    assert calls == [intel.FEATURE_PLAN]
    assert first[1] == 0.0001 and first[2] is False
    assert second[1] == 0.0 and second[2] is True
    assert second[0].search == "Qzorbit"


def test_budget_exhaustion_uses_the_free_plan(monkeypatch, cleanup) -> None:
    def refuse(*a, **kw):
        raise BudgetExceeded("cap", get_spend())

    monkeypatch.setattr(intel, "invoke_json", refuse)
    with session_scope() as s:
        plan, cost, cached = intel.compile_plan(s, "Tell me about Qzorbit")
    assert plan.search == "Qzorbit" and cost == 0.0


# --- Corpus retrieval -----------------------------------------------------------

def test_fts_query_cannot_inject_syntax() -> None:
    q = corpus.fts_query(['Qzorbit AND NOT "x" OR', "series-b*", "NEAR(a b)"])
    assert q == '"qzorbit" OR "not" OR "series" OR "near"'


def test_fts_query_runs_for_hostile_input(cleanup) -> None:
    with session_scope() as s:
        corpus.search(s, ['") OR * NEAR(', "M&A", "^col:x"])   # must not raise


def test_empty_terms_yield_no_query() -> None:
    assert corpus.fts_query(["the", "what is", ""]) is None


def test_search_returns_canonical_evidence_with_chunk(cleanup) -> None:
    canonical = _store("https://t.test/1", "Qzorbit raises $40M Series B",
                       "Qzorbit, a logistics startup, raised $40M.")
    _store("https://t.test/2", "Qzorbit bags $40 million", canonical_id=canonical)
    _store("https://t.test/3", "Unrelated fintech launch")

    with session_scope() as s:
        evidence = corpus.search(s, ["qzorbit"])
    assert [e.article_id for e in evidence] == [canonical]      # folded, once
    assert evidence[0].chunk_id is not None
    assert "logistics startup" in evidence[0].text


def test_the_entity_outranks_shared_vocabulary(cleanup) -> None:
    """Seen live: asked about Zepto's IPO, other companies' IPOs ranked first."""
    generic = [
        _store(f"https://t.test/g{i}",
               f"Acme{i} IPO listing debut lifts stock market equity offering")
        for i in range(4)
    ]
    about = _store("https://t.test/z", "Qzorbit plans IPO within three quarters")
    mention = _store("https://t.test/m", "Qzorbit hires a new CFO")

    with session_scope() as s:
        ids = [e.article_id for e in corpus.search(
            s, ["ipo", "listing", "debut", "stock", "market", "equity"],
            entity="Qzorbit", limit=3)]
    assert set(ids[:2]) == {about, mention}
    assert ids[2] in generic                      # filler only after the entity


def test_entity_query_tiers() -> None:
    assert corpus.fts_query(["ipo", "listing"], ["Qzorbit"]) == (
        '"qzorbit" AND ("ipo" OR "listing")')
    assert corpus.fts_query([], ["Qzorbit"]) == '"qzorbit"'


def test_live_finds_are_guaranteed_a_place(cleanup) -> None:
    for i in range(6):
        _store(f"https://t.test/q{i}", f"Qzorbit story number {i}")
    outsider = _store("https://t.test/x", "A story bm25 would never rank")

    with session_scope() as s:
        evidence = corpus.search(s, ["qzorbit"], limit=4, include_ids=[outsider],
                                 reserve=1)
    ids = [e.article_id for e in evidence]
    assert len(ids) == 4 and outsider in ids


def test_since_days_limits_evidence(cleanup) -> None:
    _store("https://t.test/old", "Qzorbit old news",
           published_at=utcnow() - timedelta(days=40))
    recent = _store("https://t.test/new", "Qzorbit new news")
    with session_scope() as s:
        evidence = corpus.search(s, ["qzorbit"], since_days=7)
    assert [e.article_id for e in evidence] == [recent]


def test_evidence_is_newest_first(cleanup) -> None:
    old = _store("https://t.test/o", "Qzorbit first",
                 published_at=utcnow() - timedelta(days=3))
    new = _store("https://t.test/n", "Qzorbit second")
    with session_scope() as s:
        assert [e.article_id for e in corpus.search(s, ["qzorbit"])] == [new, old]


# --- Live search: parsing -----------------------------------------------------

# Shaped like the real result pages: Indian Startup News / Entrackr cards (a
# title span, a date span, an empty overlay link) and VCCircle tag cards (an
# image link with alt text, an h4 headline, a date div, an excerpt).
RESULTS_HTML = """
<html><body>
<nav><a href="/tag/qzorbit">Qzorbit</a><a href="/category/funding-news-and-more">Funding</a></nav>
<div class="search_post_div">
  <a class="overlay__link" href="/news/qzorbit-raises-40-million-series-b-led-by-acme-12345"></a>
  <span class="card-title h5">Qzorbit raises $40 million in Series B led by Acme</span>
  <p>ISN Team</p><span class="time">31 Jul 2026</span>
</div>
<div class="newsCard_article-wrapper">
  <a href="/qzorbit-eyes-ipo-after-profitable-quarter-in-logistics"><img alt="Qzorbit eyes IPO after profitable quarter"></a>
  <h3>Finance</h3>
  <div class="newsCard_date">21 August, 2026</div>
  <h4 class="listingPage_list-heading">Qzorbit eyes IPO after profitable quarter</h4>
  <p>The logistics company reported its first profitable quarter and is now preparing to file.</p>
</div>
<aside><div><a href="/news/unrelated-trending-story-about-something-else-999">Trending: an unrelated story about something else</a></div></aside>
<a href="https://elsewhere.test/qzorbit-story-on-another-website-entirely">Qzorbit elsewhere on another website</a>
</body></html>
"""


def test_cards_parse_headline_date_and_excerpt() -> None:
    items = live.parse_cards(RESULTS_HTML, "https://news.test/search?title=q", "x")
    by_headline = {i.headline: i for i in items}

    isn = by_headline["Qzorbit raises $40 million in Series B led by Acme"]
    assert isn.url == ("https://news.test/news/"
                       "qzorbit-raises-40-million-series-b-led-by-acme-12345")
    # 31 Jul in India is 30 Jul 18:30 UTC.
    assert isn.published_at == datetime(2026, 7, 30, 18, 30, tzinfo=timezone.utc)

    vcc = by_headline["Qzorbit eyes IPO after profitable quarter"]
    assert vcc.description.startswith("The logistics company")
    assert vcc.published_at.date().isoformat() == "2026-08-20"


def test_cards_skip_navigation_and_other_hosts() -> None:
    urls = [i.url for i in live.parse_cards(RESULTS_HTML, "https://news.test/", "x")]
    assert not any("/tag/" in u or "/category/" in u for u in urls)
    assert not any("elsewhere.test" in u for u in urls)


def test_matches_requires_the_question_words() -> None:
    item = FeedItem("u", "Qzorbit eyes IPO", None, None, "x", [])
    assert live.matches(item, ["qzorbit"])
    assert live.matches(item, ["ipos"]) is False           # word prefix only
    assert live.matches(item, ["the", "of"]) is False      # no signal
    trending = FeedItem("u", "An unrelated story", None, None, "x", [])
    assert not live.matches(trending, ["qzorbit"])


def test_live_results_must_name_the_searched_entity(monkeypatch) -> None:
    """A feed re-fetch for "Qzorbit" must not keep every story saying "market"."""
    about = FeedItem("https://h.test/a-long-enough-article-path-1",
                     "Qzorbit files for IPO", None, None, "x", [])
    generic = FeedItem("https://h.test/a-long-enough-article-path-2",
                       "Stock market IPO boom continues", None, None, "x", [])
    monkeypatch.setattr(live, "_fetch", lambda source, q: ([about, generic], False))

    [result] = live.search_live("Qzorbit", ["ipo", "market"],
                                sources=(_source("feed"),))
    assert [i.headline for i in result.items] == ["Qzorbit files for IPO"]


def test_live_search_url_placeholders() -> None:
    ls = LiveSearch(kind="search_html", url="https://a.test/s?title={q}&t={slug}")
    assert ls.build("Ola Electric & co") == (
        "https://a.test/s?title=Ola+Electric+%26+co&t=ola-electric-co")


# --- Live search: fan-out --------------------------------------------------------

def _source(name: str, kind: str = "feed_refetch") -> Source:
    return Source(name=name, label=name.title(), home="https://h.test/",
                  strategy="rss",
                  live_search=LiveSearch(kind=kind, url="https://h.test/feed"))


def test_one_slow_or_broken_site_never_stalls_the_rest(monkeypatch) -> None:
    good = FeedItem("https://h.test/qzorbit-news-item-long-enough", "Qzorbit news",
                    None, None, "fast", [])
    noise = FeedItem("https://h.test/other", "Other news", None, None, "fast", [])

    def fake_fetch(source, query):
        if source.name == "slow":
            time.sleep(5)
        if source.name == "broken":
            raise ConnectionError("refused")
        return [good, noise], False

    monkeypatch.setattr(live, "_fetch", fake_fetch)
    monkeypatch.setattr(settings, "live_search_timeout", 0.2)

    started = time.monotonic()
    results = {r.name: r for r in live.search_live(
        "Qzorbit", ["qzorbit"],
        sources=(_source("fast"), _source("slow"), _source("broken")))}
    assert time.monotonic() - started < 4

    assert results["fast"].status == "ok"
    assert [i.headline for i in results["fast"].items] == ["Qzorbit news"]
    assert results["slow"].status == "timeout"
    assert results["broken"].status == "error"
    assert "refused" in results["broken"].error


# --- Write-through ----------------------------------------------------------------

def test_live_finds_are_written_through_once(monkeypatch, cleanup) -> None:
    monkeypatch.setattr(intel, "_hydrate", lambda items: None)
    item = FeedItem("https://t.test/live-qzorbit", "Qzorbit wins a big contract",
                    "Qzorbit won a contract.", utcnow(), SOURCE, [])
    result = live.SourceResult(name=SOURCE, label="T", kind="search_html",
                               items=[item])

    found, new_ids, per_source = intel.write_through([result])
    assert len(found) == 1 and found[0] in new_ids
    assert per_source == {SOURCE: 1}
    with session_scope() as s:
        chunk = s.scalars(select(Chunk).where(Chunk.article_id == found[0])).one()
        assert "won a contract" in chunk.text

    again, new_again, _ = intel.write_through([result])
    assert again == found and new_again == set()             # known now


# --- Citations ---------------------------------------------------------------------

@pytest.mark.parametrize("answer, expected", [
    ("Raised $40M [1].", {1}),
    ("It grew [2][4] and then [3, 5].", {2, 3, 4, 5}),
    ("See [2-4].", {2, 3, 4}),
    ("No citations here, and [x] is not one.", set()),
])
def test_cited_numbers(answer, expected) -> None:
    assert intel.cited_numbers(answer) == expected


def test_gpt_oss_citation_style_is_normalised() -> None:
    """Seen live: gpt-oss cites as 【9】 or 【8†L1-L4】 whatever the prompt says."""
    raw = "SEBI cleared it【9】. Revenue doubled【8†L1-L4】, then 【2, 3】."
    fixed = intel.normalise_citations(raw)
    assert fixed == "SEBI cleared it[9]. Revenue doubled[8], then [2, 3]."
    assert intel.cited_numbers(fixed) == {2, 3, 8, 9}


# --- Endpoint ------------------------------------------------------------------------

def _stub_models(monkeypatch, answer: str | Exception) -> None:
    monkeypatch.setattr(intel, "invoke_json", lambda *a, **kw: (
        {"search": "Qzorbit", "terms": ["qzorbit"]},
        LlmResult("", settings.model_cheap, 50, 20, 0.00002)))

    def fake_invoke(feature, prompt, **kw):
        if isinstance(answer, Exception):
            raise answer
        assert "[1]" in prompt
        return LlmResult(answer, settings.model_cheap, 900, 120, 0.0002)

    monkeypatch.setattr(intel, "invoke", fake_invoke)


def test_endpoint_answers_with_resolvable_citations(monkeypatch, cleanup) -> None:
    _stub_models(monkeypatch, "Qzorbit raised $40M [1].")
    article_id = _store("https://t.test/e1", "Qzorbit raises $40M",
                        "Series B led by Acme.")

    with TestClient(app) as client:
        body = client.post("/api/intelligence", json={
            "question": "How much did Qzorbit raise?", "live": False}).json()

    assert body["error"] is None
    assert body["answer"] == "Qzorbit raised $40M [1]."
    assert body["cost_usd"] == pytest.approx(0.00022)
    [cite] = body["citations"]
    assert cite["n"] == 1 and cite["cited"] is True
    assert cite["article_id"] == article_id and cite["url"] == "https://t.test/e1"
    with session_scope() as s:
        assert s.get(Chunk, cite["chunk_id"]).article_id == article_id


def test_endpoint_returns_stories_when_budget_is_spent(monkeypatch, cleanup) -> None:
    _stub_models(monkeypatch, BudgetExceeded("daily cap reached", get_spend()))
    _store("https://t.test/e2", "Qzorbit raises $40M")

    with TestClient(app) as client:
        body = client.post("/api/intelligence", json={
            "question": "How much did Qzorbit raise?", "live": False}).json()

    assert body["answer"] is None
    assert "daily cap reached" in body["error"]
    assert [c["url"] for c in body["citations"]] == ["https://t.test/e2"]


def test_endpoint_with_nothing_to_go_on_spends_nothing_on_answering(
        monkeypatch, cleanup) -> None:
    _stub_models(monkeypatch, AssertionError("must not be called"))
    with TestClient(app) as client:
        body = client.post("/api/intelligence", json={
            "question": "Anything on Qzorbit?", "live": False}).json()
    assert body["citations"] == []
    assert body["answer"].startswith("Nothing in the six sources")


def test_empty_question_is_rejected_for_free() -> None:
    with TestClient(app) as client:
        body = client.post("/api/intelligence", json={"question": "   "}).json()
    assert body["error"] == "Ask a question first."
