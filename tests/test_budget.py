"""Budget rail tests.

The contract being protected: with $7 and no top-up, no code path may reach a
model without first passing a cap check, and the recorded cost must be real
rather than estimated.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.db import session_scope
from app.llm import bedrock, budget
from app.llm.budget import (
    BudgetExceeded, cost_of, ensure_budget, estimate_cost, get_spend,
    price_for, record_call, spend_by_feature,
)
from app.models import LlmCall, utcnow

CHEAP = settings.model_cheap
PREMIUM = settings.model_premium


def burn_total_cap() -> float:
    """Record enough spend to exceed the total cap, derived from the cap itself.

    Computed rather than hard-coded so the tests stay correct if the $7 budget
    or the model prices change.
    """
    target = settings.total_usd_cap * 1.05
    tokens_out = int(target / settings.price_premium_out * 1_000_000) + 1
    return record_call("test", PREMIUM, 0, tokens_out)


# --- Pricing ----------------------------------------------------------------

def test_cheap_model_pricing() -> None:
    # 1M in + 1M out at $0.15 / $0.60
    assert cost_of(CHEAP, 1_000_000, 1_000_000) == pytest.approx(0.75)


def test_premium_model_pricing() -> None:
    # 1M in + 1M out at $3.00 / $15.00
    assert cost_of(PREMIUM, 1_000_000, 1_000_000) == pytest.approx(18.00)


def test_premium_is_about_21x_cheap_for_a_typical_question() -> None:
    """The ratio the whole architecture is built around."""
    tokens_in, tokens_out = 2600, 260
    cheap = cost_of(CHEAP, tokens_in, tokens_out)
    premium = cost_of(PREMIUM, tokens_in, tokens_out)
    assert premium / cheap == pytest.approx(21.2, abs=0.5)


def test_unknown_model_is_charged_at_premium_rate() -> None:
    """Guess high: an unpriced model billed as cheap would overshoot the cap."""
    assert price_for("some-model-we-never-configured") == (
        settings.price_premium_in, settings.price_premium_out
    )


def test_estimate_is_pessimistic() -> None:
    """The pre-check must not under-estimate, or the cap can be breached."""
    prompt = "x" * 4000                      # ~1000 tokens in
    estimated = estimate_cost(CHEAP, len(prompt), max_tokens=500)
    actual = cost_of(CHEAP, 1000, 500)       # if the model used every token
    assert estimated >= actual


# --- Caps -------------------------------------------------------------------

def test_fresh_ledger_has_full_headroom(clean_ledger) -> None:
    spend = get_spend()
    assert spend.total_usd == 0.0
    assert spend.calls == 0
    assert not spend.exhausted
    assert spend.headroom == min(settings.total_usd_cap, settings.daily_usd_cap)


def test_total_cap_blocks_a_call(clean_ledger) -> None:
    burn_total_cap()
    spend = get_spend()
    assert spend.total_usd > settings.total_usd_cap

    with pytest.raises(BudgetExceeded) as excinfo:
        ensure_budget(0.01)
    assert "total budget cap" in str(excinfo.value)
    assert excinfo.value.spend.exhausted


def test_daily_cap_blocks_a_call_while_total_still_has_room(clean_ledger) -> None:
    """The speed limit fires before the ceiling does."""
    # Spend just over the daily cap but well under the total.
    tokens = int(settings.daily_usd_cap / settings.price_cheap_in * 1_000_000) + 10_000
    record_call("test", CHEAP, tokens, 0)

    spend = get_spend()
    assert spend.last_24h_usd > settings.daily_usd_cap
    assert spend.total_usd < settings.total_usd_cap   # ceiling not reached

    with pytest.raises(BudgetExceeded) as excinfo:
        ensure_budget(0.01)
    assert "daily cap" in str(excinfo.value)


def test_daily_window_is_rolling_24h(clean_ledger) -> None:
    """Yesterday's spend counts against the total but not the daily cap."""
    with session_scope() as s:
        old = LlmCall(feature="test", model=CHEAP, tokens_in=1_000_000,
                      tokens_out=0, cost_usd=0.15, ok=True)
        old.ts = utcnow() - timedelta(hours=30)
        s.add(old)

    spend = get_spend()
    assert spend.total_usd == pytest.approx(0.15)
    assert spend.last_24h_usd == pytest.approx(0.0)


def test_headroom_is_the_tighter_of_the_two_caps(clean_ledger) -> None:
    spend = get_spend()
    assert spend.headroom == min(spend.remaining_total, spend.remaining_today)


def test_call_that_fits_is_allowed(clean_ledger) -> None:
    assert ensure_budget(0.001) is not None


# --- Ledger -----------------------------------------------------------------

def test_record_call_writes_a_row_and_returns_cost(clean_ledger) -> None:
    cost = record_call("enrich", CHEAP, 4000, 1500)
    assert cost == pytest.approx(cost_of(CHEAP, 4000, 1500))

    spend = get_spend()
    assert spend.calls == 1
    assert spend.total_usd == pytest.approx(cost)


def test_failed_calls_are_still_ledgered(clean_ledger) -> None:
    """An unrecorded failure hides a retry loop."""
    record_call("enrich", CHEAP, 500, 0, ok=False, note="boom")
    with session_scope() as s:
        row = s.query(LlmCall).one()
    assert row.ok is False
    assert row.note == "boom"


def test_spend_is_attributed_per_feature(clean_ledger) -> None:
    record_call("intelligence", PREMIUM, 10_000, 2_000)
    record_call("enrich", CHEAP, 10_000, 2_000)
    record_call("enrich", CHEAP, 10_000, 2_000)

    rows = dict((f, (c, cost)) for f, c, cost in spend_by_feature())
    assert rows["enrich"][0] == 2
    assert rows["intelligence"][0] == 1
    # Ordered by cost: premium dominates despite fewer calls.
    assert spend_by_feature()[0][0] == "intelligence"


# --- The choke point --------------------------------------------------------

def test_invoke_refuses_when_budget_is_gone(clean_ledger) -> None:
    """No Bedrock call is attempted once a cap is reached."""
    burn_total_cap()

    with pytest.raises(BudgetExceeded):
        bedrock.invoke("intelligence", "hello")


def test_invoke_checks_budget_before_building_a_client(monkeypatch, clean_ledger) -> None:
    """The cap check must come first -- a refused call costs nothing."""
    burn_total_cap()

    def _boom():
        raise AssertionError("client must not be created when over budget")

    monkeypatch.setattr(bedrock, "get_client", _boom)
    with pytest.raises(BudgetExceeded):
        bedrock.invoke("intelligence", "hello")


# --- JSON parsing -----------------------------------------------------------

def test_parse_json_plain() -> None:
    assert bedrock.parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_in_markdown_fence() -> None:
    assert bedrock.parse_json('```json\n[{"b": 2}]\n```') == [{"b": 2}]


def test_parse_json_with_surrounding_prose() -> None:
    text = 'Sure! Here is the result:\n[{"company": "Zepto"}]\nHope that helps.'
    assert bedrock.parse_json(text) == [{"company": "Zepto"}]


def test_parse_json_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        bedrock.parse_json("no json here at all")


# --- Report -----------------------------------------------------------------

def test_report_renders_and_flags_exhaustion(clean_ledger) -> None:
    record_call("enrich", CHEAP, 1000, 200)
    report = budget.format_report()
    assert "LLM spend" in report
    assert "enrich" in report
    assert "CAP REACHED" not in report

    burn_total_cap()
    assert "CAP REACHED" in budget.format_report()
