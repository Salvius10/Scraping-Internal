"""1 · Information sources and the registry. Offline: Apify and Firecrawl are stubbed.

Protected: each adapter turns what its service returns into RawItems and
nothing else -- links must be http(s), rows that are not results (samples,
diagnostics, notices, uncharged rows) are never items, local times become
UTC, every paid run carries both caps, and a failed read says why and what it
still cost. A broken registry is refused as a whole.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.pipeline import registry
from app.pipeline.items import EVENT, EVENT_PAGE, FUNDING, normalise_url
from app.pipeline.sources import FetchContext, SourceError
from app.pipeline.sources import eventbrite as eb
from app.pipeline.sources import linkedin as li
from app.pipeline.sources import luma
from app.pipeline.sources.pages import PageSource
from app.pipeline.sources.rss import RssSource, parse_feed
from app.pipeline.sources.web_search import WebSearchSource, tbs_for
from app.search import apify, firecrawl

CTX = FetchContext(now=datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc))


def rss_source() -> RssSource:
    return RssSource(key="rss:test", type="rss", label="Test News", every_hours=0,
                     paid=False, url="https://news.test/rss")


# --- RSS ------------------------------------------------------------------------------

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Acme raises &lt;b&gt;$5M&lt;/b&gt; Series A</title>
  <link>https://news.test/acme</link>
  <description>&lt;p&gt;Acme, a Pune fintech, raised $5M.&lt;/p&gt;</description>
  <pubDate>Wed, 14 Oct 2026 09:30:00 +0530</pubDate></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
<item><title></title><link>https://news.test/untitled</link></item>
</channel></rss>"""


def test_rss_keeps_web_links_and_strips_markup() -> None:
    items = parse_feed(FEED, rss_source())
    assert len(items) == 1
    item = items[0]
    assert item.kind == FUNDING and item.source_key == "rss:test"
    assert item.title == "Acme raises $5M Series A"
    assert item.text == "Acme, a Pune fintech, raised $5M."
    assert item.published_at == datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)


def test_rss_unreadable_feed_is_an_error() -> None:
    with pytest.raises(SourceError, match="could not be read"):
        parse_feed(b"<html>not a feed", rss_source())


# --- LinkedIn ---------------------------------------------------------------------------

def linkedin_source() -> li.LinkedinSource:
    url, slug = li.canonical_url("linkedin.com/in/Jane-Doe/recent-activity/")
    return li.LinkedinSource(key=f"linkedin:{slug}", type="linkedin", label="Jane",
                             every_hours=24, paid=True, url=url, slug=slug)


def test_linkedin_link_is_kept_in_one_spelling() -> None:
    assert li.canonical_url("https://www.linkedin.com/company/Acme/posts/?feedView=all") == (
        "https://www.linkedin.com/company/acme/", "acme")
    for bad in ("https://example.com/in/x", "https://www.linkedin.com/feed/", "linkedin.com/posts/x"):
        with pytest.raises(ValueError):
            li.canonical_url(bad)


def test_linkedin_later_reads_ask_only_for_newer_posts() -> None:
    source = linkedin_source()
    first = source.actor_input(None)
    assert first["maxPosts"] == settings.linkedin_first_posts and "postedLimitDate" not in first
    assert first["scrapeReactions"] is False and first["scrapeComments"] is False
    later = source.actor_input(datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc))
    assert later["postedLimitDate"] == "2026-10-01T10:00:01.000Z"


def post(**over) -> dict:
    row = {"type": "post", "id": "7001", "linkedinUrl": "https://www.linkedin.com/posts/x-7001",
           "content": "Thrilled: Acme raised a $2M seed round!\nLed by Fund One.",
           "postedAt": {"timestamp": 1_791_000_000_000},
           "author": {"name": "Jane Doe", "publicIdentifier": "jane-doe"}}
    row.update(over)
    return row


def test_linkedin_rows_become_items_and_notices_do_not(monkeypatch) -> None:
    calls = []

    def fake_run(actor, run_input, *, max_items, max_charge_usd):
        calls.append((actor, max_items, max_charge_usd))
        return [post(), post(id="7002", linkedinUrl="https://www.linkedin.com/posts/x-7002",
                             author={"name": "Other", "publicIdentifier": "other"}),
                {"type": "notice", "message": "ok"}, post(id="7003", linkedinUrl="javascript:x")]

    monkeypatch.setattr(apify, "run_actor", fake_run)
    result = linkedin_source().fetch(FetchContext(cursor=lambda key: None))
    assert [i.url for i in result.items] == ["https://www.linkedin.com/posts/x-7001",
                                            "https://www.linkedin.com/posts/x-7002"]
    first, second = result.items
    assert first.title == "Thrilled: Acme raised a $2M seed round!"
    assert first.payload == {"author": "Jane Doe", "repost": False}
    assert second.payload["repost"] is True
    assert calls[0][2] <= settings.apify_max_charge_usd
    assert result.apify_usd == pytest.approx(3 * settings.linkedin_price_per_post
                                             + settings.linkedin_price_per_run)


