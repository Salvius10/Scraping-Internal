"""Luma city feeds, via Apify.

The actor (`apify_luma_actor`) reads Luma's discover page for each city and
returns every upcoming event as structured data -- name, times in UTC with the
timezone, venue, city, host, price -- so the LLM only has to judge whether an
event is startup-related. A keyword narrows each city's feed; the run is
capped by `max_items` and by the dollar ceiling.

Luma's Indian discover pages, checked 2026-10-16: bengaluru, mumbai and
new-delhi. Other Indian cities redirect to the global discover page.

Billed per event row; the sample row and diagnostic rows (an unknown city,
NO_RESULTS) are free.
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
    return not row.get("_sample") and not row.get("_diagnostic") and bool(row.get("eventId"))


def to_payload(row: dict) -> dict | None:
    """The fields the platform keeps, JSON-safe. None for an unusable row."""
    url = web_url(row.get("url"))
    title = clean(row.get("name"), 300)
    starts = parse_iso(row.get("startAt"))
    if not (url and title and starts):
        return None
    ends = parse_iso(row.get("endAt"))
    online = row.get("isOnline")
    price = None
    if row.get("isFree") is True:
        price = "Free"
    elif isinstance(row.get("price"), (int, float)):
        price = f"{row.get('priceCurrency') or ''} {row['price']:g}".strip()
    return {
        "event_id": str(row.get("eventId")),
        "url": url,
        "title": title,
        "starts_at": starts.isoformat(),
        "ends_at": ends.isoformat() if ends else None,
        "date_only": False,
        "timezone": clean(row.get("timezone"), 60) or None,
        "venue": clean(row.get("venueName") or row.get("addressShort"), 300) or None,
        "city": clean(row.get("city"), 120) or None,
        "country_code": (clean(row.get("countryCode"), 4) or "").upper() or None,
        "online": online if isinstance(online, bool) else None,
        "organiser": clean(row.get("hostName") or row.get("calendarName"), 300) or None,
        "price": price,
        "description": clean(row.get("description"), 1500) or None,
    }


@dataclass(frozen=True)
class LumaSource(Source):
    cities: tuple[str, ...] = ()
    keyword: str = ""
    max_items: int = 60

    def actor_input(self) -> dict:
        return {"cities": list(self.cities), "searchQuery": self.keyword,
                "maxItems": self.max_items, "includeDetails": True}

    def fetch(self, ctx: FetchContext) -> FetchResult:
        ceiling = self.max_items * settings.luma_price_per_event + settings.luma_price_per_run
        try:
            rows = apify.run_actor(settings.apify_luma_actor, self.actor_input(),
                                   max_items=self.max_items,
                                   max_charge_usd=min(settings.apify_max_charge_usd, ceiling))
        except apify.ApifyError as exc:
            raise SourceError(f"Luma: {exc}") from None

        events = [r for r in rows if is_event_row(r)]
        cost = round(len(events) * settings.luma_price_per_event + settings.luma_price_per_run, 6)
        problems = [r for r in rows if r.get("_diagnostic")
                    and str(r.get("errorCode")) != "NO_RESULTS"]
        warning = None
        if problems:
            codes = sorted({str(p.get("errorCode") or "error") for p in problems})
            said = clean(problems[0].get("error") or problems[0].get("message"), 160)
            warning = f"Luma: {', '.join(codes)}" + (f" ({said})" if said else "")
            if not events:
                raise SourceError(warning, apify_usd=cost)

        result = FetchResult(apify_usd=cost, warning=warning)
        for row in events:
            payload = to_payload(row)
            if payload is None:
                continue
            # A city feed can carry an event held elsewhere (online, or a
            # touring series); the India check happens in structuring.
            result.items.append(RawItem(
                kind=EVENT, source_key=self.key, source_type=self.type, source_label=self.label,
                url=payload["url"], title=payload["title"], text=event_text(payload),
                published_at=None, payload=payload,
            ))
        return result
