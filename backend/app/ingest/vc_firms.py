"""Insights > VC firms: each firm's own website news, and news about its deals.

Every firm in `vc_firms.yaml` has up to two reads. Its own site, one way,
cheapest first:

  rss      the firm's own feed                               free
  html     its news page over plain HTTP, links picked out   free
  sitemap  its sitemap, filtered to posts, newest first      free
  scrape   Firecrawl scrape of the news page (403 / JS-only) 1 credit
  map      Firecrawl list of the site's pages, filtered      1 credit

and, for some, a Firecrawl news search about it (~2 credits).

Free reads run on every 12h refresh; Firecrawl reads at most every
`vc_firecrawl_hours`. New posts on sites we can open directly get their title
and publish time from the post's own page, also free.

Then gpt-oss reads only the *new* titles, 25 per call (~$0.00005 each), and
sorts them: Investment, Portfolio news, Fund news or Other. Only Other --
essays, podcasts, events -- is hidden by default. A post the model answered is
never read again.

    python -m app.ingest.vc_firms --once                 # all firms due
    python -m app.ingest.vc_firms --once --firm accel    # one firm
    python -m app.ingest.vc_firms --once --free-only     # no Firecrawl credits
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup
from sqlalchemy import select

from ..config import settings
from ..db import init_db, session_scope
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import VcKind, VcPost, VcRead, utcnow
from ..search import firecrawl
from .describe import extract_meta
from .rounds import clean_text, tidy_round
from .rss import parse_feed
from .web_rounds import parse_published

log = logging.getLogger(__name__)

FEATURE = "vc_posts"
BATCH_SIZE = 25
SITE_VIAS = ("rss", "html", "sitemap", "scrape", "map")
FIRECRAWL_VIAS = ("scrape", "map", "search")
# Where a post was found, as shown in the "Found via" column.
VIA_LABELS = {v: "Firm website" for v in SITE_VIAS} | {"search": "News search"}
# How each read works, as shown in the firm list.
READ_LABELS = {
    "rss": "own website feed", "html": "own website news page",
    "sitemap": "own website sitemap", "scrape": "own website, via Firecrawl",
    "map": "own website, via Firecrawl", "search": "news search, via Firecrawl",
}
# Sites we can open post by post over plain HTTP, for a title and date.
PAGE_READABLE_VIAS = ("html", "sitemap", "map")

# New posts: how many post pages to open per read for title and date. Free,
# but polite.
PAGE_FETCHES_PER_READ = 12
# A sitemap with no dates cannot say which posts are newest: keep up to this many.
UNDATED_SITEMAP_LIMIT = 100
MIN_TITLE_CHARS = 25
MIN_TITLE_WORDS = 4

# Links that are never a post: social profiles, maps, mail.
_SKIP_HOSTS = ("linkedin.com", "twitter.com", "x.com", "facebook.com",
               "instagram.com", "youtube.com", "youtu.be", "goo.gl",
               "spotify.com", "apple.com", "wa.me", "t.me")

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_IN_TEXT = re.compile(
    rf"\b(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?\s+\d{{4}})\b", re.I)
_LEADING_DAY = re.compile(rf"^\s*{_MONTH}\s+\d{{1,2}}\s*\\", re.I)   # "September 18\ Title"
_NOISE = re.compile(r"\bread more( about)?\b|\b\d+\s+mins?( read)?\b|\bopen in a new tab\b",
                    re.I)


# --- Registry --------------------------------------------------------------------

@dataclass(frozen=True)
class Read:
    """One way of reading a firm: its own site, or a news search about it."""

    via: str
    url: str | None = None
    link_pattern: str | None = None
    query: str | None = None
    note: str | None = None

    @property
    def uses_firecrawl(self) -> bool:
        return self.via in FIRECRAWL_VIAS

    @property
    def is_site(self) -> bool:
        return self.via in SITE_VIAS


EVENT_VIAS = ("luma", "page", "html")


@dataclass(frozen=True)
class EventsAt:
    """Where a firm lists the events it organises (Insights > Events organised).

    luma  a Luma calendar with a short name, read through Apify
    page  any other events page, read through Firecrawl (JavaScript-built pages,
          and Luma calendars that only have a /calendar/cal-... address)
    html  an events page readable over plain HTTP: fetched free
    For page and html, gpt-oss lists the events, only when the text changed.
    `india_only` keeps only events in India, for a global firm's calendar.
    `site` marks the firm's own home page rather than an events page or
    calendar: checked less often (`events_site_hours`), and only sent to the
    model when its text mentions an event and a date.
    """

    via: str
    url: str
    india_only: bool = False
    site: bool = False
    note: str | None = None


@dataclass(frozen=True)
class VcFirm:
    key: str
    label: str
    home: str
    site: Read | None = None           # the firm's own website news
    search: Read | None = None         # news about it from other outlets
    no_site: str | None = None         # why there is no site news to read
    aliases: tuple[str, ...] = ()
    events: tuple[EventsAt, ...] = ()  # where it lists its own events
    no_events: str | None = None       # why there is no events source, when known

    @property
    def reads(self) -> tuple[Read, ...]:
        return tuple(r for r in (self.site, self.search) if r is not None)

    def matches(self, text: str | None) -> bool:
        """True when `text` (an investor list) names this firm."""
        return bool(text) and any(re.search(a, text, re.I) for a in self.aliases)


def _parse(raw: dict) -> VcFirm:
    key = raw["key"]
    site = raw.get("site")
    if site:
        if site.get("via") not in SITE_VIAS:
            raise ValueError(f"{key}: unknown site via {site.get('via')!r}")
        if not site.get("url"):
            raise ValueError(f"{key}: a site read needs a url")
        site = Read(via=site["via"], url=site["url"],
                    link_pattern=site.get("link_pattern"), note=site.get("note"))
    elif not raw.get("no_site"):
        raise ValueError(f"{key}: give a site read, or say why not in no_site")
    query = raw.get("search")
    events = []
    for entry in raw.get("events") or ():
        if entry.get("via") not in EVENT_VIAS or not entry.get("url"):
            raise ValueError(f"{key}: an events entry needs via (luma, page or html) and a url")
        if entry.get("site") and entry["via"] == "luma":
            raise ValueError(f"{key}: a firm website is read as html or page, not luma")
        events.append(EventsAt(via=entry["via"], url=entry["url"],
                               india_only=bool(entry.get("india_only")),
                               site=bool(entry.get("site")), note=entry.get("note")))
    return VcFirm(
        key=key, label=raw.get("label", key), home=raw["home"], site=site or None,
        search=Read(via="search", query=query) if query else None,
        no_site=raw.get("no_site"), aliases=tuple(raw.get("aliases") or ()),
        events=tuple(events), no_events=raw.get("no_events"),
    )


@lru_cache(maxsize=1)
def load_firms(path: Path | None = None) -> tuple[VcFirm, ...]:
    p = Path(path or settings.vc_firms_file)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return tuple(_parse(r) for r in data.get("firms", []))


def get_firm(key: str) -> VcFirm | None:
    return next((f for f in load_firms() if f.key == key), None)


# --- Reading a page --------------------------------------------------------------

@dataclass
class Found:
    url: str
    title: str
    snippet: str | None = None
    published_at: datetime | None = None
    date_approx: bool = False


def _date_in(text: str) -> datetime | None:
    """A written date inside link text ("September 24, 2026"), as UTC."""
    m = _DATE_IN_TEXT.search(text)
    if not m:
        return None
    try:
        import dateparser
        dt = dateparser.parse(m.group(0), settings={
            "RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "Asia/Kolkata"})
    except Exception:  # noqa: BLE001 - a bad date must not fail the read
        return None
    return dt.astimezone(timezone.utc) if dt else None


def _tidy_title(text: str) -> str:
    text = _LEADING_DAY.sub(" ", text)      # a scraped card's day, with no year
    text = text.replace("\\", " ")          # markdown line breaks from a scrape
    text = _DATE_IN_TEXT.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return " ".join(text.split()).strip(" -|·,.")[:300]


def slug_title(url: str) -> str:
    """A readable stand-in title from a post's URL, until its page is read."""
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug)
    text = " ".join(re.sub(r"[-_+]+", " ", unquote(slug)).split())
    return (text[:1].upper() + text[1:])[:300]


