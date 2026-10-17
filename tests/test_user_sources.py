"""Sources added on the platform: LinkedIn accounts, websites and search phrases.

Offline: DNS, plain fetches, Firecrawl and the model are stubbed.

Protected: an added link may never reach a private address -- checked at
resolve time, at connect time and on every redirect; a website is read through
its feed when it has one (free) and through Firecrawl only when it does not;
nothing already read (built-in or added) is added twice, and the number added
is capped; added sources run through the same pipeline as built-in ones; a
page read again folds into the rows it made last time; only added sources can
be removed, and removing one keeps what it found.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import netguard
from app.config import settings
from app.db import session_scope
from app.main import app
from app.models import FundedStartup, Stage
from app.pipeline import registry, storage, structure, user_sources
from app.pipeline.items import FUNDING_PAGE
from app.pipeline.sources.linkedin import LinkedinSource
from app.pipeline.sources.pages import PageSource
from app.pipeline.sources.rss import RssSource
from app.pipeline.sources.web_search import WebSearchSource
from app.pipeline.structure import Work

client = TestClient(app)

FEED = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b'<item><title>Acme raises $5M</title><link>https://blog.test/acme</link></item>'
        b'</channel></rss>')
PAGE = ('<html><head>'
        '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        '<link rel="alternate" type="application/rss+xml" href="/funding/feed/">'
        '<link rel="alternate" type="application/rss+xml" href="/comments/feed/">'
        '</head><body>news</body></html>')


def public_dns(monkeypatch, address: str = "93.184.216.34") -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))])


def pages(monkeypatch, responses: dict) -> list[str]:
    """Stub netguard.fetch: URL -> (content type, body) or an UnsafeUrl."""
    asked: list[str] = []

    def fake_fetch(url):
        asked.append(url)
        answer = responses.get(url)
        if answer is None or isinstance(answer, Exception):
            raise answer or netguard.UnsafeUrl("The site answered HTTP 404.")
        ctype, body = answer
        return netguard.Fetched(url=url, content_type=ctype,
                                body=body if isinstance(body, bytes) else body.encode())

    monkeypatch.setattr(netguard, "fetch", fake_fetch)
    return asked


# --- The guard ---------------------------------------------------------------------------

@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1",
                                     "192.168.1.1"])
def test_private_addresses_are_refused(monkeypatch, address) -> None:
    public_dns(monkeypatch, address)
    with pytest.raises(netguard.UnsafeUrl, match="private or local"):
        netguard.check_url("https://looks-public.test/news")


@pytest.mark.parametrize("url, message", [
    ("ftp://news.test/x", "http and https"),
    ("https://user:pw@news.test/", "username or password"),
    ("https://news.test:8080/", "standard web ports"),
    ("https://localhost/", "web address"),
])
def test_odd_links_are_refused(monkeypatch, url, message) -> None:
    public_dns(monkeypatch)
    with pytest.raises(netguard.UnsafeUrl, match=message):
        netguard.check_url(url)


def test_connect_time_check_refuses_a_rebound_address(monkeypatch) -> None:
    """A host that resolved public for check_url but private at connect is refused."""
    public_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(netguard.UnsafeUrl, match="private or local"):
        netguard._PublicOnlyBackend().connect_tcp("rebind.test", 443, timeout=1)


def test_connect_goes_to_the_checked_address(monkeypatch) -> None:
    public_dns(monkeypatch)
    connected = []
    monkeypatch.setattr(netguard.httpcore.SyncBackend, "connect_tcp",
                        lambda self, host, port, **kw: connected.append(host))
    netguard._PublicOnlyBackend().connect_tcp("news.test", 443, timeout=1)
    assert connected == ["93.184.216.34"]


# --- Website: feed or page --------------------------------------------------------------

def test_a_feed_link_is_read_as_a_feed(monkeypatch) -> None:
    public_dns(monkeypatch)
    pages(monkeypatch, {"https://blog.test/feed": ("application/rss+xml", FEED)})
    assert user_sources.how_to_read("https://blog.test/feed") == ("rss", "https://blog.test/feed")


def test_a_page_is_read_through_its_own_sections_feed(monkeypatch) -> None:
    public_dns(monkeypatch)
    asked = pages(monkeypatch, {
        "https://blog.test/funding": ("text/html", PAGE),
        "https://blog.test/funding/feed/": ("application/rss+xml", FEED),
        "https://blog.test/feed/": ("application/rss+xml", FEED),
    })
    assert user_sources.how_to_read("https://blog.test/funding") == (
        "rss", "https://blog.test/funding/feed/")
    assert "https://blog.test/comments/feed/" not in asked


def test_a_page_without_a_feed_or_that_refuses_us_is_scraped(monkeypatch) -> None:
    public_dns(monkeypatch)
    pages(monkeypatch, {"https://plain.test/news": ("text/html", "<html>no feed</html>")})
    assert user_sources.how_to_read("https://plain.test/news") == ("scrape", "https://plain.test/news")
    pages(monkeypatch, {})                                # HTTP 403 / needs a browser
    assert user_sources.how_to_read("https://shy.test/news")[0] == "scrape"


def test_a_private_website_is_refused_before_any_fetch(monkeypatch) -> None:
    public_dns(monkeypatch, "10.1.2.3")
    asked = pages(monkeypatch, {})
    with pytest.raises(user_sources.SourceInputError, match="private or local"):
        user_sources.how_to_read("https://intranet.test/")
    assert asked == []


# --- Adding --------------------------------------------------------------------------

def test_linkedin_is_kept_in_one_spelling_and_never_twice(clean_db) -> None:
    row = user_sources.add("linkedin", "linkedin.com/company/Acme-Labs/posts/", None)
    assert row.value == "https://www.linkedin.com/company/acme-labs/" and row.label == "acme-labs"
    with pytest.raises(user_sources.SourceInputError, match="already a source"):
        user_sources.add("linkedin", "https://www.linkedin.com/company/acme-labs", None)
    with pytest.raises(user_sources.SourceInputError, match="already a source"):   # built-in
        user_sources.add("linkedin", "https://www.linkedin.com/in/gokul-s0123/", None)
    with pytest.raises(user_sources.SourceInputError, match="LinkedIn profile"):
        user_sources.add("linkedin", "https://example.com/in/x", None)


def test_search_phrases_are_checked(clean_db) -> None:
    row = user_sources.add("search", "  Bengaluru   fintech raises  ", "Fintech")
    assert row.value == "Bengaluru fintech raises" and row.label == "Fintech"
    for phrase in ("bengaluru FINTECH raises", "Indian startup raises seed funding round"):
        with pytest.raises(user_sources.SourceInputError, match="already searched"):
            user_sources.add("search", phrase, None)
    with pytest.raises(user_sources.SourceInputError, match="3 to 200"):
        user_sources.add("search", "ab", None)


def test_a_website_is_added_with_how_it_will_be_read(monkeypatch, clean_db) -> None:
    public_dns(monkeypatch)
    pages(monkeypatch, {"https://blog.test/funding": ("text/html", PAGE),
                        "https://blog.test/funding/feed/": ("application/rss+xml", FEED)})
    row = user_sources.add("website", "blog.test/funding", None)
    assert (row.via, row.read_url, row.label) == ("rss", "https://blog.test/funding/feed/",
                                                  "blog.test")
    with pytest.raises(user_sources.SourceInputError, match="already a source"):
        user_sources.add("website", "https://www.blog.test/funding/", None)
    with pytest.raises(user_sources.SourceInputError, match="already a source"):   # built-in feed
        user_sources.add("website", "https://entrackr.com/rss", None)


def test_the_number_added_is_capped(monkeypatch, clean_db) -> None:
    monkeypatch.setattr(settings, "max_user_sources", 2)
    user_sources.add("search", "phrase one", None)
    user_sources.add("search", "phrase two", None)
    with pytest.raises(user_sources.SourceInputError, match="the most allowed"):
        user_sources.add("search", "phrase three", None)


def test_added_sources_join_the_pipeline(monkeypatch, clean_db) -> None:
    public_dns(monkeypatch)
    pages(monkeypatch, {"https://plain.test/news": ("text/html", "<html></html>"),
                        "https://blog.test/feed": ("application/rss+xml", FEED)})
    user_sources.add("linkedin", "https://www.linkedin.com/in/jane/", "Jane")
    phrase = user_sources.add("search", "Pune EV startup raises", None)
    scraped = user_sources.add("website", "https://plain.test/news", None)
    fed = user_sources.add("website", "https://blog.test/feed", None)

    by_key = {s.key: s for s in registry.all_sources()}
    assert isinstance(by_key["linkedin:jane"], LinkedinSource) and by_key["linkedin:jane"].paid
    search = by_key[f"search:{phrase.id}"]
    assert isinstance(search, WebSearchSource) and search.queries == ("Pune EV startup raises",)
    page = by_key[f"site:{scraped.id}"]
    assert isinstance(page, PageSource) and page.item_kind == FUNDING_PAGE
    assert page.type == "website" and page.needs == "firecrawl"
    feed = by_key[f"site:{fed.id}"]
    assert isinstance(feed, RssSource) and not feed.paid and feed.every_hours == 0
    assert feed.needs is None
    assert "rss:entrackr" in by_key                         # built-ins still there


# --- A page read by the model ---------------------------------------------------------

def page_work(n: int = 1) -> Work:
    return Work(id=n, kind=FUNDING_PAGE, source_key="site:1", source_type="website",
                source_label="Blog", url="https://blog.test/funding", title="Funding news",
                text="...", published_at=None, payload={})


def test_rounds_on_a_page_keep_their_own_link_and_date(monkeypatch) -> None:
    answer = {"rounds": [
        {"company": "Acme", "stage": "Seed", "url": "/2026/10/acme", "date": "2026-10-14",
         "summary": "Acme raised a seed round."},
        {"company": "Zeta", "stage": "Series A", "url": "javascript:x", "date": "2099-01-01"},
    ]}
    monkeypatch.setattr(structure, "invoke_json",
                        lambda *a, **k: (answer, type("R", (), {"cost_usd": 0.001})()))
    out = structure.structure_funding_page(page_work())
    acme, zeta = out.rows[1]
    assert acme.url == "https://blog.test/2026/10/acme"
    assert acme.published_at == datetime(2026, 10, 13, 18, 30, tzinfo=timezone.utc)
    assert zeta.url is None and zeta.published_at is None          # bad link, future date


def test_a_page_read_again_folds_into_its_own_rows(clean_db) -> None:
    row = structure.FundingRow(company="Zeta", stage=Stage.SEED, round_label=None, amount=None,
                               investors=[], sector=None, hq_city=None, summary="Zeta raised.",
                               url=None, published_at=None)
    with session_scope() as s:
        first = storage.store_funding(s, page_work(1), row)
    with session_scope() as s:
        again = storage.store_funding(s, page_work(2), row)        # page changed, re-read
    assert first[0] == storage.NEW and again == (storage.MERGED, first[1])
    with session_scope() as s:
        stored = s.scalars(select(FundedStartup)).one()
    assert stored.headline == "Zeta raised." and stored.source_url == "https://blog.test/funding"


# --- API -------------------------------------------------------------------------------

def test_the_panel_lists_built_in_and_added_sources(clean_db) -> None:
    body = client.get("/api/sources").json()
    keys = {s["key"]: s for s in body["sources"]}
    assert "rss:entrackr" in keys and "luma" not in keys                # funding only
    assert keys["rss:entrackr"]["id"] is None                            # not removable
    assert keys["web_search"]["detail"].startswith("Indian startup raises pre-seed")
    assert body["max_added"] == settings.max_user_sources and body["added"] == 0
    events = client.get("/api/sources", params={"section": "events"}).json()
    assert {s["key"] for s in events["sources"]} >= {"luma", "eventbrite"}


def test_add_and_remove_through_the_api(clean_db) -> None:
    answer = client.post("/api/sources", json={"kind": "search", "value": "Chennai SaaS funding"}
                         ).json()
    assert answer["added"] is True
    assert answer["source"]["key"].startswith("search:") and answer["source"]["id"]
    assert "FIRECRAWL_API_KEY" in answer["message"]             # no key in tests
    refused = client.post("/api/sources", json={"kind": "search", "value": "chennai saas funding"}
                          ).json()
    assert refused == {"added": False, "message": "That phrase is already searched.",
                       "source": None}

    source_id = answer["source"]["id"]
    assert client.delete(f"/api/sources/{source_id}").json() == {"removed": True}
    assert client.delete(f"/api/sources/{source_id}").status_code == 404
    assert client.post("/api/sources", json={"kind": "fax", "value": "x"}).json()["added"] is False


def test_removing_a_source_keeps_what_it_found(clean_db) -> None:
    row = user_sources.add("search", "Delhi agritech raises", None)
    with session_scope() as s:
        s.add(FundedStartup(company="Agro", company_key="agro", stage=Stage.SEED,
                            headline="Agro raises", source_url="https://n.test/agro",
                            source_label="n.test", source_type="web_search", sources=[]))
    assert user_sources.remove(row.id)
    assert client.get("/api/funding").json()["total"] == 1
