"""LinkedIn: funding and news from the profiles and company pages the reader adds.

The reader pastes a profile (linkedin.com/in/...) or a company page
(linkedin.com/company/...) on the LinkedIn page; it is saved, read straight
away, then read again on every 12h refresh, at most every `linkedin_hours`.

Posts come from an Apify Store actor (`apify_linkedin_actor`), which needs no
LinkedIn login. It is billed per post returned, in Apify's own account:

  first read   the newest `linkedin_first_posts` (20)                ~$0.04
  later reads  only posts newer than the newest one stored
               (`postedLimitDate`), so a quiet account costs the
               empty-read charge, $0.001                              ~$0.001-0.01

Every run also carries both Apify caps (`maxItems`, `maxTotalChargeUsd`).
We never fetch the pasted link ourselves -- it only goes to Apify -- and the
post links that come back must be http(s) before they reach an href.

gpt-oss then reads each new post once, 20 a call (~$0.00007 a post,
ledgered as "linkedin_posts"): Funding, News or Other, with the company,
round, amount and investors for a raise. Only Funding and News are shown by
default.

    python -m app.ingest.linkedin --once            # every account that is due
    python -m app.ingest.linkedin --once --force    # due or not (costs Apify)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

from sqlalchemy import delete, func, select

from ..config import settings
from ..db import init_db, session_scope
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import LinkedinKind, LinkedinPost, LinkedinRead, LinkedinSource, utcnow
from ..search import apify
from .rounds import clean_investors, clean_text, tidy_round

log = logging.getLogger(__name__)

FEATURE = "linkedin_posts"
BATCH_SIZE = 20
CONTENT_CHARS = 4000           # stored per post
PROMPT_CHARS = 700             # sent to the model per post: ~180 tokens
KIND_LABELS = {"profile": "Profile", "company": "Company page"}


class SourceError(ValueError):
    """A LinkedIn link that cannot be added, worded for the reader."""


# --- Sources --------------------------------------------------------------------------

_ACCOUNT_PATH = re.compile(r"^/(in|company)/([^/?#]+)", re.I)
_SLUG = re.compile(r"[^\s/<>\"']{1,100}")


def canonical_url(url: str) -> tuple[str, str, str]:
    """(canonical url, "profile" | "company", slug) for a pasted LinkedIn link.

    Any path under the account -- /company/acme/posts/?feedView=all,
    /in/jane/recent-activity/ -- is the account itself. Slugs are
    case-insensitive on LinkedIn, so they are kept lower-case.
    """
    url = (url or "").strip()
    if not url:
        raise SourceError("Paste a LinkedIn link to add.")
    if len(url) > 1000:
        raise SourceError("That link is too long.")
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (
            host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise SourceError("That is not a LinkedIn link. Paste a profile "
                          "(linkedin.com/in/...) or a company page (linkedin.com/company/...).")
    match = _ACCOUNT_PATH.match(parts.path)
    if not match:
        if parts.path.lower().startswith(("/posts/", "/feed/", "/pulse/")):
            raise SourceError("That is a single post. Paste the profile or company page "
                              "it is from.")
        raise SourceError("Paste a profile (linkedin.com/in/...) or a company page "
                          "(linkedin.com/company/...).")
    section = match.group(1).lower()
    slug = unquote(match.group(2)).strip().lower()
    if not _SLUG.fullmatch(slug):
        raise SourceError("That LinkedIn link does not name an account.")
    kind = "profile" if section == "in" else "company"
    return f"https://www.linkedin.com/{section}/{quote(slug, safe='-_.~')}/", kind, slug


def slug_of(source: LinkedinSource) -> str:
    return unquote(source.url.rstrip("/").rsplit("/", 1)[-1])


def all_sources() -> list[LinkedinSource]:
    with session_scope() as s:
        return list(s.scalars(select(LinkedinSource).order_by(LinkedinSource.id)).all())


def ready() -> bool:
    return bool(settings.apify_api_token)


def add_source(url: str, label: str | None = None) -> LinkedinSource:
    """Check a pasted LinkedIn link and save it. Does not read it."""
    url, kind, slug = canonical_url(url)
    if not ready():
        raise SourceError("LinkedIn is read through Apify, which is not set up: add "
                          "APIFY_API_TOKEN to .env and restart the server.")
    if any(src.url == url for src in all_sources()):
        raise SourceError("That account is already a source.")
    label = " ".join((label or "").split())[:200]
    with session_scope() as s:
        row = LinkedinSource(url=url, kind=kind, label=label or slug, label_auto=not label)
        s.add(row)
    return row


def remove_source(source_id: int) -> bool:
    """Drop an account with every post and read that came from it."""
    with session_scope() as s:
        row = s.get(LinkedinSource, source_id)
        if row is None:
            return False
        s.execute(delete(LinkedinPost).where(LinkedinPost.source_id == source_id))
        s.execute(delete(LinkedinRead).where(LinkedinRead.source_id == source_id))
        s.delete(row)
    return True


# --- Reading ------------------------------------------------------------------------------

@dataclass
class Found:
    post_id: str
    url: str
    content: str
    posted_at: datetime | None
    author: str | None
    author_id: str | None
    repost: bool


def _web_url(value: object) -> str | None:
    """An absolute http(s) link, or None. Anything else never reaches an href."""
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    return url if urlparse(url).scheme in ("http", "https") and len(url) <= 1000 else None


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


def _content(row: dict) -> str:
    """The post's words, paragraphs kept. A post with no text of its own falls
    back to its document's title."""
    text = row.get("content")
    if not isinstance(text, str) or not text.strip():
        document = row.get("document")
        text = document.get("title") if isinstance(document, dict) else None
    if not isinstance(text, str):
        return ""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)[:CONTENT_CHARS]


