"""Insights > VC firms: who each firm funded or helped, on screen and as Excel.

  GET /api/insights/vcs?firm=accel        posts newest first, plus every firm
                                          with its count and last read
  GET /api/insights/vcs.xlsx?firm=...     the same rows as an Excel file

Both take `start` / `end` (YYYY-MM-DD, IST, inclusive) like the funding tabs,
`origin` (site | added | search | news) to show one kind of source, and
`all=true` to include posts sorted as Other (essays, podcasts, events).

Rows come from four places, merged by date:
  - site:   the firm's own website news, read on the 12h refresh by
            `ingest/vc_firms.py`;
  - added:  websites pasted on the page (`ingest/pasted_sources.py`), shown
            under the tracked firm they were tied to, else under their name;
  - search: a news search about the firm, for firms that have one;
  - news:   funding rounds already pulled from the news feed and "Search web"
            that name the firm among the investors. Free: a regex over rows.
One deal shows once: when the same firm and company appear within
DUPLICATE_DAYS, the firm's own post wins, then a pasted website's, then the
search result.

Reads only: no fetch, no model.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..ingest.pasted_sources import all_sources, firm_key
from ..ingest.sources import load_sources
from ..ingest.vc_firms import (
    READ_LABELS, SITE_VIAS, VIA_LABELS, VcFirm, get_firm, load_firms,
)
from ..models import (
    Article, Category, FundingRound, PastedSource, VcKind, VcPost, VcRead, WebRound, utcnow,
)
from ..search.firecrawl import domain_of
from .insights import IST, Window
from .pasted import PastedSourceOut, pasted_sources

router = APIRouter(prefix="/api/insights", tags=["insights"])

DUPLICATE_DAYS = 14
ORIGINS = ("site", "added", "search", "news")   # also the priority order for one deal


class VcPostOut(BaseModel):
    id: str                      # "site-4" or "news-12" / "web-3"
    firm: str
    firm_label: str
    kind: str                    # Investment | Portfolio news | Fund news | Other
    headline: str
    company: str | None
    round: str | None
    amount: str | None
    published_at: datetime | None
    date_approx: bool = False    # a day only, no time
    url: str
    origin: str                  # "site" | "added" | "search" | "news"
    source_label: str


class ReadOut(BaseModel):
    via: str
    label: str                   # "own website news page", "news search, via Firecrawl"
    is_site: bool
    last_read: datetime | None
    last_error: str | None


class FirmOut(BaseModel):
    key: str
    label: str
    home: str
    count: int                   # rows shown for the current filters
    reads: list[ReadOut]
    no_site: str | None          # why the firm's own site has no news to read
    pasted: bool = False         # a pasted website shown as a firm of its own


class VcsOut(BaseModel):
    firm: str | None
    firms: list[FirmOut]
    posts: list[VcPostOut]
    pending: int                 # posts read but not yet sorted
    hidden_other: int            # posts sorted as Other, not shown
    sources: list[PastedSourceOut]   # websites pasted on this page


def _in_window(when: datetime | None, window: Window) -> bool:
    if window.since is None and window.until is None:
        return True
    if when is None:
        return False            # undated rows cannot be placed in a range
    return ((window.since is None or when >= window.since)
            and (window.until is None or when < window.until))


def _pasted() -> dict[str, PastedSource]:
    """Websites pasted on this page, by the firm key their posts are stored under."""
    return {firm_key(src.id): src for src in all_sources("vcs")}


def _shown_as(src: PastedSource, firms: dict[str, VcFirm]) -> tuple[str, str]:
    """(key, label) a pasted website's posts are shown under."""
    owner = firms.get(src.firm or "")
    return (owner.key, owner.label) if owner else (firm_key(src.id), src.label)


