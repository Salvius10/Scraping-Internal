"""Insights > Events organised: events the tracked VC firms run, and events on
websites the reader adds.

Two kinds of source:

  luma   a Luma calendar, read through an Apify Store actor
         (`apify_luma_actor`). It returns each event as structured data --
         name, times, venue, city -- so no model is needed. Billed per event
         in Apify, ~$0.002 each. Upcoming events are read every
         `events_hours`; past events once per calendar, as a backfill. After
         that, upcoming events become past ones in our own table.
  page   any other events page. Firecrawl scrapes it (1 credit), then gpt-oss
         lists the events in the page text (~$0.001, ledgered). A page whose
         text has not changed since the last read is not sent to the model.
  html   an events page that plain HTTP can read: fetched free (public
         addresses only, 3 MB cap), then the same as page.

Sources are each firm's `events:` in vc_firms.yaml, plus the websites pasted
on the page (EventSource rows). A pasted Luma link is read as a calendar.

    python -m app.ingest.events --once                   # every source that is due
    python -m app.ingest.events --once --source peak-xv  # one firm, or site-3
    python -m app.ingest.events --once --force           # due or not (costs)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy import delete, select

from ..config import settings
from ..db import init_db, session_scope
from ..extract import ExtractError, fetch_page
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import Event, EventRead, EventSource, utcnow
from ..search import apify, firecrawl
from .vc_firms import load_firms

log = logging.getLogger(__name__)

FEATURE = "events_page"
IST = timezone(timedelta(hours=5, minutes=30))
INDIA_TIMEZONES = ("Asia/Kolkata", "Asia/Calcutta")
LUMA_HOSTS = ("lu.ma", "luma.com")
MAX_PAGE_CHARS = 20_000        # page text sent to the model: ~5k tokens, ~$0.001
VIA_LABELS = {"luma": "Luma, via Apify", "page": "Website, via Firecrawl",
              "html": "Website"}


# --- Sources ----------------------------------------------------------------------

@dataclass(frozen=True)
class Feed:
    """One place events are read from."""

    key: str                   # "peak-xv:luma:1a2b3c4d" (registry) or "site-3" (pasted)
    via: str                   # luma | page
    url: str
    label: str                 # the organiser, as shown on the page
    firm: str | None = None    # tracked firm key, when it is one
    india_only: bool = False
    site: bool = False         # the firm's own home page, checked weekly
    source_id: int | None = None
    note: str | None = None

    @property
    def custom(self) -> bool:
        return self.source_id is not None

    @property
    def via_label(self) -> str:
        if self.site:
            return "Firm website" if self.via == "html" else "Firm website, via Firecrawl"
        return VIA_LABELS[self.via]

    @property
    def every(self) -> timedelta:
        return timedelta(hours=settings.events_site_hours if self.site
                         else settings.events_hours)


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:8]


def is_luma(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.") in LUMA_HOSTS


def actor_can_read(url: str) -> bool:
    """A Luma link the actor can read. A calendar with no short name lives at
    /calendar/cal-..., which it cannot (NOT_FOUND); that is read as a page."""
    return is_luma(url) and not urlparse(url).path.lower().startswith("/calendar/")


def actor_url(url: str) -> str:
    """The link as the Luma actor accepts it.

    Luma moved from lu.ma to luma.com (lu.ma now redirects), but the actor
    only accepts lu.ma links -- a luma.com link comes back as BAD_URL ("That
    link is not on lu.ma"), found on the first live run, 2026-10-06. The
    paths are the same on both, so only the host changes.
    """
    parts = urlparse(url)
    if (parts.hostname or "").lower().removeprefix("www.") == "luma.com":
        parts = parts._replace(netloc="lu.ma")
    return parts._replace(scheme="https").geturl()


def registry_feeds() -> list[Feed]:
    return [
        Feed(key=f"{firm.key}:{at.via}:{_short_hash(at.url)}", via=at.via, url=at.url,
             label=firm.label, firm=firm.key, india_only=at.india_only, site=at.site,
             note=at.note)
        for firm in load_firms() for at in firm.events
    ]


def all_feeds() -> list[Feed]:
    """The firms' calendars from the registry, then the pasted websites."""
    firms = {f.key: f.label for f in load_firms()}
    with session_scope() as s:
        pasted = [
            Feed(key=f"site-{row.id}", via=row.via, url=row.url, label=row.label,
                 firm=row.firm if row.firm in firms else None, source_id=row.id)
            for row in s.scalars(select(EventSource).order_by(EventSource.id)).all()
        ]
    return registry_feeds() + pasted


class SourceError(ValueError):
    """A pasted source that cannot be added, worded for the reader."""


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise SourceError("Paste a link to add.")
    if len(url) > 1000:
        raise SourceError("That link is too long.")
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise SourceError("That does not look like a web address.")
    if parts.username or parts.password:
        raise SourceError("Links with a username or password are not allowed.")
    return url


def add_source(url: str, label: str | None = None, firm: str | None = None) -> Feed:
    """Save a pasted website as an event source. Does not read it."""
    url = normalise_url(url)
    firms = {f.key: f.label for f in load_firms()}
    if firm and firm not in firms:
        raise SourceError(f"Unknown firm {firm!r}.")
    if any(f.url.rstrip("/").lower() == url.rstrip("/").lower() for f in all_feeds()):
        raise SourceError("That link is already a source.")

    label = " ".join((label or "").split())[:200] or (firms.get(firm) if firm else None) \
        or (urlparse(url).hostname or url).removeprefix("www.")
    with session_scope() as s:
        row = EventSource(url=url, label=label, firm=firm or None,
                          via="luma" if actor_can_read(url) else "page")
        s.add(row)
        s.flush()
        source_id = row.id
    return next(f for f in all_feeds() if f.source_id == source_id)


def remove_source(source_id: int) -> bool:
    """Drop a pasted source with the events and reads that came from it."""
    key = f"site-{source_id}"
    with session_scope() as s:
        row = s.get(EventSource, source_id)
        if row is None:
            return False
        s.execute(delete(Event).where(Event.source_key == key))
        s.execute(delete(EventRead).where(EventRead.source_key == key))
        s.delete(row)
    return True


# --- Reading ----------------------------------------------------------------------

@dataclass
class Found:
    url: str
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    date_only: bool = False
    timezone: str | None = None
    venue: str | None = None
    city: str | None = None
    country: str | None = None
    online: bool | None = None
    host: str | None = None
    description: str | None = None
    own_link: bool = True          # False when the url is just the page read


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if not text or text.lower() in ("null", "none", "n/a", "tba", "tbd"):
        return None
    return text[:limit]


def _web_url(value: object, base: str) -> str | None:
    """An absolute http(s) link, or None. Anything else never reaches an href."""
    if not isinstance(value, str) or not value.strip():
        return None
    url = urljoin(base, value.strip())
    return url if urlparse(url).scheme in ("http", "https") else None


def parse_when(value: object) -> tuple[datetime | None, bool]:
    """(UTC time, day only?) from "2026-10-14T18:30", "2026-10-14" or "14 Oct 2026".

    A time with no zone is taken as Indian time. A day with no time is stored
    as midnight IST and flagged, so it is shown as a day, never a made-up hour.
    """
    text = _text(value, 80)
    if not text:
        return None, False
    has_time = bool(re.search(r"\d{1,2}:\d{2}|\d\s*(am|pm)\b", text, re.I))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            import dateparser
            parsed = dateparser.parse(text, settings={"TIMEZONE": "Asia/Kolkata",
                                                      "RETURN_AS_TIMEZONE_AWARE": True})
        except Exception:  # noqa: BLE001 - a bad date must not fail the read
            parsed = None
    if parsed is None:
        return None, False
    if not has_time:
        parsed = datetime.combine(parsed.date(), datetime.min.time(), IST)
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc), not has_time