def _is_post_link(url: str, page_url: str, pattern: str | None) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.lower()
    if any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS):
        return False
    if url.rstrip("/") == page_url.rstrip("/"):
        return False
    return not pattern or re.search(pattern, url, re.I) is not None


def _collect(pairs, page_url: str, pattern: str | None, limit: int) -> list[Found]:
    """(text, href) pairs -> posts. One per URL, keeping the fullest text."""
    best: dict[str, tuple[str, str]] = {}
    for raw_text, href in pairs:
        raw_text = " ".join((raw_text or "").split())
        url = urljoin(page_url, (href or "").strip()).split("#")[0]
        if not _is_post_link(url, page_url, pattern):
            continue
        title = _tidy_title(raw_text)
        if len(title) < MIN_TITLE_CHARS or len(title.split()) < MIN_TITLE_WORDS:
            continue
        if url not in best or len(raw_text) > len(best[url][0]):
            best[url] = (raw_text, title)
    found = []
    for url, (raw, title) in list(best.items())[:limit]:
        # A date written on the card is a day, not a time: shown as a day only.
        when = _date_in(raw)
        found.append(Found(url=url, title=title, published_at=when,
                           date_approx=when is not None))
    return found


def posts_from_html(html: str, page_url: str, pattern: str | None = None,
                    limit: int | None = None) -> list[Found]:
    """Post links on a news page, in page order (newest first on every page seen)."""
    soup = BeautifulSoup(html, "html.parser")
    # Site menus and footers are never posts. <header> stays: some sites
    # (Peak XV) wrap each post card in one.
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    pairs = ((a.get_text(" "), a["href"]) for a in soup.find_all("a", href=True))
    return _collect(pairs, page_url, pattern, limit or settings.vc_items_per_firm)


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((\S+?)(?:\s+\"[^\"]*\")?\)")


def posts_from_markdown(markdown: str, page_url: str, pattern: str | None = None,
                        limit: int | None = None) -> list[Found]:
    """The same, from a Firecrawl scrape's markdown."""
    text = _MD_IMAGE.sub(" ", markdown or "")
    pairs = ((firecrawl.plain(m.group(1), 600), m.group(2)) for m in _MD_LINK.finditer(text))
    return _collect(pairs, page_url, pattern, limit or settings.vc_items_per_firm)


_SITEMAP_ENTRY = re.compile(r"<(url|sitemap)>(.*?)</\1>", re.S | re.I)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", re.I)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)", re.I)