def is_post(row: object) -> bool:
    """A row the actor charges as a post: anything else (an error, a notice) is not."""
    return (isinstance(row, dict) and row.get("type") in (None, "post")
            and bool(row.get("id")) and not row.get("error"))


def from_actor(row: dict, slug: str) -> Found | None:
    """One post from the actor's output, or None for a row that is not one."""
    if not is_post(row):
        return None
    social = row.get("socialContent") if isinstance(row.get("socialContent"), dict) else {}
    url = _web_url(row.get("linkedinUrl")) or _web_url(social.get("shareUrl"))
    content = _content(row)
    if not (url and content):
        return None
    author = row.get("author") if isinstance(row.get("author"), dict) else {}
    author_id = (author.get("publicIdentifier") or author.get("universalName") or "")
    author_id = str(author_id).strip().lower() or None
    # A repost is marked by the actor, or written by someone else. A company
    # added by its numeric id cannot be compared by name, so it never is.
    repost = bool(row.get("repostedBy") or row.get("isRepost") or row.get("resharedPost"))
    if not repost and author_id and not slug.isdigit():
        repost = author_id != slug.lower()
    return Found(
        post_id=str(row.get("id")).strip()[:80], url=url, content=content,
        posted_at=_posted(row.get("postedAt")),
        author=clean_text(author.get("name"), 200), author_id=author_id, repost=repost,
    )


def _newest(source_id: int) -> datetime | None:
    with session_scope() as s:
        return s.scalar(select(func.max(LinkedinPost.posted_at))
                        .where(LinkedinPost.source_id == source_id))


def actor_input(source: LinkedinSource, since: datetime | None) -> dict:
    """The actor's input: one account, posts only -- reactions and comments are
    charged as posts of their own, so they are never asked for."""
    run_input = {
        "targetUrls": [source.url],
        "maxPosts": settings.linkedin_posts_per_read if since else settings.linkedin_first_posts,
        "includeReposts": True,
        "includeQuotePosts": True,
        "scrapeReactions": False,
        "scrapeComments": False,
    }
    if since is not None:
        # Inclusive on the actor's side, so one second on: the newest stored
        # post is not paid for again.
        after = since.astimezone(timezone.utc) + timedelta(seconds=1)
        run_input["postedLimitDate"] = after.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return run_input


def _problem(rows: list[dict]) -> str | None:
    """What the actor said went wrong, when it returned no posts."""
    for row in rows:
        said = row.get("error") or row.get("message")
        if isinstance(said, dict):
            said = said.get("message")
        if isinstance(said, str) and said.strip():
            return " ".join(said.split())[:200]
    return None


def fetch(source: LinkedinSource, run: LinkedinRead) -> list[Found]:
    """An account's new posts, through Apify. Records the estimated charge."""
    since = _newest(source.id)
    run_input = actor_input(source, since)
    limit = run_input["maxPosts"]
    ceiling = (limit * settings.linkedin_price_per_post + settings.linkedin_price_per_run
               + settings.linkedin_price_per_empty)
    rows = apify.run_actor(settings.apify_linkedin_actor, run_input, max_items=limit,
                           max_charge_usd=min(settings.apify_max_charge_usd, ceiling))
    charged = sum(1 for r in rows if is_post(r))
    run.apify_usd = round(charged * settings.linkedin_price_per_post
                          + settings.linkedin_price_per_run
                          + (settings.linkedin_price_per_empty if charged == 0 else 0), 6)
    if charged == 0:
        problem = _problem(rows)
        if problem:
            raise ValueError(f"LinkedIn could not be read for that account: {problem}")
        if since is None:
            raise ValueError("No posts found. Check that the link opens a public profile "
                             "or company page that has posted.")
        return []
    slug = slug_of(source)
    return [f for f in (from_actor(r, slug) for r in rows) if f is not None]


