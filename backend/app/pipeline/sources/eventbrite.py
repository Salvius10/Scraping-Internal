"""Eventbrite city search, via Apify.

Eventbrite closed its public event-search API in 2020, so the actor
(`apify_eventbrite_actor`) runs the site's own search for each location in the
registry, narrowed by a keyword. Locations are Eventbrite browse URLs
(eventbrite.com/d/india--bangalore/), which pin the exact place: a bare name
like "Hyderabad" can resolve to another country.

Rows carry local wall-clock times with the venue's timezone; they are turned
into UTC here. Billed per event row delivered; rows outside the place asked
for, the sample row and diagnostics are written uncharged and skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...config import settings
from ...search import apify
from ..items import EVENT, RawItem, clean, web_url
from ..timeutil import parse_iso
from . import FetchContext, FetchResult, Source, SourceError
from .events_text import event_text


def is_event_row(row: dict) -> bool:
    """A charged event row. The sample row also says recordType "event"."""
    return (not row.get("_sample") and not row.get("_diagnostic")
            and row.get("recordType") == "event" and row.get("charged") is not False
            and bool(row.get("eventId")))


def _price(row: dict) -> str | None:
    if row.get("isFree") is True:
        return "Free"
    low, high, currency = row.get("minPrice"), row.get("maxPrice"), row.get("currency") or ""
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high > low:
        return f"{currency} {low:g}-{high:g}".strip()
    if isinstance(low, (int, float)):
        return f"{currency} {low:g}".strip()
    return None


def to_payload(row: dict) -> dict | None:
    url = web_url(row.get("url"))
    title = clean(row.get("name"), 300)
    zone = clean(row.get("timezone"), 60) or "Asia/Kolkata"
    starts = parse_iso(row.get("startsAt"), zone)
    if not (url and title and starts):
        return None
    ends = parse_iso(row.get("endsAt"), zone)
    online = row.get("isOnline")
    return {
        "event_id": str(row.get("eventId")),
        "url": url,
        "title": title,
        "starts_at": starts.isoformat(),
        "ends_at": ends.isoformat() if ends else None,
        "date_only": False,
        "timezone": zone,
        "venue": clean(row.get("venueName") or row.get("venueAddress"), 300) or None,
        "city": clean(row.get("city"), 120) or None,
        "country_code": (clean(row.get("country"), 4) or "").upper() or None,
        "online": online if isinstance(online, bool) else None,
        "organiser": clean(row.get("organizerName"), 300) or None,
        "price": _price(row),
        "description": clean(row.get("summary"), 1500) or None,
    }


@dataclass(frozen=True)
class EventbriteSource(Source):
    locations: tuple[str, ...] = ()
    keyword: str = ""
    max_items: int = 60

    def actor_input(self) -> dict:
        return {"location": list(self.locations), "keyword": self.keyword,
                "dateRange": "current_future", "maxItems": self.max_items}

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ceiling = (self.max_items * settings.eventbrite_price_per_event
                   + settings.eventbrite_price_per_run)
        try:
            rows = apify.run_actor(settings.apify_eventbrite_actor, self.actor_input(),
                                   max_items=self.max_items,
                                   max_charge_usd=min(settings.apify_max_charge_usd, ceiling))
        except apify.ApifyError as exc:
            raise SourceError(f"Eventbrite: {exc}") from None

        events = [r for r in rows if is_event_row(r)]
        cost = round(len(events) * settings.eventbrite_price_per_event
                     + settings.eventbrite_price_per_run, 6)
        problems = [r for r in rows if r.get("_diagnostic")
                    and str(r.get("errorCode")) != "NO_RESULTS"]
        warning = None
        if problems:
            codes = sorted({str(p.get("errorCode") or "error") for p in problems})
            said = clean(problems[0].get("error") or problems[0].get("message"), 160)
            warning = f"Eventbrite: {', '.join(codes)}" + (f" ({said})" if said else "")
            if not events:
                raise SourceError(warning, apify_usd=cost)

        result = FetchResult(apify_usd=cost, warning=warning)
        for row in events:
            payload = to_payload(row)
            if payload is None:
                continue
            result.items.append(RawItem(
                kind=EVENT, source_key=self.key, source_type=self.type, source_label=self.label,
                url=payload["url"], title=payload["title"], text=event_text(payload),
                published_at=None, payload=payload,
            ))
        return result