def in_india(row: dict) -> bool:
    return ((row.get("countryCode") or "").upper() == "IN"
            or row.get("timezone") in INDIA_TIMEZONES)


def from_luma(row: dict) -> Found | None:
    """One event row from the Luma actor, or None for a row that is not one."""
    if row.get("_diagnostic") or row.get("_sample"):
        return None
    url = _web_url(row.get("url"), "https://luma.com/")
    title = _text(row.get("name"), 300)
    starts, _ = parse_when(row.get("startAt"))
    if not (url and title and starts):
        return None
    ends, _ = parse_when(row.get("endAt"))
    online = row.get("isOnline")
    return Found(
        url=url, title=title, starts_at=starts, ends_at=ends,
        timezone=_text(row.get("timezone"), 60),
        venue=_text(row.get("venueName") or row.get("addressShort"), 300),
        city=_text(row.get("city"), 120), country=_text(row.get("country"), 80),
        online=online if isinstance(online, bool) else None,
        host=_text(row.get("hostName") or row.get("calendarName"), 300),
        description=_text(row.get("description"), 400),
    )


def read_luma(feed: Feed, period: str, run: EventRead) -> list[Found]:
    """A Luma calendar's upcoming ("future") or past events, through Apify."""
    limit = settings.events_upcoming_items if period == "future" else settings.events_past_items
    ceiling = limit * settings.apify_price_per_event + settings.apify_price_per_run
    rows = apify.run_actor(
        settings.apify_luma_actor,
        {"startUrls": [actor_url(feed.url)], "maxItems": limit, "period": period,
         "includeDetails": True},
        max_items=limit, max_charge_usd=min(settings.apify_max_charge_usd, ceiling),
    )
    events = [r for r in rows if not r.get("_diagnostic") and not r.get("_sample")]
    # Charged per event row delivered; diagnostic and sample rows are free.
    run.apify_usd = round(len(events) * settings.apify_price_per_event
                          + settings.apify_price_per_run, 6)

    problems = [r for r in rows if r.get("_diagnostic")]
    if problems and not events:
        code = str(problems[0].get("errorCode") or "error")
        if code == "UNSUPPORTED_URL":
            raise ValueError("Luma cannot read that link as a calendar -- a personal "
                             "profile is not one. Paste the calendar's link instead.")
        if code == "NO_RESULTS":
            return []
        said = _text(problems[0].get("error") or problems[0].get("message"), 200)
        raise ValueError(f"Luma could not read that calendar ({code}"
                         + (f": {said}" if said else "") + ").")

    kept = [r for r in events if not feed.india_only or in_india(r)]
    return [f for f in (from_luma(r) for r in kept) if f is not None]


