"""The blocked / rejected log: every item stopped before storage, and why.

  guardrail_raw     Guardrail #1 blocked the raw content
  guardrail_input   Guardrail #2 blocked the text bound for the LLM
  guardrail_output  Guardrail #3 blocked a row the LLM wrote
  validation        the LLM's answer failed validation (e.g. a round with no company)
  model             the LLM could not answer it in `max_item_attempts` tries

An item blocked by Guardrail #1 is re-fetched on every run of a source that
keeps listing it; its cached verdict blocks it again for free, and it is
logged once, not every time.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BlockedItem

STAGES = ("guardrail_raw", "guardrail_input", "guardrail_output", "validation", "model")


def log_blocked(session: Session, *, stage: str, source_key: str, url: str, title: str,
                reason: str, seen_item_id: int | None = None) -> bool:
    """Add one entry, unless the same URL and title are already logged at this
    stage. True when added."""
    if stage not in STAGES:
        raise ValueError(f"unknown blocked stage {stage!r}")
    url, title = url[:1000], title[:1000]
    exists = session.scalar(select(BlockedItem.id).where(
        BlockedItem.stage == stage, BlockedItem.url == url, BlockedItem.title == title).limit(1))
    if exists is not None:
        return False
    session.add(BlockedItem(stage=stage, source_key=source_key, url=url, title=title,
                            reason=reason[:1000], seen_item_id=seen_item_id))
    return True
