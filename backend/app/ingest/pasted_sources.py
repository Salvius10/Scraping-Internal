"""Insights: websites the reader pastes on Startup firms or VC firms.

Each page keeps its own sources and shows what they publish in its own format:

  startups  every new post is read by gpt-oss for a funding round, as the
            feed's Funding stories are; only rounds reach the stage tabs
            (`PastedRound` rows, `kept`)
  vcs       new posts are sorted like a tracked firm's own news -- Investment,
            Portfolio news, Fund news or Other -- by the VC firms' own pass
            (`VcPost` rows under the firm key "pasted-<id>")

The link is untrusted, so every plain fetch, post pages included, goes through
Extract's guarded fetcher: public addresses, standard ports, 3 MB. How a
source is read is settled when it is added, cheapest way that finds posts:

  rss     the link is a feed, or its page declares one           free
  html    post links on the page itself, over plain HTTP         free
  scrape  Firecrawl, for a page that refuses plain HTTP or
          shows no links without a browser                       1 credit

Every 12h refresh reads the free ones, and a Firecrawl one at most every
`pasted_firecrawl_hours`. The model reads only new posts, 20 (startups) or 25
(VC) a call, ~$0.00005 each. A source is read once straight away when added.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..extract import ExtractError, check_url, fetch_page
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import (
    Article, Category, FundingRound, PastedRound, PastedSource, PastedSourceRead, VcPost,
    WebRound, utcnow,
)
from ..search import firecrawl
from .describe import extract_meta
from .events import SourceError, normalise_url
from .rounds import (
    PROMPT_HEAD, SYSTEM_PROMPT, _Pending, build_prompt, clean_investors, clean_text,
    coerce_stage, tidy_round,
)
from .rss import parse_feed
from .sources import load_sources
from .vc_firms import ClassifyResult, Found, classify_pending, load_firms, posts_from_html, \
    posts_from_markdown
from .web_rounds import DUPLICATE_DAYS

log = logging.getLogger(__name__)

SECTIONS = ("startups", "vcs")
FIRM_PREFIX = "pasted-"
FEATURE = "pasted_rounds"
BATCH_SIZE = 20
# New posts on a news page: how many to open for an exact date. Free, but polite.
PAGE_FETCHES_PER_READ = 12
VIA_LABELS = {"rss": "its feed", "html": "its news page", "scrape": "its page, via Firecrawl"}
_FEED_TYPES = ("application/rss+xml", "application/atom+xml")


def firm_key(source_id: int) -> str:
    """The VcPost firm a pasted VC source's posts are stored under."""
    return f"{FIRM_PREFIX}{source_id}"


def all_sources(section: str | None = None) -> list[PastedSource]:
    with session_scope() as s:
        stmt = select(PastedSource).order_by(PastedSource.id)
        if section:
            stmt = stmt.where(PastedSource.section == section)
        return list(s.scalars(stmt).all())


def vc_labels() -> dict[str, str]:
    """The firm each pasted VC source's posts are sorted as, by firm key: the
    tracked firm it was tied to, else the source's own name."""
    firms = {f.key: f.label for f in load_firms()}
    return {firm_key(src.id): firms.get(src.firm or "", src.label)
            for src in all_sources("vcs")}


# --- Settling how to read a link ------------------------------------------------------

_XML_ENCODING = re.compile(r"""^(\s*<\?xml[^>]*?\sencoding=)(["'])[^"']*\2""")


def _feed(body: str, url: str) -> list[Found]:
    """Posts from a feed, or [] when the text is not one."""
    # Bytes, never a str: feedparser fetches a string that looks like a URL.
    # The text is already decoded, so its declaration must say UTF-8 too.
    raw = _XML_ENCODING.sub(r"\1\2utf-8\2", body, count=1).encode("utf-8")
    try:
        items = parse_feed(raw, urlparse(url).hostname or "pasted")
    except ValueError:
        return []
    return [Found(url=i.url, title=i.headline, snippet=i.description,
                  published_at=i.published_at)
            for i in items
            if urlparse(i.url).scheme in ("http", "https")][:settings.pasted_items_per_read]