def posts_from_sitemap(xml: str, pattern: str | None = None,
                       limit: int | None = None) -> tuple[list[Found], list[str]]:
    """Post URLs from a sitemap, newest `lastmod` first, plus any child sitemaps
    (for a sitemap index). Titles come from the URL until the page is read;
    `lastmod` stands in as an approximate date."""
    entries, children = [], []
    for kind, body in _SITEMAP_ENTRY.findall(xml or ""):
        loc = _LOC.search(body)
        if not loc:
            continue
        if kind.lower() == "sitemap":
            children.append(loc.group(1))
            continue
        url = loc.group(1)
        if pattern and not re.search(pattern, url, re.I):
            continue
        mod = _LASTMOD.search(body)
        entries.append((url, firecrawl.parse_time(mod.group(1)) if mod else None))
    # Newest first. A sitemap without dates has no "newest", so every post is
    # kept rather than risk cutting off the latest ones.
    entries.sort(key=lambda e: e[1].timestamp() if e[1] else 0, reverse=True)
    limit = limit or settings.vc_items_per_firm
    if not any(when for _, when in entries):
        limit = max(limit, UNDATED_SITEMAP_LIMIT)
    found = [Found(url=u, title=slug_title(u), published_at=when, date_approx=when is not None)
             for u, when in entries[:limit]]
    return found, children


def _read_rss(read: Read, client: httpx.Client) -> list[Found]:
    resp = client.get(read.url)
    resp.raise_for_status()
    return [Found(url=i.url, title=i.headline, snippet=i.description,
                  published_at=i.published_at)
            for i in parse_feed(resp.content, "vc")[:settings.vc_items_per_firm]]


def _read_html(read: Read, client: httpx.Client) -> list[Found]:
    resp = client.get(read.url)
    resp.raise_for_status()
    return posts_from_html(resp.text, str(resp.url), read.link_pattern)


def _read_sitemap(read: Read, client: httpx.Client) -> list[Found]:
    resp = client.get(read.url)
    resp.raise_for_status()
    found, children = posts_from_sitemap(resp.text, read.link_pattern)
    for child in children[:10]:            # a sitemap index: read its parts
        part = client.get(child)
        if part.status_code == 200:
            found += posts_from_sitemap(part.text, read.link_pattern)[0]
    found.sort(key=lambda f: f.published_at.timestamp() if f.published_at else 0,
               reverse=True)
    dated = any(f.published_at for f in found)
    return found[:settings.vc_items_per_firm if dated else UNDATED_SITEMAP_LIMIT]


