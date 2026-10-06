"""Hosted Apify: run a Store actor and get its results, in one call.

Plain HTTP, no SDK, the same way `firecrawl.py` talks to Firecrawl:

  POST /v2/actors/{actor}/run-sync-get-dataset-items
       input as the JSON body -> the run's dataset items as a JSON array.
       The run must finish within 300 seconds.

Apify bills in its own account, not our $7 LLM ledger. Every run is capped
twice: `maxItems` (results charged) and `maxTotalChargeUsd` (dollars), so a
misbehaving actor cannot run up a bill. Every failure is turned into a
sentence a reader can act on.
"""

from __future__ import annotations

import httpx

from ..config import settings


class ApifyError(RuntimeError):
    """An Apify failure, worded for the reader."""


def _client() -> httpx.Client:
    """The HTTP client for Apify. A seam tests replace with a mock."""
    return httpx.Client(timeout=settings.apify_timeout + 30)


def run_actor(actor: str, run_input: dict, *, max_items: int,
              max_charge_usd: float | None = None) -> list[dict]:
    """Run `actor` ("username~actor-name") to completion and return its items."""
    if not settings.apify_api_token:
        raise ApifyError(
            "Apify is not set up yet. Add APIFY_API_TOKEN to .env and restart the server.")

    charge = settings.apify_max_charge_usd if max_charge_usd is None else max_charge_usd
    url = f"{settings.apify_api_url.rstrip('/')}/v2/actors/{actor}/run-sync-get-dataset-items"
    params = {
        "timeout": settings.apify_timeout,
        "maxItems": max_items,
        "maxTotalChargeUsd": f"{charge:.4f}",
        "format": "json",
    }
    headers = {"Authorization": f"Bearer {settings.apify_api_token}"}

    try:
        with _client() as client:
            resp = client.post(url, params=params, json=run_input, headers=headers)
    except httpx.TimeoutException:
        raise ApifyError("Apify took too long to answer. Try again.") from None
    except httpx.HTTPError as exc:
        raise ApifyError(f"Could not reach Apify ({type(exc).__name__}).") from None

    if resp.status_code == 401:
        raise ApifyError("Apify rejected the token. Check APIFY_API_TOKEN in .env.")
    if resp.status_code == 402:
        raise ApifyError("Apify credits or plan limit used up. Check your Apify billing.")
    if resp.status_code == 403:
        raise ApifyError("Apify refused to run that actor with this token.")
    if resp.status_code == 404:
        raise ApifyError(f"Apify has no actor {actor!r}.")
    if resp.status_code == 408:
        raise ApifyError("The Apify run did not finish within 5 minutes.")
    if resp.status_code == 429:
        raise ApifyError("Apify's rate limit was hit. Wait a moment and try again.")
    if resp.status_code >= 400:
        try:
            detail = (resp.json().get("error") or {}).get("message")
        except (ValueError, AttributeError):
            detail = None
        raise ApifyError(f"Apify could not do that: {(detail or f'HTTP {resp.status_code}')[:200]}")

    try:
        items = resp.json()
    except ValueError:
        raise ApifyError("Apify sent back something that was not JSON.") from None
    if not isinstance(items, list):
        raise ApifyError("Apify sent back an unexpected answer.")
    return [item for item in items if isinstance(item, dict)]
