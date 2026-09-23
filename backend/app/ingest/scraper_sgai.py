"""ScrapeGraphAI extraction for sources with no RSS feed.

VCCircle is the only one. Everything else in `sources.yaml` is RSS and costs
nothing, so this module is the entire paid surface of the ingest pipeline.

We scrape the *listing* page, not individual articles. One page already holds
20+ headlines with dates and links, so cost scales with the number of sites
rather than the number of articles -- the difference between ~$0.15/month and
something that grows every time a source gets busier.

SPEND IS NOT LEDGERED HERE. ScrapeGraphAI builds its own LangChain client and
calls Bedrock directly, so these calls bypass `app.llm.budget` entirely: they
produce no ledger rows and no cap check. This was a deliberate choice; the
consequence is that `python -m app.llm` under-reports total spend by whatever
this module costs. `estimate_run_cost()` exists so the gap can at least be
quantified rather than invisible.
"""

from __future__ import annotations

import logging
import re
from datetime import timezone
from urllib.parse import urljoin, urlparse

from langchain_aws import ChatBedrockConverse

from ..config import settings
from .rss import FeedItem, clean_text
from .sources import Source

log = logging.getLogger(__name__)

EXTRACTION_PROMPT = (
    "List every news article linked on this page. "
    "Return ONLY a JSON array, no prose. Each element must have exactly these "
    'keys: "headline" (the article title), "url" (the link to the article), '
    'and "published" (the date shown, copied verbatim; use null if absent). '
    "Ignore navigation links, advertisements, newsletter sign-ups, category "
    "pages and 'most read' sidebars -- include only actual news articles."
)

# gpt-oss-120b's real context window. ScrapeGraphAI's models_tokens table has
# no entry for it, and its KeyError fallback is a silent 8192 -- which would
# truncate the listing page and drop articles without raising anything.
MODEL_TOKENS = 128_000

# Output ceiling. ~25 articles of JSON plus gpt-oss reasoning tokens, which
# are billed and counted as output on this model.
MAX_OUTPUT_TOKENS = 8_000


def _flatten_content(content: object) -> object:
    """Reduce a Converse content-block list to the plain answer text.

    The Converse API returns `content` as a list of typed blocks, e.g.
    ``[{"type": "reasoning_content", ...}, {"type": "text", "text": "..."}]``.
    ScrapeGraphAI expects a string and yields `{'content': []}` when handed a
    list, so the reasoning block is dropped and the text blocks joined here.
    """
    if not isinstance(content, list):
        return content
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class _FlatChatBedrockConverse(ChatBedrockConverse):
    """ChatBedrockConverse that hands downstream code a plain string."""

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        for generation in result.generations:
            generation.message.content = _flatten_content(
                generation.message.content
            )
        return result

    async def _agenerate(self, *args, **kwargs):
        result = await super()._agenerate(*args, **kwargs)
        for generation in result.generations:
            generation.message.content = _flatten_content(
                generation.message.content
            )
        return result


def _graph_config() -> dict:
    """Build the graph config around OUR Bedrock client.

    Letting ScrapeGraphAI construct the model itself (`"model": "bedrock/..."`)
    routes through langchain-aws, which builds its own boto3 client, resolves
    SigV4 credentials from ~/.aws and ignores AWS_BEARER_TOKEN_BEDROCK --
    silently billing whatever account happens to be configured on the machine.
    It also selects the legacy Invoke API, which inlines gpt-oss reasoning as
    <reasoning> tags in the response body.

    Passing a prebuilt `model_instance` fixes all three at once: our client and
    therefore the Bedrock API key from .env, the Converse API (reasoning in its
    own block), and reasoning_effort, which is ~3x cheaper on output tokens.
    """
    from ..llm.bedrock import get_client

    llm = _FlatChatBedrockConverse(
        client=get_client(),
        model=settings.model_cheap,
        temperature=0.0,
        # A listing page of 20+ articles needs room; the default cuts the JSON
        # array off mid-object, wasting a whole extraction.
        max_tokens=MAX_OUTPUT_TOKENS,
        additional_model_request_fields={
            "reasoning_effort": settings.reasoning_effort
        },
    )

    return {
        "llm": {
            "model_instance": llm,
            # Required whenever model_instance is used, and not inferable:
            # ScrapeGraphAI's token table has no gpt-oss entry.
            "model_tokens": MODEL_TOKENS,
        },
        "verbose": False,
        "headless": True,
    }


