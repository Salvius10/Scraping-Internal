"""Platform: Export to Excel.

  GET /api/export/funding.xlsx?stage=&source=&start=&end=&q=
  GET /api/export/events.xlsx?when=&city=&start=&end=&q=

Each takes the same filters as its feed, so the file holds exactly what the
reader is looking at -- every page of it. Every row carries its source URL
(clickable) and date. Reads only: no fetch, no model, so it costs nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..db import get_session
from . import events as events_api
from . import funding as funding_api
from .common import download, excel_date, sheet

router = APIRouter(prefix="/api/export", tags=["export"])

EXPORT_LIMIT = 50_000

FUNDING_COLUMNS = [
    ("Company", 28), ("Stage", 11), ("Round", 16), ("Amount", 18), ("Investors", 40),
    ("Sector", 18), ("HQ city", 14), ("Summary", 60), ("Published (IST)", 18),
    ("Source", 22), ("Found via", 14), ("Source URL", 60), ("Also reported at", 60),
]

EVENT_COLUMNS = [
    ("Event", 44), ("Starts (IST)", 18), ("Ends (IST)", 18), ("City", 14), ("Venue", 30),
    ("Online", 8), ("Organiser", 28), ("Category", 16), ("Price", 14), ("Summary", 60),
    ("Source", 14), ("Event URL", 60), ("Also listed at", 60),
]


@router.get("/funding.xlsx")
def funding_excel(f: funding_api.FundingFilter = Depends(funding_api.funding_filter),
                  session: Session = Depends(get_session)) -> StreamingResponse:
    from openpyxl import Workbook

    rows = session.scalars(funding_api.ordered(funding_api.query(f)).limit(EXPORT_LIMIT)).all()
    values = [[
        r.company, r.stage.value, r.round_label or "", r.amount or "Undisclosed",
        ", ".join(r.investors or []), r.sector or "", r.hq_city or "", r.summary or "",
        excel_date(r.published_at), r.source_label,
        funding_api.SOURCE_TYPES.get(r.source_type, r.source_type), r.source_url,
        "\n".join(s["url"] for s in (r.sources or [])
                  if isinstance(s, dict) and s.get("url") and s["url"] != r.source_url),
    ] for r in rows]
    workbook = Workbook()
    workbook.remove(workbook.active)
    sheet(workbook, "Funded startups", FUNDING_COLUMNS, values,
          date_columns={9: "dd mmm yyyy hh:mm"}, link_column=12)
    return download(workbook, f"funded-startups-{f.stage or 'all-stages'}")


@router.get("/events.xlsx")
def events_excel(f: events_api.EventFilter = Depends(events_api.event_filter),
                 session: Session = Depends(get_session)) -> StreamingResponse:
    from openpyxl import Workbook

    rows = session.scalars(events_api.ordered(events_api.query(f), f.when)
                           .limit(EXPORT_LIMIT)).all()
    values = [[
        r.title, excel_date(r.starts_at), excel_date(r.ends_at), r.city or "", r.venue or "",
        "Yes" if r.online else ("No" if r.online is False else ""), r.organiser or "",
        r.category or "", r.price or "", r.summary or "", r.source_label, r.url,
        "\n".join(s["url"] for s in (r.sources or [])
                  if isinstance(s, dict) and s.get("url") and s["url"] != r.url),
    ] for r in rows]
    workbook = Workbook()
    workbook.remove(workbook.active)
    sheet(workbook, "India startup events", EVENT_COLUMNS, values,
          date_columns={2: "dd mmm yyyy hh:mm", 3: "dd mmm yyyy hh:mm"}, link_column=12)
    return download(workbook, f"startup-events-{f.when}")