SYSTEM_PROMPT = (
    "You list events from a web page for someone tracking Indian startups and "
    "venture capital. You reply with JSON only -- no prose, no markdown fences."
)


def page_prompt(feed: Feed, page_url: str, text: str, today: date | None = None) -> str:
    today = today or utcnow().astimezone(IST).date()
    where = ("\nOnly list events held in India, or online. Leave out events elsewhere.\n"
             if feed.india_only else "")
    return f"""Today is {today.isoformat()} (India).

The page below, {page_url}, is from {feed.label}. List every event it
announces or records that {feed.label} organises or hosts, alone or with
partners, upcoming or past: demo days, meetups, office hours, summits,
webinars, workshops, pitch sessions.
Leave out: other organisers' conferences where its people only speak or
attend; videos, podcasts and recorded talks; news, portfolio announcements
and blog posts.{where}
Return one JSON object per event with keys:
  "title"        the event's name
  "start"        when it starts, ISO 8601: "2026-10-14T18:30" when the page
                 gives a time, "2026-10-14" when it gives only the day. If the
                 page shows no year, use the year that puts the date nearest today.
  "end"          when it ends, same format, or null
  "city"         the city, or null
  "venue"        the venue, or null
  "online"       true if online, false if in person, null if the page does not say
  "url"          the link to the event's own page if the page gives one, else null
  "description"  one factual sentence, at most 25 words, or null

Skip anything without a date. Do not invent events, dates or links.
Return a JSON array (empty if there are no events) and nothing else.

Page text:
{text}"""