def store(source: LinkedinSource, found: list[Found]) -> int:
    """Insert the posts not seen before. Returns how many were new."""
    new = 0
    with session_scope() as s:
        known = set(s.scalars(select(LinkedinPost.post_id).where(
            LinkedinPost.source_id == source.id,
            LinkedinPost.post_id.in_([f.post_id for f in found]))).all())
        for f in found:
            if f.post_id in known:
                continue
            known.add(f.post_id)
            s.add(LinkedinPost(source_id=source.id, post_id=f.post_id, url=f.url,
                               content=f.content, author=f.author, repost=f.repost,
                               posted_at=f.posted_at))
            new += 1
    return new


def _name_from_posts(source: LinkedinSource, found: list[Found]) -> None:
    """Replace a label that is only the link's slug with the account's own name."""
    if not source.label_auto:
        return
    own = next((f.author for f in found if f.author and not f.repost), None)
    if not own:
        return
    with session_scope() as s:
        row = s.get(LinkedinSource, source.id)
        if row is not None and row.label_auto:
            row.label, row.label_auto = own, False
    source.label, source.label_auto = own, False


def read_source(source: LinkedinSource) -> LinkedinRead:
    """One read of one account. Never raises; the outcome is on the returned read."""
    run = LinkedinRead(source_id=source.id, started_at=utcnow(), items_seen=0,
                       items_new=0, apify_usd=0.0)
    try:
        found = fetch(source, run)
        run.items_seen = len(found)
        run.items_new = store(source, found)
        _name_from_posts(source, found)
    except apify.ApifyError as exc:
        run.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
        run.error = (str(exc) if isinstance(exc, ValueError)
                     else f"{type(exc).__name__}: {exc}")[:300]
    if run.error:
        log.warning("linkedin source %d: %s", source.id, run.error)
    with session_scope() as s:
        s.add(run)
    return run


# --- Classifying ---------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You read LinkedIn posts for someone tracking Indian startups and venture "
    "capital. You reply with JSON only -- no prose, no markdown fences."
)

PROMPT_HEAD = """Each numbered item is a LinkedIn post and the account that posted it.
For each item return one JSON object with keys:
  "i"          the item number
  "kind"       EXACTLY one of: Funding, News, Other
  "headline"   what the post announces, as a short plain news headline, under 15 words
  "company"    the company the post is about, or null
  "round"      Funding only: the round as written ("Seed", "Pre-Series A",
               "Series B", "Debt"), else null
  "amount"     Funding only: the amount as written, with currency
               ("Rs 12.5 crore", "$4.5M"), else null
  "investors"  Funding only: array of investor names mentioned, else []

Kinds:
  Funding  a company raised money (any round, debt or IPO), or the account
           invested in, led or joined a company's funding round
  News     company news: a launch, partnership, acquisition, expansion, key
           hire, results, a milestone, a fund or programme launched
  Other    opinions, advice, hiring ads, event invitations, greetings, personal
           updates, and congratulations that announce nothing

Use only the text given. Do not guess amounts or investors.
Return a JSON array of these objects and nothing else.
"""

_KIND_BY_KEY = {k.value.lower(): k for k in LinkedinKind}
_KIND_BY_KEY.update({"investment": LinkedinKind.FUNDING, "fundraise": LinkedinKind.FUNDING})


def coerce_kind(value: object) -> LinkedinKind:
    if isinstance(value, str):
        return _KIND_BY_KEY.get(value.strip().lower(), LinkedinKind.OTHER)
    return LinkedinKind.OTHER


@dataclass
class _Pending:
    id: int
    account: str
    author: str | None
    repost: bool
    content: str


def _load_pending(source_ids: list[int] | None = None) -> list[_Pending]:
    stmt = (select(LinkedinPost.id, LinkedinSource.label, LinkedinPost.author,
                   LinkedinPost.repost, LinkedinPost.content)
            .join(LinkedinSource, LinkedinSource.id == LinkedinPost.source_id)
            .where(LinkedinPost.classified_at.is_(None))
            .order_by(LinkedinPost.id))
    if source_ids is not None:
        stmt = stmt.where(LinkedinPost.source_id.in_(source_ids))
    with session_scope() as s:
        return [_Pending(*row) for row in s.execute(stmt).all()]


def build_prompt(batch: list[_Pending]) -> str:
    lines = [PROMPT_HEAD]
    for n, p in enumerate(batch, start=1):
        who = f"Account: {p.account}"
        if p.repost and p.author:
            who += f", reposting {p.author}"
        text = " ".join(p.content.split())[:PROMPT_CHARS]
        lines.append(f"[{n}] ({who})\n    {text}")
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
            post = s.get(LinkedinPost, pending.id) if entry else None
            if post is None:
                continue
            post.kind = coerce_kind(entry.get("kind"))
            post.headline = clean_text(entry.get("headline"), 300)
            post.company = clean_text(entry.get("company"), 200)
            funding = post.kind == LinkedinKind.FUNDING
            post.round_label = tidy_round(clean_text(entry.get("round"), 80)) if funding else None
            post.amount = clean_text(entry.get("amount"), 120) if funding else None
            post.investors = clean_investors(entry.get("investors")) if funding else None
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


