"""Platform: Enrichment -- the leaders of funded startups, with their contacts.

  GET  /api/enrichment/preview?<funding filters>   what sending them would cost
  POST /api/enrichment?<funding filters>           send them: queue, start the job
  GET  /api/enrichment?designation=&q=             the leaders found, with counts
  GET  /api/enrichment/status                      the job and the queue
  GET  /api/enrichment/companies                   every company sent, and how it went
  POST /api/enrichment/resume                      work the companies still waiting
  POST /api/enrichment/retry                       queue the failed ones again, and work them
  DELETE /api/enrichment                           clear the list (not while a job runs)

"Send data for enrichment" on Funded startups takes the same filters as the
feed and its export, so exactly the companies the reader is looking at are
sent. A company already enriched is not sent -- or paid for -- again. Looking
people up costs SignalHire credits; the preview says how many at most before
anything is spent. A refused send is an ordinary answer (`sent: false` with
the reason), not an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import LIKE_ESCAPE, get_session, like_pattern
from ..enrichment import roles, worker
from ..enrichment.worker import LISTED
from ..models import EnrichedContact, EnrichmentCompany, EnrichStatus
from ..search import signalhire
from . import funding as funding_api
from .common import MAX_LIMIT, search_text

router = APIRouter(prefix="/api/enrichment", tags=["enrichment"])

SEND_LIMIT = 50_000
NOT_READY = ("SignalHire is not set up yet. Add SIGNALHIRE_API_KEY to .env and restart "
             "the server.")


def _companies(n: int) -> str:
    return f"{n} {'company' if n == 1 else 'companies'}"


# --- The leaders found ---------------------------------------------------------------

@dataclass
class ContactFilter:
    designation: str | None = None
    q: str | None = None


def contact_filter(
    designation: str | None = Query(None, description=" | ".join(roles.DESIGNATION_KEYS)),
    q: str | None = Query(None, max_length=200),
) -> ContactFilter:
    if designation is not None and designation not in roles.DESIGNATION_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown designation {designation!r}.")
    return ContactFilter(designation=designation, q=search_text(q))


def _holds(label: str):
    # No designation's name is part of another's, so a substring test is exact.
    return EnrichedContact.designation.ilike(like_pattern(label), escape=LIKE_ESCAPE)


def query(f: ContactFilter, *columns, with_designation: bool = True) -> Select:
    stmt = (select(*(columns or (EnrichedContact, EnrichmentCompany)))
            .select_from(EnrichedContact)
            .join(EnrichmentCompany, EnrichedContact.company_id == EnrichmentCompany.id)
            .where(LISTED))
    if f.q:
        pattern = like_pattern(f.q)
        stmt = stmt.where(or_(*(column.ilike(pattern, escape=LIKE_ESCAPE) for column in (
            EnrichedContact.full_name, EnrichedContact.title, EnrichedContact.email,
            EnrichedContact.location, EnrichmentCompany.company))))
    if with_designation and f.designation:
        stmt = stmt.where(_holds(roles.DESIGNATION_KEYS[f.designation]))
    return stmt


_NOT_SHOWN_ORDER = {EnrichStatus.NO_MATCH: 0, EnrichStatus.FAILED: 1, EnrichStatus.QUEUED: 2,
                    EnrichStatus.WORKING: 2, EnrichStatus.DONE: 3}


def not_shown(session: Session, f: ContactFilter, shown: set[int]) -> list[EnrichmentCompany]:
    """Every company sent that none of the leaders `f` selects belongs to: no
    leaders found, failed, still waiting, or none with the chosen designation.
    A search narrows them by company name. Not enriched first, then by name."""
    stmt = select(EnrichmentCompany).where(LISTED)
    if f.q:
        stmt = stmt.where(EnrichmentCompany.company.ilike(like_pattern(f.q), escape=LIKE_ESCAPE))
    rows = [c for c in session.scalars(stmt) if c.id not in shown]
    return sorted(rows, key=lambda c: (_NOT_SHOWN_ORDER[c.status], c.company.lower()))


def ordered(stmt: Select) -> Select:
    """Companies most recently enriched first; within one, founders first, and
    anyone who may be at another company of the same name last."""
    return stmt.order_by(EnrichmentCompany.enriched_at.desc().nullslast(),
                         EnrichmentCompany.company, EnrichedContact.maybe_other,
                         EnrichedContact.rank, EnrichedContact.id)


class ContactOut(BaseModel):
    id: int
    full_name: str
    designation: str
    title: str | None
    company: str
    company_id: int
    email: str | None
    phone: str | None
    linkedin: str | None
    other_emails: list[str]
    other_phones: list[str]
    location: str | None
    company_page: str | None          # the LinkedIn page of the company they work at
    company_site: str | None
    maybe_other: bool                 # may be at another company with the same name
    stage: str | None
    sector: str | None
    hq_city: str | None
    lookup: str
    looked_up_at: datetime

    @classmethod
    def of(cls, contact: EnrichedContact, company: EnrichmentCompany) -> "ContactOut":
        return cls(
            id=contact.id, full_name=contact.full_name, designation=contact.designation,
            title=contact.title, company=company.company, company_id=company.id,
            email=contact.email, phone=contact.phone, linkedin=contact.linkedin,
            other_emails=[str(e) for e in contact.other_emails or []],
            other_phones=[str(p) for p in contact.other_phones or []],
            location=contact.location, company_page=contact.company_page,
            company_site=contact.company_site, maybe_other=bool(contact.maybe_other),
            stage=company.stage, sector=company.sector,
            hq_city=company.hq_city, lookup=contact.lookup, looked_up_at=contact.looked_up_at,
        )


class DesignationCount(BaseModel):
    key: str
    label: str
    count: int


class ContactsPage(BaseModel):
    rows: list[ContactOut]
    total: int
    all_designations: int             # the same search, every designation
    limit: int
    offset: int
    designations: list[DesignationCount]


@router.get("", response_model=ContactsPage)
def contacts(
    f: ContactFilter = Depends(contact_filter),
    limit: int = Query(200, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> ContactsPage:
    total = session.scalar(query(f, func.count(EnrichedContact.id))) or 0
    rows = session.execute(ordered(query(f)).limit(limit).offset(offset)).all()
    # A person can hold two designations ("Co-founder, CEO"), so the tabs' counts
    # do not add up to everyone: count everyone separately.
    everyone, *counts = session.execute(query(f, func.count(EnrichedContact.id), *(
        func.coalesce(func.sum(case((_holds(label), 1), else_=0)), 0)
        for label in roles.DESIGNATION_KEYS.values()), with_designation=False)).one()
    return ContactsPage(
        rows=[ContactOut.of(contact, company) for contact, company in rows],
        total=total, all_designations=everyone or 0, limit=limit, offset=offset,
        designations=[DesignationCount(key=key, label=label, count=int(count or 0))
                      for (key, label), count in zip(roles.DESIGNATION_KEYS.items(), counts)])


# --- The job and the queue ---------------------------------------------------------------

class EnrichmentStatus(BaseModel):
    ready: bool                       # a SignalHire key is set
    running: bool
    started_at: datetime | None
    finished_at: datetime | None
    current: str | None               # the company being enriched now
    companies: int                    # every company sent
    waiting: int                      # queued, or claimed by a job
    done: int
    no_match: int
    failed: int
    contacts: int                     # leaders found
    reachable: int                    # of them, with an email or a phone
    credits_used: int                 # every job, all time
    credits_left: int | None
    per_company: int                  # most people looked up per company
    message: str | None               # how the last job went
    error: str | None                 # why it stopped early


def enrichment_status(session: Session) -> EnrichmentStatus:
    job = worker.state()
    counts = dict(session.execute(select(EnrichmentCompany.status, func.count())
                                  .where(LISTED).group_by(EnrichmentCompany.status)).all())
    everyone = query(ContactFilter(), func.count(EnrichedContact.id))
    contacts_total = session.scalar(everyone) or 0
    reachable = session.scalar(everyone.where(or_(
        EnrichedContact.email.is_not(None), EnrichedContact.phone.is_not(None)))) or 0
    used = session.scalar(select(func.coalesce(func.sum(EnrichmentCompany.credits_used), 0))
                          .where(LISTED))
    return EnrichmentStatus(
        ready=signalhire.ready(), running=job.running, started_at=job.started_at,
        finished_at=job.finished_at, current=job.current,
        companies=sum(counts.values()),
        waiting=counts.get(EnrichStatus.QUEUED, 0) + counts.get(EnrichStatus.WORKING, 0),
        done=counts.get(EnrichStatus.DONE, 0), no_match=counts.get(EnrichStatus.NO_MATCH, 0),
        failed=counts.get(EnrichStatus.FAILED, 0), contacts=contacts_total, reachable=reachable,
        credits_used=int(used or 0), credits_left=worker.credits_left(),
        per_company=settings.signalhire_contacts_per_company,
        message=job.message, error=job.error,
    )


@router.get("/status", response_model=EnrichmentStatus)
def status(session: Session = Depends(get_session)) -> EnrichmentStatus:
    return enrichment_status(session)


class CompanyOut(BaseModel):
    id: int
    company: str
    stage: str | None
    amount: str | None
    sector: str | None
    hq_city: str | None
    status: str
    note: str | None
    people_found: int
    credits_used: int
    requested_at: datetime
    enriched_at: datetime | None


@router.get("/companies", response_model=list[CompanyOut])
def companies(session: Session = Depends(get_session)) -> list[CompanyOut]:
    rows = session.scalars(select(EnrichmentCompany).where(LISTED).order_by(
        EnrichmentCompany.requested_at.desc(), EnrichmentCompany.id.desc()).limit(2000)).all()
    return [CompanyOut(
        id=r.id, company=r.company, stage=r.stage, amount=r.amount, sector=r.sector,
        hq_city=r.hq_city, status=r.status.value, note=r.note, people_found=r.people_found,
        credits_used=r.credits_used, requested_at=r.requested_at, enriched_at=r.enriched_at,
    ) for r in rows]


# --- Sending companies ---------------------------------------------------------------

def _funded(session: Session, f: funding_api.FundingFilter):
    return session.scalars(funding_api.ordered(funding_api.query(f)).limit(SEND_LIMIT)).all()


class PreviewOut(BaseModel):
    ready: bool
    companies: int                    # distinct companies the filters show
    new: int                          # would be sent now
    waiting: int                      # already waiting in the queue
    enriched: int                     # enriched before: not sent again
    over_cap: int                     # past the per-send cap: left for another send
    max_credits: int                  # at most this many credits for the new ones
    per_company: int
    credits_left: int | None


@router.get("/preview", response_model=PreviewOut)
def preview(f: funding_api.FundingFilter = Depends(funding_api.funding_filter),
            session: Session = Depends(get_session)) -> PreviewOut:
    p = worker.plan(session, _funded(session, f))
    ready = signalhire.ready()
    return PreviewOut(ready=ready, companies=p.companies, new=len(p.new), waiting=p.waiting,
                      enriched=p.enriched, over_cap=p.over_cap, max_credits=p.max_credits,
                      per_company=settings.signalhire_contacts_per_company,
                      credits_left=worker.credits_left() if ready else None)


class SendOut(BaseModel):
    sent: bool
    message: str
    queued: int = 0
    waiting: int = 0
    enriched: int = 0
    over_cap: int = 0
    status: EnrichmentStatus


@router.post("", response_model=SendOut)
def send(f: funding_api.FundingFilter = Depends(funding_api.funding_filter),
         session: Session = Depends(get_session)) -> SendOut:
    if not signalhire.ready():
        return SendOut(sent=False, message=NOT_READY, status=enrichment_status(session))
    rows = _funded(session, f)
    if not rows:
        return SendOut(sent=False, message="No funded startups match these filters.",
                       status=enrichment_status(session))

    p = worker.enqueue(session, rows)
    if p.new or p.waiting:
        worker.start()
    parts = []
    if p.new:
        parts.append(f"Sent {_companies(len(p.new))} for enrichment; their leaders appear "
                     "here as each is done.")
    if p.waiting:
        parts.append(f"{_companies(p.waiting)} {'was' if p.waiting == 1 else 'were'} "
                     "already waiting.")
    if p.enriched:
        parts.append(f"{_companies(p.enriched)} {'was' if p.enriched == 1 else 'were'} "
                     "enriched before and not sent again.")
    if p.over_cap:
        parts.append(f"{_companies(p.over_cap)} left out: at most "
                     f"{settings.signalhire_max_companies} are sent at a time. Send again "
                     "for the rest.")
    return SendOut(sent=True, message=" ".join(parts), queued=len(p.new), waiting=p.waiting,
                   enriched=p.enriched, over_cap=p.over_cap, status=enrichment_status(session))


class RunOut(BaseModel):
    started: bool
    message: str
    status: EnrichmentStatus


def _run(session: Session, requeued: int = 0) -> RunOut:
    current = enrichment_status(session)
    if not current.waiting:
        return RunOut(started=False, message="Nothing is waiting to be enriched.",
                      status=current)
    started = worker.start()
    message = (f"Enriching {_companies(current.waiting)}." if started
               else "Enrichment is already running; it takes every company waiting.")
    if requeued:
        message = f"Retrying {_companies(requeued)}. {message}"
    return RunOut(started=started, message=message, status=enrichment_status(session))


@router.post("/resume", response_model=RunOut)
def resume(session: Session = Depends(get_session)) -> RunOut:
    if not signalhire.ready():
        return RunOut(started=False, message=NOT_READY, status=enrichment_status(session))
    return _run(session)


@router.post("/retry", response_model=RunOut)
def retry(session: Session = Depends(get_session)) -> RunOut:
    if not signalhire.ready():
        return RunOut(started=False, message=NOT_READY, status=enrichment_status(session))
    return _run(session, worker.requeue_failed(session))


class ClearOut(BaseModel):
    cleared: bool
    companies: int                    # taken off the list
    message: str
    status: EnrichmentStatus


@router.delete("", response_model=ClearOut)
def clear(session: Session = Depends(get_session)) -> ClearOut:
    count = worker.clear()
    if count is None:
        return ClearOut(cleared=False, companies=0, status=enrichment_status(session),
                        message="Enrichment is running. Clear the list once it is done.")
    return ClearOut(cleared=True, companies=count, status=enrichment_status(session),
                    message=f"Cleared {_companies(count)} from the list. Sending one again "
                            "costs no credits for the people already found.")