def test_linkedin_failed_read_reports_what_it_cost(monkeypatch) -> None:
    monkeypatch.setattr(apify, "run_actor", lambda *a, **k: [{"error": "Profile is private"}])
    with pytest.raises(SourceError, match="Profile is private") as raised:
        linkedin_source().fetch(FetchContext())
    assert raised.value.apify_usd == pytest.approx(settings.linkedin_price_per_run
                                                   + settings.linkedin_price_per_empty)


# --- Luma -----------------------------------------------------------------------------

def luma_row(**over) -> dict:
    row = {"eventId": "evt-1", "url": "https://lu.ma/abc", "name": "Founders Breakfast",
           "startAt": "2026-10-20T03:30:00.000Z", "endAt": "2026-10-20T05:30:00.000Z",
           "timezone": "Asia/Kolkata", "venueName": "91springboard", "city": "Bengaluru",
           "countryCode": "IN", "isOnline": False, "hostName": "Startup Club",
           "isFree": True, "description": "Meet founders."}
    row.update(over)
    return row


def luma_source() -> luma.LumaSource:
    return luma.LumaSource(key="luma", type="luma", label="Luma", every_hours=24, paid=True,
                           cities=("bengaluru",), keyword="startup", max_items=10)


def test_luma_rows_become_structured_events(monkeypatch) -> None:
    seen_input = {}

    def fake_run(actor, run_input, *, max_items, max_charge_usd):
        seen_input.update(run_input, _max=max_items, _charge=max_charge_usd)
        return [luma_row(), {"_sample": True, "eventId": "s"},
                {"_diagnostic": True, "errorCode": "NO_RESULTS"}]

    monkeypatch.setattr(apify, "run_actor", fake_run)
    result = luma_source().fetch(CTX)
    assert seen_input["cities"] == ["bengaluru"] and seen_input["searchQuery"] == "startup"
    assert seen_input["_max"] == 10
    assert len(result.items) == 1 and result.warning is None
    item = result.items[0]
    assert item.kind == EVENT and item.payload["country_code"] == "IN"
    assert item.payload["starts_at"] == "2026-10-20T03:30:00+00:00"
    assert item.payload["price"] == "Free"
    assert "20 Oct 2026, 09:00 IST" in item.text
    assert result.apify_usd == pytest.approx(settings.luma_price_per_event
                                             + settings.luma_price_per_run)


def test_luma_unknown_city_is_a_warning_or_an_error(monkeypatch) -> None:
    diag = {"_diagnostic": True, "errorCode": "CITY_NOT_FOUND", "error": "No such city"}
    monkeypatch.setattr(apify, "run_actor", lambda *a, **k: [luma_row(), diag])
    assert "CITY_NOT_FOUND" in luma_source().fetch(CTX).warning
    monkeypatch.setattr(apify, "run_actor", lambda *a, **k: [diag])
    with pytest.raises(SourceError, match="CITY_NOT_FOUND"):
        luma_source().fetch(CTX)


def test_apify_failure_becomes_a_source_error(monkeypatch) -> None:
    def boom(*a, **k):
        raise apify.ApifyError("Apify credits or plan limit used up.")
    monkeypatch.setattr(apify, "run_actor", boom)
    with pytest.raises(SourceError, match="Luma: Apify credits"):
        luma_source().fetch(CTX)


# --- Eventbrite -------------------------------------------------------------------------

def eb_row(**over) -> dict:
    row = {"ok": True, "charged": True, "recordType": "event", "eventId": "111",
           "name": "Startup Pitch Night", "summary": "Pitch to angels.",
           "startsAt": "2026-10-22T18:00:00", "endsAt": "2026-10-22T21:00:00",
           "timezone": "Asia/Kolkata", "venueName": "WeWork", "city": "Mumbai",
           "country": "IN", "organizerName": "Mumbai Angels", "minPrice": 499,
           "maxPrice": 999, "currency": "INR", "isFree": False, "isOnline": False,
           "url": "https://www.eventbrite.com/e/startup-pitch-night-111"}
    row.update(over)
    return row


def test_eventbrite_local_times_become_utc_and_uncharged_rows_are_skipped(monkeypatch) -> None:
    rows = [eb_row(), eb_row(eventId="222", charged=False),
            eb_row(eventId="333", _sample=True), {"_diagnostic": True, "errorCode": "NO_RESULTS"}]
    monkeypatch.setattr(apify, "run_actor", lambda *a, **k: rows)
    source = eb.EventbriteSource(key="eventbrite", type="eventbrite", label="Eventbrite",
                                 every_hours=24, paid=True,
                                 locations=("https://www.eventbrite.com/d/india--mumbai/",),
                                 keyword="startup", max_items=20)
    result = source.fetch(CTX)
    assert len(result.items) == 1
    payload = result.items[0].payload
    assert payload["starts_at"] == "2026-10-22T12:30:00+00:00"    # 18:00 IST
    assert payload["price"] == "INR 499-999"
    assert result.apify_usd == pytest.approx(settings.eventbrite_price_per_event
                                             + settings.eventbrite_price_per_run)
    assert source.actor_input()["location"] == ["https://www.eventbrite.com/d/india--mumbai/"]


