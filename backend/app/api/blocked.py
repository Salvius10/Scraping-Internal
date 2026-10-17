"""The blocked / rejected log, read-only.

  GET /api/blocked?stage=&limit=   newest first

For checking what the guardrails and validation stopped, and why.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import BlockedItem
from ..pipeline.blocklog import STAGES
from .common import MAX_LIMIT

router = APIRouter(prefix="/api", tags=["blocked"])


class BlockedOut(BaseModel):
    id: int
    stage: str
    source_key: str
    url: str
    title: str
    reason: str
    created_at: datetime


@router.get("/blocked", response_model=list[BlockedOut])
def blocked(stage: str | None = Query(None), limit: int = Query(100, ge=1, le=MAX_LIMIT),
            session: Session = Depends(get_session)) -> list[BlockedOut]:
    if stage is not None and stage not in STAGES:
        raise HTTPException(status_code=400, detail=f"Unknown stage {stage!r}.")
    stmt = select(BlockedItem).order_by(BlockedItem.created_at.desc(), BlockedItem.id.desc())
    if stage:
        stmt = stmt.where(BlockedItem.stage == stage)
    return [BlockedOut(id=r.id, stage=r.stage, source_key=r.source_key, url=r.url,
                       title=r.title, reason=r.reason, created_at=r.created_at)
            for r in session.scalars(stmt.limit(limit)).all()]
