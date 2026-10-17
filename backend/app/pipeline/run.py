"""One run of the whole pipeline, as drawn:

  Scheduler / Manual Refresh
    -> Ingestion Orchestrator      fan out to every source        (orchestrator.py)
    -> Guardrail #1                raw scraped content            (guardrails.py)
    -> Dedup Stage 1               URL + content hash             (seen.py)
    -> Guardrail #2                LLM input                      (process.py)
    -> LLM Structuring             table rows                     (structure.py)
    -> Guardrail #3                LLM output                     (process.py)
    -> Dedup Stage 2 + Storage     one row per deal / event       (storage.py)
  Anything a guardrail stops goes to the blocked / rejected log    (blocklog.py)

The scheduler, "Refresh now" and this CLI all call `run_pipeline`, so a manual
run and a scheduled one are the same code path. Each takes the cross-process
refresh lock first.

    python -m app.pipeline --once                    # everything that is due
    python -m app.pipeline --once --source luma      # one source
    python -m app.pipeline --once --force            # due or not (paid sources cost)
    python -m app.pipeline --once --free-only        # RSS only; no Apify or Firecrawl
    python -m app.pipeline --once --no-llm           # fetch and index; LLM stages next run
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict

from sqlalchemy import func, select

from ..config import settings
from ..db import init_db, session_scope
from ..models import ItemStatus, PipelineRun, SeenItem, SourceRun, utcnow
from . import guardrails, orchestrator, seen
from .blocklog import log_blocked
from .process import ProcessStats, process_pending
from .registry import RegistryError, all_sources

log = logging.getLogger(__name__)


def _start(trigger: str) -> PipelineRun:
    with session_scope() as s:
        run = PipelineRun(trigger=trigger, started_at=utcnow())
        s.add(run)
        s.flush()
        return run


def _save(run: PipelineRun) -> None:
    with session_scope() as s:
        s.merge(run)


def _screen_raw(run: PipelineRun, fetched: list[orchestrator.Fetched]):
    """Guardrail #1 over everything fetched. Returns the items that passed."""
    items = [item for job in fetched for item in job.items]
    screened = guardrails.screen(guardrails.RAW, items,
                                 lambda item: item.screen_text[:settings.guardrail_raw_chars])
    run.blocked_raw = len(screened.blocked)
    if screened.blocked:
        with session_scope() as s:
            for item, reason in screened.blocked:
                log_blocked(s, stage="guardrail_raw", source_key=item.source_key,
                            url=item.url, title=item.title, reason=reason)
    if screened.held:
        # Not checked, so not admitted: the source is marked unread, so its
        # cursor does not move and these items are fetched again next run.
        held_by_source: dict[str, int] = defaultdict(int)
        for item, _ in screened.held:
            held_by_source[item.source_key] += 1
        reason = screened.held[0][1]
        for job in fetched:
            if job.source.key in held_by_source:
                job.run.error = (f"Guardrail #1 could not check {held_by_source[job.source.key]} "
                                 f"items: {reason}")[:500]
                job.run.cursor = None
    return screened.passed, screened.cost_usd


def _pending_count() -> int:
    with session_scope() as s:
        return s.scalar(select(func.count(SeenItem.id)).where(
            SeenItem.status == ItemStatus.PENDING)) or 0


def run_pipeline(trigger: str = "cli", *, only: str | None = None, force: bool = False,
                 free_only: bool = False, structure: bool = True) -> PipelineRun:
    """One full run. Never raises for a failing source or stage; the outcome,
    including any error, is on the returned PipelineRun."""
    init_db()
    run = _start(trigger)
    stats = ProcessStats()
    fetched: list[orchestrator.Fetched] = []
    recorded = False
    try:
        sources = all_sources()
        if only:
            sources = [s for s in sources if s.key == only]
            if not sources:
                raise RegistryError(f"no source {only!r} in the registry")

        fetched = orchestrator.fetch_all(sources, run.id, force=force, free_only=free_only)
        run.fetched = sum(len(job.items) for job in fetched)
        run.apify_usd = round(sum(job.run.apify_usd for job in fetched), 6)
        run.firecrawl_credits = sum(job.run.firecrawl_credits for job in fetched)

        passed, guardrail_usd = _screen_raw(run, fetched)
        orchestrator.record(fetched)
        recorded = True

        admitted = seen.admit(passed)
        run.admitted, run.already_seen = admitted.new, admitted.seen
        run.events_updated = admitted.events_updated

        if structure:
            stats = process_pending()
        run.llm_usd = round(stats.llm_usd + stats.guardrail_usd + guardrail_usd, 6)
    except Exception as exc:  # noqa: BLE001 - recorded on the run, reported by the caller
        log.exception("pipeline run %d failed", run.id)
        run.error = (str(exc) if isinstance(exc, RegistryError)
                     else f"{type(exc).__name__}: {exc}")[:500]
    finally:
        if fetched and not recorded:
            orchestrator.record(fetched)
        run.blocked_input, run.blocked_output = stats.blocked_input, stats.blocked_output
        run.structured, run.irrelevant, run.rejected = (stats.structured, stats.irrelevant,
                                                        stats.rejected)
        run.funding_new, run.funding_merged = stats.funding_new, stats.funding_merged
        run.events_new, run.events_merged = stats.events_new, stats.events_merged
        run.stopped_reason = stats.stopped_reason
        run.pending_left = _pending_count()
        run.finished_at = utcnow()
        _save(run)
    return run