def _read_scrape(read: Read) -> tuple[list[Found], int]:
    page, cached = firecrawl.scrape(read.url)
    found = posts_from_markdown(page.markdown, page.url or read.url, read.link_pattern)
    return found, 0 if cached else page.credits_used


def _read_map(read: Read) -> tuple[list[Found], int]:
    links, credits, cached = firecrawl.map_site(read.url)
    found = [Found(url=link.url, title=link.title or slug_title(link.url),
                   snippet=link.description or None)
             for link in links if _is_post_link(link.url, read.url, read.link_pattern)]
    return found[:settings.vc_items_per_firm], 0 if cached else credits


def _read_search(read: Read, first: bool) -> tuple[list[Found], int]:
    # The first search looks back a month; later ones only need the last week.
    found_search = firecrawl.search(read.query, kind="news",
                                    tbs="qdr:m" if first else "qdr:w", steered=False)
    found = []
    for r in found_search.results:
        when, approx = parse_published(r.published)
        found.append(Found(url=r.url, title=r.title, snippet=r.description or None,
                           published_at=when, date_approx=approx))
    return found, found_search.credits_used


# --- Storing -------------------------------------------------------------------------

def _store(firm: VcFirm, via: str, found: list[Found]) -> list[int]:
    """Insert posts not seen before for this firm. Returns the new ids."""
    new_ids: list[int] = []
    with session_scope() as s:
        known = {p.url: p for p in s.scalars(
            select(VcPost).where(VcPost.firm == firm.key,
                                 VcPost.url.in_([f.url for f in found]))).all()}
        for f in found:
            existing = known.get(f.url)
            if existing is not None:
                if existing.published_at is None and f.published_at is not None:
                    existing.published_at = f.published_at
                    existing.date_approx = f.date_approx
                continue
            post = VcPost(firm=firm.key, url=f.url[:1000], title=f.title,
                          snippet=clean_text(f.snippet, 400), published_at=f.published_at,
                          date_approx=f.date_approx, via=via)
            s.add(post)
            s.flush()
            known[f.url] = post
            new_ids.append(post.id)
    return new_ids


def _page_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content", "").strip():
            return " ".join(tag["content"].split())[:300]
    return " ".join(soup.title.get_text().split())[:300] if soup.title else None


def _fill_from_pages(ids: list[int], client: httpx.Client) -> int:
    """New posts with no exact date, or a title made from the URL: open the
    post and read its own metadata. Free; a page that fails is left as is."""
    with session_scope() as s:
        todo = [(p.id, p.url, p.title) for p in s.scalars(
            select(VcPost).where(VcPost.id.in_(ids)).order_by(VcPost.id)).all()
            if p.title == slug_title(p.url) or p.published_at is None or p.date_approx]
    filled = 0
    for n, (post_id, url, title) in enumerate(todo[:PAGE_FETCHES_PER_READ]):
        if n:
            time.sleep(settings.per_domain_delay / 2)
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - one bad page must not stop the read
            continue
        meta = extract_meta(resp.text)
        better_title = _page_title(resp.text) if title == slug_title(url) else None
        with session_scope() as s:
            post = s.get(VcPost, post_id)
            if meta.published_at:
                post.published_at, post.date_approx = meta.published_at, False
            if better_title:
                post.title = better_title
            if not post.snippet and meta.description:
                post.snippet = clean_text(meta.description, 400)
        filled += 1
    return filled


def _last_good_read(firm: VcFirm, via: str) -> datetime | None:
    with session_scope() as s:
        return s.scalar(
            select(VcRead.started_at)
            .where(VcRead.firm == firm.key, VcRead.via == via, VcRead.error.is_(None))
            .order_by(VcRead.started_at.desc()).limit(1))


def firecrawl_due(firm: VcFirm, read: Read, now: datetime | None = None) -> bool:
    """Firecrawl reads cost credits, so each is made at most every
    `vc_firecrawl_hours`. A failed read does not count."""
    last = _last_good_read(firm, read.via)
    now = now or utcnow()
    return last is None or now - last >= timedelta(hours=settings.vc_firecrawl_hours)


def _client() -> httpx.Client:
    return httpx.Client(timeout=settings.http_timeout,
                        headers={"User-Agent": settings.user_agent},
                        follow_redirects=True)


