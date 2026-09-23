"""FastAPI application.

    uvicorn app.main:app --reload --port 8000

Serves the built frontend from ../frontend/dist when it exists, so production
is a single process; in development the Vite dev server proxies here instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import chat, feed, filter as filter_api, status
from .config import BACKEND_DIR, settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="India Startup Ecosystem Dashboard",
    version="0.1.0",
    description="Aggregated startup news with natural-language filtering.",
)

# The Vite dev server runs on another port during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(feed.router)
app.include_router(status.router)
app.include_router(filter_api.router)
app.include_router(chat.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    logging.getLogger(__name__).info(
        "ready: db=%s region=%s cheap=%s",
        settings.db_path.name, settings.aws_region, settings.model_cheap,
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# --- Static frontend (production) -------------------------------------------

FRONTEND_DIST = BACKEND_DIR.parent / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        """Serve index.html for any non-API route (client-side routing)."""
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
