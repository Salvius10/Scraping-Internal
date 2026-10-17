"""Dedup Stage 1 -- URL + content hash, against the seen-items index.

Drops already-seen items before they reach the LLM, which is what keeps a
source that repeats itself every run (an RSS feed's last 50 stories, a city's
upcoming events) free after the first read:

  same URL (normalised: tracking parameters, "www." and the fragment aside)
  same content (the same words under another URL: a syndicated copy)
  or a repeat within this batch

Everything else is written to the index as `pending`: the index is also the
work queue the LLM stages read from, so an item the model could not answer
this run is picked up next run without fetching it again.

One exception for events: a known event whose text changed (moved to another
day or venue) updates its stored row from the portal's structured fields. The
event was already judged relevant, so no model call is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from ..db import session_scope
from ..models import ItemStatus, SeenItem, StartupEvent, utcnow
from .items import EVENT, RawItem, content_hash, url_hash
from .storage import canonical_city
from .timeutil import parse_iso

log = logging.getLogger(__name__)

_CHUNK = 500        # hashes per IN (...) lookup; SQLite allows 999 parameters


@dataclass
class Admitted:
    new: int = 0
    seen: int = 0
    events_updated: int = 0


def _existing(column, values: list[str]) -> dict[str, SeenItem]:
    found: dict[str, SeenItem] = {}
    with session_scope() as s:
        for start in range(0, len(values), _CHUNK):
            chunk = values[start:start + _CHUNK]
            for row in s.scalars(select(SeenItem).where(column.in_(chunk))).all():
                found.setdefault(getattr(row, column.key), row)
    return found


def _update_event(seen_id: int, item: RawItem, new_hash: str) -> bool:
    """Bring a stored event in line with its portal. True when a row changed."""
    payload = item.payload
    starts = parse_iso(payload.get("starts_at"))
    if starts is None:
        return False
    with session_scope() as s:
        seen = s.get(SeenItem, seen_id)
        if seen is None:
            return False
        seen.content_hash = new_hash
        seen.text = item.text
        seen.payload = dict(payload)
        if seen.status != ItemStatus.STORED:
            return False
        event = s.scalars(select(StartupEvent).where(StartupEvent.source_url == item.url)).first()
        if event is None:
            return False
        event.starts_at = starts
        event.ends_at = parse_iso(payload.get("ends_at"))
        event.date_only = bool(payload.get("date_only"))
        for name in ("venue", "online", "price"):
            if payload.get(name) is not None:
                setattr(event, name, payload[name])
        if payload.get("city"):
            event.city = canonical_city(payload["city"])
        event.updated_at = utcnow()
        return True


def admit(items: list[RawItem]) -> Admitted:
    """Write every item not seen before to the index as pending."""
    result = Admitted()
    if not items:
        return result
    keyed = [(item, url_hash(item), content_hash(item)) for item in items]
    by_url = _existing(SeenItem.url_hash, sorted({u for _, u, _ in keyed}))
    by_content = _existing(SeenItem.content_hash, sorted({c for _, _, c in keyed}))

    batch_urls: set[str] = set()
    batch_contents: set[str] = set()
    fresh: list[SeenItem] = []
    for item, uh, ch in keyed:
        if uh in batch_urls or ch in batch_contents:
            result.seen += 1
            continue
        batch_urls.add(uh)
        batch_contents.add(ch)

        known = by_url.get(uh)
        if known is not None:
            result.seen += 1
            if item.kind == EVENT and known.content_hash != ch and _update_event(known.id, item, ch):
                result.events_updated += 1
            continue
        if ch in by_content:
            result.seen += 1
            continue
        fresh.append(SeenItem(
            url_hash=uh, content_hash=ch, url=item.url, kind=item.kind,
            source_key=item.source_key, source_type=item.source_type,
            source_label=item.source_label, title=item.title, text=item.text,
            published_at=item.published_at, payload=dict(item.payload),
            status=ItemStatus.PENDING, attempts=0,
        ))

    if fresh:
        with session_scope() as s:
            s.add_all(fresh)
    result.new = len(fresh)
    log.info("dedup stage 1: %d new, %d already seen, %d events updated",
             result.new, result.seen, result.events_updated)
    return result
