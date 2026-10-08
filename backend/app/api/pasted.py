"""Insights: websites the reader pastes on Startup firms or VC firms.

  POST   /api/insights/sources {section, url, label?, firm?}
                                        add a website and read it now
  DELETE /api/insights/sources/{id}     remove it, with what was read from it

`section` is "startups" or "vcs"; each page lists only its own sources, in
the `sources` of its GET (`/insights/rounds`, `/insights/vcs`). Adding reads
the website straight away: free when it has a feed or a plain news page, 1
Firecrawl credit otherwise, then gpt-oss reads its new posts (~$0.00005 each).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..ingest.pasted_sources import (
    VIA_LABELS, SourceError, add_source, all_sources, firm_key, read_now, remove_source,
)
from ..ingest.vc_firms import load_firms
from ..models import PastedRound, PastedSourceRead, VcKind, VcPost

router = APIRouter(prefix="/api/insights", tags=["insights"])


class PastedSourceOut(BaseModel):
    id: int
    key: str                     # "pasted-3": the firm key its VC posts are under
    label: str
    url: str
    via: str                     # rss | html | scrape
    via_label: str               # "its feed", "its news page", "its page, via Firecrawl"
    firm: str | None             # the tracked firm it belongs to (VC firms only)
    firm_label: str | None
    shown: int                   # rounds in the tabs, or VC posts that are not Other
    last_read: datetime | None
    last_error: str | None


def pasted_sources(session: Session, section: str) -> list[PastedSourceOut]:
    """One page's pasted sources, with what each shows and how its last read went."""
    sources = all_sources(section)
    if not sources:
        return []
    ids = [s.id for s in sources]
    latest = (select(PastedSourceRead.source_id, func.max(PastedSourceRead.id).label("id"))
              .where(PastedSourceRead.source_id.in_(ids))
              .group_by(PastedSourceRead.source_id).subquery())
    last = {r.source_id: r for r in session.scalars(
        select(PastedSourceRead).join(latest, PastedSourceRead.id == latest.c.id)).all()}
    if section == "vcs":
        shown = dict(session.execute(
            select(VcPost.firm, func.count(VcPost.id))
            .where(VcPost.firm.in_([firm_key(i) for i in ids]),
                   VcPost.classified_at.is_not(None), VcPost.kind != VcKind.OTHER)
            .group_by(VcPost.firm)).all())
        shown = {int(key.rsplit("-", 1)[1]): n for key, n in shown.items()}
    else:
        shown = dict(session.execute(
            select(PastedRound.source_id, func.count(PastedRound.id))
            .where(PastedRound.source_id.in_(ids), PastedRound.kept.is_(True))
            .group_by(PastedRound.source_id)).all())

    firms = {f.key: f.label for f in load_firms()}
    out = []
    for src in sources:
        read = last.get(src.id)
        out.append(PastedSourceOut(
            id=src.id, key=firm_key(src.id), label=src.label, url=src.url, via=src.via,
            via_label=VIA_LABELS[src.via], firm=src.firm, firm_label=firms.get(src.firm or ""),
            shown=shown.get(src.id, 0),
            last_read=read.started_at if read else None,
            last_error=read.error if read else None,
        ))
    return out


class AddSourceIn(BaseModel):
    section: str
    url: str
    label: str | None = None
    firm: str | None = None


class AddSourceOut(BaseModel):
    added: bool
    source_id: int | None = None
    via: str | None = None
    via_label: str | None = None
    found: int = 0               # posts on the website
    new: int = 0                 # of which not seen before
    shown: int = 0               # rounds kept, or VC posts that are not Other
    credits_used: int = 0
    llm_usd: float = 0.0
    error: str | None = None


@router.post("/sources", response_model=AddSourceOut)
def add_pasted_source(request: AddSourceIn) -> AddSourceOut:
    """Save a website as a source for one page, then read it straight away."""
    try:
        source = add_source(request.section, request.url, request.label, request.firm)
    except SourceError as exc:
        return AddSourceOut(added=False, error=str(exc))

    result = read_now(source)
    read = result.reads[0]
    with session_scope() as s:
        shown = next((p.shown for p in pasted_sources(s, source.section) if p.id == source.id), 0)
    stopped = next((x.stopped_reason for x in (result.sorted, result.rounds)
                    if x is not None and x.stopped_reason), None)
    return AddSourceOut(
        added=True, source_id=source.id, via=source.via, via_label=VIA_LABELS[source.via],
        found=read.items_seen, new=read.items_new, shown=shown,
        credits_used=read.credits_used, llm_usd=round(result.llm_usd, 6),
        error=read.error or (f"The posts were not read yet: {stopped}" if stopped else None),
    )


@router.delete("/sources/{source_id}")
def delete_pasted_source(source_id: int) -> dict:
    if not remove_source(source_id):
        raise HTTPException(status_code=404, detail="no such source")
    return {"removed": True}
