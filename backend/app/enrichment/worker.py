"""The enrichment queue and the background job that works through it.

  plan(session, rows)     what sending these funded rows would queue; writes nothing
  enqueue(session, rows)  queue them; a company already enriched is not sent again
  start()                 start the job unless one is running
  requeue_failed(session) put every failed company back in the queue
  clear()                 take every company off the list ("Clear list")
  state()                 the job, for the page to follow

The queue is the `enrichment_companies` table, so a job stopped by a restart,
spent credits or a rate limit resumes where it stopped -- by hand ("Resume"),
never on its own, since every run costs credits. One job runs at a time across
every process (`proclock.enrichment_lock`), and a company is claimed before it
is worked (queued -> working, in one UPDATE), so nothing is paid for twice.
For each company:

  1. search SignalHire for its leaders                  free (a daily quota)
  2. keep those holding a wanted designation there      roles.pick_leaders
  3. reuse anyone looked up before; look up the rest    credits, per person found
  4. store the leaders and the company's outcome        one transaction
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..cache import TTLCache
from ..config import settings
from ..db import session_scope
from ..models import EnrichedContact, EnrichmentCompany, EnrichStatus, FundedStartup, utcnow
from ..proclock import ProcessLock, enrichment_lock
from ..search import signalhire
from ..search.signalhire import CREDITS_ARE_OVER, SUCCESS, Lookup, SignalHireError
from . import roles

log = logging.getLogger(__name__)

# A company on the list: not cleared. A cleared one is as good as never sent,
# except that the people already looked up for it are reused, not paid again.
LISTED = EnrichmentCompany.cleared_at.is_(None)

# Credits left are asked for at most once a minute, and briefly: the page waits on it.
CREDITS_TTL = 60
CREDITS_TIMEOUT = 8.0


# --- Sending companies -------------------------------------------------------------------

@dataclass
class Plan:
    """What sending a set of funded rows does."""

    companies: int                                  # distinct companies in the rows
    new: list[FundedStartup] = field(default_factory=list)   # queued now (within the cap)
    waiting: int = 0                                # already queued or being worked
    enriched: int = 0                               # enriched before: not sent again
    over_cap: int = 0                               # past signalhire_max_companies

    @property
    def max_credits(self) -> int:
        return len(self.new) * max(0, settings.signalhire_contacts_per_company)


def plan(session: Session, rows: list[FundedStartup]) -> Plan:
    """`rows` come newest first, so a company's newest round is the one sent."""
    newest: dict[str, FundedStartup] = {}
    for row in rows:
        newest.setdefault(row.company_key, row)
    known = dict(session.execute(
        select(EnrichmentCompany.company_key, EnrichmentCompany.status).where(LISTED)).all())

    result = Plan(companies=len(newest))
    fresh: list[FundedStartup] = []
    for key, row in newest.items():
        status = known.get(key)
        if status in (EnrichStatus.QUEUED, EnrichStatus.WORKING):
            result.waiting += 1
        elif status in (EnrichStatus.DONE, EnrichStatus.NO_MATCH):
            result.enriched += 1
        else:                                       # never sent, or failed: (re)queue
            fresh.append(row)
    cap = max(0, settings.signalhire_max_companies)
    result.new, result.over_cap = fresh[:cap], max(0, len(fresh) - cap)
    return result


def enqueue(session: Session, rows: list[FundedStartup]) -> Plan:
    """Queue the companies in `rows` that are not queued or enriched already."""
    result = plan(session, rows)
    keys = [row.company_key for row in result.new]
    # Sent before and failed, or cleared from the list: queued again in the same row.
    sent_before = {c.company_key: c for c in session.scalars(select(EnrichmentCompany).where(
        EnrichmentCompany.company_key.in_(keys)))} if keys else {}
    now = utcnow()
    for row in result.new:
        target = sent_before.get(row.company_key)
        if target is None:
            target = EnrichmentCompany(company_key=row.company_key)
        elif target.cleared_at is not None:
            target.cleared_at, target.people_found, target.credits_used = None, 0, 0
        target.company, target.funding_id = row.company, row.id
        target.stage, target.amount = row.stage.value, row.amount
        target.sector, target.hq_city = row.sector, row.hq_city
        target.status, target.note = EnrichStatus.QUEUED, None
        target.requested_at, target.claimed_at = now, None
        session.add(target)
    try:
        session.commit()
    except IntegrityError:
        # Another request queued the same company a moment ago: it is waiting.
        session.rollback()
        return plan(session, rows)
    return result


def requeue_failed(session: Session) -> int:
    count = session.execute(update(EnrichmentCompany)
                            .where(EnrichmentCompany.status == EnrichStatus.FAILED, LISTED)
                            .values(status=EnrichStatus.QUEUED, note=None,
                                    claimed_at=None)).rowcount
    session.commit()
    return count or 0


