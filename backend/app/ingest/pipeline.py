"""Ingest orchestration.

`refresh()` is the full run: fetch every source, then describe (free), enrich
(paid) and dedupe by company (free). The CLI and the 12-hour scheduler
(`app.scheduler`) both call it, so a manual run and a scheduled one are the
same code path.

Run it directly:
    python -m app.ingest.pipeline --once
    python -m app.ingest.pipeline --once --source entrackr
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import init_db, session_scope
from ..models import Article, Chunk, DescriptionOrigin, IngestRun, utcnow
from .chunks import chunk_body, sync_chunk
from .dedupe import find_canonical, story_key, url_hash
from .rss import FeedItem, fetch_feed
from .sources import Source, load_sources

log = logging.getLogger(__name__)


def _content_hash(item: FeedItem) -> str:
    payload = f"{item.headline}\n{item.description or ''}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def store_item(session: Session, item: FeedItem) -> str:
    """Insert one item. Returns 'new', 'duplicate', or 'known'."""
    existing = session.scalars(
        select(Article).where(Article.url == item.url).limit(1)
    ).first()
    if existing is not None:
        # Seen before. Refresh the description only if we now have a better one.
        if not existing.description and item.description:
            existing.description = item.description
            existing.description_origin = DescriptionOrigin.FEED
            sync_chunk(session, existing)
        return "known"

    canonical = find_canonical(session, item.headline, item.published_at)

    article = Article(
        url=item.url,
        url_hash=url_hash(item.url),
        source=item.source,
        headline=item.headline,
        description=item.description,
        description_origin=DescriptionOrigin.FEED if item.description else None,
        published_at=item.published_at,
        story_key=story_key(item.headline),
        canonical_id=canonical.id if canonical else None,
        content_hash=_content_hash(item),
    )
    session.add(article)
    session.flush()  # assign article.id

    session.add(Chunk(article_id=article.id, chunk_index=0,
                      text=chunk_body(item.headline, item.description)))

    return "duplicate" if canonical else "new"


def ingest_source(source: Source, client: httpx.Client) -> IngestRun:
    """Ingest a single source, always recording an IngestRun."""
    # Counters are set explicitly: mapped_column(default=0) only fires at
    # INSERT, so a freshly constructed IngestRun has None until it is flushed.
    run = IngestRun(
        source=source.name,
        strategy=source.strategy,
        items_seen=0,
        items_new=0,
        items_duplicate=0,
    )

    try:
        if source.strategy == "rss":
            items = fetch_feed(source, client=client)
        elif source.strategy == "scrape":
            # The only paid path; each call is cap-checked and ledgered.
            from .scraper_sgai import scrape_listing
            items = scrape_listing(source)
        else:
            log.warning("%s: unknown strategy %r, skipping",
                        source.name, source.strategy)
            items = []

        run.items_seen = len(items)
        with session_scope() as session:
            for item in items:
                outcome = store_item(session, item)
                if outcome == "new":
                    run.items_new += 1
                elif outcome == "duplicate":
                    run.items_duplicate += 1

    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
        run.error = f"{type(exc).__name__}: {exc}"
        log.error("%s failed: %s", source.name, run.error)

    run.finished_at = utcnow()
    with session_scope() as session:
        session.add(run)
    return run


def run_once(only: str | None = None, skip_paid: bool = False) -> list[IngestRun]:
    init_db()
    sources = list(load_sources())
    if only:
        sources = [s for s in sources if s.name == only]
        if not sources:
            raise SystemExit(f"unknown source: {only}")
    if skip_paid:
        sources = [s for s in sources if not s.is_paid]

    runs: list[IngestRun] = []
    with httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        for i, source in enumerate(sources):
            if i:
                time.sleep(settings.per_domain_delay)  # be a polite guest
            runs.append(ingest_source(source, client))
    return runs


@dataclass
class RefreshResult:
    """Everything one full refresh did, for the CLI report and the scheduler log."""

    runs: list[IngestRun] = field(default_factory=list)
    described: int | None = None       # None when the step was skipped
    enrich: object | None = None       # EnrichResult, or None when skipped
    merged: int | None = None
    rounds: object | None = None       # RoundsResult, or None when skipped
    chunks_synced: int = 0

    @property
    def failed(self) -> bool:
        return any(r.error for r in self.runs)


def refresh(
    only: str | None = None,
    skip_paid: bool = False,
    describe: bool = True,
    enrich: bool = True,
) -> RefreshResult:
    """One full refresh: ingest, describe, enrich, second dedupe pass."""
    result = RefreshResult(runs=run_once(only=only, skip_paid=skip_paid))

    if describe:
        from .describe import describe_pending
        result.described = describe_pending()

    if enrich:
        from .enrich import enrich_pending
        result.enrich = enrich_pending()

        # Second dedupe pass, now that companies are known. Free.
        from .dedupe import dedupe_by_company
        with session_scope() as s:
            result.merged = dedupe_by_company(s)

        # Funding-round details for Insights, after dedupe so only one copy of
        # each story is read. Paid, batched, only new Funding stories.
        from .rounds import extract_pending
        result.rounds = extract_pending()

    # Belt and braces: citations must quote what is stored. Free.
    from .chunks import sync_all_chunks
    with session_scope() as s:
        result.chunks_synced = sync_all_chunks(s)

    return result


def _report(runs: list[IngestRun]) -> None:
    # On a first run every item is legitimately new, so the overflow signal is
    # meaningless until the source has been ingested at least once before.
    with session_scope() as s:
        prior = {
            r.source: (s.scalar(
                select(func.count(IngestRun.id)).where(IngestRun.source == r.source)
            ) or 0) > 1
            for r in runs
        }

    hdr = "%-20s %-7s %-7s %-7s %s" % ("SOURCE", "SEEN", "NEW", "DUP", "STATUS")
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in runs:
        if r.error:
            status = "ERROR: " + r.error[:48]
        elif r.window_overflowed and prior.get(r.source):
            status = "WINDOW OVERFLOW - stories may be lost"
        elif r.window_overflowed:
            status = "ok (first run)"
        else:
            status = "ok"
        print("%-20s %-7d %-7d %-7d %s"
              % (r.source, r.items_seen, r.items_new, r.items_duplicate, status))

    with session_scope() as s:
        total = s.scalar(select(func.count(Article.id))) or 0
        canon = s.scalar(
            select(func.count(Article.id)).where(Article.canonical_id.is_(None))
        ) or 0
        newest = s.scalar(select(func.max(Article.published_at)))
    print(f"\ncorpus: {total} articles ({canon} canonical, {total - canon} deduped)")
    print(f"newest published_at: {newest}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser(description="Ingest startup news sources.")
    ap.add_argument("--once", action="store_true", help="run a single pass")
    ap.add_argument("--source", help="restrict to one source by name")
    ap.add_argument("--skip-paid", action="store_true",
                    help="RSS sources only; skip anything that costs money")
    ap.add_argument("--no-describe", action="store_true",
                    help="skip fetching publisher descriptions (free step)")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip company/category classification (the paid step)")
    args = ap.parse_args(argv)

    if not args.once:
        ap.error("pass --once for a single run; for the 12h schedule run "
                 "`python -m app.scheduler` or start the API server")

    result = refresh(
        only=args.source, skip_paid=args.skip_paid,
        describe=not args.no_describe, enrich=not args.no_enrich,
    )
    _report(result.runs)

    if result.described is not None:
        print("\ndescribe: %d articles improved from publisher metadata (free)"
              % result.described)

    if result.enrich is not None:
        e = result.enrich
        print("enrich:   %d/%d classified in %d batch(es), $%.6f"
              % (e.updated, e.considered, e.batches, e.cost_usd))
        if e.stopped_reason:
            print("          stopped early: %s" % e.stopped_reason[:90])
        print("dedupe:   %d further duplicates merged by company (free)"
              % result.merged)
        r = result.rounds
        print("rounds:   %d/%d funding stories read for Insights, $%.6f"
              % (r.extracted, r.considered, r.cost_usd))

    if result.chunks_synced:
        print("chunks:   %d citation passages brought up to date (free)"
              % result.chunks_synced)

    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
