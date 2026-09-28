"""Manual refresh: the "Refresh now" buttons on the feed and Insights pages.

  POST /api/refresh {"scope": "feed" | "vcs"}   start one in the background
  GET  /api/refresh                             is one running, and how it went

"feed" is the full refresh the scheduler runs (news, funding rounds, VC
firms); "vcs" reads the VC firms only. Both share the scheduler's lock, so a
manual run never overlaps a scheduled one, and a scope refreshed within
`manual_refresh_cooldown_minutes` is not refreshed again. A refused start is
an ordinary answer (`started: false` with the reason), not an error.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import scheduler

router = APIRouter(prefix="/api", tags=["refresh"])


class RefreshRequest(BaseModel):
    scope: str = "feed"


class RefreshStatus(BaseModel):
    scope: str | None
    running: bool
    started_at: datetime | None
    finished_at: datetime | None
    summary: str | None
    error: str | None


class RefreshStart(BaseModel):
    started: bool
    reason: str | None = None
    status: RefreshStatus


def _status() -> RefreshStatus:
    return RefreshStatus(**scheduler.manual_status().__dict__)


@router.get("/refresh", response_model=RefreshStatus)
def refresh_status() -> RefreshStatus:
    return _status()


@router.post("/refresh", response_model=RefreshStart)
def refresh_now(request: RefreshRequest) -> RefreshStart:
    reason = scheduler.start_manual(request.scope)
    return RefreshStart(started=reason is None, reason=reason, status=_status())