def clear() -> int | None:
    """Take every company, and so its leaders, off the list. None while a job
    runs: it would go on writing to the list being cleared. The people already
    looked up stay in the database, so sending a company again reuses them."""
    with _guard:                    # start() takes it too: no job starts meanwhile
        if _state.running:
            return None
        with session_scope() as s:
            count = s.execute(update(EnrichmentCompany).where(LISTED)
                              .values(cleared_at=utcnow())).rowcount
        # The last job's outcome was about the companies just cleared.
        _state.message = _state.error = None
    log.info("enrichment: %d companies cleared from the list", count or 0)
    return count or 0


# --- The job -------------------------------------------------------------------------

@dataclass
class JobState:
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current: str | None = None          # the company being enriched now
    worked: int = 0                     # companies this job finished
    people: int = 0                     # leaders this job found
    credits_used: int = 0               # by this job
    credits_left: int | None = None     # as SignalHire last reported it
    message: str | None = None          # how the last job went
    error: str | None = None            # why it stopped early


_state = JobState()
_guard = threading.Lock()
_held: ProcessLock | None = None    # the cross-process lock, while a job holds it
_credits: TTLCache[int] = TTLCache(max_entries=2)     # "left", and "failed" after an error


def state() -> JobState:
    with _guard:
        return JobState(**_state.__dict__)


def _note_credits(left: int | None) -> None:
    if left is None:
        return
    _credits.set("left", left, CREDITS_TTL)
    with _guard:
        _state.credits_left = left


def credits_left() -> int | None:
    """Credits left, at most a minute old. None when SignalHire cannot say."""
    if not signalhire.ready():
        return None
    hit = _credits.get("left")
    if hit is not None:
        return hit
    if _credits.get("failed") is None:
        try:
            _note_credits(signalhire.credits(timeout=CREDITS_TIMEOUT))
        except Exception as exc:  # noqa: BLE001 - the page shows the last count instead
            log.warning("enrichment: could not read SignalHire credits: %s", exc)
            _credits.set("failed", 1, CREDITS_TTL)
    return state().credits_left


def _release_lock() -> None:
    """Drop the cross-process lock. Call with `_guard` held."""
    global _held
    lock, _held = _held, None
    if lock is not None:
        lock.release()


def start() -> bool:
    """Start the job in the background. False when one is running already --
    here, it takes every queued company, including any queued after it started;
    or in another process using the same database."""
    global _held
    with _guard:
        if _state.running:
            return False
        lock = enrichment_lock()
        try:
            got = lock.acquire()
        except OSError:
            log.exception("enrichment: could not open %s", lock.path)
            got = False
        if not got:
            return False
        _held = lock
        _state.running, _state.started_at, _state.finished_at = True, utcnow(), None
        _state.current, _state.message, _state.error = None, None, None
        _state.worked = _state.people = _state.credits_used = 0
    try:
        threading.Thread(target=_work, daemon=True, name="enrichment").start()
    except Exception:
        # No thread means nothing will release the lock; do it here.
        with _guard:
            _state.running = False
            _release_lock()
        raise
    log.info("enrichment: job started")
    return True


@dataclass
class _Claimed:
    id: int
    company: str


def _requeue_abandoned() -> None:
    """Companies left mid-work by a job that died. This job holds the lock every
    job takes, so no other job can be working on them."""
    with session_scope() as s:
        s.execute(update(EnrichmentCompany)
                  .where(EnrichmentCompany.status == EnrichStatus.WORKING)
                  .values(status=EnrichStatus.QUEUED, claimed_at=None))


def _claim_next() -> _Claimed | None:
    """Take the oldest queued company, unless another job takes it first."""
    with session_scope() as s:
        while True:
            row = s.execute(select(EnrichmentCompany.id, EnrichmentCompany.company)
                            .where(EnrichmentCompany.status == EnrichStatus.QUEUED, LISTED)
                            .order_by(EnrichmentCompany.requested_at, EnrichmentCompany.id)
                            .limit(1)).first()
            if row is None:
                return None
            taken = s.execute(update(EnrichmentCompany).where(
                EnrichmentCompany.id == row.id,
                EnrichmentCompany.status == EnrichStatus.QUEUED,
            ).values(status=EnrichStatus.WORKING, claimed_at=utcnow())).rowcount
            if taken:
                return _Claimed(id=row.id, company=row.company)


def _set_status(company_id: int, status: EnrichStatus, note: str | None) -> None:
    with session_scope() as s:
        company = s.get(EnrichmentCompany, company_id)
        if company is not None:
            company.status, company.note, company.claimed_at = status, note, None
            if status != EnrichStatus.QUEUED:
                company.enriched_at = utcnow()


