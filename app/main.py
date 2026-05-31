"""CryFi — FastAPI application entrypoint.

A local GUI over the aircrack-ng suite for *authorized* network auditing.
Mounts API routers and the static frontend.
"""
from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .core import auth as auth_core
from .core.process_manager import manager
from .routers import (
    auth, crack, execution, files, handshakes, interfaces, regulatory, scan, wordlist_gen,
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: read the regulatory domain so we know what's allowed where, and
    # log a quick summary (transmit-blocked channels) for operator visibility.
    with contextlib.suppress(Exception):
        info = await regulatory.regulatory()
        blocked = info.get("tx_blocked_channels") or []
        logging.getLogger("uvicorn.error").info(
            "Regulatory domain: %s%s — transmit (deauth) blocked on channels: %s",
            info.get("country", "??"),
            " [self-managed]" if info.get("self_managed") else "",
            ", ".join(map(str, blocked)) if blocked else "none",
        )
    yield
    # Shutdown: ensure no aircrack processes survive the server.
    await manager.stop_all()


app = FastAPI(title="CryFi", version="1.0.0", lifespan=lifespan)


# Paths reachable without a session.
_AUTH_PUBLIC = {"/login", "/api/login", "/api/logout", "/api/me", "/api/health", "/favicon.ico"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Require a valid session for everything except the public paths/static."""
    path = request.url.path
    public = (
        request.method == "OPTIONS"
        or path in _AUTH_PUBLIC
        or path.startswith("/static")
    )
    if public or auth_core.valid_session(request.cookies.get(auth_core.COOKIE_NAME)):
        return await call_next(request)
    if path.startswith("/api") or path.startswith("/ws"):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return RedirectResponse("/login")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Force browsers to revalidate the UI assets every load.

    StaticFiles ships an ETag/Last-Modified but no Cache-Control, so browsers
    happily serve stale JS/CSS after a rebuild. ``no-cache`` keeps the 304
    fast-path but guarantees the client never runs an outdated bundle.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# API routers
app.include_router(auth.router)
app.include_router(interfaces.router)
app.include_router(regulatory.router)
app.include_router(scan.router)
app.include_router(execution.router)
app.include_router(files.router)
app.include_router(crack.router)
app.include_router(handshakes.router)
app.include_router(wordlist_gen.router)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "use_sudo": config.USE_SUDO}


@app.get("/login")
async def login_page():
    return FileResponse(config.STATIC_DIR / "login.html")


@app.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")


# Static assets (css/js). Mounted last so it doesn't shadow API routes.
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
