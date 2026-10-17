"""The unit of work that flows from the sources through every stage.

A source turns whatever it reads -- an RSS entry, a LinkedIn post, a search
result, a Luma or Eventbrite event, an events page -- into a `RawItem`.
Everything after the orchestrator works on RawItems only, so adding a source
never touches the stages.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# kind: what the LLM is asked to do with the item.
FUNDING = "funding"        # a story or post that may announce a funding round
EVENT = "event"            # one event, already structured by its portal
EVENT_PAGE = "event_page"  # a web page that may list several events
FUNDING_PAGE = "funding_page"  # a web page that may announce several rounds
KINDS = (FUNDING, EVENT, EVENT_PAGE, FUNDING_PAGE)

TEXT_CHARS = 20_000        # the most text kept per item (an events page)

_WS = re.compile(r"\s+")

# Query parameters that only track the click; dropping them makes the same
# article shared from two places one URL.
_TRACKING = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref|ref_src|"
    r"trk|trackingid|_hsenc|_hsmi|spm|cmpid|ito|src)$", re.I)


@dataclass
class RawItem:
    kind: str
    source_key: str            # "rss:entrackr", "linkedin:gokul-s0123", "luma", ...
    source_type: str           # rss | linkedin | web_search | luma | eventbrite | page
    source_label: str          # shown on the platform and in Excel
    url: str
    title: str
    text: str
    published_at: datetime | None = None
    payload: dict = field(default_factory=dict)   # structured fields, JSON-safe
    # Identity for the seen-items index when the URL alone is not one: an
    # events page is re-read whenever its text changes, so its key includes
    # the content.
    identity: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown item kind {self.kind!r}")
        self.title = clean(self.title, 500)
        self.text = clean_block(self.text, TEXT_CHARS)

    @property
    def screen_text(self) -> str:
        """What the guardrails read for this item before the LLM stages."""
        return f"{self.title}\n{self.text}".strip()


def clean(value: object, limit: int) -> str:
    """One line of text: whitespace collapsed, cut on a word boundary."""
    if not isinstance(value, str):
        return ""
    text = _WS.sub(" ", value).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def clean_block(value: object, limit: int) -> str:
    """Text whose line breaks matter (a post, a page): each line collapsed,
    blank lines dropped."""
    if not isinstance(value, str):
        return ""
    lines = (_WS.sub(" ", line).strip() for line in value.splitlines())
    return "\n".join(line for line in lines if line)[:limit]


def web_url(value: object, limit: int = 1000) -> str | None:
    """An absolute http(s) URL, or None. Nothing else may reach an href."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or len(url) > limit:
        return None
    return url


def normalise_url(url: str) -> str:
    """One spelling per page: scheme and host lower-cased, "www." and the
    fragment dropped, tracking parameters removed, the rest sorted, and no
    trailing slash."""
    parts = urlparse(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _TRACKING.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunparse(("https" if parts.scheme in ("http", "https") else parts.scheme,
                       host, path, "", query, ""))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def url_hash(item: RawItem) -> str:
    return sha256(item.identity or normalise_url(item.url))


def content_hash(item: RawItem) -> str:
    """The item's words, case and spacing aside. Catches the same story
    syndicated under another URL."""
    return sha256(_WS.sub(" ", item.screen_text.lower()).strip())
