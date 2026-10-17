"""Guardrails #1, #2 and #3 -- Amazon Bedrock Guardrails.

  #1  raw     raw scraped content, right after the sources      source=INPUT
  #2  input   exactly the text about to be sent to the LLM      source=INPUT
  #3  output  the rows the LLM produced, before storage          source=OUTPUT

Each stage calls Bedrock's ApplyGuardrail with one configured guardrail
(GUARDRAIL_ID / GUARDRAIL_VERSION). An item is blocked when the guardrail
intervenes with a BLOCKED action -- a harmful-content filter, a denied topic,
a prompt attack hidden in scraped text, a blocked word or PII entity. An
intervention that only masks PII is not a block. Every block goes to the
blocked / rejected log.

Off until switched on (GUARDRAILS_ENABLED=true and a GUARDRAIL_ID): every stage
then passes every item through unchecked, and the status page says so.

Spend: guardrails bill per text unit (up to 1,000 characters) per policy, so
each check is priced from the units Bedrock reports, cap-checked beforehand
and ledgered like a model call. A verdict is stored per stage and text, so
the same text is never paid for twice.

A guardrail that cannot be reached never lets an item through: the item is
held and checked again on the next run (fail closed).

    python -m app.pipeline.guardrails --create   # make the guardrail, print its id
    python -m app.pipeline.guardrails --status   # what is configured
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db import session_scope
from ..llm.bedrock import LlmUnavailable, apply_guardrail, control_client
from ..llm.budget import BudgetExceeded
from ..models import GuardrailVerdict
from .items import sha256

log = logging.getLogger(__name__)

RAW, INPUT, OUTPUT = "raw", "input", "output"
_SOURCE = {RAW: "INPUT", INPUT: "INPUT", OUTPUT: "OUTPUT"}
TEXT_UNIT = 1000

T = TypeVar("T")


def enabled() -> bool:
    return settings.guardrails_enabled and bool(settings.guardrail_id)


def state() -> str:
    """One line for the status page."""
    if enabled():
        return f"on ({settings.guardrail_id} v{settings.guardrail_version})"
    if settings.guardrails_enabled:
        return "misconfigured: GUARDRAILS_ENABLED is set but GUARDRAIL_ID is empty -- items are held"
    return "off -- items pass unchecked"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str | None = None
    checked: bool = True          # False when guardrails are off
    cost_usd: float = 0.0


class GuardrailUnavailable(RuntimeError):
    """The check could not be made. The item must be held, never passed."""


# --- Pricing ---------------------------------------------------------------------------

def cost_of_usage(usage: dict) -> float:
    """USD for one ApplyGuardrail response, from the units it reports."""
    def units(name: str) -> int:
        value = usage.get(name)
        return int(value) if isinstance(value, (int, float)) and value > 0 else 0

    return (units("contentPolicyUnits") * settings.guardrail_price_content
            + units("topicPolicyUnits") * settings.guardrail_price_topic
            + units("sensitiveInformationPolicyUnits") * settings.guardrail_price_pii
            + units("contextualGroundingPolicyUnits") * settings.guardrail_price_grounding
            ) / 1000


def estimate(text: str) -> float:
    """Pessimistic pre-call cost: every priced policy reads every unit."""
    units = max(1, math.ceil(len(text) / TEXT_UNIT))
    per_unit = (settings.guardrail_price_content + settings.guardrail_price_topic
                + settings.guardrail_price_pii + settings.guardrail_price_grounding) / 1000
    return units * per_unit


# --- Reading an answer -----------------------------------------------------------------

def blocked_reasons(assessments: list) -> tuple[list[str], bool]:
    """(reasons for every BLOCKED finding, whether anything was only masked)."""
    reasons: list[str] = []
    masked = False
    for assessment in assessments or []:
        if not isinstance(assessment, dict):
            continue
        for topic in (assessment.get("topicPolicy") or {}).get("topics") or []:
            if topic.get("action") == "BLOCKED":
                reasons.append(f"denied topic: {topic.get('name')}")
        for found in (assessment.get("contentPolicy") or {}).get("filters") or []:
            if found.get("action") == "BLOCKED":
                reasons.append(f"content filter: {found.get('type')} "
                               f"({found.get('confidence', '?')})")
        words = assessment.get("wordPolicy") or {}
        for found in (words.get("customWords") or []) + (words.get("managedWordLists") or []):
            if found.get("action") == "BLOCKED":
                reasons.append(f"blocked word: {found.get('match')}")
        pii = assessment.get("sensitiveInformationPolicy") or {}
        for found in (pii.get("piiEntities") or []) + (pii.get("regexes") or []):
            if found.get("action") == "BLOCKED":
                reasons.append(f"sensitive information: {found.get('type') or found.get('name')}")
            elif found.get("action") == "ANONYMIZED":
                masked = True
        grounding = assessment.get("contextualGroundingPolicy") or {}
        for found in grounding.get("filters") or []:
            if found.get("action") == "BLOCKED":
                reasons.append(f"grounding: {found.get('type')}")
    return reasons, masked


def _decide(action: str, assessments: list) -> tuple[bool, str | None]:
    if action != "GUARDRAIL_INTERVENED":
        return True, None
    reasons, masked = blocked_reasons(assessments)
    if reasons:
        return False, "; ".join(dict.fromkeys(reasons))[:500]
    if masked:
        return True, None           # PII masked only: the item is still usable
    # Intervened without a finding we can name: treat it as a block.
    return False, "guardrail intervened"


# --- Checking --------------------------------------------------------------------------

def _cached(stage: str, text_hash: str) -> Verdict | None:
    with session_scope() as s:
        row = s.scalars(select(GuardrailVerdict).where(
            GuardrailVerdict.stage == stage, GuardrailVerdict.text_hash == text_hash)).first()
        return None if row is None else Verdict(allowed=row.allowed, reason=row.reason)


def _remember(stage: str, text_hash: str, verdict: Verdict) -> None:
    try:
        with session_scope() as s:
            s.add(GuardrailVerdict(stage=stage, text_hash=text_hash, allowed=verdict.allowed,
                                   reason=verdict.reason))
    except IntegrityError:
        pass                         # the same text was checked at the same moment


def check(stage: str, text: str) -> Verdict:
    """Screen one text at one stage. Raises GuardrailUnavailable when the
    check cannot be made -- the caller holds the item."""
    if stage not in _SOURCE:
        raise ValueError(f"unknown guardrail stage {stage!r}")
    if not settings.guardrails_enabled:
        return Verdict(allowed=True, checked=False)
    if not settings.guardrail_id:
        raise GuardrailUnavailable("GUARDRAILS_ENABLED is set but GUARDRAIL_ID is empty.")
    text = text.strip()
    if not text:
        return Verdict(allowed=True)

    text_hash = sha256(f"{settings.guardrail_id}:{settings.guardrail_version}\n{text}")
    hit = _cached(stage, text_hash)
    if hit is not None:
        return hit
    try:
        result = apply_guardrail(f"guardrail_{stage}", text, source=_SOURCE[stage],
                                 estimated_usd=estimate(text), cost_of_usage=cost_of_usage)
    except (BudgetExceeded, LlmUnavailable) as exc:
        raise GuardrailUnavailable(str(exc)) from exc
    allowed, reason = _decide(result.action, result.assessments)
    verdict = Verdict(allowed=allowed, reason=reason, cost_usd=result.cost_usd)
    _remember(stage, text_hash, verdict)
    return verdict


@dataclass
class Screened(Generic[T]):
    passed: list[T] = field(default_factory=list)
    blocked: list[tuple[T, str]] = field(default_factory=list)
    held: list[tuple[T, str]] = field(default_factory=list)    # could not be checked
    cost_usd: float = 0.0


def screen(stage: str, things: Sequence[T], text_of: Callable[[T], str]) -> Screened[T]:
    """Screen many things at one stage, a few checks at a time. Order is kept."""
    out: Screened[T] = Screened()
    if not things:
        return out
    if not settings.guardrails_enabled:
        out.passed = list(things)
        return out

    def one(thing: T):
        try:
            return thing, check(stage, text_of(thing))
        except GuardrailUnavailable as exc:
            return thing, exc

    with ThreadPoolExecutor(max_workers=max(1, settings.guardrail_workers)) as pool:
        for thing, outcome in pool.map(one, things):
            if isinstance(outcome, GuardrailUnavailable):
                out.held.append((thing, str(outcome)))
            elif outcome.allowed:
                out.passed.append(thing)
                out.cost_usd += outcome.cost_usd
            else:
                out.blocked.append((thing, outcome.reason or "blocked"))
                out.cost_usd += outcome.cost_usd
    if out.held:
        log.warning("guardrail %s: %d held, could not be checked: %s",
                    stage, len(out.held), out.held[0][1])
    return out


# --- Setup ----------------------------------------------------------------------------

GUARDRAIL_NAME = "gps-pipeline"

# Filters for startup news, posts and event listings. PROMPT_ATTACK catches
# instructions planted in scraped text; it applies to input only, so its
# output strength must be NONE.
CONTENT_FILTERS = [
    {"type": "SEXUAL", "inputStrength": "HIGH", "outputStrength": "HIGH"},
    {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
    {"type": "VIOLENCE", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
    {"type": "INSULTS", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
    {"type": "MISCONDUCT", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
    {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
]


def create_guardrail() -> tuple[str, str]:
    """Create the guardrail and publish version 1. Returns (id, version).
    Creating one costs nothing; checks against it are billed."""
    client = control_client()
    try:
        made = client.create_guardrail(
            name=GUARDRAIL_NAME,
            description="Screens scraped startup content, LLM input and LLM output for GPS.",
            contentPolicyConfig={"filtersConfig": CONTENT_FILTERS},
            wordPolicyConfig={"managedWordListsConfig": [{"type": "PROFANITY"}]},
            blockedInputMessaging="Blocked by the GPS pipeline guardrail.",
            blockedOutputsMessaging="Blocked by the GPS pipeline guardrail.",
        )
        version = client.create_guardrail_version(
            guardrailIdentifier=made["guardrailId"],
            description="First version, created by app.pipeline.guardrails --create.")
    except Exception as exc:  # noqa: BLE001 - boto3 raises botocore types
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConflictException":
            raise SystemExit(f"A guardrail named {GUARDRAIL_NAME!r} already exists. Find its id "
                             f"under Bedrock > Guardrails and set GUARDRAIL_ID.") from None
        raise SystemExit(
            f"Could not create the guardrail ({code or type(exc).__name__}): {exc}\n"
            "A Bedrock API key may not be allowed to create guardrails. Create one in the "
            "console (Bedrock > Guardrails) with the filters in CONTENT_FILTERS, publish a "
            "version, and set GUARDRAIL_ID and GUARDRAIL_VERSION in .env.") from None
    return made["guardrailId"], version["version"]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Bedrock guardrail for the pipeline.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true",
                       help="create the guardrail and publish version 1 (free)")
    group.add_argument("--status", action="store_true", help="show what is configured")
    args = ap.parse_args(argv)

    if args.status:
        print(f"guardrails  {state()}")
        print(f"region      {settings.aws_region}")
        return 0
    guardrail_id, version = create_guardrail()
    print("Created. Add these to .env, then restart:\n")
    print(f"GUARDRAILS_ENABLED=true\nGUARDRAIL_ID={guardrail_id}\nGUARDRAIL_VERSION={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