def coerce_page_events(raw: object, base_url: str) -> list[Found]:
    """The model's answer as events. Untrusted: anything without a title and a
    readable start date is dropped, and links must be http(s)."""
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    if not isinstance(raw, list):
        return []
    out: list[Found] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = _text(entry.get("title") or entry.get("name"), 300)
        starts, date_only = parse_when(entry.get("start"))
        if not title or starts is None:
            continue
        ends, _ = parse_when(entry.get("end"))
        link = _web_url(entry.get("url"), base_url)
        online = entry.get("online")
        out.append(Found(
            url=link or base_url, own_link=link is not None and link != base_url,
            title=title, starts_at=starts, ends_at=ends, date_only=date_only,
            venue=_text(entry.get("venue"), 300), city=_text(entry.get("city"), 120),
            online=online if isinstance(online, bool) else None,
            description=_text(entry.get("description"), 400),
        ))
    return out


def _last_hash(key: str) -> str | None:
    with session_scope() as s:
        return s.scalar(
            select(EventRead.content_hash)
            .where(EventRead.source_key == key, EventRead.kind == "page",
                   EventRead.error.is_(None), EventRead.content_hash.is_not(None))
            .order_by(EventRead.started_at.desc(), EventRead.id.desc()).limit(1))


def html_text(html: str, base: str) -> str:
    """A page as plain lines for the model, each link kept as [text](url) so
    the model can give an event its own link."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "iframe", "form"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ").split())
        url = _web_url(a["href"], base)
        a.replace_with(f" [{text}]({url}) " if text and url else f" {text} ")
    lines = (" ".join(line.split()) for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


_EVENT_WORDS = re.compile(
    r"\b(events?|summit|demo ?days?|meetups?|webinars?|conference|conclave|workshops?|"
    r"office hours|bootcamp|hackathon|masterclass|mixer|roundtable|fireside chat|"
    r"rsvp|register now)\b", re.I)
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_A_DATE = re.compile(
    rf"\b(\d{{1,2}}(st|nd|rd|th)?\s+{_MONTH}|{_MONTH}\s+\d{{1,2}}(st|nd|rd|th)?\b"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[./]\d{{1,2}}[./]\d{{2,4}})", re.I)


def mentions_events(text: str) -> bool:
    """A free check before paying the model: does the page name an event and a
    date anywhere? Most firms' home pages do neither."""
    return bool(_EVENT_WORDS.search(text) and _A_DATE.search(text))


# Firecrawl's rate limit was hit with four page reads at once (first live run
# of every firm's home page, 2026-10-06). Page reads now go one at a time, and
# a rate-limited one waits this long and tries once more.
RATE_LIMIT_WAIT = 20.0


def _scrape(url: str):
    try:
        return firecrawl.scrape(url)
    except firecrawl.FirecrawlError as exc:
        if "rate limit" not in str(exc):
            raise
        time.sleep(RATE_LIMIT_WAIT)
        return firecrawl.scrape(url)


