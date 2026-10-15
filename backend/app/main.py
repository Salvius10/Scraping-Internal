"""FastAPI application.

    uvicorn app.main:app --reload --port 8000

Serves the built frontend from ../frontend/dist when it exists, so production
is a single process; in development the Vite dev server proxies here instead.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .api import (
    buckets, chat, events, export, extract, feed, filter as filter_api, insights,
    intelligence, linkedin, pasted, refresh, status, vcs,
)
from .config import BACKEND_DIR, settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    log.info("ready: db=%s region=%s cheap=%s",
             settings.db_path.name, settings.aws_region, settings.model_cheap)
    # The 12h refresh. Off in tests, and when ingest runs in its own process.
    if settings.scheduler_enabled:
        scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(
    title="GPS - Ganit Pursuit OS",
    version="0.1.0",
    description="Aggregated startup news with natural-language filtering.",
    lifespan=lifespan,
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
app.include_router(buckets.router)
app.include_router(insights.router)
app.include_router(vcs.router)
app.include_router(events.router)
app.include_router(pasted.router)
app.include_router(linkedin.router)
app.include_router(export.router)
app.include_router(status.router)
app.include_router(refresh.router)
app.include_router(filter_api.router)
app.include_router(chat.router)
app.include_router(intelligence.router)
app.include_router(extract.router)

# Headers every response carries. The pages show text and links taken from
# other sites, so the browser is told not to sniff types, not to be framed,
# and not to send our URLs on to the sites a reader clicks through to.
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]


class SecurityHeaders:
    """Plain ASGI middleware, so streamed downloads pass through untouched."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                message["headers"] = [*message.get("headers", []), *(
                    h for h in _SECURITY_HEADERS if h[0] not in present)]
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Log the traceback here; send the browser a plain message, never the details."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# --- Static frontend (production) -------------------------------------------

FRONTEND_DIST = (BACKEND_DIR.parent / "frontend" / "dist").resolve()


def _dist_file(full_path: str) -> Path | None:
    """The file under dist/ that `full_path` names, or None.

    The path comes from the URL, and "%2e%2e%2f" decodes to "../" before it
    gets here, so the resolved file must still sit inside dist/ -- otherwise
    `/..%2F..%2F.env` would serve the repo's secrets.
    """
    if not full_path:
        return None
    candidate = (FRONTEND_DIST / full_path).resolve()
    if not candidate.is_relative_to(FRONTEND_DIST) or not candidate.is_file():
        return None
    return candidate


if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        """Serve index.html for any non-API route (client-side routing)."""
        if full_path == "api" or full_path.startswith("api/"):
            # An unknown API route is a 404, not the app's HTML.
            raise HTTPException(status_code=404, detail="Not Found")
        if file := _dist_file(full_path):
            return FileResponse(file)
        # index.html names the hashed bundles of the current build. Without
        # this, browsers cache it heuristically and keep loading the previous
        # build -- whose bundles a rebuild has deleted -- after a deploy.
        return FileResponse(FRONTEND_DIST / "index.html",
                            headers={"Cache-Control": "no-cache"})
