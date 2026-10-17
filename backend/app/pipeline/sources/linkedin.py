"""LinkedIn accounts, via Apify -- one source per profile or company page.

Posts come from the Apify actor `apify_linkedin_actor` ("No Cookies": no
LinkedIn login, cookies or personal account; only APIFY_API_TOKEN). We never
fetch LinkedIn ourselves; the account link only goes to Apify.

Billed per post returned. The first read takes the newest
`linkedin_first_posts`; later reads ask only for posts newer than the newest
one already read (`postedLimitDate`), so a quiet account costs the
empty-read charge. Reactions and comments are charged as posts of their own,
so they are never asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

from ...config import settings
from ...search import apify
from ..items import FUNDING, RawItem, clean, clean_block, web_url
from . import FetchContext, FetchResult, Source, SourceError

POST_CHARS = 4000

_ACCOUNT_PATH = re.compile(r"^/(in|company)/([^/?#]+)", re.I)
_SLUG = re.compile(r"[^\s/<>\"']{1,100}")


def canonical_url(url: str) -> tuple[str, str]:
    """(https://www.linkedin.com/{in|company}/<slug>/, slug) for an account link.

    Any path under the account -- /company/acme/posts/, /in/jane/recent-activity/
    -- is the account itself. Slugs are case-insensitive on LinkedIn.
    """
    url = (url or "").strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (
            host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise ValueError(f"not a LinkedIn link: {url!r}")
    match = _ACCOUNT_PATH.match(parts.path)
    if not match:
        raise ValueError(f"not a LinkedIn profile or company page: {url!r}")
    section = match.group(1).lower()
    slug = unquote(match.group(2)).strip().lower()
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"LinkedIn link names no account: {url!r}")
    return f"https://www.linkedin.com/{section}/{quote(slug, safe='-_.~')}/", slug


def _posted(value: object) -> datetime | None:
    """postedAt as {"timestamp": ms, "date": ISO}, or either alone."""
    if isinstance(value, dict):
        stamp, text = value.get("timestamp"), value.get("date")
    else:
        stamp, text = (value, None) if isinstance(value, (int, float)) else (None, value)
    if isinstance(stamp, (int, float)) and stamp > 0:
        try:
            return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(text, str) and text.strip():
        try:
            parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                ).astimezone(timezone.utc)
    return None


def is_post(row: object) -> bool:
    """A row the actor charges as a post; an error or notice row is not one."""
    return (isinstance(row, dict) and row.get("type") in (None, "post")
            and bool(row.get("id")) and not row.get("error"))


def _problem(rows: list[dict]) -> str | None:
    for row in rows:
        said = row.get("error") or row.get("message")
        if isinstance(said, dict):
            said = said.get("message")
        if isinstance(said, str) and said.strip():
            return clean(said, 200)
    return None


@dataclass(frozen=True)
class LinkedinSource(Source):
    url: str = ""
    slug: str = ""

    def actor_input(self, since: datetime | None) -> dict:
        run_input = {
            "targetUrls": [self.url],
            "maxPosts": settings.linkedin_posts_per_read if since else settings.linkedin_first_posts,
            "includeReposts": True,
            "includeQuotePosts": True,
            "scrapeReactions": False,
            "scrapeComments": False,
        }
        if since is not None:
            # Inclusive on the actor's side, so one second on: the newest post
            # already read is not paid for again.
            after = since.astimezone(timezone.utc) + timedelta(seconds=1)
            run_input["postedLimitDate"] = after.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return run_input

    def to_item(self, row: dict) -> RawItem | None:
        if not is_post(row):
            return None
        social = row.get("socialContent") if isinstance(row.get("socialContent"), dict) else {}
        url = web_url(row.get("linkedinUrl")) or web_url(social.get("shareUrl"))
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            document = row.get("document")
            content = document.get("title") if isinstance(document, dict) else None
        content = clean_block(content, POST_CHARS)
        if not (url and content):
            return None
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        author_name = clean(author.get("name"), 200) or None
        author_id = str(author.get("publicIdentifier") or author.get("universalName") or "")
        repost = bool(row.get("repostedBy") or row.get("isRepost") or row.get("resharedPost"))
        if not repost and author_id.strip() and not self.slug.isdigit():
            repost = author_id.strip().lower() != self.slug
        first_line = content.split("\n", 1)[0]
        return RawItem(
            kind=FUNDING, source_key=self.key, source_type=self.type,
            source_label=self.label, url=url, title=clean(first_line, 200), text=content,
            published_at=_posted(row.get("postedAt")),
            payload={"author": author_name, "repost": repost},
        )

    def fetch(self, ctx: FetchContext) -> FetchResult:
        since = ctx.cursor(self.key)
        run_input = self.actor_input(since)
        limit = run_input["maxPosts"]
        ceiling = (limit * settings.linkedin_price_per_post + settings.linkedin_price_per_run
                   + settings.linkedin_price_per_empty)
        try:
            rows = apify.run_actor(settings.apify_linkedin_actor, run_input, max_items=limit,
                                   max_charge_usd=min(settings.apify_max_charge_usd, ceiling))
        except apify.ApifyError as exc:
            raise SourceError(f"{self.label}: {exc}") from None
        charged = sum(1 for r in rows if is_post(r))
        cost = round(charged * settings.linkedin_price_per_post + settings.linkedin_price_per_run
                     + (settings.linkedin_price_per_empty if charged == 0 else 0), 6)
        if charged == 0:
            problem = _problem(rows)
            if problem:
                raise SourceError(f"{self.label}: LinkedIn could not be read: {problem}",
                                  apify_usd=cost)
            if since is None:
                raise SourceError(f"{self.label}: no posts found. Check that the link opens a "
                                  f"public profile or company page that has posted.",
                                  apify_usd=cost)
        items = [i for i in (self.to_item(r) for r in rows) if i is not None]
        return FetchResult(items=items, apify_usd=cost)