def read_page(feed: Feed, run: EventRead) -> list[Found] | None:
    """Events on a web page. None when the page is unchanged since last time.

    A firm's home page (`site`) is sent to the model only when it mentions an
    event and a date; otherwise it simply has none, for free."""
    if feed.via == "html":
        page_url, html = fetch_page(feed.url)      # free; guarded like Extract
        text = html_text(html, page_url)
    else:
        page, cached = _scrape(feed.url)
        run.credits_used = 0 if cached else page.credits_used
        page_url, text = page.url or feed.url, page.markdown
    text = text[:MAX_PAGE_CHARS]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if digest == _last_hash(feed.key):
        run.content_hash = digest
        return None
    if feed.site and not mentions_events(text):
        run.content_hash = digest
        return []

    parsed, call = invoke_json(FEATURE, page_prompt(feed, page_url, text),
                               system=SYSTEM_PROMPT, max_tokens=3000)
    run.llm_usd = round(call.cost_usd, 6)
    run.content_hash = digest      # only once the model has read it
    return coerce_page_events(parsed, page_url)


# --- Storing --------------------------------------------------------------------------

def event_uid(feed: Feed, found: Found) -> str:
    """Stable within a source: the event's own link, or its title and day."""
    if found.own_link:
        ident = found.url.rstrip("/").lower()
    else:
        day = found.starts_at.astimezone(IST).date().isoformat()
        ident = f"{found.url}#{' '.join(found.title.lower().split())}|{day}"
    return hashlib.sha1(f"{feed.key}\n{ident}".encode("utf-8")).hexdigest()


_FIELDS = ("url", "title", "starts_at", "ends_at", "date_only", "timezone", "venue",
           "city", "country", "online", "host", "description")


def store(feed: Feed, found: list[Found], via: str) -> int:
    """Insert new events, update known ones (times and venues change). Returns new."""
    new = 0
    now = utcnow()
    with session_scope() as s:
        for f in found:
            uid = event_uid(feed, f)
            values = {name: getattr(f, name) for name in _FIELDS}
            row = s.scalar(select(Event).where(Event.uid == uid))
            if row is None:
                s.add(Event(uid=uid, source_key=feed.key, firm=feed.firm, via=via,
                            found_at=now, updated_at=now, **values))
                s.flush()
                new += 1
            else:
                for name, value in values.items():
                    setattr(row, name, value)
                row.firm, row.updated_at = feed.firm, now
    return new


# --- One refresh ------------------------------------------------------------------------

def _last_good(key: str, kind: str) -> datetime | None:
    with session_scope() as s:
        return s.scalar(
            select(EventRead.started_at)
            .where(EventRead.source_key == key, EventRead.kind == kind,
                   EventRead.error.is_(None))
            .order_by(EventRead.started_at.desc()).limit(1))


def due_kinds(feed: Feed, now: datetime | None = None, force: bool = False) -> list[str]:
    """Which reads of this source are due. Past events are a one-off backfill."""
    now = now or utcnow()

    def stale(kind: str) -> bool:
        last = _last_good(feed.key, kind)
        return last is None or now - last >= feed.every

    if feed.via == "luma":
        kinds = ["upcoming"] if force or stale("upcoming") else []
        if force or _last_good(feed.key, "past") is None:
            kinds.append("past")
        return kinds
    return ["page"] if force or stale("page") else []


def read_feed(feed: Feed, kind: str) -> EventRead:
    """Make one read of one source. Never raises; the outcome is on the EventRead."""
    run = EventRead(source_key=feed.key, kind=kind, started_at=utcnow(), items_seen=0,
                    items_new=0, apify_usd=0.0, credits_used=0, llm_usd=0.0)
    try:
        if kind == "page":
            found = read_page(feed, run)
            via = "firecrawl" if feed.via == "page" else "web"
        else:
            found = read_luma(feed, "future" if kind == "upcoming" else "past", run)
            via = "apify"
        if found is not None:
            run.items_seen = len(found)
            run.items_new = store(feed, found, via)
    except (apify.ApifyError, firecrawl.FirecrawlError, ExtractError) as exc:
        run.error = str(exc)
    except BudgetExceeded as exc:
        run.error = f"The LLM budget refused this read: {exc}"
    except LlmUnavailable:
        run.error = "The model is unreachable right now; the page is read again next refresh."
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
        run.error = f"{type(exc).__name__}: {exc}"[:300]
    if run.error:
        log.warning("events %s (%s): %s", feed.key, kind, run.error)
    with session_scope() as s:
        s.add(run)
    return run


