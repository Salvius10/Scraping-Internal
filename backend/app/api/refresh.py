"""Platform: Manual Refresh -- an on-demand run from the platform.

  POST /api/refresh      start one in the background
  GET  /api/refresh      is one running, and how it went

The same run the scheduler starts. It shares the scheduler's lock, so a manual
run never overlaps a scheduled one, and a run started within
`manual_refresh_cooldown_minutes` is not started again. A refused start is an
ordinary answer (`started: false` with the reason), not an error.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from .. import scheduler

router = APIRouter(prefix="/api", tags=["refresh"])


class RefreshStatus(BaseModel):
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
def refresh_now() -> RefreshStart:
    reason = scheduler.start_manual()
    return RefreshStart(started=reason is None, reason=reason, status=_status())
