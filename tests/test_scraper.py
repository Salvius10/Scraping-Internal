"""Response-shape tests for the ScrapeGraphAI extractor.

ScrapeGraphAI returns a different shape depending on how the model happened to
format its answer, and the shape varied between runs against the *same* page.
Every case below was observed live against VCCircle, so each one is a bug that
silently produced zero articles until it was handled.

All offline -- no Bedrock calls, no cost.
"""

from __future__ import annotations

from app.ingest.scraper_sgai import (
    _coerce_rows, _flatten_content, _salvage_objects, _strip_reasoning,
)

ARTICLE = {
    "headline": "Multiples PE bets on Prime Focus unit Brahma AI at $2 bn valuation",
    "url": "/multiplespe-bets-on-prime-focus-unit-brahma-ai-at-2-bn-valuation",
    "published": "23 September",
}
ARTICLE_JSON = (
    '[{"headline": "Multiples PE bets on Prime Focus unit Brahma AI at $2 bn '
    'valuation", "url": "/multiplespe-bets-on-prime-focus-unit-brahma-ai-at-2-bn'
    '-valuation", "published": "23 September"}]'
)


class FakeMessage:
    """Stand-in for a LangChain AIMessage."""

    def __init__(self, content):
        self.content = content


# --- Shapes ScrapeGraphAI actually returned ---------------------------------

def test_plain_list() -> None:
    assert _coerce_rows([ARTICLE]) == [ARTICLE]


def test_dict_wrapping_a_list() -> None:
    assert _coerce_rows({"articles": [ARTICLE]}) == [ARTICLE]


def test_dict_wrapping_a_json_string() -> None:
    """The regression that made VCCircle return 0 after a run that returned 72."""
    rows = _coerce_rows({"content": ARTICLE_JSON})
    assert len(rows) == 1
    assert rows[0]["headline"].startswith("Multiples PE")


def test_single_article_dict() -> None:
    assert _coerce_rows(ARTICLE) == [ARTICLE]


def test_message_with_json_content() -> None:
    rows = _coerce_rows(FakeMessage(ARTICLE_JSON))
    assert len(rows) == 1


def test_empty_content_dict_yields_nothing() -> None:
    assert _coerce_rows({"content": []}) == []


def test_none_yields_nothing() -> None:
    assert _coerce_rows(None) == []


# --- Reasoning-model noise --------------------------------------------------

def test_reasoning_block_is_stripped() -> None:
    text = "<reasoning>We must list the articles.</reasoning>\n" + ARTICLE_JSON
    assert _strip_reasoning(text).startswith("[")


def test_payload_survives_an_unterminated_reasoning_block() -> None:
    """No closing tag: stripping would eat the payload, so salvage must win."""
    text = "<reasoning>Thinking out loud...\n" + ARTICLE_JSON
    rows = _coerce_rows(FakeMessage(text))
    assert len(rows) == 1
    assert rows[0]["url"].startswith("/multiplespe")


def test_flatten_converse_content_blocks() -> None:
    """Converse returns typed blocks; reasoning must be dropped, text kept."""
    blocks = [
        {"type": "reasoning_content",
         "reasoning_content": {"text": "deliberating", "signature": ""}},
        {"type": "text", "text": '[{"a": 1}]'},
    ]
    assert _flatten_content(blocks) == '[{"a": 1}]'


def test_flatten_passes_strings_through() -> None:
    assert _flatten_content("already a string") == "already a string"


# --- Truncation -------------------------------------------------------------

def test_salvage_recovers_objects_from_a_truncated_array() -> None:
    """A model that runs out of output tokens leaves valid objects behind."""
    truncated = '[{"headline": "A", "url": "/a"}, {"headline": "B", "url": "/b"}, {"headl'
    rows = _salvage_objects(truncated)
    assert [r["headline"] for r in rows] == ["A", "B"]


def test_salvage_ignores_braces_inside_strings() -> None:
    text = '[{"headline": "Deal worth {x} crore", "url": "/a"}]'
    rows = _salvage_objects(text)
    assert len(rows) == 1
    assert rows[0]["headline"] == "Deal worth {x} crore"


def test_truncated_array_routed_through_coerce_rows() -> None:
    truncated = '[{"headline": "A", "url": "/a"}, {"headline": "B", "url": "/b"}, {"headl'
    assert len(_coerce_rows(FakeMessage(truncated))) == 2
