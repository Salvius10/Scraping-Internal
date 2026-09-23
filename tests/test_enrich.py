"""Enrichment helper tests. Offline -- no model calls, no cost.

Quality of the model's answers is measured separately by
`scripts/eval_enrich.py`, which spends real money and so is not a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingest.describe import extract_meta
from app.ingest.enrich import (
    _clean_company, _coerce_category, build_prompt_from_headlines,
)
from app.models import Category

FIXTURE = Path(__file__).parent / "fixtures" / "labeled_articles.json"


# --- Category coercion: the taxonomy must stay closed --------------------

@pytest.mark.parametrize("raw,expected", [
    ("Funding", Category.FUNDING),
    ("funding", Category.FUNDING),
    ("  FUNDING  ", Category.FUNDING),
    ("M&A", Category.MA),
    ("Policy/Regulation", Category.POLICY),
    ("policy regulation", Category.POLICY),
    ("Product Launch", Category.PRODUCT_LAUNCH),
    ("productlaunch", Category.PRODUCT_LAUNCH),
])
def test_known_categories_are_accepted(raw, expected) -> None:
    assert _coerce_category(raw) is expected


@pytest.mark.parametrize("raw", [
    "Fundraise",        # plausible invention
    "Funding News",     # plausible invention
    "",
    None,
    123,
    ["Funding"],
])
def test_unknown_categories_collapse_to_other(raw) -> None:
    """Never widen the taxonomy: a new label silently breaks every filter."""
    assert _coerce_category(raw) is Category.OTHER


def test_every_enum_value_round_trips() -> None:
    for category in Category:
        assert _coerce_category(category.value) is category


# --- Company cleaning ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Zepto", "Zepto"),
    ("  Zepto  ", "Zepto"),
    ("Zepto.", "Zepto"),
    ("Kotak   Alternate   Asset", "Kotak Alternate Asset"),
])
def test_company_is_normalised(raw, expected) -> None:
    assert _clean_company(raw) == expected


@pytest.mark.parametrize("raw", ["null", "None", "N/A", "unknown", "na", "", "   ", None, 42])
def test_absent_company_becomes_none(raw) -> None:
    """The model writes 'null' as a string often enough to matter."""
    assert _clean_company(raw) is None


def test_company_is_length_capped() -> None:
    assert len(_clean_company("x" * 500)) == 200


# --- Prompt --------------------------------------------------------------

def test_prompt_lists_every_category() -> None:
    prompt = build_prompt_from_headlines(["Zepto raises Rs 500 crore"])
    for category in Category:
        assert category.value in prompt


def test_prompt_numbers_articles_from_one() -> None:
    prompt = build_prompt_from_headlines(["first", "second"])
    assert "[1] first" in prompt
    assert "[2] second" in prompt


# --- Publisher metadata extraction (free path) ---------------------------

def test_og_description_is_preferred() -> None:
    html = """<html><head>
        <meta name="description" content="short one">
        <meta property="og:description" content="The publisher's own summary.">
        </head><body></body></html>"""
    assert extract_meta(html).description == "The publisher's own summary."


def test_published_time_is_parsed() -> None:
    html = """<html><head>
        <meta property="article:published_time" content="2026-09-23T09:05:20+05:30">
        </head></html>"""
    meta = extract_meta(html)
    assert meta.published_at is not None
    assert meta.published_at.year == 2026
    assert meta.published_at.month == 9


def test_time_element_is_the_fallback_date() -> None:
    html = '<html><body><time datetime="2026-09-20T10:00:00Z">Sept 20</time></body></html>'
    assert extract_meta(html).published_at is not None


def test_page_without_metadata_yields_empty() -> None:
    meta = extract_meta("<html><head><title>x</title></head><body>hi</body></html>")
    assert meta.description is None
    assert meta.published_at is None


# --- The eval fixture itself --------------------------------------------

def test_fixture_labels_are_valid_categories() -> None:
    """A typo in the fixture would silently mark correct answers wrong."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    valid = {c.value for c in Category}
    for article in data["articles"]:
        assert article["category"] in valid, article["headline"]
        for alternative in article.get("also_ok", []):
            assert alternative in valid, article["headline"]


def test_fixture_has_reasonable_coverage() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    used = {a["category"] for a in data["articles"]}
    assert len(data["articles"]) >= 40
    assert len(used) >= 8, f"only {len(used)} categories exercised"