def failed_sources(run_id: int) -> list[str]:
    with session_scope() as s:
        return list(s.scalars(select(SourceRun.source_key).where(
            SourceRun.run_id == run_id, SourceRun.error.is_not(None))).all())


def describe(run: PipelineRun) -> str:
    """One line for the page after "Refresh now"."""
    if run.error:
        return f"The refresh failed: {run.error}"
    plural = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"  # noqa: E731
    parts = [f"{plural(run.funding_new, 'new funded startup')}",
             f"{plural(run.events_new, 'new event')}"]
    merged = run.funding_merged + run.events_merged
    if merged:
        parts.append(f"{merged} merged into existing rows")
    blocked = run.blocked_raw + run.blocked_input + run.blocked_output
    if blocked:
        parts.append(f"{blocked} blocked by guardrails")
    text = "Refreshed: " + ", ".join(parts) + f" ({run.admitted} new of {run.fetched} items read)"
    failed = failed_sources(run.id)
    if failed:
        text += f". Could not read {', '.join(failed)}"
    if run.stopped_reason:
        text += f". LLM stopped early: {run.stopped_reason[:120]}"
    if run.pending_left:
        text += f". {run.pending_left} items wait for the next run"
    return text + "."


def _print(run: PipelineRun) -> None:
    with session_scope() as s:
        sources = s.scalars(select(SourceRun).where(SourceRun.run_id == run.id)
                            .order_by(SourceRun.source_key)).all()
    print("\n%-34s %-6s %s" % ("SOURCE", "ITEMS", "STATUS"))
    for r in sources:
        status = ("ERROR: " + r.error[:70]) if r.error else (
            "skipped: " + r.skipped if r.skipped else ("ok" + (f" ({r.warning[:60]})"
                                                               if r.warning else "")))
        print("%-34s %-6d %s" % (r.source_key[:34], r.items, status))
    print(f"\nguardrails      {guardrails.state()}")
    print(f"fetched         {run.fetched} items, {run.blocked_raw} blocked by Guardrail #1")
    print(f"dedup stage 1   {run.admitted} new, {run.already_seen} already seen, "
          f"{run.events_updated} events updated")
    print(f"llm             {run.structured} structured, {run.irrelevant} irrelevant, "
          f"{run.blocked_input} blocked in, {run.blocked_output} rows blocked out, "
          f"{run.rejected} rejected")
    print(f"storage         funding {run.funding_new} new / {run.funding_merged} merged; "
          f"events {run.events_new} new / {run.events_merged} merged")
    print(f"spend           ${run.llm_usd:.6f} Bedrock, ~${run.apify_usd:.4f} Apify, "
          f"{run.firecrawl_credits} Firecrawl credits")
    if run.stopped_reason:
        print(f"stopped early   {run.stopped_reason}")
    print(f"still pending   {run.pending_left}")
    if run.error:
        print(f"\nRUN FAILED: {run.error}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Run the funding and events pipeline.")
    ap.add_argument("--once", action="store_true", help="run a single pass")
    ap.add_argument("--source", help="one source, by key (e.g. rss:entrackr, luma)")
    ap.add_argument("--force", action="store_true",
                    help="read sources even if read recently (paid ones cost)")
    ap.add_argument("--free-only", action="store_true",
                    help="read free sources only (RSS); no Apify or Firecrawl")
    ap.add_argument("--no-llm", action="store_true",
                    help="fetch and index only; the LLM stages run next time")
    args = ap.parse_args(argv)
    if not args.once:
        ap.error("pass --once for a single run; for the 12h schedule run "
                 "`python -m app.scheduler` or start the API server")

    from ..proclock import refresh_lock
    lock = refresh_lock()
    if not lock.acquire():
        print("A refresh is already running in another process; try again when it ends.")
        return 1
    try:
        run = run_pipeline("cli", only=args.source, force=args.force,
                           free_only=args.free_only, structure=not args.no_llm)
    finally:
        lock.release()
    _print(run)
    return 1 if run.error or failed_sources(run.id) else 0


if __name__ == "__main__":
    sys.exit(main())
