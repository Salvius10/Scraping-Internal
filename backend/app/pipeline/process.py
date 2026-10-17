"""The queue after Dedup Stage 1: Guardrail #2 -> LLM -> Guardrail #3 -> storage.

Reads pending items from the seen-items index, oldest first, at most
`max_items_per_run` a run:

  Guardrail #2  screens exactly the text each item will put in the prompt;
                a blocked item is logged and never reaches the model
  LLM           structures items in batches by kind (structure.py)
  Guardrail #3  screens every row the model wrote; a blocked row is logged
                and never stored
  Storage       Dedup Stage 2, then insert or merge (storage.py)

Each item ends in one state -- stored, irrelevant, blocked or rejected -- written
in the same transaction as its rows, so a crash never leaves an item stored
twice or marked done without its rows. An item that cannot be finished this
run (the model skipped it, a guardrail could not be reached, the budget ran
out) stays pending for the next run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import ItemStatus, SeenItem, utcnow
from . import guardrails
from .blocklog import log_blocked
from .items import EVENT, EVENT_PAGE, FUNDING, FUNDING_PAGE
from .storage import NEW, store_event, store_funding
from .structure import (
    EventRow, FundingRow, Outcome, Row, Work, prompt_text, row_text, structure_events,
    structure_funding, structure_funding_page, structure_page,
)

log = logging.getLogger(__name__)


@dataclass
class ProcessStats:
    considered: int = 0
    blocked_input: int = 0
    held: int = 0                  # a guardrail could not be reached; still pending
    structured: int = 0            # items the model answered
    irrelevant: int = 0
    blocked_output: int = 0        # rows
    rejected: int = 0
    funding_new: int = 0
    funding_merged: int = 0
    events_new: int = 0
    events_merged: int = 0
    llm_usd: float = 0.0
    guardrail_usd: float = 0.0
    stopped_reason: str | None = None


def load_pending(limit: int) -> list[Work]:
    with session_scope() as s:
        rows = s.scalars(select(SeenItem).where(SeenItem.status == ItemStatus.PENDING)
                         .order_by(SeenItem.id).limit(limit)).all()
        return [Work(id=r.id, kind=r.kind, source_key=r.source_key, source_type=r.source_type,
                     source_label=r.source_label, url=r.url, title=r.title, text=r.text,
                     published_at=r.published_at, payload=dict(r.payload or {}),
                     attempts=r.attempts) for r in rows]


def _finish(session, work: Work, status: ItemStatus, note: str | None = None) -> None:
    seen = session.get(SeenItem, work.id)
    if seen is not None:
        seen.status, seen.processed_at = status, utcnow()
        seen.last_error = note


def _count_attempt(works: list[Work], reason: str, stats: ProcessStats) -> None:
    """The model did not answer these. Retry later, or give up after max attempts."""
    if not works:
        return
    with session_scope() as s:
        for work in works:
            seen = s.get(SeenItem, work.id)
            if seen is None:
                continue
            seen.attempts = (seen.attempts or 0) + 1
            seen.last_error = reason[:500]
            if seen.attempts >= settings.max_item_attempts:
                seen.status, seen.processed_at = ItemStatus.REJECTED, utcnow()
                log_blocked(s, stage="model", source_key=work.source_key, url=work.url,
                            title=work.title, reason=f"no answer after {seen.attempts} tries: "
                            f"{reason}", seen_item_id=work.id)
                stats.rejected += 1


def _store_rows(work: Work, rows: list[Row], blocked: list[tuple[Row, str]],
                stats: ProcessStats) -> None:
    """One item's outcome, rows and status together, in one transaction."""
    with session_scope() as s:
        for row, reason in blocked:
            title = row.company if isinstance(row, FundingRow) else row.title
            log_blocked(s, stage="guardrail_output", source_key=work.source_key, url=work.url,
                        title=title, reason=reason, seen_item_id=work.id)
        for row in rows:
            if isinstance(row, FundingRow):
                outcome, _ = store_funding(s, work, row)
                if outcome == NEW:
                    stats.funding_new += 1
                else:
                    stats.funding_merged += 1
            else:
                outcome, _ = store_event(s, work, row)
                if outcome == NEW:
                    stats.events_new += 1
                else:
                    stats.events_merged += 1
        if rows:
            _finish(s, work, ItemStatus.STORED)
        elif blocked:
            _finish(s, work, ItemStatus.BLOCKED, blocked[0][1])
        else:
            _finish(s, work, ItemStatus.IRRELEVANT)
    stats.blocked_output += len(blocked)
    if not rows and not blocked:
        stats.irrelevant += 1