_REASONING_RE = re.compile(r"<reasoning>.*?(?:</reasoning>|$)", re.S | re.I)


def _strip_reasoning(text: str) -> str:
    """Remove gpt-oss chain-of-thought from a response.

    Through langchain-aws the reasoning arrives inline as <reasoning>...</reasoning>
    rather than in a separate content block the way the raw Converse API
    returns it. ScrapeGraphAI's JSON parser does not expect that and returns
    the unparsed message, so we strip it before parsing ourselves.
    """
    return _REASONING_RE.sub("", text or "").strip()


def _coerce_rows(raw: object) -> list[dict]:
    """Normalise whatever ScrapeGraphAI hands back into a list of dicts.

    Observed shapes: a list, a dict wrapping a list, a dict for a single
    article, or -- when its own parsing fails -- a LangChain message whose
    text still contains the JSON we asked for.
    """
    if raw is None:
        return []

    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]

    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        if "headline" in raw or "url" in raw:
            return [raw]
        # ScrapeGraphAI sometimes wraps the payload without parsing it, e.g.
        # {"content": '[{"headline": ...}]'} -- the value is a JSON *string*.
        # Which shape comes back varies between runs on the same page, so both
        # have to be handled or extraction fails intermittently.
        for value in raw.values():
            if isinstance(value, str) and value.strip():
                rows = _coerce_rows(value)
                if rows:
                    return rows
        return []

    # A LangChain AIMessage, or a bare string: recover the JSON ourselves.
    text = getattr(raw, "content", None)
    if not isinstance(text, str):
        text = raw if isinstance(raw, str) else str(raw)

    cleaned = _strip_reasoning(text)

    for candidate in (cleaned, text):
        if not candidate:
            continue
        try:
            from ..llm.bedrock import parse_json
            rows = _coerce_rows(parse_json(candidate))
            if rows:
                return rows
        except Exception:  # noqa: BLE001 - fall through to salvage
            pass

    # Last resort: scan the *unstripped* text for balanced objects. This
    # survives both a truncated array (no closing bracket) and an unterminated
    # <reasoning> block that would otherwise swallow the payload.
    salvaged = [r for r in _salvage_objects(text)
                if "headline" in r or "url" in r]
    if salvaged:
        log.warning("recovered %d objects from an unparseable response",
                    len(salvaged))
        return salvaged

    _dump_debug(text)
    return []


def _dump_debug(text: str) -> None:
    """Persist a failed response so the next fix does not need another run."""
    try:
        path = settings.db_path.parent / "scrape_debug.txt"
        path.write_text(text, encoding="utf-8")
        log.warning("raw response written to %s (%d chars)", path, len(text))
    except Exception:  # noqa: BLE001
        pass


def _salvage_objects(text: str) -> list[dict]:
    """Recover whole objects from a truncated JSON array.

    A model that runs out of output tokens mid-array leaves valid objects
    followed by a half-written one and no closing bracket. Dropping the whole
    response would discard articles we already paid to extract, so scan for
    balanced top-level objects and parse them individually.
    """
    import json

    out: list[dict] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        out.append(obj)
                except json.JSONDecodeError:
                    pass
                start = -1
    return out


