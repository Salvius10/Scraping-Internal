"""Cross-source deduplication.

The same funding round gets written up by five outlets within an hour. Without
this the feed shows the same story five times and looks broken on day one.

Approach: normalise the headline to a token set, then compare against articles
already stored in a recent time window, scoring with both Jaccard and
containment. The first
article seen for a story stays canonical; later ones point at it via
`canonical_id` and are hidden from the default feed.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Article, utcnow

# Headline furniture that carries no identifying signal.
STOPWORDS = frozenset("""
a an the and or of in on at to for from with by as is are was were be been
its it this that these those new now after over into amid ahead
said says report reports reportedly exclusive update updates breaking
crore lakh million billion mn bn cr rs inr usd
startup startups company firm platform based india indian
""".split())

# Funding-round boilerplate, stripped for the same reason as the words above.
# Nearly every article here contains some of it, so leaving it in lets two
# unrelated roundups -- "Creedom, Guickly raise early-stage funding" and
# "Yuma Energy, Kepler Aerospace, DocPharma, others raise early-stage funding"
# -- score 0.67 on shared jargon while sharing no actual subject. Removing it
# forces the match onto the company names, which are what identify a story.
STOPWORDS |= frozenset("""
raise raises raised raising round rounds funding funds fund
secure secures secured bags nets gets wins
early stage seed pre series capital
investment investments invest invests investing
""".split())

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalise_tokens(headline: str) -> frozenset[str]:
    """Lowercase, strip punctuation, drop stopwords and 1-2 char noise.

    Short *numeric* tokens are kept: "Sept 22" vs "Sept 10" is the only thing
    separating two editions of a recurring daily column, and dropping them
    merges the whole series into one story.
    """
    words = _WORD_RE.findall((headline or "").lower())
    return frozenset(
        w for w in words
        if (len(w) > 2 or w.isdigit()) and w not in STOPWORDS
    )


def numeric_signature(headline: str) -> frozenset[str]:
    """Every number in a headline: dates, amounts, percentages, round sizes."""
    return frozenset(w for w in _WORD_RE.findall((headline or "").lower())
                     if w.isdigit())


def story_key(headline: str) -> str:
    """Stable key from the most distinctive tokens.

    Sorted so word order does not matter -- "Spinny files IPO papers" and
    "IPO papers filed by Spinny" collapse to the same key.
    """
    tokens = sorted(normalise_tokens(headline))[:8]
    if not tokens:
        tokens = [(headline or "").strip().lower()[:50]]
    return hashlib.sha1(" ".join(tokens).encode("utf-8")).hexdigest()


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: frozenset[str], b: frozenset[str]) -> float:
    """Overlap relative to the *shorter* headline.

    Outlets cover the same event at different lengths -- "Pune-based fintech
    and brokerage startup Definedge raises Rs 22 crore in funding" against
    "Fintech and brokerage startup Definedge raises Rs 22 Cr in pre-Series A".
    Jaccard punishes the extra context words and scores that pair 0.56, under
    any sane threshold. Containment scores it 0.71, because one headline is
    substantially contained in the other.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# Below this many tokens, containment saturates on trivial overlaps, so short
# headlines are judged on Jaccard alone.
MIN_TOKENS_FOR_CONTAINMENT = 4

# Headlines that bundle several unrelated stories into one article.
_ROUNDUP_MARKER = re.compile(
    r"&\s*more|and more|round-?up|this week|weekly|digest|highlights"
    r"|top stories|ecosystem (pulse|buzz)",
    re.I,
)
_CLAUSE_SPLIT = re.compile(r"[;|]")

# How much of a clause must overlap the other headline before we accept that
# both headlines are about the same subject.
_CLAUSE_OVERLAP_FLOOR = 0.25


def is_multi_topic(longer: str, other: str) -> bool:
    """True when `longer` covers a topic `other` says nothing about.

    Containment alone cannot tell a rewrite from a digest: an individual story
    is genuinely "contained" in a roundup that mentions it, so
    "Indian Startup IPO Sprint, Zetwerk-Ayr Settle Dispute & More" would
    swallow "Zetwerk, Ayr Energy Settle Legal Dispute" and hide the real
    article behind the summary.

    Two signals, both needed in practice:
      - an explicit roundup marker ("& More", "this week", "Weekly Digest")
      - a headline split across clauses where at least one clause shares
        almost nothing with the other headline. That distinguishes
        "Succession test at Hikal; Nothing spins off CMF" (two subjects) from
        "Moneyview sets IPO price band at Rs 32-34; offer size ..." (one).
    """
    if _ROUNDUP_MARKER.search(longer):
        return True

    clauses = [c for c in _CLAUSE_SPLIT.split(longer)
               if len(normalise_tokens(c)) >= 3]
    if len(clauses) < 2:
        return False

    other_tokens = normalise_tokens(other)
    return any(
        containment(normalise_tokens(c), other_tokens) < _CLAUSE_OVERLAP_FLOOR
        for c in clauses
    )


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Set-only score: best of Jaccard and length-gated containment."""
    score = jaccard(a, b)
    if min(len(a), len(b)) >= MIN_TOKENS_FOR_CONTAINMENT:
        score = max(score, containment(a, b))
    return score


def headline_similarity(a: str, b: str) -> float:
    """Full comparison, including the multi-topic guard.

    This is what callers should use; `similarity` is the pure set measure.
    """
    ta, tb = normalise_tokens(a), normalise_tokens(b)
    longer, other = (a, b) if len(ta) >= len(tb) else (b, a)
    if is_multi_topic(longer, other):
        return jaccard(ta, tb)  # containment is untrustworthy here
    return similarity(ta, tb)


def url_hash(url: str) -> str:
    return hashlib.sha1((url or "").strip().lower().encode("utf-8")).hexdigest()


def find_canonical(
    session: Session,
    headline: str,
    published_at=None,
    window_hours: int | None = None,
    threshold: float | None = None,
) -> Article | None:
    """Return an existing canonical Article telling the same story, if any.

    Scans only a recent window, so this stays cheap as the corpus grows.
    """
    window_hours = window_hours or settings.dedupe_window_hours
    threshold = threshold if threshold is not None else settings.dedupe_threshold

    anchor = published_at or utcnow()
    if anchor.tzinfo is None:
        from datetime import timezone
        anchor = anchor.replace(tzinfo=timezone.utc)
    since = anchor - timedelta(hours=window_hours)
    until = anchor + timedelta(hours=window_hours)

    tokens = normalise_tokens(headline)
    if not tokens:
        return None

    key = story_key(headline)
    numbers = numeric_signature(headline)

    # Exact key match first -- cheap and catches near-identical rewrites.
    exact = session.scalars(
        select(Article)
        .where(Article.story_key == key, Article.canonical_id.is_(None))
        .limit(1)
    ).first()
    if exact is not None:
        return exact

    candidates = session.scalars(
        select(Article).where(
            Article.canonical_id.is_(None),
            Article.published_at.is_not(None),
            Article.published_at >= since,
            Article.published_at <= until,
        )
    ).all()

    best, best_score = None, 0.0
    for cand in candidates:
        # Two headlines reporting the same event agree on its numbers -- the
        # round size, the stake, the date. Differing numbers mean different
        # stories however similar the wording, which is what stops a recurring
        # column ("Ecosystem Pulse - Sept 22" vs "- Sept 10") self-merging.
        if numbers != numeric_signature(cand.headline):
            continue
        score = headline_similarity(headline, cand.headline)
        if score > best_score:
            best, best_score = cand, score

    return best if best_score >= threshold else None