def feed_link(html: str, base: str) -> str | None:
    """The feed a page declares in its <head>, skipping comment feeds. A page
    can declare several -- a WordPress category page lists the whole site's
    feed first -- so one under the page's own path wins."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for tag in soup.find_all("link", href=True):
        rels = [r.lower() for r in tag.get("rel") or []]
        if "alternate" not in rels or (tag.get("type") or "").lower() not in _FEED_TYPES:
            continue
        if "comment" in (tag.get("title") or "").lower():
            continue
        url = urljoin(base, tag["href"].strip())
        if urlparse(url).scheme in ("http", "https"):
            found.append(url)
    own = base.split("?")[0].rstrip("/").lower() + "/"
    return next((u for u in found if u.lower().startswith(own)), found[0] if found else None)


@dataclass(frozen=True)
class Plan:
    via: str
    read_url: str


def plan_read(url: str) -> Plan:
    """How to read a pasted link: the cheapest way that finds posts. Plain
    fetches only; Firecrawl is the fallback, and is not called here."""
    page = None
    try:
        page = fetch_page(url)
    except (ExtractError, httpx.HTTPError) as exc:
        log.info("pasted %s: plain fetch failed (%s); Firecrawl next", url, exc)
    if page is not None:
        final, body = page
        if _feed(body, final):
            return Plan("rss", final)
        declared = feed_link(body, final)
        if declared:
            try:
                feed_url, feed_body = fetch_page(declared)
                if _feed(feed_body, feed_url):
                    return Plan("rss", feed_url)
            except (ExtractError, httpx.HTTPError):
                pass
        if posts_from_html(body, final, limit=settings.pasted_items_per_read):
            return Plan("html", final)
    if not firecrawl.ready():
        raise SourceError(
            "That page cannot be read directly, and Firecrawl, which could read it, is "
            "not set up: add FIRECRAWL_API_KEY to .env and restart the server.")
    return Plan("scrape", url)


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.rstrip("/").lower() == b.rstrip("/").lower()


def add_source(section: str, url: str, label: str | None = None,
               firm: str | None = None) -> PastedSource:
    """Check a pasted link, settle how to read it and save it. Does not read it."""
    if section not in SECTIONS:
        raise SourceError(f"Unknown section {section!r}.")
    url = normalise_url(url)
    try:
        url = check_url(url)            # resolves, and to a public address
    except ExtractError as exc:
        raise SourceError(str(exc)) from None

    firms = {f.key: f for f in load_firms()}
    firm = (firm or None) if section == "vcs" else None
    if firm and firm not in firms:
        raise SourceError(f"Unknown firm {firm!r}.")
    if any(_same(url, src.url) for src in all_sources(section)):
        raise SourceError("That link is already a source here.")
    if section == "vcs":
        taken = next((f for f in firms.values() if f.site and _same(url, f.site.url)), None)
        if taken:
            raise SourceError(f"{taken.label}'s news is already read from that link.")
    else:
        taken = next((s for s in load_sources() if _same(url, s.home) or _same(url, s.feed_url)),
                     None)
        if taken:
            raise SourceError(f"{taken.label} is already in the news feed.")

    plan = plan_read(url)
    label = (" ".join((label or "").split())[:200]
             or (firms[firm].label if firm else None)
             or (urlparse(url).hostname or url).removeprefix("www."))
    with session_scope() as s:
        row = PastedSource(section=section, url=url, read_url=plan.read_url, via=plan.via,
                           label=label, firm=firm)
        s.add(row)
    return row


def remove_source(source_id: int) -> bool:
    """Drop a pasted source with everything read from it."""
    with session_scope() as s:
        row = s.get(PastedSource, source_id)
        if row is None:
            return False
        s.execute(delete(VcPost).where(VcPost.firm == firm_key(source_id)))
        s.execute(delete(PastedRound).where(PastedRound.source_id == source_id))
        s.execute(delete(PastedSourceRead).where(PastedSourceRead.source_id == source_id))
        s.delete(row)
    return True


# --- Reading ------------------------------------------------------------------------------

def _fetch(source: PastedSource) -> tuple[list[Found], int]:
    """The posts a source lists now. Returns (posts, Firecrawl credits)."""
    limit = settings.pasted_items_per_read
    if source.via == "scrape":
        page, cached = firecrawl.scrape(source.read_url)
        found = posts_from_markdown(page.markdown, page.url or source.read_url, limit=limit)
        return found, 0 if cached else page.credits_used
    final, body = fetch_page(source.read_url)
    if source.via == "rss":
        return _feed(body, final), 0
    return posts_from_html(body, final, limit=limit), 0


def _unseen(source: PastedSource, found: list[Found]) -> list[Found]:
    urls = [f.url for f in found]
    with session_scope() as s:
        if source.section == "vcs":
            stmt = select(VcPost.url).where(VcPost.firm == firm_key(source.id),
                                            VcPost.url.in_(urls))
        else:
            stmt = select(PastedRound.url).where(PastedRound.source_id == source.id,
                                                 PastedRound.url.in_(urls))
        known = set(s.scalars(stmt).all())
    new = []
    for f in found:
        if f.url not in known:
            known.add(f.url)
            new.append(f)
    return new


def _fill_from_pages(found: list[Found]) -> None:
    """New posts from a news page with no exact date: open each post (guarded)
    and read its own publish time and description. Free; a page that fails is
    left as it is."""
    todo = [f for f in found if f.published_at is None or f.date_approx]
    for n, f in enumerate(todo[:PAGE_FETCHES_PER_READ]):
        if n:
            time.sleep(settings.per_domain_delay / 2)
        try:
            _, html = fetch_page(f.url)
        except (ExtractError, httpx.HTTPError):
            continue
        meta = extract_meta(html)
        if meta.published_at:
            f.published_at, f.date_approx = meta.published_at, False
        if not f.snippet and meta.description:
            f.snippet = meta.description


def _store(source: PastedSource, new: list[Found]) -> None:
    with session_scope() as s:
        for f in new:
            values = {"url": f.url[:1000], "snippet": clean_text(f.snippet, 400),
                      "published_at": f.published_at, "date_approx": f.date_approx}
            if source.section == "vcs":
                s.add(VcPost(firm=firm_key(source.id), title=f.title, via="added", **values))
            else:
                s.add(PastedRound(source_id=source.id, headline=f.title, **values))


def read_source(source: PastedSource) -> PastedSourceRead:
    """One read of one source. Never raises; the outcome is on the returned read."""
    run = PastedSourceRead(source_id=source.id, started_at=utcnow(), items_seen=0,
                           items_new=0, credits_used=0)
    try:
        found, run.credits_used = _fetch(source)
        if not found:
            run.error = "No posts found on that page. Has it changed?"
        else:
            run.items_seen = len(found)
            new = _unseen(source, found)
            if source.via == "html":
                _fill_from_pages(new)
            _store(source, new)
            run.items_new = len(new)
    except (ExtractError, firecrawl.FirecrawlError) as exc:
        run.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
        run.error = f"{type(exc).__name__}: {exc}"[:300]
    if run.error:
        log.warning("pasted source %d (%s): %s", source.id, source.via, run.error)
    with session_scope() as s:
        s.add(run)
    return run


# --- Startup posts -> funding rounds ------------------------------------------------------

PASTED_NOTE = (
    "These are posts from a website a reader added, and many are not about funding. "
    "Also return \"raise\": true when the item announces a company raising money "
    "(any round, debt or IPO money), else false.\n"
)


@dataclass
class RoundsRead:
    considered: int = 0
    extracted: int = 0
    kept: int = 0                # rounds that reach the tabs
    batches: int = 0
    cost_usd: float = 0.0
    stopped_reason: str | None = None


def _round_known(s: Session, row: PastedRound) -> bool:
    """The same company at the same stage within DUPLICATE_DAYS -- in the feed,
    Search web or another pasted post -- is the same round, shown once."""
    name = row.company.strip().lower()
    feed = (select(func.count(FundingRound.id))
            .join(Article, Article.id == FundingRound.article_id)
            .where(func.lower(FundingRound.company) == name, FundingRound.stage == row.stage,
                   Article.canonical_id.is_(None), Article.category == Category.FUNDING))
    web = select(func.count(WebRound.id)).where(
        WebRound.kept.is_(True), func.lower(WebRound.company) == name,
        WebRound.stage == row.stage)
    pasted = select(func.count(PastedRound.id)).where(
        PastedRound.kept.is_(True), PastedRound.id != row.id,
        func.lower(PastedRound.company) == name, PastedRound.stage == row.stage)
    if row.published_at is not None:
        lo = row.published_at - timedelta(days=DUPLICATE_DAYS)
        hi = row.published_at + timedelta(days=DUPLICATE_DAYS)
        feed = feed.where(Article.published_at.between(lo, hi))
        web = web.where(WebRound.published_at.between(lo, hi))
        pasted = pasted.where(PastedRound.published_at.between(lo, hi))
    return any(s.scalar(q) for q in (feed, web, pasted))


def _apply(entries: list, by_number: dict[int, _Pending]) -> tuple[int, int]:
    """Write one batch. A post the model skipped stays pending for next time.
    Returns (posts read, rounds kept)."""
    got: dict[int, dict] = {}
    for entry in entries:
        if isinstance(entry, dict):
            try:
                got[int(entry.get("i"))] = entry
            except (TypeError, ValueError):
                continue
    written = kept = 0
    now = utcnow()
    with session_scope() as s:
        for number, pending in by_number.items():
            entry = got.get(number)
            row = s.get(PastedRound, pending.id) if entry else None
            if row is None:
                continue
            row.round_label = tidy_round(clean_text(entry.get("round"), 80))
            row.company = clean_text(entry.get("company"), 200)
            row.stage = coerce_stage(entry.get("stage"), row.round_label)
            row.amount = clean_text(entry.get("amount"), 120)
            row.investors = clean_investors(entry.get("investors"))
            row.extracted_at = now
            row.kept = (entry.get("raise") is True and row.company is not None
                        and not _round_known(s, row))
            written += 1
            kept += row.kept
    return written, kept


def extract_rounds(source_ids: list[int] | None = None) -> RoundsRead:
    """Read every new pasted startup post for a round, or only those of
    `source_ids`. Stops cleanly when the budget says so."""
    stmt = (select(PastedRound.id, PastedRound.headline, PastedRound.snippet)
            .where(PastedRound.extracted_at.is_(None)).order_by(PastedRound.id))
    if source_ids is not None:
        stmt = stmt.where(PastedRound.source_id.in_(source_ids))
    with session_scope() as s:
        pending = [_Pending(i, headline, snippet, None)
                   for i, headline, snippet in s.execute(stmt).all()]

    result = RoundsRead(considered=len(pending))
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        prompt = build_prompt(batch).replace(PROMPT_HEAD, PROMPT_HEAD + "\n" + PASTED_NOTE, 1)
        try:
            parsed, call = invoke_json(FEATURE, prompt, system=SYSTEM_PROMPT, max_tokens=4096)
        except (BudgetExceeded, LlmUnavailable, ValueError) as exc:
            log.warning("pasted rounds: %s", exc)
            result.stopped_reason = str(exc)
            break
        result.batches += 1
        result.cost_usd += call.cost_usd
        if isinstance(parsed, list):
            written, kept = _apply(parsed, dict(enumerate(batch, start=1)))
            result.extracted += written
            result.kept += kept
    return result


# --- One refresh ----------------------------------------------------------------------

@dataclass
class PastedResult:
    reads: list[PastedSourceRead] = field(default_factory=list)
    skipped: int = 0                         # Firecrawl reads made too recently
    sorted: ClassifyResult | None = None     # VC posts
    rounds: RoundsRead | None = None         # startup posts

    @property
    def new(self) -> int:
        return sum(r.items_new for r in self.reads)

    @property
    def credits(self) -> int:
        return sum(r.credits_used for r in self.reads)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.reads if r.error)

    @property
    def llm_usd(self) -> float:
        return sum(x.cost_usd for x in (self.sorted, self.rounds) if x is not None)


def sort_new(sources: list[PastedSource], result: PastedResult) -> None:
    """Send the sources' unread posts to the model: VC posts are sorted, startup
    posts read for a round. No call when nothing is unread."""
    vc_keys = [firm_key(s.id) for s in sources if s.section == "vcs"]
    startup_ids = [s.id for s in sources if s.section == "startups"]
    if vc_keys:
        result.sorted = classify_pending(firms=vc_keys)
    if startup_ids:
        result.rounds = extract_rounds(startup_ids)


def _last_good_read(source_id: int) -> datetime | None:
    with session_scope() as s:
        return s.scalar(
            select(PastedSourceRead.started_at)
            .where(PastedSourceRead.source_id == source_id, PastedSourceRead.error.is_(None))
            .order_by(PastedSourceRead.started_at.desc()).limit(1))


def firecrawl_due(source: PastedSource, now: datetime | None = None) -> bool:
    """A Firecrawl read costs a credit, so it is made at most every
    `pasted_firecrawl_hours`. A failed read does not count."""
    last = _last_good_read(source.id)
    return last is None or (now or utcnow()) - last >= timedelta(
        hours=settings.pasted_firecrawl_hours)


def refresh_pasted(section: str | None = None, free_only: bool = False,
                   classify: bool = True) -> PastedResult:
    """Read every pasted source that is due, then send new posts to the model."""
    sources = all_sources(section)
    result = PastedResult()
    due = []
    for source in sources:
        if source.via == "scrape" and (free_only or not firecrawl_due(source)):
            result.skipped += 1
        else:
            due.append(source)

    # Every source is a different site, so a few at once is still polite.
    with ThreadPoolExecutor(max_workers=4) as pool:
        result.reads = list(pool.map(read_source, due))
    if classify:
        sort_new(sources, result)
    log.info("pasted sources: %d reads (%d failed, %d not due), %d new posts, "
             "%d Firecrawl credits, $%.6f", len(result.reads), result.failed,
             result.skipped, result.new, result.credits, result.llm_usd)
    return result


def read_now(source: PastedSource) -> PastedResult:
    """Read one source straight away -- just after it was pasted -- and send its
    posts to the model."""
    result = PastedResult(reads=[read_source(source)])
    sort_new([source], result)
    return result