def _parse_date(value: object):
    """Listing pages show '2 hours ago' as often as a real date."""
    if not value or not isinstance(value, str):
        return None
    try:
        import dateparser
        dt = dateparser.parse(
            value, settings={"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "Asia/Kolkata"}
        )
        return dt.astimezone(timezone.utc) if dt else None
    except Exception:  # noqa: BLE001 - a bad date must not kill the run
        return None


def scrape_listing(source: Source, prompt: str = EXTRACTION_PROMPT) -> list[FeedItem]:
    """Extract articles from a source's listing page via ScrapeGraphAI."""
    url = source.listing_url or source.home
    if not url:
        raise ValueError(f"{source.name} has no listing_url")

    from scrapegraphai.graphs import SmartScraperGraph

    graph = SmartScraperGraph(prompt=prompt, source=url, config=_graph_config())

    # Guard against the silent 8192 fallback described above.
    if getattr(graph, "model_tokens_defaulted", False):
        log.error("%s: ScrapeGraphAI fell back to an 8192 token window -- the "
                  "listing page will be truncated", source.name)

    raw = graph.run()
    rows = _coerce_rows(raw)
    if not rows:
        log.warning("%s: extraction returned nothing usable (%r) -- "
                    "falling back to anchor parsing",
                    source.name, str(raw)[:160])
        return fallback_anchors(source)

    host = urlparse(url).netloc
    items: list[FeedItem] = []
    seen: set[str] = set()

    for row in rows:
        headline = clean_text(str(row.get("headline") or ""), limit=300)
        link = str(row.get("url") or "").strip()
        if not headline or not link:
            continue

        link = urljoin(url, link)
        # A model asked for links will sometimes invent or wander off-site.
        if urlparse(link).netloc != host or link in seen:
            continue
        seen.add(link)

        items.append(FeedItem(
            url=link,
            headline=headline,
            description=None,          # listing pages rarely carry one
            published_at=_parse_date(row.get("published")),
            source=source.name,
            feed_categories=[],
        ))

    log.info("%s: %d articles from %s", source.name, len(items), url)
    return items


def fallback_anchors(source: Source) -> list[FeedItem]:
    """Deterministic listing extraction, used only when the model returns nothing.

    Measured against VCCircle over five runs, ScrapeGraphAI failed roughly 60%
    of the time with three distinct failure modes (an unparsed JSON string, a
    truncated array, and a bare "NA"). The cause is not the site blocking us --
    plain HTTP returns the full 633KB page with all 72 article links -- but the
    page's *visible* text being mostly navigation, so what reaches the model
    varies run to run.

    The links are plainly in the HTML, so this recovers them for free and
    without an LLM. Headlines come from the anchor text; dates are left to the
    description pass, which fetches each article anyway.
    """
    import httpx
    from bs4 import BeautifulSoup

    url = source.listing_url or source.home
    try:
        resp = httpx.get(url, headers={"User-Agent": settings.user_agent},
                         timeout=settings.http_timeout, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("%s: fallback fetch failed: %s", source.name, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    host = urlparse(url).netloc
    items: list[FeedItem] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        # Article slugs sit at the site root and are long; section pages are
        # short and nested.
        if not href.startswith("/") or len(href) <= 25 or href.count("/") != 1:
            continue

        headline = clean_text(anchor.get_text(" ", strip=True), limit=300)
        if not headline or len(headline) < 25:
            continue

        link = urljoin(url, href)
        if urlparse(link).netloc != host or link in seen:
            continue
        seen.add(link)

        items.append(FeedItem(
            url=link,
            headline=headline,
            description=None,
            published_at=None,
            source=source.name,
            feed_categories=[],
        ))

    log.info("%s: %d articles recovered by anchor fallback (free)",
             source.name, len(items))
    return items


def estimate_run_cost(page_chars: int = 60_000, output_tokens: int = 1_500) -> float:
    """Rough cost of one scrape, since the real figure is never ledgered.

    ScrapeGraphAI chunks the page and may make several calls, so treat this as
    a floor rather than a precise number.
    """
    tokens_in = page_chars // 4
    return (tokens_in * settings.price_cheap_in
            + output_tokens * settings.price_cheap_out) / 1_000_000