def _finish(error: str | None) -> None:
    """Close the job and say how it went. Call with `_guard` held."""
    _release_lock()
    s = _state
    s.running, s.current, s.finished_at, s.error = False, None, utcnow(), error
    done = (f"{s.worked} {'company' if s.worked == 1 else 'companies'} enriched, "
            f"{s.people} {'leader' if s.people == 1 else 'leaders'} found")
    spent = (f", {s.credits_used} SignalHire {'credit' if s.credits_used == 1 else 'credits'}"
             " used" if s.credits_used else "")
    left = f" ({s.credits_left} left)" if s.credits_left is not None else ""
    s.message = (f"Enrichment stopped: {error} So far {done}{spent}{left}." if error
                 else f"Enrichment finished: {done}{spent}{left}.")
    log.info("enrichment: %s", s.message)


def _refresh_credits() -> None:
    """Read the balance once a job is done. A lookup's X-Credits-Left was seen
    live to give the balance from before that lookup's charge."""
    try:
        _note_credits(signalhire.credits(timeout=CREDITS_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 - an unread balance must not keep the job open
        log.warning("enrichment: could not read SignalHire credits: %s", exc)


def _work() -> None:
    error: str | None = None
    refreshed = False
    try:
        _requeue_abandoned()
        while True:
            with _guard:
                claimed = _claim_next()
                if claimed is None and refreshed:
                    # Checked under the lock `start()` takes, so a company queued
                    # now is either claimed here or starts a new job.
                    _finish(None)
                    return
                if claimed is not None:
                    _state.current = claimed.company
            if claimed is None:
                _refresh_credits()
                refreshed = True
                continue
            refreshed = False
            _enrich(claimed)
    except SignalHireError as exc:              # only the ones that stop the job get here
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - reported on the page, not raised
        log.exception("enrichment job failed")
        error = f"The enrichment job failed ({type(exc).__name__}: {exc})."[:300]
    _refresh_credits()
    with _guard:
        _finish(error)


# --- One company -------------------------------------------------------------------------

def _known(uids: list[str]) -> dict[str, roles.Details]:
    """People looked up successfully before, for any company, cleared from the
    list or not: never paid for twice."""
    found: dict[str, roles.Details] = {}
    with session_scope() as s:
        for row in s.scalars(select(EnrichedContact).where(
                EnrichedContact.uid.in_(uids), EnrichedContact.lookup == SUCCESS)
                .order_by(EnrichedContact.looked_up_at.desc())):
            found.setdefault(row.uid, roles.Details(
                email=row.email, phone=row.phone, linkedin=row.linkedin,
                other_emails=list(row.other_emails or []),
                other_phones=list(row.other_phones or []), location=row.location,
                company_page=row.company_page, company_site=row.company_site))
    return found


def _enrich(claimed: _Claimed) -> None:
    """Search, look up and store one company. Raises SignalHireError only when
    the job must stop; the company then goes back in the queue."""
    try:
        found = signalhire.search_people(
            company=roles.search_name(claimed.company), title=roles.TITLE_QUERY,
            location=settings.signalhire_location.strip() or None,
            size=settings.signalhire_search_size)
        _note_credits(found.credits_left)
        leaders = roles.pick_leaders(found.profiles, claimed.company,
                                     settings.signalhire_contacts_per_company)
        known = _known([leader.uid for leader in leaders]) if leaders else {}
        fresh = [leader.uid for leader in leaders if leader.uid not in known]
        answer = signalhire.lookup(fresh) if fresh else Lookup(results=[])
        _note_credits(answer.credits_left)
    except SignalHireError as exc:
        if exc.stop:
            _set_status(claimed.id, EnrichStatus.QUEUED, f"Waiting: {exc}")
            raise
        _set_status(claimed.id, EnrichStatus.FAILED, str(exc))
        with _guard:
            _state.worked += 1
        return
    except Exception as exc:
        # A fault here would repeat for every company, so fail this one and stop.
        _set_status(claimed.id, EnrichStatus.FAILED,
                    f"Could not be enriched ({type(exc).__name__}).")
        raise

    results = {str(r.get("item")): r for r in answer.results}
    # SignalHire charges each person found with contacts: seen live, 5 found, one
    # of them with none, cost 4. Not the drop in the balance: the account may be
    # shared, and others' lookups would be counted as ours.
    used = sum(1 for r in answer.results if r.get("status") == SUCCESS
               and isinstance(r.get("candidate"), dict) and r["candidate"].get("contacts"))
    out_of_credits = any(r.get("status") == CREDITS_ARE_OVER for r in answer.results)

    status, note, reachable = _store(claimed, leaders, known, results, used, found.total,
                                     out_of_credits)
    with _guard:
        _state.credits_used += used
        if status != EnrichStatus.QUEUED:
            _state.worked += 1
            _state.people += len(leaders)
    log.info("enrichment: %s -> %s, %d leaders, %d reachable, %d credits",
             claimed.company, status.value, len(leaders), reachable, used)
    if out_of_credits:
        raise SignalHireError("SignalHire credits ran out. Top up at signalhire.com, then "
                              "resume.", stop=True)


def _mark_other_companies(company: str, rows: list[EnrichedContact]) -> str:
    """When the people found work at more than one company page, mark those not
    at the page that matches the name. Returns a sentence for the company's note."""
    pages: dict[str, str | None] = {}
    for row in rows:
        row.maybe_other = False
        if row.company_page:
            pages[row.company_page] = pages.get(row.company_page) or row.company_site
    if len(pages) < 2:
        return ""
    scores = {page: roles.page_score(company, page, site) for page, site in pages.items()}
    best = max(scores.values())
    winners = [page for page, score in scores.items() if score == best]
    if best == 0 or len(winners) > 1:
        return (" SignalHire lists more than one company by this name: check each person's "
                "company page.")
    marked = [row for row in rows if row.company_page and row.company_page != winners[0]]
    for row in marked:
        row.maybe_other = True
    return (f" {len(marked)} {'person seems' if len(marked) == 1 else 'people seem'} to be at "
            "another company with the same name, and marked so.")


def _store(claimed: _Claimed, leaders: list[roles.Leader], known: dict[str, roles.Details],
           results: dict[str, dict], used: int, searched: int,
           out_of_credits: bool) -> tuple[EnrichStatus, str, int]:
    """Write the leaders and the company's outcome in one transaction."""
    retry = 0
    reachable = 0
    with session_scope() as s:
        company = s.get(EnrichmentCompany, claimed.id)
        existing = {c.uid: c for c in s.scalars(select(EnrichedContact).where(
            EnrichedContact.company_id == claimed.id))}
        everyone = dict(existing)
        for leader in leaders:
            if leader.uid in known:
                answer, info = SUCCESS, known[leader.uid]
            else:
                result = results.get(leader.uid) or {}
                answer = str(result.get("status") or "missing")[:30]
                candidate = result.get("candidate")
                info = (roles.details(candidate, claimed.company)
                        if answer == SUCCESS and isinstance(candidate, dict) else roles.Details())
            if answer not in (SUCCESS, "failed"):
                retry += 1                      # timed out, a duplicate, no credits: try again

            title, held = leader.title, leader.designations
            if info.title and roles.designations(info.title):
                title, held = info.title, roles.designations(info.title)

            row = existing.get(leader.uid)
            if row is None:
                row = EnrichedContact(company_id=claimed.id, uid=leader.uid)
                s.add(row)
                everyone[leader.uid] = row
            row.full_name, row.title = leader.full_name, title[:300] or None
            row.designation = ", ".join(held)
            row.rank = roles.DESIGNATIONS.index(held[0])
            row.location = info.location or leader.location
            if answer == SUCCESS or row.lookup != SUCCESS:
                # An earlier success is never overwritten by a failed retry.
                row.email, row.phone, row.linkedin = info.email, info.phone, info.linkedin
                row.other_emails, row.other_phones = info.other_emails, info.other_phones
                row.company_page, row.company_site = info.company_page, info.company_site
                row.lookup, row.looked_up_at = answer, utcnow()
            if row.email or row.phone:
                reachable += 1
        same_name = _mark_other_companies(claimed.company, list(everyone.values()))

        if not leaders:
            status = EnrichStatus.NO_MATCH
            where = f" in {settings.signalhire_location}" if settings.signalhire_location else ""
            note = (f"SignalHire found no leaders of this company{where}." if not searched
                    else f"None of the {searched} profiles SignalHire matched holds a wanted "
                         "designation at this company.")
        elif out_of_credits:
            status, note = EnrichStatus.QUEUED, "Waiting: SignalHire credits ran out."
        elif retry:
            status = EnrichStatus.FAILED
            note = (f"{retry} of {len(leaders)} people could not be looked up this time. "
                    "Retry to finish.")
        else:
            status = EnrichStatus.DONE
            note = (f"{len(leaders)} {'leader' if len(leaders) == 1 else 'leaders'}, "
                    f"{reachable} with an email or phone.{same_name}")
        company.status, company.note, company.claimed_at = status, note, None
        company.people_found = len(leaders)
        company.credits_used = (company.credits_used or 0) + used
        if status != EnrichStatus.QUEUED:
            company.enriched_at = utcnow()
    return status, note, reachable
