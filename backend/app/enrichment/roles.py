"""Who counts as a leader, at which company, and what SignalHire has for them.

SignalHire's search matches the title query and the company query against any
of a person's jobs, not the same one. Seen live for "Zepto": a Zepto project
associate who co-founded a construction firm, an executive assistant "to the
Co-Founder & CEO", and people at "Zepto Digital Labs". Paying to look those up
would waste credits, so every hit is checked here first: the person's newest
job *at this company* must hold a wanted designation, and the company must be
the same one, not one whose name merely starts the same.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..search.signalhire import Profile

# The wanted designations, in display order. A leader's rank is the first one
# they hold, so when a company has more leaders than the per-company cap the
# founders and the C-suite are looked up first.
DESIGNATIONS = ["Co-founder", "CEO", "CTO", "COO", "VP Engineering", "Head of Engineering",
                "Head of Product"]

# URL keys for the designation filter.
DESIGNATION_KEYS = {"cofounder": "Co-founder", "ceo": "CEO", "cto": "CTO", "coo": "COO",
                    "vp-engineering": "VP Engineering",
                    "head-engineering": "Head of Engineering",
                    "head-product": "Head of Product"}

_SEP = r"\s*(?:of\s+|[-–—,:|/]\s*)?"
_RULES: list[tuple[str, re.Pattern]] = [(name, re.compile(pattern, re.I)) for name, pattern in [
    ("Co-founder", r"\bco[\s-]?founder\b|\bfounder\b"),
    ("CEO", r"\bceo\b|\bchief\s+executive\s+officer\b"),
    ("CTO", r"\bcto\b|\bchief\s+(?:technology|technical)\s+officer\b"),
    ("COO", r"\bcoo\b|\bchief\s+operati(?:ng|ons)\s+officer\b"),
    ("VP Engineering", rf"\b(?:s?vp|vice\s+president){_SEP}engineering\b"),
    ("Head of Engineering", rf"\bhead{_SEP}engineering\b|\bengineering\s+head\b"),
    ("Head of Product",
     rf"\bhead{_SEP}products?\b(?!\s+(?:marketing|design|support|sales|operations|analytics))"
     r"|\bproduct\s+head\b"),
]]

# Titles that name a role without holding it: "Executive Assistant to the CEO",
# "Founder's Office", "Chief of Staff to the CTO", "Ex-CEO", "Assistant VP".
_NOT_THE_ROLE = re.compile(
    r"\b(?:assistant|office|intern|ex|former|formerly|chief\s+of\s+staff)\b"
    r"|\b(?:advisor|adviser|associate|consultant|reporting)\s+to\b"
    r"|\bto\s+(?:the\s+)?(?:ceo|cto|coo|md|co[\s-]?founders?|founders?)\b",
    re.I)

# The search query: every spelling the rules above accept.
TITLE_QUERY = " OR ".join([
    "CTO", '"Chief Technology Officer"', "CEO", '"Chief Executive Officer"', "COO",
    '"Chief Operating Officer"', "Founder", '"Co-founder"', "Cofounder",
    '"Head of Engineering"', '"VP Engineering"', '"VP of Engineering"',
    '"Vice President Engineering"', '"Head of Product"',
])


def designations(title: str) -> list[str]:
    """The wanted designations `title` holds, in display order; [] for none."""
    title = " ".join((title or "").split())
    if not title or _NOT_THE_ROLE.search(title):
        return []
    return [name for name, rule in _RULES if rule.search(title)]


# Words that only say what kind of company it is, dropped from the end of a
# name before comparing: "Classplus Technologies Pvt Ltd" is "Classplus".
_GENERIC = {"pvt", "private", "ltd", "limited", "inc", "llp", "llc", "corp", "corporation",
            "co", "company", "technologies", "technology", "tech", "labs", "india",
            "solutions", "software", "systems", "ai", "app", "hq"}


def company_core(name: str) -> str:
    """A company name reduced to what identifies it: case, punctuation, a
    bracketed aside, a "| tagline" and trailing generic words aside."""
    name = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", (name or "").split("|")[0])
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _GENERIC:
        words.pop()
    return " ".join(words)


def same_company(a: str, b: str) -> bool:
    """True when `a` and `b` name the same company: "Zepto (KiranaKart)" and
    "Zepto" do, "Zepto Digital Labs" and "Zepto" do not."""
    x, y = company_core(a), company_core(b)
    return bool(x) and (x == y or x.replace(" ", "") == y.replace(" ", ""))


def search_name(company: str) -> str:
    """The name to search for: the core, so every spelling of it is found."""
    return company_core(company) or " ".join(company.split())


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def page_score(company: str, page: str | None, site: str | None) -> int:
    """How well a company's LinkedIn page and website match the name `company`:
    3 exactly, 2 the whole name inside, 1 the core inside, 0 not at all.

    Seen live: for "Karta AI", getkarta.ai scores 2 and linkedin.com/company/
    kartashop 1 -- two companies SignalHire both calls "Karta".
    """
    full, core = _squash(company), _squash(company_core(company))
    slug = _squash(urlparse(page).path.rstrip("/").rsplit("/", 1)[-1]) if page else ""
    host = (urlparse(site).hostname or "") if site else ""
    host = _squash(host[4:] if host.startswith("www.") else host)
    score = 0
    for text in (slug, host):
        if not text or not core:
            continue
        if text in (full, core):
            score = max(score, 3)
        elif full in text:
            score = max(score, 2)
        elif core in text:
            score = max(score, 1)
    return score


@dataclass
class Leader:
    uid: str
    full_name: str
    title: str
    designations: list[str]
    location: str | None

    @property
    def rank(self) -> int:
        return DESIGNATIONS.index(self.designations[0])


def pick_leaders(profiles: list[Profile], company: str, limit: int) -> list[Leader]:
    """The people in `profiles` who hold a wanted designation at `company`,
    founders and the C-suite first, at most `limit` of them."""
    leaders: dict[str, Leader] = {}
    for p in profiles:
        title = next((t for c, t in p.jobs if same_company(c, company)), None)
        held = designations(title or "")
        if held and p.uid not in leaders:
            leaders[p.uid] = Leader(uid=p.uid, full_name=p.full_name, title=title or "",
                                    designations=held, location=p.location)
    ranked = sorted(leaders.values(), key=lambda leader: leader.rank)   # stable: search order
    return ranked[:max(0, limit)]


# --- What SignalHire has for a person ------------------------------------------------

@dataclass
class Details:
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    other_emails: list[str] = field(default_factory=list)
    other_phones: list[str] = field(default_factory=list)
    title: str | None = None
    location: str | None = None
    company_page: str | None = None
    company_site: str | None = None


def _rating(contact: dict) -> int:
    try:
        return int(contact.get("rating") or 0)
    except (TypeError, ValueError):
        return 0


def _values(contacts: list, kind: str, preferred: str) -> list[str]:
    """Contacts of one kind, the preferred sub-type and the most certain first."""
    found = [c for c in contacts if isinstance(c, dict) and c.get("type") == kind
             and isinstance(c.get("value"), str) and c["value"].strip()]
    found.sort(key=lambda c: (c.get("subType") != preferred, -_rating(c)))
    return list(dict.fromkeys(" ".join(c["value"].split()) for c in found))


def _web(url: object) -> str | None:
    return url.strip() if isinstance(url, str) and url.strip().startswith(
        ("https://", "http://")) else None


def details(candidate: dict, company: str) -> Details:
    """The best email (work first), phone (mobile first) and LinkedIn link in a
    SignalHire candidate; their current title at `company` when it names one,
    and that company's LinkedIn page and website."""
    contacts = candidate.get("contacts") if isinstance(candidate.get("contacts"), list) else []
    emails = _values(contacts, "email", "work")
    phones = _values(contacts, "phone", "mobile")

    social = candidate.get("social") if isinstance(candidate.get("social"), list) else []
    linkedin = next((_web(s.get("link")) for s in social
                     if isinstance(s, dict) and s.get("type") == "li" and _web(s.get("link"))),
                    None)
    if linkedin is None:
        linkedin = next((_web(c.get("value")) for c in contacts if isinstance(c, dict)
                         and c.get("type") == "link" and "linkedin.com/" in str(c.get("value"))
                         and _web(c.get("value"))), None)

    jobs = [j for j in candidate.get("experience") or [] if isinstance(j, dict)
            and isinstance(j.get("company"), str) and same_company(j["company"], company)]
    current = next((j for j in jobs if j.get("current")), None)
    title = current.get("position") if current else None
    job = current or (jobs[0] if jobs else {})
    page, site = _web(job.get("companyUrl")), _web(job.get("website"))

    locations = candidate.get("locations") if isinstance(candidate.get("locations"), list) else []
    location = next((loc["name"].strip() for loc in locations if isinstance(loc, dict)
                     and isinstance(loc.get("name"), str) and loc["name"].strip()), None)
    return Details(
        email=emails[0][:320] if emails else None,
        phone=phones[0][:64] if phones else None,
        linkedin=linkedin[:1000] if linkedin else None,
        other_emails=emails[1:], other_phones=phones[1:],
        title=title.strip()[:300] if isinstance(title, str) and title.strip() else None,
        location=location[:200] if location else None,
        company_page=page[:1000] if page else None,
        company_site=site[:1000] if site else None,
    )