@dataclass
class EventsResult:
    reads: list[EventRead] = field(default_factory=list)
    skipped: int = 0               # sources read recently enough

    @property
    def new(self) -> int:
        return sum(r.items_new for r in self.reads)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.reads if r.error)

    @property
    def apify_usd(self) -> float:
        return sum(r.apify_usd for r in self.reads)

    @property
    def credits(self) -> int:
        return sum(r.credits_used for r in self.reads)

    @property
    def llm_usd(self) -> float:
        return sum(r.llm_usd for r in self.reads)


def refresh_events(only: str | None = None, force: bool = False,
                   free_only: bool = False) -> EventsResult:
    """Read every source that is due. Every read here is paid (Apify, or
    Firecrawl and gpt-oss), so `free_only` reads nothing."""
    feeds = [f for f in all_feeds() if not only or only in (f.key, f.firm)]
    if only and not feeds:
        raise ValueError(f"unknown events source: {only}")

    result = EventsResult()
    jobs: list[tuple[Feed, str]] = []
    now = utcnow()
    for feed in feeds:
        kinds = [] if free_only else due_kinds(feed, now, force)
        if kinds:
            jobs += [(feed, kind) for kind in kinds]
        else:
            result.skipped += 1

    # Each source is a different site, so a few at once is still polite --
    # except Firecrawl page reads, which share one rate limit and go one at a
    # time, alongside the rest.
    firecrawl_jobs = [job for job in jobs if job[0].via == "page"]
    other_jobs = [job for job in jobs if job[0].via != "page"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        others = pool.map(lambda job: read_feed(*job), other_jobs)
        paced = [read_feed(*job) for job in firecrawl_jobs]
        result.reads = list(others) + paced

    log.info("events: %d reads (%d failed, %d sources not due), %d new events, "
             "~$%.4f Apify, %d Firecrawl credits, $%.6f LLM",
             len(result.reads), result.failed, result.skipped, result.new,
             result.apify_usd, result.credits, result.llm_usd)
    return result


def read_now(feed: Feed) -> EventsResult:
    """Read one source straight away, e.g. just after it was pasted. A new
    Luma calendar gets its upcoming and past events; anything else, one read."""
    kinds = due_kinds(feed) or (["upcoming"] if feed.via == "luma" else ["page"])
    return EventsResult(reads=[read_feed(feed, kind) for kind in kinds])


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Read events organised by VC firms.")
    ap.add_argument("--once", action="store_true", help="run a single pass")
    ap.add_argument("--source", help="one firm key or source key (e.g. site-3)")
    ap.add_argument("--force", action="store_true",
                    help="read even if read recently (costs Apify / Firecrawl)")
    args = ap.parse_args(argv)
    if not args.once:
        ap.error("pass --once; the 12h refresh runs this after the VC firms")

    init_db()
    labels = {f.key: f.label for f in all_feeds()}
    result = refresh_events(only=args.source, force=args.force)
    print("\n%-34s %-9s %-5s %-5s %s" % ("SOURCE", "READ", "SEEN", "NEW", "STATUS"))
    for r in sorted(result.reads, key=lambda r: r.source_key):
        print("%-34s %-9s %-5d %-5d %s" % (
            labels.get(r.source_key, r.source_key)[:34], r.kind, r.items_seen,
            r.items_new, ("ERROR: " + r.error[:70]) if r.error else "ok"))
    print(f"\n{len(result.reads)} reads, {result.skipped} sources not due, {result.new} new "
          f"events; ~${result.apify_usd:.4f} Apify, {result.credits} Firecrawl credits, "
          f"${result.llm_usd:.6f} LLM")
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
