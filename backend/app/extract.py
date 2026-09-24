"""User-directed extraction: any public page, any question, via ScrapeGraphAI.

A reader pastes a URL and says in plain words what to pull out ("every
funding round with amount and investors"). ScrapeGraphAI's prompt-driven
extraction is exactly that, so this module puts it behind two kinds of guard:

  Safety -- the URL is untrusted. Our server fetches it, not ScrapeGraphAI:
    http(s) only, standard ports, public addresses only (no localhost, no
    private network, no cloud metadata endpoint), every redirect re-checked,
    3MB cap, HTML/text only. ScrapeGraphAI is then handed the HTML, so it
    never makes a network request of its own.

  Spend -- every model call is cap-checked and ledgered under "extract". The
    page is stripped to text-bearing markup and capped, which bounds the
    worst case near $0.01; one extraction runs at a time; and the same URL
    and question within an hour is served from cache for $0.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .llm.budget import BudgetExceeded, ensure_budget, estimate_cost

log = logging.getLogger(__name__)

FEATURE = "extract"

MAX_URL_CHARS = 2000
MAX_PROMPT_CHARS = 500
MAX_BYTES = 3_000_000          # raw download ceiling
MAX_HTML_CHARS = 120_000       # after cleaning: ~30k tokens at worst, ~$0.0045 in
MAX_REDIRECTS = 5
CACHE_TTL = 3600

_ALLOWED_PORTS = {80, 443}
_ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain",
                  "application/xml", "text/xml")
# Markup that carries no readable content but a great many tokens.
_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "canvas",
              "template", "link", "meta", "object", "embed", "picture source")
_KEEP_ATTRS = {"href", "src", "alt", "title", "datetime"}

_run_lock = threading.Lock()
_cache: dict[str, tuple[float, "Extraction"]] = {}
_cache_lock = threading.Lock()


class ExtractError(ValueError):
    """A problem the reader can fix: a bad URL, a blocked address, no content."""


@dataclass
class Extraction:
    url: str
    final_url: str
    prompt: str
    result: object = None
    rows: list[dict] | None = None       # set when the result is a table
    cost_usd: float = 0.0
    calls: int = 0
    page_chars: int = 0
    truncated: bool = False
    cached: bool = False
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


# --- Safe fetching ------------------------------------------------------------

def check_url(url: str) -> str:
    """Validate an untrusted URL. Returns it normalised, or raises ExtractError."""
    url = (url or "").strip()
    if not url:
        raise ExtractError("Paste a link to extract from.")
    if len(url) > MAX_URL_CHARS:
        raise ExtractError("That link is too long.")
    if "://" not in url:
        url = "https://" + url

    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise ExtractError("Only http and https links can be extracted.")
    if parts.username or parts.password:
        raise ExtractError("Links with a username or password are not allowed.")
    if not parts.hostname:
        raise ExtractError("That does not look like a web address.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in _ALLOWED_PORTS:
        raise ExtractError("Only standard web ports (80 and 443) are allowed.")

    try:
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ExtractError(f"Could not find the site {parts.hostname}.") from None

    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        # Anything not on the public internet -- loopback, private ranges,
        # link-local (incl. the 169.254.169.254 metadata endpoint), multicast,
        # reserved -- could reach this machine or its network, never allowed.
        if not address.is_global or address.is_multicast:
            raise ExtractError(
                "That address is on a private or local network and cannot be fetched."
            )
    return url


def fetch_page(url: str) -> tuple[str, str]:
    """Fetch an untrusted URL safely. Returns (final_url, html).

    Redirects are followed by hand so every hop is re-validated; letting the
    client follow them would allow a public page to bounce us to localhost.
    """
    current = check_url(url)
    with httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent,
                 "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
        follow_redirects=False,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ExtractError("The site sent a redirect with no target.")
                    current = check_url(urljoin(current, location))
                    continue

                if resp.status_code >= 400:
                    raise ExtractError(
                        f"The site answered {resp.status_code} for that link.")

                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and not ctype.startswith(_ALLOWED_TYPES):
                    raise ExtractError(
                        f"That link is a {ctype} file, not a web page.")

                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ExtractError("That page is larger than 3 MB.")
                encoding = resp.encoding or "utf-8"
                return current, body.decode(encoding, errors="replace")

    raise ExtractError("That link redirects too many times.")


# --- Cleaning -------------------------------------------------------------------

def clean_html(html: str, base_url: str) -> tuple[str, int, bool]:
    """Strip a page to text-bearing markup. Returns (html, text_chars, truncated).

    A typical news page is 90% scripts, styles and class names. Removing them
    before the model sees the page is the single biggest cost lever here, and
    links are made absolute so anything extracted is usable as-is.
    """
    soup = BeautifulSoup(html, "html.parser")
    for selector in _DROP_TAGS:
        for tag in soup.select(selector):
            tag.decompose()

    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in _KEEP_ATTRS}
        for attr in ("href", "src"):
            if attr in tag.attrs and isinstance(tag.attrs[attr], str):
                tag.attrs[attr] = urljoin(base_url, tag.attrs[attr])

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    root = soup.body or soup
    cleaned = str(root)
    if title:
        cleaned = f"<h1>{title}</h1>\n{cleaned}"

    truncated = len(cleaned) > MAX_HTML_CHARS
    if truncated:
        cleaned = cleaned[:MAX_HTML_CHARS]

    text_chars = len(BeautifulSoup(cleaned, "html.parser").get_text(" ", strip=True))
    return cleaned, text_chars, truncated


# --- Result shaping ---------------------------------------------------------------

def unwrap(raw: object) -> object:
    """Reduce ScrapeGraphAI's envelope to the answer itself.

    It wraps answers as {"content": ...}, sometimes with the payload still a
    JSON string inside -- the same shapes seen on the VCCircle scraper.
    """
    from .llm.bedrock import parse_json

    value = raw
    if isinstance(value, dict) and set(value) == {"content"}:
        value = value["content"]
    if isinstance(value, str):
        try:
            value = parse_json(value)
        except ValueError:
            return value.strip()
        if isinstance(value, dict) and set(value) == {"content"}:
            value = value["content"]
    return value


def as_rows(value: object) -> list[dict] | None:
    """The result as a table, when it is (or holds) a list of flat records."""
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        return value
    if isinstance(value, dict):
        lists = [v for v in value.values()
                 if isinstance(v, list) and v and all(isinstance(r, dict) for r in v)]
        if len(lists) == 1:
            return lists[0]
    return None


# --- Running ------------------------------------------------------------------------

def _cache_key(url: str, prompt: str) -> str:
    raw = f"{url.strip().lower()}\n{' '.join(prompt.lower().split())}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def run(url: str, prompt: str) -> Extraction:
    """Fetch, clean and extract. Raises ExtractError or BudgetExceeded."""
    prompt = " ".join((prompt or "").split())
    if len(prompt) < 3:
        raise ExtractError("Say what you want extracted from the page.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ExtractError(f"Keep the request under {MAX_PROMPT_CHARS} characters.")

    key = _cache_key(url, prompt)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            cached = hit[1]
            return Extraction(**{**cached.__dict__, "cached": True,
                                 "cost_usd": 0.0, "calls": 0})

    # One at a time: two concurrent extractions are two bills, and a reader
    # double-clicking Extract should not pay twice.
    if not _run_lock.acquire(blocking=False):
        raise ExtractError("Another extraction is running. Try again in a moment.")
    try:
        started = time.monotonic()
        final_url, html = fetch_page(url)
        page, text_chars, truncated = clean_html(html, final_url)
        if text_chars < 20:
            raise ExtractError(
                "That page has almost no readable text. It may be built in the "
                "browser with JavaScript, which this cannot run.")

        # Refuse up front, before any money moves, if even one call won't fit.
        from .ingest.scraper_sgai import MAX_OUTPUT_TOKENS
        ensure_budget(estimate_cost(settings.model_cheap, len(page), MAX_OUTPUT_TOKENS))

        from scrapegraphai.graphs import SmartScraperGraph
        from .ingest.scraper_sgai import _graph_config, make_llm

        llm = make_llm(FEATURE)
        # Source is the HTML itself, not the URL: ScrapeGraphAI then parses it
        # locally and never fetches anything on its own.
        graph = SmartScraperGraph(prompt=prompt, source=page,
                                  config=_graph_config(llm))
        raw = graph.run()

        value = unwrap(raw)
        result = Extraction(
            url=url, final_url=final_url, prompt=prompt, result=value,
            rows=as_rows(value), cost_usd=round(llm.spent_usd, 6),
            calls=llm.calls, page_chars=text_chars, truncated=truncated,
            seconds=round(time.monotonic() - started, 1),
        )
        if truncated:
            result.notes.append(
                "The page was long, so only its first part was read.")
        if isinstance(value, dict) and value.get("error"):
            result.notes.append(str(value.get("error")))

        with _cache_lock:
            _cache[key] = (time.monotonic() + CACHE_TTL, result)
        return result
    finally:
        _run_lock.release()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = ["Extraction", "ExtractError", "BudgetExceeded", "run", "check_url",
           "fetch_page", "clean_html", "unwrap", "as_rows"]
