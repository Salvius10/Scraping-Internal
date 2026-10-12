"""Export: one list of funding rounds, duplicates removed, ready to download.

  GET    /api/export                      the merged rounds, newest first, with
                                          the selections that fill them
  GET    /api/export.xlsx                 the same rounds as Excel, in the
                                          Startup firms layout: a sheet per stage
  POST   /api/export/selections {...}     "Add to Export" on Startup firms or LinkedIn
  DELETE /api/export/selections/{id}      take one selection back
  DELETE /api/export/selections           empty the list

A selection is the filter the reader was looking at -- a stage tab and dates
on Startup firms, an account and dates on LinkedIn (Funding posts only) --
saved, never copied. Every selection is re-run whenever the page is read, so
rounds the 12h refresh brings in appear on their own, and a story later
merged or reclassified drops out.

Duplicates: the same row reached by two selections counts once, and two rows
are one deal when they name the same company (case, punctuation and "Pvt
Ltd" aside) at the same stage within DUPLICATE_DAYS of each other -- the rule
Search web and pasted websites already use. The kept row is the most trusted
source's (news feed, then your websites, Search web, LinkedIn); details it
lacks are filled from the others, and Found via names every source.

Reads only: no fetch, no model, so it costs nothing.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_session, session_scope
from ..ingest.linkedin import all_sources as linkedin_sources
from ..ingest.web_rounds import DUPLICATE_DAYS
from ..models import ExportSelection, utcnow
from .insights import (
    IST, STAGE_KEYS, STAGE_TITLES, RoundOut, StageCount, Window, _rows as startup_rows,
    rounds_sheet,
)
from .linkedin import funding_by_stage

router = APIRouter(prefix="/api/export", tags=["export"])

SECTIONS = {"startups": "Startup firms", "linkedin": "LinkedIn"}

# Which copy of a deal is kept: the lower, the more trusted.
PRIORITY = {"feed": 0, "pasted": 1, "web": 2, "linkedin": 3}

_SUFFIXES = {"pvt", "private", "ltd", "limited", "inc", "llp", "llc", "corp"}


def company_key(name: str | None) -> str | None:
    """A company name in one spelling: "Orbo Pvt. Ltd." and "orbo" match."""
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words) or None


def _same_deal(a: RoundOut, b: RoundOut) -> bool:
    if a.stage != b.stage or not a.published_at or not b.published_at:
        return False
    key = company_key(a.company)
    if key is None or key != company_key(b.company):
        return False
    return abs(a.published_at - b.published_at).days <= DUPLICATE_DAYS


def _absorb(kept: RoundOut, dup: RoundOut) -> None:
    """Fill what the kept row lacks from a duplicate, and note where it was found."""
    if not kept.round and dup.round:
        kept.round = dup.round
    if not kept.amount and dup.amount:
        kept.amount = dup.amount
    if not kept.investors and dup.investors:
        kept.investors = list(dup.investors)
    for origin in (dup.origin, *dup.also_via):
        if origin != kept.origin and origin not in kept.also_via:
            kept.also_via.append(origin)


def merge(rows: list[RoundOut]) -> list[RoundOut]:
    """One row per deal, newest first; undated rows last."""
    unique: dict[str, RoundOut] = {}
    for row in rows:
        unique.setdefault(row.id, row.model_copy(deep=True))
    # Most trusted first, so a duplicate always folds into the better copy.
    ordered = sorted(unique.values(), key=lambda r: (
        PRIORITY.get(r.origin, len(PRIORITY)),
        -(r.published_at.timestamp() if r.published_at else 0)))
    kept: list[RoundOut] = []
    for row in ordered:
        match = next((k for k in kept if _same_deal(k, row)), None)
        if match is None:
            kept.append(row)
        else:
            _absorb(match, row)
    kept.sort(key=lambda r: (r.published_at is not None,
                             r.published_at.timestamp() if r.published_at else 0),
              reverse=True)
    return kept


# --- Selections -----------------------------------------------------------------------

def _selection_rows(session: Session, sel: ExportSelection) -> list[RoundOut]:
    """What one selection holds right now."""
    window = Window(sel.start_day, sel.end_day)
    if sel.section == "startups":
        stages = [STAGE_KEYS[sel.stage]] if sel.stage else list(STAGE_KEYS.values())
        return [r for stage in stages for r in startup_rows(session, stage, window)]
    by_stage = funding_by_stage(session, window, sel.source_id)
    return [r for rows in by_stage.values() for r in rows]


def _all_selections(session: Session) -> list[ExportSelection]:
    return list(session.scalars(select(ExportSelection).order_by(ExportSelection.id)).all())


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def _dates(sel: ExportSelection) -> str:
    if not (sel.start_day or sel.end_day):
        return "all dates"
    return (f"{_day(sel.start_day) if sel.start_day else 'the beginning'} to "
            f"{_day(sel.end_day) if sel.end_day else 'today'}")


def _label(sel: ExportSelection, accounts: dict[int, str]) -> str:
    if sel.section == "startups":
        what = STAGE_TITLES[STAGE_KEYS[sel.stage]] if sel.stage else "all stages"
    elif sel.source_id is None:
        what = "all accounts"
    else:
        what = accounts.get(sel.source_id, "a removed account")
    return f"{SECTIONS[sel.section]} · {what}"


class SelectionOut(BaseModel):
    id: int
    section: str                 # startups | linkedin
    label: str                   # "Startup firms · Seed"
    dates: str                   # "13 Sep 2026 to today"
    rounds: int                  # rows it holds now, before duplicates are removed
    added_at: datetime


class ExportOut(BaseModel):
    selections: list[SelectionOut]
    rounds: list[RoundOut]
    stages: list[StageCount]     # merged rounds per stage
    total: int                   # merged rounds
    duplicates: int              # rows removed as repeats of one deal


def _build(session: Session) -> tuple[list[SelectionOut], list[RoundOut], int]:
    accounts = {s.id: s.label for s in linkedin_sources()}
    selections, rows = [], []
    for sel in _all_selections(session):
        held = _selection_rows(session, sel)
        rows += held
        selections.append(SelectionOut(
            id=sel.id, section=sel.section, label=_label(sel, accounts), dates=_dates(sel),
            rounds=len(held), added_at=sel.added_at))
    merged = merge(rows)
    return selections, merged, len(rows) - len(merged)


@router.get("", response_model=ExportOut)
def export(session: Session = Depends(get_session)) -> ExportOut:
    selections, rounds, duplicates = _build(session)
    stages = [StageCount(key=key, label=STAGE_TITLES[stage],
                         count=sum(1 for r in rounds if r.stage == STAGE_TITLES[stage]))
              for key, stage in STAGE_KEYS.items()]
    return ExportOut(selections=selections, rounds=rounds, stages=stages,
                     total=len(rounds), duplicates=duplicates)


class AddIn(BaseModel):
    section: str                 # startups | linkedin
    stage: str | None = None     # startups: a tab key, or None for every stage
    source_id: int | None = None  # linkedin: an account id, or None for every account
    start: date | None = None
    end: date | None = None


class AddOut(BaseModel):
    added: bool
    selection_id: int | None = None
    label: str | None = None
    rounds: int = 0              # rows the selection holds
    new: int = 0                 # of which new to Export
    duplicates: int = 0          # of which already in Export, merged away
    total: int = 0               # rounds in Export now
    error: str | None = None


def _check(request: AddIn) -> str | None:
    """Why this selection cannot be added, or None."""
    if request.section not in SECTIONS:
        return "Only Startup firms and LinkedIn can be added to Export."
    if request.start and request.end and request.start > request.end:
        return "The From date is after the To date."
    if request.section == "startups":
        if request.source_id is not None:
            return "Startup firms has no accounts."
        if request.stage is not None and request.stage not in STAGE_KEYS:
            return f"Unknown stage {request.stage!r}."
    else:
        if request.stage is not None:
            return "LinkedIn is added by account, not by stage."
        if (request.source_id is not None
                and request.source_id not in {s.id for s in linkedin_sources()}):
            return "That LinkedIn account is no longer a source."
    return None


@router.post("/selections", response_model=AddOut)
def add_selection(request: AddIn) -> AddOut:
    """Add what the reader is looking at, and say how much of it was already there."""
    problem = _check(request)
    if problem:
        return AddOut(added=False, error=problem)
    with session_scope() as s:
        existing = s.scalars(select(ExportSelection).where(
            ExportSelection.section == request.section,
            ExportSelection.stage.is_(None) if request.stage is None
            else ExportSelection.stage == request.stage,
            ExportSelection.source_id.is_(None) if request.source_id is None
            else ExportSelection.source_id == request.source_id,
            ExportSelection.start_day.is_(None) if request.start is None
            else ExportSelection.start_day == request.start,
            ExportSelection.end_day.is_(None) if request.end is None
            else ExportSelection.end_day == request.end,
        )).first()
        if existing is not None:
            return AddOut(added=False, error="That is already in Export.")
        _, before, _ = _build(s)
        sel = ExportSelection(section=request.section, stage=request.stage,
                              source_id=request.source_id, start_day=request.start,
                              end_day=request.end)
        s.add(sel)
        s.flush()
        held = len(_selection_rows(s, sel))
        _, after, _ = _build(s)
        label = _label(sel, {a.id: a.label for a in linkedin_sources()})
        new = max(0, len(after) - len(before))
        return AddOut(added=True, selection_id=sel.id, label=label, rounds=held, new=new,
                      duplicates=max(0, held - new), total=len(after))


@router.delete("/selections/{selection_id}")
def remove_selection(selection_id: int) -> dict:
    with session_scope() as s:
        sel = s.get(ExportSelection, selection_id)
        if sel is None:
            raise HTTPException(status_code=404, detail="no such selection")
        s.delete(sel)
    return {"removed": True}


@router.delete("/selections")
def clear_selections() -> dict:
    with session_scope() as s:
        removed = s.execute(delete(ExportSelection)).rowcount
    return {"removed": removed}


# --- Excel ------------------------------------------------------------------------

@router.get(".xlsx")
def export_excel(session: Session = Depends(get_session)) -> StreamingResponse:
    from openpyxl import Workbook

    _, rounds, _ = _build(session)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for stage in STAGE_KEYS.values():
        title = STAGE_TITLES[stage]
        rounds_sheet(workbook, title, [r for r in rounds if r.stage == title])

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    day = utcnow().astimezone(IST).strftime("%Y-%m-%d")
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="gps-export-funding-{day}.xlsx"'},
    )
