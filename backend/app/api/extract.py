"""POST /api/extract -- a reader's URL plus a plain-words request.

The endpoint is thin: `app.extract` does the fetching, the guarding and the
metering. Problems a reader can fix, a spent budget and an unreachable model
all come back as HTTP 200 with an `error`, because each is an expected state
the dashboard renders, not a crash.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from .. import extract
from ..llm.bedrock import LlmUnavailable
from ..llm.budget import BudgetExceeded, get_spend

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["extract"])


class ExtractRequest(BaseModel):
    url: str
    prompt: str


class ExtractResponse(BaseModel):
    url: str
    final_url: str | None = None
    prompt: str
    result: object = None
    rows: list[dict] | None = None
    cost_usd: float = 0.0
    calls: int = 0
    page_chars: int = 0
    truncated: bool = False
    cached: bool = False
    seconds: float = 0.0
    notes: list[str] = []
    error: str | None = None
    budget_remaining: float = 0.0


@router.post("/extract", response_model=ExtractResponse)
def run_extract(request: ExtractRequest) -> ExtractResponse:
    try:
        done = extract.run(request.url, request.prompt)
    except extract.ExtractError as exc:
        return _failed(request, str(exc))
    except BudgetExceeded as exc:
        return _failed(request, f"{exc}. Nothing was spent.")
    except LlmUnavailable as exc:
        log.error("extract: %s", exc)
        return _failed(request, "The model is unreachable right now.")
    except Exception as exc:  # noqa: BLE001 - ScrapeGraphAI raises assorted types
        log.exception("extract failed for %s", request.url)
        return _failed(request, f"Extraction failed ({type(exc).__name__}).")

    return ExtractResponse(
        **done.__dict__,
        budget_remaining=round(get_spend().remaining_total, 6),
    )


def _failed(request: ExtractRequest, message: str) -> ExtractResponse:
    return ExtractResponse(
        url=request.url, prompt=request.prompt, error=message,
        budget_remaining=round(get_spend().remaining_total, 6),
    )
