"""One readable text per structured event, shared by the event portals.

It is what the guardrails screen and the LLM reads, and -- through the content
hash -- what tells the seen-items index that a known event has changed (moved
to another day or venue).
"""

from __future__ import annotations

from ..timeutil import IST, parse_iso


def _when(payload: dict) -> str:
    starts = parse_iso(payload.get("starts_at"))
    if starts is None:
        return "unknown"
    local = starts.astimezone(IST)
    text = local.strftime("%d %b %Y") if payload.get("date_only") else \
        local.strftime("%d %b %Y, %H:%M IST")
    ends = parse_iso(payload.get("ends_at"))
    if ends is not None:
        text += " to " + ends.astimezone(IST).strftime("%d %b %Y, %H:%M IST")
    return text


def event_text(payload: dict) -> str:
    where = ", ".join(p for p in (payload.get("venue"), payload.get("city"),
                                  payload.get("country_code")) if p)
    if payload.get("online"):
        where = f"Online{f' ({where})' if where else ''}"
    lines = [
        f"When: {_when(payload)}",
        f"Where: {where or 'not given'}",
        f"Organiser: {payload.get('organiser') or 'not given'}",
    ]
    if payload.get("price"):
        lines.append(f"Price: {payload['price']}")
    if payload.get("description"):
        lines.append(payload["description"])
    return "\n".join(lines)