def _site_posts(session: Session, firms: dict[str, VcFirm], pasted: dict[str, PastedSource],
                include_other: bool) -> tuple[list[VcPostOut], int]:
    rows = session.scalars(
        select(VcPost).where(VcPost.classified_at.is_not(None))).all()
    out, hidden = [], 0
    for p in rows:
        if p.firm in pasted:
            src = pasted[p.firm]
            key, label = _shown_as(src, firms)
            origin, source_label = "added", domain_of(src.url)
        elif p.firm in firms:
            key, label = p.firm, firms[p.firm].label
            origin = "site" if p.via in SITE_VIAS else "search"
            source_label = VIA_LABELS.get(p.via, p.via)
        else:
            continue            # a firm removed from the registry
        if p.kind == VcKind.OTHER and not include_other:
            hidden += 1
            continue
        out.append(VcPostOut(
            id=f"site-{p.id}", firm=key, firm_label=label,
            kind=(p.kind or VcKind.OTHER).value, headline=p.headline or p.title,
            company=p.company, round=p.round_label, amount=p.amount,
            published_at=p.published_at, date_approx=p.date_approx, url=p.url,
            origin=origin, source_label=source_label,
        ))
    return out, hidden


def _news_rounds(session: Session, firms: dict[str, VcFirm]) -> list[VcPostOut]:
    """Rounds from the news feed and Search web that name a firm as investor."""
    outlets = {s.name: s.label for s in load_sources()}
    candidates = []
    for fr, article in session.execute(
        select(FundingRound, Article)
        .join(Article, Article.id == FundingRound.article_id)
        .where(Article.canonical_id.is_(None), Article.category == Category.FUNDING,
               FundingRound.investors.is_not(None))
    ).all():
        candidates.append((f"news-{fr.id}", fr.investors, fr.company, fr.round_label,
                           fr.amount, article.published_at, False, article.url,
                           article.headline, outlets.get(article.source, article.source)))
    for wr in session.scalars(
        select(WebRound).where(WebRound.kept.is_(True), WebRound.investors.is_not(None))
    ).all():
        candidates.append((f"web-{wr.id}", wr.investors, wr.company, wr.round_label,
                           wr.amount, wr.published_at, wr.date_approx, wr.url,
                           wr.headline, wr.domain))

    out = []
    for (rid, investors, company, round_label, amount, published, approx, url,
         headline, source) in candidates:
        for firm in firms.values():
            if firm.matches(investors):
                out.append(VcPostOut(
                    id=f"{rid}-{firm.key}", firm=firm.key, firm_label=firm.label,
                    kind=VcKind.INVESTMENT.value, headline=headline, company=company,
                    round=round_label, amount=amount, published_at=published,
                    date_approx=approx, url=url, origin="news", source_label=source,
                ))
    return out


def _same_deal(a: VcPostOut, b: VcPostOut) -> bool:
    if a.firm != b.firm or not a.company or not b.company:
        return False
    if a.company.strip().lower() != b.company.strip().lower():
        return False
    if a.published_at is None or b.published_at is None:
        return True
    return abs(a.published_at - b.published_at) <= timedelta(days=DUPLICATE_DAYS)


def _rows(session: Session, window: Window, include_other: bool,
          origin: str | None = None) -> tuple[list[VcPostOut], int]:
    firms = {f.key: f for f in load_firms()}
    posts, hidden = _site_posts(session, firms, _pasted(), include_other)
    kept: list[VcPostOut] = []
    for row in sorted(posts + _news_rounds(session, firms),
                      key=lambda r: ORIGINS.index(r.origin)):
        if not any(_same_deal(row, k) for k in kept):
            kept.append(row)
    rows = [r for r in kept
            if _in_window(r.published_at, window) and (not origin or r.origin == origin)]
    # Newest first; undated rows last.
    rows.sort(key=lambda r: (r.published_at is not None,
                             r.published_at.timestamp() if r.published_at else 0),
              reverse=True)
    return rows, hidden


def _pasted_read(src: PastedSourceOut) -> ReadOut:
    return ReadOut(via=src.via, label=f"added by you, from {src.via_label}", is_site=True,
                   last_read=src.last_read, last_error=src.last_error)