# --- Web search -------------------------------------------------------------------------

def test_web_search_reads_results_only_and_survives_one_failed_query(monkeypatch) -> None:
    def fake_search(query, *, limit, kind, tbs, steered):
        assert kind == "news" and steered is False and tbs == "cdr:1,cd_min:10/14/2026,cd_max:10/16/2026"
        if "seed" in query:
            raise firecrawl.FirecrawlError("Firecrawl's rate limit was hit.")
        return firecrawl.Search(query=query, searched=query, credits_used=2, results=[
            firecrawl.SearchResult(n=1, url="https://news.test/zeta", title="Zeta raises $3M",
                                   description="Zeta, a Delhi startup...", domain="news.test",
                                   published="3 hours ago"),
            firecrawl.SearchResult(n=2, url="ftp://bad", title="x", description="",
                                   domain="bad"),
        ])

    monkeypatch.setattr(firecrawl, "search", fake_search)
    source = WebSearchSource(key="web_search", type="web_search", label="Web search",
                             every_hours=24, paid=True, queries=("a series a", "a seed round"),
                             lookback_days=2, results_per_query=20)
    result = source.fetch(CTX)
    assert [i.url for i in result.items] == ["https://news.test/zeta"]
    assert result.items[0].payload["date_approx"] is True
    assert result.credits == 2
    assert "1 of 2 searches failed" in result.warning


def test_web_search_fails_when_every_query_fails(monkeypatch) -> None:
    def boom(*a, **k):
        raise firecrawl.FirecrawlError("Firecrawl credits are used up.")
    monkeypatch.setattr(firecrawl, "search", boom)
    source = WebSearchSource(key="web_search", type="web_search", label="Web search",
                             every_hours=24, paid=True, queries=("q",))
    with pytest.raises(SourceError, match="credits are used up"):
        source.fetch(CTX)


def test_tbs_is_googles_custom_range() -> None:
    from datetime import date
    assert tbs_for(date(2026, 9, 1), date(2026, 9, 25)) == "cdr:1,cd_min:9/1/2026,cd_max:9/25/2026"


# --- Pages ----------------------------------------------------------------------------

def test_a_page_is_one_item_whose_identity_follows_its_text(monkeypatch) -> None:
    texts = iter(["Events: A on 1 Nov", "Events: A on 1 Nov, B on 2 Nov"])

    def fake_scrape(url):
        return firecrawl.Page(url=url, title="Portal", description="", markdown=next(texts),
                              credits_used=1), False

    monkeypatch.setattr(firecrawl, "scrape", fake_scrape)
    source = PageSource(key="page:portal", type="page", label="Portal", every_hours=24,
                        paid=True, url="https://portal.test/events")
    first, second = source.fetch(CTX), source.fetch(CTX)
    assert first.items[0].kind == EVENT_PAGE and first.credits == 1
    assert first.items[0].identity != second.items[0].identity


# --- Items ----------------------------------------------------------------------------

def test_urls_are_normalised_for_the_seen_index() -> None:
    a = normalise_url("https://www.News.test/story/?utm_source=x&b=2&a=1#top")
    b = normalise_url("http://news.test/story?a=1&b=2&fbclid=zz")
    assert a == b == "https://news.test/story?a=1&b=2"


# --- Registry -------------------------------------------------------------------------

def test_the_shipped_registry_loads() -> None:
    sources = registry.load_sources()
    keys = {s.key for s in sources}
    assert {"rss:entrackr", "linkedin:gokul-s0123", "web_search", "luma", "eventbrite"} <= keys
    assert all(not s.paid for s in sources if s.type == "rss")


@pytest.mark.parametrize("data, message", [
    ({"funding": {"rss": {"feeds": [{"key": "a", "url": "https://a.test/rss"},
                                    {"key": "a", "url": "https://b.test/rss"}]}}}, "duplicate"),
    ({"funding": {"rss": {"feeds": [{"key": "a", "url": "file:///etc/passwd"}]}}}, "http"),
    ({"funding": {"linkedin": {"accounts": [{"url": "https://example.com/in/x"}]}}}, "LinkedIn"),
    ({"funding": {"rss": {"every_hours": -1}}}, "every_hours"),
    ({"events": {"luma": {"cities": ["x"], "max_items": 0}}}, "max_items"),
])
def test_a_broken_registry_is_refused(data, message) -> None:
    with pytest.raises(registry.RegistryError, match=message):
        registry.parse(data)