def _apply(batch: list[Work], outcome: Outcome, stats: ProcessStats) -> None:
    answered = set(outcome.rows) | set(outcome.rejected)
    stats.structured += len(answered)
    _count_attempt([w for w in batch if w.id not in answered],
                   "the model left this item out of its answer", stats)

    if outcome.rejected:
        with session_scope() as s:
            for work in batch:
                reason = outcome.rejected.get(work.id)
                if reason is None:
                    continue
                _finish(s, work, ItemStatus.REJECTED, reason)
                log_blocked(s, stage="validation", source_key=work.source_key, url=work.url,
                            title=work.title, reason=reason, seen_item_id=work.id)
                stats.rejected += 1

    by_id = {w.id: w for w in batch}
    pairs = [(by_id[item_id], row) for item_id, rows in outcome.rows.items() for row in rows]
    screened = guardrails.screen(guardrails.OUTPUT, pairs, lambda pair: row_text(pair[1]))
    stats.guardrail_usd += screened.cost_usd
    held = {work.id for (work, _), _ in screened.held}
    stats.held += len(held)
    passed: dict[int, list[Row]] = {}
    for work, row in screened.passed:
        passed.setdefault(work.id, []).append(row)
    blocked: dict[int, list[tuple[Row, str]]] = {}
    for (work, row), reason in screened.blocked:
        blocked.setdefault(work.id, []).append((row, reason))

    for item_id in outcome.rows:
        if item_id in held:
            continue                 # Guardrail #3 unreachable: the whole item waits
        _store_rows(by_id[item_id], passed.get(item_id, []), blocked.get(item_id, []), stats)


def _jobs(works: list[Work]) -> list[tuple[list[Work], Callable[[list[Work]], Outcome]]]:
    size = max(1, settings.llm_batch_size)
    jobs: list[tuple[list[Work], Callable[[list[Work]], Outcome]]] = []
    for kind, structure in ((FUNDING, structure_funding), (EVENT, structure_events)):
        group = [w for w in works if w.kind == kind]
        jobs += [(group[i:i + size], structure) for i in range(0, len(group), size)]
    jobs += [([w], lambda batch: structure_page(batch[0])) for w in works if w.kind == EVENT_PAGE]
    jobs += [([w], lambda batch: structure_funding_page(batch[0]))
             for w in works if w.kind == FUNDING_PAGE]
    return jobs


def process_pending(limit: int | None = None) -> ProcessStats:
    stats = ProcessStats()
    works = load_pending(limit or settings.max_items_per_run)
    stats.considered = len(works)
    if not works:
        return stats

    # Guardrail #2 -- the LLM input.
    screened = guardrails.screen(guardrails.INPUT, works, prompt_text)
    stats.guardrail_usd += screened.cost_usd
    stats.held += len(screened.held)
    if screened.blocked:
        with session_scope() as s:
            for work, reason in screened.blocked:
                _finish(s, work, ItemStatus.BLOCKED, reason)
                log_blocked(s, stage="guardrail_input", source_key=work.source_key,
                            url=work.url, title=work.title, reason=reason, seen_item_id=work.id)
        stats.blocked_input = len(screened.blocked)

    # LLM structuring, then Guardrail #3 and storage, batch by batch.
    for batch, structure in _jobs(screened.passed):
        outcome = structure(batch)
        stats.llm_usd += outcome.cost_usd
        if outcome.error:
            if outcome.error_kind in ("budget", "unavailable"):
                stats.stopped_reason = outcome.error
                log.warning("structuring stopped: %s", outcome.error)
                break
            _count_attempt(batch, f"unreadable answer: {outcome.error}", stats)
            continue
        _apply(batch, outcome, stats)

    log.info("process: %d considered, %d structured, %d irrelevant, %d blocked in / %d out, "
             "%d rejected, %d held; funding %d new %d merged; events %d new %d merged; "
             "$%.6f LLM, $%.6f guardrails", stats.considered, stats.structured,
             stats.irrelevant, stats.blocked_input, stats.blocked_output, stats.rejected,
             stats.held, stats.funding_new, stats.funding_merged, stats.events_new,
             stats.events_merged, stats.llm_usd, stats.guardrail_usd)
    return stats


__all__ = ["EventRow", "FundingRow", "ProcessStats", "load_pending", "process_pending"]
