"""Pull funding-round details out of Funding stories, for Insights.

Company, round, stage, amount and investors are written inside a headline and
its description ("ONYA raises Rs 12.5 crore in a pre-Series A round led by
Divis..."). gpt-oss reads them out in batches, once per canonical Funding
story, as the last paid step of each scheduled refresh -- so Insights moves
with the feed.

Cost: one call per 20 stories, about $0.00005 a story. A story the model
answered is marked done -- even if it turns out not to be a startup round,
which lands under Other -- so nothing is paid for twice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select

from ..db import session_scope
from ..llm.bedrock import LlmUnavailable, invoke_json
from ..llm.budget import BudgetExceeded
from ..models import Article, Category, FundingRound, Stage

log = logging.getLogger(__name__)

BATCH_SIZE = 20
FEATURE = "rounds"

SYSTEM_PROMPT = (
    "You extract funding-round facts from Indian startup news. You reply with "
    "JSON only -- no prose, no markdown fences."
)

PROMPT_HEAD = """For each numbered story below, return one JSON object with keys:
  "i"          the story number
  "company"    the company that raised the money, or null
  "round"      the round exactly as the story names it ("Pre-Series A",
               "Seed", "Series B", "Bridge", "Debt", "Pre-IPO"), or null
  "stage"      EXACTLY one of: Pre-Seed, Seed, Series A, Series B, Other
  "amount"     the amount as written, with currency ("Rs 12.5 crore",
               "$4.5 Mn"), or null if undisclosed
  "investors"  array of investor names mentioned, [] if none

Stage rules:
  Pre-Seed   pre-seed and angel rounds
  Seed       seed rounds, and pre-Series A / bridge-to-A rounds
  Series A   Series A (including A1, A2, extensions of A)
  Series B   Series B (including extensions of B)
  Other      Series C and later, debt, IPO and anchor money, company or
             government investment, and anything that is not a startup round

Use only the text given. Do not guess amounts or investors.
Return a JSON array of these objects and nothing else.
"""

_STAGE_BY_KEY = {s.value.lower().replace("-", "").replace(" ", ""): s for s in Stage}
_NULLS = {"", "null", "none", "n/a", "na", "undisclosed", "unknown", "not disclosed"}


def coerce_stage(value: object, round_label: str | None = None) -> Stage:
    """The model's stage, validated; unknown values fall back to the round name."""
    for candidate in (value, round_label):
        if isinstance(candidate, str):
            key = candidate.strip().lower().replace("-", "").replace(" ", "")
            if key in _STAGE_BY_KEY:
                return _STAGE_BY_KEY[key]
            if key.startswith("preseriesa") or key in ("bridge",):
                return Stage.SEED
            if key.startswith("preseed") or key.startswith("angel"):
                return Stage.PRE_SEED
            if key.startswith("seriesa"):
                return Stage.SERIES_A
            if key.startswith("seriesb"):
                return Stage.SERIES_B
    return Stage.OTHER


def clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip(" .,;")
    if text.lower() in _NULLS:
        return None
    return text[:limit] or None


_ACRONYMS = {"ipo": "IPO", "ncd": "NCD", "vc": "VC"}


def tidy_round(label: str | None) -> str | None:
    """One spelling for round names: "pre seed" -> "Pre-Seed",
    "pre-series a" -> "Pre-Series A", "bridge" -> "Bridge".

    The model copies the round as each outlet wrote it; this makes the column
    consistent on screen and in Excel without re-reading anything.
    """
    if not label:
        return None
    text = re.sub(r"\bpre[\s-]+", "pre-", " ".join(label.lower().split()))
    words = []
    for word in text.split(" "):
        parts = []
        for part in word.split("-"):
            if part in _ACRONYMS:
                parts.append(_ACRONYMS[part])
            elif len(part) <= 2 and part.isalpha() and words and words[-1] == "Series":
                parts.append(part.upper())          # "series a1" -> "Series A1"
            else:
                parts.append(part[:1].upper() + part[1:])
        words.append("-".join(parts))
    return " ".join(words)


