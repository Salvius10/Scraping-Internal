"""Filter-compiler tests. Offline -- no model calls, no cost.

The model's JSON is untrusted input: it can invent categories, name sources we
do not carry, or return nonsense for a date range. Everything it produces is
validated before it reaches a SQL query.
"""

from __future__ import annotations

import pytest

from app.api.filter import _coerce, _describe, _phrase_hash
from app.models import Category
from app.schemas import FilterSpec


# --- Valid output --------------------------------------------------------

def test_categories_and_days_are_kept() -> None:
    spec = _coerce({"categories": ["Funding"], "since_days": 7})
    assert spec.categories == [Category.FUNDING]
    assert spec.since_days == 7


def test_category_casing_is_tolerated() -> None:
    assert _coerce({"categories": ["funding", "M&A"]}).categories == [
        Category.FUNDING, Category.MA
    ]


def test_company_and_query_are_trimmed() -> None:
    spec = _coerce({"company": "  Zepto ", "query": " fintech "})
    assert spec.company == "Zepto"
    assert spec.query == "fintech"


def test_known_source_is_kept() -> None:
    assert _coerce({"sources": ["entrackr"]}).sources == ["entrackr"]


# --- Untrusted output ----------------------------------------------------

def test_invented_categories_are_dropped() -> None:
    """The taxonomy is closed; a new label would break every saved filter."""
    spec = _coerce({"categories": ["Fundraise", "Funding News", "Funding"]})
    assert spec.categories == [Category.FUNDING]


def test_unknown_sources_are_dropped() -> None:
    """Only sources we actually ingest can be filtered on."""
    assert _coerce({"sources": ["techcrunch", "entrackr"]}).sources == ["entrackr"]


@pytest.mark.parametrize("value", [0, -5, 9999, "soon", None, [7]])
def test_nonsense_day_ranges_are_dropped(value) -> None:
    assert _coerce({"since_days": value}).since_days is None


@pytest.mark.parametrize("raw", ["not a dict", [], None, 42])
def test_non_object_responses_yield_an_empty_filter(raw) -> None:
    assert _coerce(raw) == FilterSpec()


def test_empty_strings_become_none() -> None:
    spec = _coerce({"company": "   ", "query": ""})
    assert spec.company is None
    assert spec.query is None


# --- Cache key -----------------------------------------------------------

def test_cache_key_ignores_case_and_padding() -> None:
    assert _phrase_hash("  Funding This Week ") == _phrase_hash("funding this week")


def test_cache_key_differs_for_different_phrases() -> None:
    assert _phrase_hash("funding this week") != _phrase_hash("IPO this week")


# --- Plain-language echo -------------------------------------------------

def test_description_reads_as_a_sentence() -> None:
    spec = FilterSpec(categories=[Category.FUNDING], since_days=7)
    assert _describe(spec) == "Showing stories Funding, from the last 7 days"


def test_description_of_an_empty_filter() -> None:
    assert _describe(FilterSpec()) == "Showing everything"


def test_description_mentions_the_company() -> None:
    assert "about Zepto" in _describe(FilterSpec(company="Zepto"))