def classify_pending(source_ids: list[int] | None = None,
                     batch_size: int = BATCH_SIZE) -> ClassifyResult:
    """Read every unread post, or only those of `source_ids`. Stops cleanly
    when the budget says so."""
    pending = _load_pending(source_ids)
    result = ClassifyResult(considered=len(pending))
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        try:
            parsed, call = invoke_json(FEATURE, build_prompt(batch), system=SYSTEM_PROMPT,
                                       max_tokens=4096)
        except (BudgetExceeded, LlmUnavailable, ValueError) as exc:
            log.warning("linkedin posts: %s", exc)
            result.stopped_reason = str(exc)
            break
        result.batches += 1
        result.cost_usd += call.cost_usd
        if isinstance(parsed, list):
            result.classified += _apply(parsed, dict(enumerate(batch, start=1)))
    return result


# --- One refresh ----------------------------------------------------------------------

@dataclass
class LinkedinResult:
    reads: list[LinkedinRead] = field(default_factory=list)
    skipped: int = 0                         # accounts read recently enough
    classify: ClassifyResult | None = None

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
    def llm_usd(self) -> float:
        return self.classify.cost_usd if self.classify else 0.0


def _last_good_read(source_id: int) -> datetime | None:
    with session_scope() as s:
        return s.scalar(
            select(LinkedinRead.started_at)
            .where(LinkedinRead.source_id == source_id, LinkedinRead.error.is_(None))
            .order_by(LinkedinRead.started_at.desc()).limit(1))


def is_due(source: LinkedinSource, now: datetime | None = None) -> bool:
    """Every read is paid, so an account is read at most every
    `linkedin_hours`. A failed read does not count."""
    last = _last_good_read(source.id)
    return last is None or (now or utcnow()) - last >= timedelta(hours=settings.linkedin_hours)


def refresh_linkedin(force: bool = False, free_only: bool = False,
                     classify: bool = True, only: int | None = None) -> LinkedinResult:
    """Read every account that is due, then send new posts to the model. Every
    read is paid in Apify, so `free_only` reads none."""
    sources = [s for s in all_sources() if only is None or s.id == only]
    if only is not None and not sources:
        raise ValueError(f"unknown LinkedIn source: {only}")
    result = LinkedinResult()
    now = utcnow()
    due = []
    for source in sources:
        if not free_only and (force or is_due(source, now)):
            due.append(source)
        else:
            result.skipped += 1
    if due and ready():
        with ThreadPoolExecutor(max_workers=3) as pool:
            result.reads = list(pool.map(read_source, due))
    elif due:
        result.skipped += len(due)
        log.warning("linkedin: %d accounts due, but APIFY_API_TOKEN is not set", len(due))
    if classify:
        result.classify = classify_pending()
    log.info("linkedin: %d reads (%d failed, %d not due), %d new posts, ~$%.4f Apify, "
             "$%.6f LLM", len(result.reads), result.failed, result.skipped, result.new,
             result.apify_usd, result.llm_usd)
    return result


def read_now(source: LinkedinSource) -> LinkedinResult:
    """Read one account straight away -- just after it was added -- and send
    its posts to the model."""
    result = LinkedinResult(reads=[read_source(source)])
    result.classify = classify_pending([source.id])
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Read the LinkedIn accounts added on the page.")
    ap.add_argument("--once", action="store_true", help="run a single pass")
    ap.add_argument("--source", type=int, help="one account, by its id")
    ap.add_argument("--force", action="store_true",
                    help="read even if read recently (costs Apify)")
    args = ap.parse_args(argv)
    if not args.once:
        ap.error("pass --once; the 12h refresh runs this after the events")

    init_db()
    labels = {s.id: s.label for s in all_sources()}
    result = refresh_linkedin(force=args.force, only=args.source)
    print("\n%-34s %-5s %-5s %s" % ("ACCOUNT", "SEEN", "NEW", "STATUS"))
    for r in result.reads:
        print("%-34s %-5d %-5d %s" % (
            labels.get(r.source_id, r.source_id)[:34], r.items_seen, r.items_new,
            ("ERROR: " + r.error[:70]) if r.error else "ok"))
    sorted_ = result.classify.classified if result.classify else 0
    print(f"\n{len(result.reads)} reads, {result.skipped} accounts not due, {result.new} new "
          f"posts, {sorted_} read by the model; ~${result.apify_usd:.4f} Apify, "
          f"${result.llm_usd:.6f} LLM")
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
