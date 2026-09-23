"""Measure enrichment quality against hand-labelled articles.

Deliberately a script, not a pytest test: it makes a real (metered) model call,
and tests that quietly spend money on every run are a bad idea on a $7 budget.

    python scripts/eval_enrich.py
    python scripts/eval_enrich.py --effort medium   # does more thinking help?

Reports category accuracy, company accuracy, every miss, and what it cost.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.db import init_db  # noqa: E402
from app.ingest.enrich import (  # noqa: E402
    SYSTEM_PROMPT, _clean_company, _coerce_category,
    build_prompt_from_headlines,
)
from app.llm.bedrock import invoke_json  # noqa: E402
from app.models import Category  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "labeled_articles.json"
_CATEGORY_LIST = ", ".join(c.value for c in Category)


def company_matches(expected: str | None, got: str | None) -> bool:
    """Loose match: 'Kotak Alts' should accept 'Kotak Alternate Asset Managers'."""
    if expected is None:
        return got is None
    if not got:
        return False
    a, b = expected.lower(), got.lower()
    return a in b or b in a


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate enrichment quality.")
    ap.add_argument("--effort", default=None,
                    help="gpt-oss reasoning effort: low, medium, high")
    ap.add_argument("--batch", type=int, default=30, help="articles per call")
    args = ap.parse_args(argv)

    init_db()
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    articles = data["articles"]
    print(f"Evaluating {len(articles)} hand-labelled articles "
          f"(effort={args.effort or 'config default'})\n")

    results: dict[int, dict] = {}
    cost = 0.0

    for start in range(0, len(articles), args.batch):
        batch = articles[start:start + args.batch]
        parsed, call = invoke_json(
            "eval_enrich",
            build_prompt_from_headlines([a["headline"] for a in batch]),
            system=SYSTEM_PROMPT,
            max_tokens=4096,
        )
        cost += call.cost_usd
        if not isinstance(parsed, list):
            print(f"  batch at {start}: expected a list, got "
                  f"{type(parsed).__name__}")
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            try:
                idx = start + int(entry["i"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(articles):
                results[idx] = entry

    cat_ok = cat_loose = comp_ok = scored = 0
    misses: list[str] = []

    for i, article in enumerate(articles):
        got = results.get(i)
        if got is None:
            misses.append(f"  NO ANSWER  {article['headline'][:66]}")
            continue
        scored += 1

        want = article["category"]
        also = set(article.get("also_ok") or [])
        got_cat = _coerce_category(got.get("category")).value

        if got_cat == want:
            cat_ok += 1
            cat_loose += 1
        elif got_cat in also:
            cat_loose += 1
            misses.append(
                f"  acceptable  {article['headline'][:56]}\n"
                f"              wanted {want}, got {got_cat} (listed as ok)")
        else:
            misses.append(
                f"  WRONG       {article['headline'][:56]}\n"
                f"              wanted {want}, got {got_cat}")

        got_company = _clean_company(got.get("company"))
        if company_matches(article["company"], got_company):
            comp_ok += 1
        else:
            misses.append(
                f"  company     {article['headline'][:56]}\n"
                f"              wanted {article['company']!r}, "
                f"got {got_company!r}")

    total = len(articles)
    print("RESULTS")
    print(f"  answered            {scored}/{total}")
    print(f"  category exact      {cat_ok}/{total}  ({100*cat_ok/total:.1f}%)")
    print(f"  category acceptable {cat_loose}/{total}  "
          f"({100*cat_loose/total:.1f}%)   <- the number that matters")
    print(f"  company             {comp_ok}/{total}  ({100*comp_ok/total:.1f}%)")
    print(f"  cost                ${cost:.6f}  "
          f"(${cost/max(total,1):.8f}/article)")

    if misses:
        print("\nDISAGREEMENTS")
        for line in misses:
            print(line)

    # The acceptable-category rate is the bar; exact-match punishes the model
    # for defensible readings the fixture itself admits are defensible.
    return 0 if cat_loose >= 0.85 * total else 1


if __name__ == "__main__":
    sys.exit(main())