def clean_investors(value: object) -> str | None:
    """A list of names -> "A; B; C". Duplicates and placeholders dropped."""
    if isinstance(value, str):
        value = re.split(r"\s*(?:,|;| and )\s*", value)
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for item in value:
        name = clean_text(item, 120)
        if name and name.lower() not in (n.lower() for n in names):
            names.append(name)
    return "; ".join(names) or None


@dataclass
class RoundsResult:
    considered: int = 0
    extracted: int = 0
    batches: int = 0
    cost_usd: float = 0.0
    stopped_reason: str | None = None


@dataclass
class _Pending:
    id: int
    headline: str
    description: str | None
    company: str | None


def _load_pending(limit: int | None) -> list[_Pending]:
    """Canonical Funding stories with no extracted round yet, newest first."""
    done = select(FundingRound.article_id)
    stmt = (
        select(Article.id, Article.headline, Article.description, Article.company)
        .where(Article.canonical_id.is_(None),
               Article.category == Category.FUNDING,
               Article.id.not_in(done))
        .order_by(Article.published_at.desc().nullslast())
    )
    if limit:
        stmt = stmt.limit(limit)
    with session_scope() as s:
        return [_Pending(*row) for row in s.execute(stmt).all()]


def build_prompt(batch: list[_Pending]) -> str:
    lines = [PROMPT_HEAD]
    for n, p in enumerate(batch, start=1):
        body = f"[{n}] {p.headline}"
        if p.description:
            body += f"\n    {p.description[:300]}"
        lines.append(body)
    return "\n".join(lines)


def _apply(entries: list, by_number: dict[int, _Pending]) -> int:
    """Write one batch in a short transaction.

    Only stories the model actually answered are marked done; one it skipped
    stays pending and is retried on the next refresh rather than being
    stored empty forever.
    """
    got: dict[int, dict] = {}
    for entry in entries:
        if isinstance(entry, dict):
            try:
                got[int(entry.get("i"))] = entry
            except (TypeError, ValueError):
                continue

    written = 0
    with session_scope() as s:
        for number, pending in by_number.items():
            entry = got.get(number)
            if entry is None:
                continue
            round_label = tidy_round(clean_text(entry.get("round"), 80))
            s.add(FundingRound(
                article_id=pending.id,
                company=clean_text(entry.get("company"), 200) or pending.company,
                stage=coerce_stage(entry.get("stage"), round_label),
                round_label=round_label,
                amount=clean_text(entry.get("amount"), 120),
                investors=clean_investors(entry.get("investors")),
            ))
            written += 1
    return written


def extract_pending(limit: int | None = None, batch_size: int = BATCH_SIZE) -> RoundsResult:
    """Extract rounds for every Funding story not yet done. Stops cleanly on budget."""
    result = RoundsResult()
    pending = _load_pending(limit)
    result.considered = len(pending)
    if not pending:
        log.info("rounds: nothing pending")
        return result

    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        by_number = dict(enumerate(batch, start=1))
        try:
            parsed, call = invoke_json(FEATURE, build_prompt(batch),
                                       system=SYSTEM_PROMPT, max_tokens=4096)
        except BudgetExceeded as exc:
            log.warning("rounds: %s", exc)
            result.stopped_reason = str(exc)
            break
        except (LlmUnavailable, ValueError) as exc:
            log.error("rounds: batch failed: %s", exc)
            result.stopped_reason = str(exc)
            break

        result.batches += 1
        result.cost_usd += call.cost_usd
        if not isinstance(parsed, list):
            log.warning("rounds: expected a JSON array, got %s", type(parsed).__name__)
            continue
        result.extracted += _apply(parsed, by_number)

    log.info("rounds: %d/%d stories in %d batch(es), $%.6f",
             result.extracted, result.considered, result.batches, result.cost_usd)
    return result
