"""Dedupe regression tests.

Every case here came from real feed data on 2026-09-23. The false-positive
cases are the important ones: an over-eager matcher hides real stories, which
is worse than showing a duplicate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.ingest.dedupe import (  # noqa: E402
    containment, headline_similarity, is_multi_topic, normalise_tokens,
    numeric_signature, similarity, story_key,
)

THRESHOLD = 0.60


def looks_duplicate(a: str, b: str) -> bool:
    """Mirror of find_canonical's matching rule, without a database."""
    if numeric_signature(a) != numeric_signature(b):
        return False
    return headline_similarity(a, b) >= THRESHOLD


# --- Same story, different outlet -------------------------------------------

SAME_STORY = [
    ("Laundry and home cleaning brand Ecosys raises Rs 5 crore in pre-Series A funding",
     "Laundry and home cleaning brand Ecosys raises Rs 5 Cr in pre-Series A round"),
    ("Flipkart-backed super.money appoints former BharatPe CPO Rohan Khara as Chief Product Officer",
     "super.money appoints former BharatPe CPO Rohan Khara as Chief Product Officer"),
    ("Spinny Confidentially Files IPO Papers, Eyes Up To ₹3,000 Cr Issue",
     "Spinny files confidential IPO papers, eyes upto Rs 3,000 Cr"),
    ("Babycare quick-commerce startup Kiddo raises Rs 12.5 crore from Campus Fund, others",
     "Baby-focused quick commerce startup Kiddo raises Rs 12.5 Cr led by Campus Fund"),
    # Jaccard scores this pair 0.56 -- under threshold -- because one outlet
    # adds context the other omits. Containment is what catches it.
    ("Pune-based fintech and brokerage startup Definedge raises Rs 22 crore in funding",
     "Fintech and brokerage startup Definedge raises Rs 22 Cr in pre-Series A"),
]


@pytest.mark.parametrize("a,b", SAME_STORY)
def test_same_story_is_deduped(a: str, b: str) -> None:
    assert looks_duplicate(a, b), f"should have matched:\n  {a}\n  {b}"


# --- Different stories that LOOK alike --------------------------------------

DIFFERENT_STORY = [
    # Recurring daily column -- only the date distinguishes the editions.
    ("Ecosystem Pulse — Sept 22, 2026",
     "Ecosystem Pulse — Sept 10, 2026"),
    ("Ecosystem Highlights — Sept 14, 2026",
     "Ecosystem Buzz – Sept 11, 2026"),
    # Same company, different rounds.
    ("Zepto raises Rs 500 crore in Series F funding",
     "Zepto raises Rs 900 crore in Series G funding"),
    # Genuinely unrelated.
    ("Mastercard sells entire 4.31% Pine Labs stake for Rs 934 Cr",
     "Silence Laboratories is building privacy-first cybersecurity technology"),
    # A roundup must not swallow the individual story it mentions -- doing so
    # hides the real article behind a summary.
    ("Indian Startup IPO Sprint, Zetwerk-Ayr Settle Dispute & More",
     "Zetwerk, Ayr Energy Settle Legal Dispute"),
    ("Succession test at Hikal; Nothing spins off CMF as majority Indian-owned company",
     "Nothing to spin off CMF as majority Indian-owned company"),
]


@pytest.mark.parametrize("a,b", DIFFERENT_STORY)
def test_different_stories_are_kept_apart(a: str, b: str) -> None:
    assert not looks_duplicate(a, b), f"should NOT have matched:\n  {a}\n  {b}"


# --- Token and key behaviour ------------------------------------------------

def test_short_numbers_survive_tokenisation() -> None:
    """The bug that merged a recurring column: dates were being dropped."""
    tokens = normalise_tokens("Ecosystem Pulse — Sept 22, 2026")
    assert "22" in tokens
    assert "2026" in tokens


def test_numeric_signature_extracts_amounts() -> None:
    assert numeric_signature("raises Rs 12.5 crore") == frozenset({"12", "5"})
    assert numeric_signature("no numbers here") == frozenset()


def test_story_key_is_word_order_independent() -> None:
    assert story_key("Spinny files IPO papers") == story_key("IPO papers files Spinny")


def test_story_key_is_stable_and_differs_on_content() -> None:
    assert story_key("Zepto raises 500 crore") == story_key("Zepto raises 500 crore")
    assert story_key("Zepto raises 500 crore") != story_key("Swiggy raises 500 crore")


def test_empty_headline_does_not_crash() -> None:
    assert normalise_tokens("") == frozenset()
    assert story_key("")  # still returns a usable key


def test_containment_catches_asymmetric_headlines() -> None:
    """The Definedge miss: same story, very different headline lengths."""
    a = normalise_tokens(
        "Pune-based fintech and brokerage startup Definedge raises Rs 22 crore in funding")
    b = normalise_tokens(
        "Fintech and brokerage startup Definedge raises Rs 22 Cr in pre-Series A")
    assert containment(a, b) > similarity(a, b) - 0.001  # containment is the winner
    assert similarity(a, b) >= THRESHOLD


def test_short_headlines_do_not_over_match_on_containment() -> None:
    """Containment saturates on tiny token sets, so it is gated by length."""
    a = normalise_tokens("Zepto raises")
    b = normalise_tokens("Zepto raises massive round from Nexus Venture Partners today")
    assert similarity(a, b) < 1.0


# --- Multi-topic guard ------------------------------------------------------

def test_roundup_marker_is_detected() -> None:
    assert is_multi_topic("Indian Startup IPO Sprint, Zetwerk-Ayr Settle Dispute & More",
                          "Zetwerk, Ayr Energy Settle Legal Dispute")
    assert is_multi_topic("Funding and acquisitions in Indian startups this week",
                          "Zepto raises Rs 500 crore")


def test_semicolon_with_two_subjects_is_multi_topic() -> None:
    assert is_multi_topic(
        "Succession test at Hikal; Nothing spins off CMF as majority Indian-owned company",
        "Nothing to spin off CMF as majority Indian-owned company")


def test_semicolon_on_one_subject_is_not_multi_topic() -> None:
    """Moneyview: a semicolon continuing the SAME story must stay deduped."""
    longer = ("Digital lending platform Moneyview sets IPO price band at Rs 32-34; "
              "offer size Rs 1,092 Cr")
    other = "Moneyview fixes IPO price band at Rs 32-34; eyes Rs 1,092 Cr"
    assert not is_multi_topic(longer, other)
    assert headline_similarity(longer, other) >= THRESHOLD


def test_generic_funding_jargon_does_not_create_matches() -> None:
    """Two roundups sharing only boilerplate are different stories."""
    assert not looks_duplicate(
        "Creedom, Guickly raise early-stage funding",
        "Yuma Energy, Kepler Aerospace, DocPharma, others raise early-stage funding")


def test_company_name_still_drives_a_real_match() -> None:
    """Stripping jargon must not break genuine cross-source duplicates."""
    assert looks_duplicate(
        "Industrial workforce startup Factrika raises Rs 8.9 crore in seed funding",
        "Info Edge leads Rs 8.9 Cr seed round in Factrika")
