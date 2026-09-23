"""Spend ledger and cap enforcement.

This module exists because $7 is a hard ceiling with no top-up. Every model
call is metered here and refused here; nothing in the codebase is allowed to
reach Bedrock without passing through `ensure_budget` and `record_call`.

Two limits:
  total   -- the whole budget, the real ceiling
  daily   -- a rolling 24h speed limit, so a retry loop cannot drain
             everything overnight before anyone notices

Both are checked *before* the call, using a deliberately pessimistic estimate,
so we never discover an overspend after the money is gone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import LlmCall, utcnow

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that would breach a cap.

    Callers should catch this and degrade gracefully -- the news feed is free
    and must keep working even when every LLM feature is switched off.
    """

    def __init__(self, message: str, spend: "Spend") -> None:
        super().__init__(message)
        self.spend = spend


# Price per 1M tokens, keyed by Bedrock model id. Mirrors config so that a
# model we have no price for cannot be billed silently.
PRICES: dict[str, tuple[float, float]] = {
    settings.model_cheap: (settings.price_cheap_in, settings.price_cheap_out),
    settings.model_premium: (settings.price_premium_in, settings.price_premium_out),
}


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens. Unknown models are treated as premium.

    Guessing high is the safe direction: an unpriced model that we assume is
    cheap would quietly overshoot the cap.
    """
    if model in PRICES:
        return PRICES[model]
    log.warning("no price for model %r -- charging at premium rate", model)
    return settings.price_premium_in, settings.price_premium_out


def cost_of(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = price_for(model)
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000


@dataclass(frozen=True)
class Spend:
    """A snapshot of budget state, safe to hand to the API layer."""

    total_usd: float
    last_24h_usd: float
    total_cap: float
    daily_cap: float
    calls: int

    @property
    def remaining_total(self) -> float:
        return max(0.0, self.total_cap - self.total_usd)

    @property
    def remaining_today(self) -> float:
        return max(0.0, self.daily_cap - self.last_24h_usd)

    @property
    def headroom(self) -> float:
        """What we can actually spend right now -- the tighter of the two."""
        return min(self.remaining_total, self.remaining_today)

    @property
    def exhausted(self) -> bool:
        return self.headroom <= 0

    def as_dict(self) -> dict:
        return {
            "total_usd": round(self.total_usd, 6),
            "last_24h_usd": round(self.last_24h_usd, 6),
            "total_cap": self.total_cap,
            "daily_cap": self.daily_cap,
            "remaining_total": round(self.remaining_total, 6),
            "remaining_today": round(self.remaining_today, 6),
            "calls": self.calls,
            "exhausted": self.exhausted,
        }


def get_spend(session: Session | None = None) -> Spend:
    """Current spend against both caps."""
    def _query(s: Session) -> Spend:
        total = s.scalar(select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0))) or 0.0
        calls = s.scalar(select(func.count(LlmCall.id))) or 0
        since = utcnow() - timedelta(hours=24)
        today = s.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0.0))
            .where(LlmCall.ts >= since)
        ) or 0.0
        return Spend(
            total_usd=float(total),
            last_24h_usd=float(today),
            total_cap=settings.total_usd_cap,
            daily_cap=settings.daily_usd_cap,
            calls=int(calls),
        )

    if session is not None:
        return _query(session)
    with session_scope() as s:
        return _query(s)


def estimate_cost(model: str, prompt_chars: int, max_tokens: int) -> float:
    """Pessimistic pre-call estimate.

    Input tokens are approximated at 4 characters each, and output is assumed
    to hit `max_tokens` in full. Both err high on purpose -- the point of the
    pre-check is that we cannot overshoot, not that we predict precisely.
    """
    est_in = max(1, prompt_chars // 4)
    return cost_of(model, est_in, max_tokens)


def ensure_budget(estimated_usd: float = 0.0,
                  session: Session | None = None) -> Spend:
    """Raise BudgetExceeded unless `estimated_usd` fits under both caps."""
    spend = get_spend(session)

    if spend.remaining_total < estimated_usd:
        raise BudgetExceeded(
            f"total budget cap reached: ${spend.total_usd:.4f} of "
            f"${spend.total_cap:.2f} spent, this call needs "
            f"~${estimated_usd:.4f}",
            spend,
        )

    if spend.remaining_today < estimated_usd:
        raise BudgetExceeded(
            f"daily cap reached: ${spend.last_24h_usd:.4f} of "
            f"${spend.daily_cap:.2f} spent in the last 24h, this call needs "
            f"~${estimated_usd:.4f}",
            spend,
        )

    return spend


def record_call(
    feature: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    ok: bool = True,
    note: str | None = None,
    session: Session | None = None,
) -> float:
    """Write one ledger row. Returns the cost of the call.

    Failed calls are recorded too -- a provider error can still bill for the
    input tokens, and an unrecorded failure hides a retry loop.
    """
    cost = cost_of(model, tokens_in, tokens_out)
    row = LlmCall(
        feature=feature,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        ok=ok,
        note=note,
    )
    if session is not None:
        session.add(row)
    else:
        with session_scope() as s:
            s.add(row)

    log.info(
        "llm %s via %s: %d in / %d out = $%.6f%s",
        feature, model, tokens_in, tokens_out, cost, "" if ok else " (FAILED)",
    )
    return cost


def spend_by_feature(session: Session | None = None) -> list[tuple[str, int, float]]:
    """(feature, calls, cost) descending by cost -- the 'where did it go' view."""
    def _query(s: Session):
        rows = s.execute(
            select(
                LlmCall.feature,
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
            )
            .group_by(LlmCall.feature)
            .order_by(func.sum(LlmCall.cost_usd).desc())
        ).all()
        return [(r[0], int(r[1]), float(r[2])) for r in rows]

    if session is not None:
        return _query(session)
    with session_scope() as s:
        return _query(s)


def format_report(session: Session | None = None) -> str:
    """Human-readable spend summary for the CLI."""
    spend = get_spend(session)
    lines = [
        "LLM spend",
        "  total     $%.6f of $%.2f  (%.1f%% used, $%.4f left)" % (
            spend.total_usd, spend.total_cap,
            100 * spend.total_usd / spend.total_cap if spend.total_cap else 0,
            spend.remaining_total,
        ),
        "  last 24h  $%.6f of $%.2f  ($%.4f left)" % (
            spend.last_24h_usd, spend.daily_cap, spend.remaining_today,
        ),
        "  calls     %d" % spend.calls,
    ]
    by_feature = spend_by_feature(session)
    if by_feature:
        lines.append("")
        lines.append("  %-26s %7s %12s" % ("FEATURE", "CALLS", "COST"))
        for feature, calls, cost in by_feature:
            lines.append("  %-26s %7d %12s" % (feature, calls, "$%.6f" % cost))
    if spend.exhausted:
        lines.append("")
        lines.append("  CAP REACHED -- LLM features are disabled, the feed is unaffected.")
    return "\n".join(lines)
