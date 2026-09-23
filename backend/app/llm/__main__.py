"""LLM spend and credential CLI.

    python -m app.llm                # where the money went
    python -m app.llm --check        # exit 1 if a cap is reached (for scripts)
    python -m app.llm --preflight    # verify credentials + model access, FREE
    python -m app.llm --reset-ledger # wipe recorded spend (e.g. new account)

--preflight makes no Bedrock inference calls, so it costs nothing. Use it
after changing credentials, before running anything that spends.
"""

from __future__ import annotations

import argparse
import sys

from ..config import settings
from ..db import init_db, session_scope
from ..models import LlmCall
from .bedrock import LlmUnavailable, _boto_session, whoami
from .budget import format_report, get_spend

REQUIRED_MODELS = (
    ("cheap  (bulk work)", settings.model_cheap),
    ("premium (opt-in)", settings.model_premium),
)


def untracked_note() -> str:
    """Warn about spend the ledger cannot see.

    ScrapeGraphAI calls Bedrock through its own LangChain client, so scraped
    sources never reach `record_call`. Saying so keeps the headline figure
    from reading as the whole truth.
    """
    try:
        from ..ingest.scraper_sgai import estimate_run_cost
        from ..ingest.sources import scrape_sources
        paid = scrape_sources()
    except Exception:  # noqa: BLE001 - reporting must not fail
        return ""
    if not paid:
        return ""
    per_run = estimate_run_cost()
    names = ", ".join(s.name for s in paid)
    lines = [
        "",
        "  NOT INCLUDED ABOVE: %s scraped via ScrapeGraphAI, which calls" % names,
        "  Bedrock directly and bypasses this ledger. Estimated ~$%.5f per"
        % per_run,
        "  source per run (~$%.2f/month at 2 runs/day)."
        % (per_run * len(paid) * 2 * 30),
    ]
    return "\n".join(lines)


def preflight() -> int:
    """Check identity and model access without invoking a model."""
    print("AWS identity")
    try:
        who = whoami()
        print("  account   %s" % who["account"])
        print("  arn       %s" % who["arn"])
        print("  region    %s" % who["region"])
        print("  source    %s" % who["source"])
    except LlmUnavailable as exc:
        print("  FAILED: %s" % exc)
        return 1

    print()
    print("Model access (no inference, no cost)")
    try:
        bedrock = _boto_session().client("bedrock")
        available = {}
        for m in bedrock.list_foundation_models().get("modelSummaries", []):
            available[m["modelId"]] = m.get("inferenceTypesSupported", [])
        profiles = {
            p["inferenceProfileId"]
            for p in bedrock.list_inference_profiles().get(
                "inferenceProfileSummaries", [])
        }
    except Exception as exc:  # noqa: BLE001
        print("  could not list models: %s" % str(exc)[:160])
        return 1

    ok = True
    for label, model_id in REQUIRED_MODELS:
        if model_id in profiles:
            print("  %-20s %-44s inference profile OK" % (label, model_id))
        elif model_id in available:
            kinds = ",".join(available[model_id])
            if "ON_DEMAND" in kinds:
                print("  %-20s %-44s ON_DEMAND OK" % (label, model_id))
            else:
                print("  %-20s %-44s NOT on-demand (%s)" % (label, model_id, kinds))
                ok = False
        else:
            print("  %-20s %-44s NOT FOUND in this region" % (label, model_id))
            ok = False

    if not ok:
        print()
        print("  A model is listed but not usable. Enable it under")
        print("  Bedrock > Model access in the AWS console for %s."
              % settings.aws_region)
        print("  Listing a model does not mean access is granted -- only an")
        print("  actual call proves that.")

    print()
    print(format_report())
    return 0 if ok else 1


def reset_ledger() -> int:
    with session_scope() as s:
        removed = s.query(LlmCall).delete()
    print("ledger cleared: %d call(s) removed; budget starts fresh at $%.2f"
          % (removed, settings.total_usd_cap))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report LLM spend and check access.")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero when a cap is reached")
    ap.add_argument("--preflight", action="store_true",
                    help="verify credentials and model access (free)")
    ap.add_argument("--reset-ledger", action="store_true",
                    help="delete all recorded spend")
    args = ap.parse_args(argv)

    init_db()

    if args.reset_ledger:
        return reset_ledger()
    if args.preflight:
        return preflight()

    print(format_report())
    print(untracked_note())
    if args.check and get_spend().exhausted:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