def _firms(session: Session, rows: list[VcPostOut],
           pasted: list[PastedSourceOut]) -> list[FirmOut]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.firm] = counts.get(r.firm, 0) + 1

    latest = (select(VcRead.firm, VcRead.via, func.max(VcRead.id).label("id"))
              .group_by(VcRead.firm, VcRead.via).subquery())
    last = {(r.firm, r.via): r for r in session.scalars(
        select(VcRead).join(latest, VcRead.id == latest.c.id)).all()}
    out = []
    for f in load_firms():
        reads = []
        for r in f.reads:
            done = last.get((f.key, r.via))
            reads.append(ReadOut(
                via=r.via, label=READ_LABELS[r.via], is_site=r.is_site,
                last_read=done.started_at if done else None,
                last_error=done.error if done else None,
            ))
        reads += [_pasted_read(p) for p in pasted if p.firm == f.key]
        out.append(FirmOut(key=f.key, label=f.label, home=f.home,
                           count=counts.get(f.key, 0), reads=reads, no_site=f.no_site))
    # A pasted website not tied to a tracked firm is listed as a firm of its own.
    for p in pasted:
        if p.firm_label is None:
            out.append(FirmOut(key=p.key, label=p.label, home=p.url, count=counts.get(p.key, 0),
                               reads=[_pasted_read(p)], no_site=None, pasted=True))
    return out


def _firm_label(firm: str) -> str | None:
    """A tracked firm's name, or a pasted website's shown as a firm of its own."""
    found = get_firm(firm)
    if found:
        return found.label
    src = _pasted().get(firm)
    return src.label if src else None


def _check_firm(firm: str | None) -> str | None:
    if firm and _firm_label(firm) is None:
        raise HTTPException(status_code=404, detail=f"unknown firm {firm!r}")
    return firm or None


def _check_origin(origin: str | None) -> str | None:
    if origin and origin not in ORIGINS:
        raise HTTPException(status_code=400, detail="origin is site, added, search or news")
    return origin or None


@router.get("/vcs", response_model=VcsOut)
def vcs(
    firm: str | None = Query(None, description="one firm's key, or omit for all"),
    start: date | None = Query(None, description="published on or after (YYYY-MM-DD, IST)"),
    end: date | None = Query(None, description="published on or before (YYYY-MM-DD, IST)"),
    origin: str | None = Query(None, description="site | search | news"),
    all: bool = Query(False, description="include posts sorted as Other"),
    session: Session = Depends(get_session),
) -> VcsOut:
    firm = _check_firm(firm)
    rows, hidden = _rows(session, Window(start, end), all, _check_origin(origin))
    pending = session.scalar(
        select(func.count(VcPost.id)).where(VcPost.classified_at.is_(None))) or 0
    sources = pasted_sources(session, "vcs")
    return VcsOut(
        firm=firm, firms=_firms(session, rows, sources),
        posts=[r for r in rows if not firm or r.firm == firm],
        pending=pending, hidden_other=hidden, sources=sources,
    )


# --- Excel ------------------------------------------------------------------------

COLUMNS = [
    ("VC firm", 26), ("What", 15), ("Company", 26), ("Round", 16), ("Amount", 18),
    ("Published (IST)", 19), ("Found via", 20), ("Headline", 60), ("Link", 50),
]


@router.get("/vcs.xlsx")
def vcs_excel(
    firm: str | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
    origin: str | None = Query(None),
    all: bool = Query(False),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    firm = _check_firm(firm)
    window = Window(start, end)
    rows, _ = _rows(session, window, all, _check_origin(origin))
    rows = [r for r in rows if not firm or r.firm == firm]

    workbook = Workbook()
    ws = workbook.active
    ws.title = (_firm_label(firm) if firm else "VC firms")[:31]
    ws.append([name for name, _ in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1A00D9")
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    for r in rows:
        published = (r.published_at.astimezone(IST).replace(tzinfo=None)
                     if r.published_at else None)
        found_via = {"news": "News feed: ", "added": "Added by you: "}.get(r.origin, "") \
            + r.source_label
        ws.append([r.firm_label, r.kind, r.company or "", r.round or "",
                   r.amount or "", published, found_via, r.headline, r.url])
        row = ws.max_row
        ws.cell(row=row, column=6).number_format = (
            "dd mmm yyyy" if r.date_approx else "dd mmm yyyy hh:mm")
        link = ws.cell(row=row, column=9)
        link.hyperlink = r.url
        link.font = Font(color="1A00D9", underline="single")

    for index, (_, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=index).column_letter].width = width
    ws.auto_filter.ref = ws.dimensions

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    day = utcnow().astimezone(IST).strftime("%Y-%m-%d")
    name = f"gps-vc-{firm or 'all-firms'}-{window.label()}-{day}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
