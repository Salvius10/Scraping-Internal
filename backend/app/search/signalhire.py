"""Hosted SignalHire: find a company's leaders, then the contacts of each.

Plain HTTP, no SDK, the same way `apify.py` talks to Apify. The key goes in an
`apikey` header. Three calls:

  POST /candidate/searchByQuery   title, company and location queries ->
                                  matching profiles (uid, name, jobs). Free:
                                  draws on a daily quota, not credits.
  POST /candidate/search          up to 100 profile uids -> each person's
                                  emails, phones and links. Costs credits per
                                  person found. Sent with `withoutWaterfall`,
                                  so the answer comes back in the response;
                                  the waterfall mode needs a public callback URL.
  GET  /credits                   credits left. Free.

Every answer carries `X-Credits-Left`. Every failure is turned into a sentence
a reader can act on; `stop` marks the ones no other company can get past
(key, credits, quota, rate limit, network), so the job stops instead of
failing every company in the queue the same way.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings

_UID = re.compile(r"^[0-9A-Za-z]{32}$")

# Person API per-item answers.
SUCCESS = "success"
CREDITS_ARE_OVER = "credits_are_over"


class SignalHireError(RuntimeError):
    """A SignalHire failure, worded for the reader."""

    def __init__(self, message: str, *, stop: bool = False) -> None:
        super().__init__(message)
        self.stop = stop


@dataclass
class Profile:
    """One search hit: who, where, and their jobs as (company, title), newest first."""

    uid: str
    full_name: str
    location: str | None
    jobs: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Found:
    total: int
    profiles: list[Profile]
    credits_left: int | None = None


@dataclass
class Lookup:
    """The Person API's answer: one {item, status, candidate?} per uid sent."""

    results: list[dict]
    credits_left: int | None = None


def ready() -> bool:
    return bool(settings.signalhire_api_key)


def _client(timeout: float | None = None) -> httpx.Client:
    """The HTTP client for SignalHire. A seam tests replace with a mock."""
    return httpx.Client(timeout=timeout or settings.signalhire_timeout)


def _sleep(seconds: float) -> None:
    """Waiting out a rate limit. A seam tests replace so they do not wait."""
    time.sleep(seconds)


def _credits_left(resp: httpx.Response) -> int | None:
    try:
        return int(resp.headers.get("X-Credits-Left", ""))
    except ValueError:
        return None


def _detail(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            if isinstance(body.get(key), str) and body[key].strip():
                return body[key].strip()[:200]
    return None


def _retry_wait(resp: httpx.Response, default: float) -> float:
    try:
        return min(max(float(resp.headers.get("Retry-After", "")), 1.0), 120.0)
    except ValueError:
        return default


def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None,
             paid: bool = False, timeout: float | None = None) -> tuple[Any, int | None]:
    """One call, with rate-limit retries. Returns (JSON body, credits left)."""
    if not ready():
        raise SignalHireError(
            "SignalHire is not set up yet. Add SIGNALHIRE_API_KEY to .env and restart "
            "the server.", stop=True)

    url = settings.signalhire_api_url.rstrip("/") + path
    headers = {"apikey": settings.signalhire_api_key or "", "Content-Type": "application/json"}
    waits = list(settings.signalhire_retry_waits)
    while True:
        try:
            with _client(timeout) as client:
                resp = client.request(method, url, json=json, params=params, headers=headers)
        except httpx.TimeoutException:
            raise SignalHireError("SignalHire took too long to answer.") from None
        except httpx.HTTPError as exc:
            raise SignalHireError(f"Could not reach SignalHire ({type(exc).__name__}).",
                                  stop=True) from None
        if resp.status_code != 429 or not waits:
            break
        _sleep(_retry_wait(resp, waits.pop(0)))

    status = resp.status_code
    if status == 401:
        raise SignalHireError("SignalHire rejected the API key. Check SIGNALHIRE_API_KEY in .env.",
                              stop=True)
    if status == 402:
        raise SignalHireError(
            "SignalHire credits are used up. Top up at signalhire.com, then resume." if paid
            else "SignalHire's daily search quota is used up. It resets tomorrow; resume then.",
            stop=True)
    if status == 403:
        raise SignalHireError("SignalHire refused: the account is disabled or this key may not "
                              "use that API.", stop=True)
    if status == 429:
        raise SignalHireError("SignalHire's rate limit was hit. Resume in a minute or two.",
                              stop=True)
    if status >= 500:
        raise SignalHireError(f"SignalHire had a server error (HTTP {status}). Try again later.")
    if status >= 400:
        raise SignalHireError(
            f"SignalHire refused the request: {_detail(resp) or f'HTTP {status}'}")

    try:
        body = resp.json()
    except ValueError:
        raise SignalHireError("SignalHire sent back something that was not JSON.") from None
    return body, _credits_left(resp)


def phrase(text: str) -> str:
    """`text` as one exact phrase in a SignalHire boolean query."""
    return '"' + " ".join(text.replace('"', " ").split()) + '"'


def search_people(*, company: str, title: str, location: str | None, size: int) -> Found:
    """Profiles whose current company matches the phrase `company` and whose
    title matches the boolean query `title`. SignalHire matches the two against
    any of a person's jobs, not necessarily the same one: check each job."""
    body: dict[str, Any] = {
        "currentCompany": phrase(company),
        "currentTitle": title,
        "size": max(1, min(int(size), 100)),
    }
    if location:
        body["location"] = location
    payload, left = _request("POST", "/candidate/searchByQuery", json=body)
    if not isinstance(payload, dict):
        raise SignalHireError("SignalHire sent back an unexpected search answer.")

    profiles: list[Profile] = []
    for row in payload.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("uid") or "")
        name = " ".join(str(row.get("fullName") or "").split())
        if not _UID.match(uid) or not name:
            continue
        jobs = [(str(j.get("company") or "").strip(), str(j.get("title") or "").strip())
                for j in row.get("experience") or [] if isinstance(j, dict)]
        location_text = row.get("location")
        profiles.append(Profile(
            uid=uid, full_name=name[:200],
            location=location_text.strip()[:200] if isinstance(location_text, str)
            and location_text.strip() else None,
            jobs=[(c, t) for c, t in jobs if c or t]))
    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        total = len(profiles)
    return Found(total=total, profiles=profiles, credits_left=left)


def lookup(uids: list[str]) -> Lookup:
    """Contacts for up to 100 profiles, synchronously. Each person found costs credits."""
    items = list(dict.fromkeys(u for u in uids if _UID.match(u)))
    if not items:
        return Lookup(results=[])
    if len(items) > 100:
        raise SignalHireError("At most 100 people can be looked up at once.")
    payload, left = _request("POST", "/candidate/search",
                             json={"items": items, "withoutWaterfall": True}, paid=True)
    if not isinstance(payload, list):
        raise SignalHireError("SignalHire sent back an unexpected lookup answer.")
    return Lookup(results=[r for r in payload if isinstance(r, dict)], credits_left=left)


def credits(timeout: float | None = None) -> int:
    """Standard credits left on the account."""
    payload, left = _request("GET", "/credits", timeout=timeout)
    value = payload.get("credits") if isinstance(payload, dict) else None
    if isinstance(value, int):
        return value
    if left is not None:
        return left
    raise SignalHireError("SignalHire sent back an unexpected credits answer.")