def read_firm(firm: VcFirm, read: Read | None = None) -> VcRead:
    """Make one read of one firm -- its site unless told otherwise. Never
    raises; the outcome is on the returned VcRead."""
    read = read or firm.site or firm.search
    run = VcRead(firm=firm.key, via=read.via, started_at=utcnow(),
                 items_seen=0, items_new=0, credits_used=0)
    try:
        with _client() as client:
            if read.via == "rss":
                found = _read_rss(read, client)
            elif read.via == "html":
                found = _read_html(read, client)
            elif read.via == "sitemap":
                found = _read_sitemap(read, client)
            elif read.via == "scrape":
                found, run.credits_used = _read_scrape(read)
            elif read.via == "map":
                found, run.credits_used = _read_map(read)
            else:
                found, run.credits_used = _read_search(
                    read, _last_good_read(firm, "search") is None)
            if not found and read.is_site:
                raise ValueError("no posts found on the site -- has it changed?")
            run.items_seen = len(found)
            new_ids = _store(firm, read.via, found)
            run.items_new = len(new_ids)
            if read.via in PAGE_READABLE_VIAS and new_ids:
                _fill_from_pages(new_ids, client)
    except firecrawl.FirecrawlError as exc:
        run.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad read must not stop the rest
        run.error = f"{type(exc).__name__}: {exc}"[:300]
    if run.error:
        log.warning("vc %s (%s): %s", firm.key, read.via, run.error)
    with session_scope() as s:
        s.add(run)
    return run


# --- Classifying ---------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You sort posts by Indian venture capital firms, and news about them. You "
    "reply with JSON only -- no prose, no markdown fences."
)

PROMPT_HEAD = """Each numbered item names a VC firm and a post from its website or a
news result about it. For each item return one JSON object with keys:
  "i"         the item number
  "kind"      EXACTLY one of: Investment, Portfolio news, Fund news, Other
  "company"   the startup the item is about, or null
  "round"     the round as written ("Seed", "Pre-Series A", "Series B"), or null
  "amount"    the amount as written, with currency ("Rs 12.5 crore", "$4.5M"), or null
  "headline"  the item as a short plain headline, under 15 words

Kinds:
  Investment      the named firm invested in, led or joined a company's funding round
  Portfolio news  a company the firm backs lists, exits, is acquired or hits a milestone
  Fund news       the firm raises or launches a fund, programme or cohort
  Other           essays, podcasts, events, hiring, team news, market commentary, or
                  anything not about the named firm and the companies it backs

Use only the text given. Do not guess amounts.
Return a JSON array of these objects and nothing else.
"""

_KIND_BY_KEY = {k.value.lower().replace(" ", ""): k for k in VcKind}
_KIND_BY_KEY.update({"portfolio": VcKind.PORTFOLIO, "fund": VcKind.FUND,
                     "investments": VcKind.INVESTMENT})


def coerce_kind(value: object) -> VcKind:
    if isinstance(value, str):
        return _KIND_BY_KEY.get(value.strip().lower().replace(" ", ""), VcKind.OTHER)
    return VcKind.OTHER


@dataclass
class _Pending:
    id: int
    firm: str
    title: str
    snippet: str | None


def _load_pending(limit: int | None) -> list[_Pending]:
    stmt = (select(VcPost.id, VcPost.firm, VcPost.title, VcPost.snippet)
            .where(VcPost.classified_at.is_(None))
            .order_by(VcPost.found_at.desc(), VcPost.id))
    if limit:
        stmt = stmt.limit(limit)
    with session_scope() as s:
        return [_Pending(*row) for row in s.execute(stmt).all()]


def build_prompt(batch: list[_Pending]) -> str:
    labels = {f.key: f.label for f in load_firms()}
    lines = [PROMPT_HEAD]
    for n, p in enumerate(batch, start=1):
        body = f"[{n}] (Firm: {labels.get(p.firm, p.firm)}) {p.title}"
        if p.snippet:
            body += f"\n    {p.snippet[:240]}"
        lines.append(body)
    return "\n".join(lines)


def _apply(entries: list, by_number: dict[int, _Pending]) -> int:
    """Write one batch. A post the model skipped stays pending for next time."""
    got: dict[int, dict] = {}
    for entry in entries:
        if isinstance(entry, dict):
            try:
                got[int(entry.get("i"))] = entry
            except (TypeError, ValueError):
                continue
    written = 0
    now = utcnow()
    with session_scope() as s:
        for number, pending in by_number.items():
            entry = got.get(number)
            if entry is None:
                continue
            post = s.get(VcPost, pending.id)
            if post is None:
                continue
            post.kind = coerce_kind(entry.get("kind"))
            post.headline = clean_text(entry.get("headline"), 300)
            post.company = clean_text(entry.get("company"), 200)
            post.round_label = tidy_round(clean_text(entry.get("round"), 80))
            post.amount = clean_text(entry.get("amount"), 120)
            post.classified_at = now
            written += 1
    return written


@dataclass
class ClassifyResult:
    considered: int = 0
    classified: int = 0
    batches: int = 0
    cost_usd: float = 0.0
    stopped_reason: str | None = None


def classify_pending(limit: int | None = None,
                     batch_size: int = BATCH_SIZE) -> ClassifyResult:
    """Sort every unread post. Stops cleanly when the budget says so."""
    result = ClassifyResult()
    pending = _load_pending(limit)
    result.considered = len(pending)
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        by_number = dict(enumerate(batch, start=1))
        try:
            parsed, call = invoke_json(FEATURE, build_prompt(batch),
                                       system=SYSTEM_PROMPT, max_tokens=4096)
        except BudgetExceeded as exc:
            result.stopped_reason = str(exc)
            log.warning("vc_posts: %s", exc)
            break
        except (LlmUnavailable, ValueError) as exc:
            result.stopped_reason = str(exc)
            log.error("vc_posts: batch failed: %s", exc)
            break
        result.batches += 1
        result.cost_usd += call.cost_usd
        if isinstance(parsed, list):
            result.classified += _apply(parsed, by_number)
    return result


# --- One refresh ------------------------------------------------------------------------

@dataclass
class VcResult:
    reads: list[VcRead] = field(default_factory=list)
    skipped: int = 0                   # Firecrawl reads made too recently
    classify: ClassifyResult | None = None

    @property
    def new(self) -> int:
        return sum(r.items_new for r in self.reads)

    @property
    def credits(self) -> int:
        return sum(r.credits_used for r in self.reads)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.reads if r.error)


def refresh_firms(only: str | None = None, force: bool = False,
                  free_only: bool = False, classify: bool = True) -> VcResult:
    """Read every firm that is due, then sort the new posts."""
    firms = [f for f in load_firms() if not only or f.key == only]
    if only and not firms:
        raise ValueError(f"unknown firm: {only}")

    due: list[tuple[VcFirm, Read]] = []
    result = VcResult()
    for f in firms:
        for r in f.reads:
            if r.uses_firecrawl and (free_only or not (force or firecrawl_due(f, r))):
                result.skipped += 1
            else:
                due.append((f, r))

    # Every firm is a different site, so reading a few at once is still polite.
    with ThreadPoolExecutor(max_workers=6) as pool:
        result.reads = list(pool.map(lambda job: read_firm(*job), due))

    if classify:
        result.classify = classify_pending()
    log.info("vc firms: %d reads (%d failed, %d not due), %d new posts, %d credits%s",
             len(result.reads), result.failed, result.skipped, result.new, result.credits,
             f", ${result.classify.cost_usd:.6f} to sort {result.classify.classified}"
             if result.classify else "")
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Read VC firms' news for Insights.")
    ap.add_argument("--once", action="store_true", help="run a single pass")
    ap.add_argument("--firm", help="restrict to one firm by key")
    ap.add_argument("--force", action="store_true",
                    help="make Firecrawl reads even if made recently (costs credits)")
    ap.add_argument("--free-only", action="store_true",
                    help="feeds and plain HTML only; no Firecrawl credits")
    ap.add_argument("--no-classify", action="store_true",
                    help="skip sorting new posts (the paid step)")
    args = ap.parse_args(argv)
    if not args.once:
        ap.error("pass --once; the 12h refresh runs this after the news feed")

    init_db()
    result = refresh_firms(only=args.firm, force=args.force, free_only=args.free_only,
                           classify=not args.no_classify)
    print("\n%-26s %-7s %-6s %-5s %-4s %s" % ("FIRM", "VIA", "SEEN", "NEW", "CR", "STATUS"))
    for r in sorted(result.reads, key=lambda r: r.firm):
        print("%-26s %-7s %-6d %-5d %-4d %s" % (r.firm[:26], r.via, r.items_seen,
                                                 r.items_new, r.credits_used,
                                                 ("ERROR: " + r.error[:60]) if r.error else "ok"))
    print(f"\n{len(result.reads)} reads, {result.skipped} Firecrawl reads not due, "
          f"{result.new} new posts, {result.credits} Firecrawl credits")
    if result.classify:
        c = result.classify
        print(f"sorted {c.classified}/{c.considered} in {c.batches} batch(es), ${c.cost_usd:.6f}"
              + (f" -- stopped: {c.stopped_reason[:80]}" if c.stopped_reason else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
