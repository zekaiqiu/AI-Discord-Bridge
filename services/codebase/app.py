"""portfolio-codebase — 3D codebase map at codebase.ald3.com.

Routes (all auth-gated except /healthz):
  GET /healthz                — liveness, no auth.
  GET /api/codebase/scan      — fresh scan of the bind-mounted repos.
  GET /api/codebase/snapshot  — most recent cached scan (much cheaper than
                                /scan; the page polls this on interval).
  POST /api/codebase/refresh  — force a fresh scan and update the cache.
  GET /                       — Jinja shell hosting the three.js scene.

Auth: services.chat.auth.require_user (CF Access JWT verification, same
pattern as the spend service). The chat package is bind-mounted read-only
at /app/services/chat for shared truth.

Scan cache: in-process, refreshed lazily when older than CACHE_TTL_SECONDS.
The compose-level RW bind-mount of the three repos is :ro, so even if the
walker raced a write we'd only ever read partial state — never corrupt it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import scanner

logger = logging.getLogger("codebase")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


# Bind-mount imports — same convention as services/spend/app.py.
sys.path.insert(0, "/app")


def _import_chat_auth():
    from services.chat import auth as chat_auth  # type: ignore
    return chat_auth


# ---------------------------------------------------------------------------
# Cached scan.
#
# A full scan of three modest repos is cheap (~100ms locally) but we still
# don't want every page render to trigger one. Hold the latest result in
# memory; refresh when stale or on explicit POST /refresh.
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = int(os.environ.get("CODEBASE_CACHE_TTL", "300"))

_scan_lock = asyncio.Lock()
_scan_cache: dict[str, Any] | None = None
_scan_cache_at: float = 0.0


async def _ensure_scan() -> dict[str, Any]:
    global _scan_cache, _scan_cache_at
    now = time.time()
    if _scan_cache is not None and (now - _scan_cache_at) < CACHE_TTL_SECONDS:
        return _scan_cache
    async with _scan_lock:
        # Recheck after acquiring the lock — another coroutine may have just
        # populated the cache.
        if _scan_cache is not None and (time.time() - _scan_cache_at) < CACHE_TTL_SECONDS:
            return _scan_cache
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, scanner.scan_all, scanner.DEFAULT_REPOS)
        _scan_cache = data
        _scan_cache_at = time.time()
        return data


# ---------------------------------------------------------------------------
# App.
# ---------------------------------------------------------------------------
app = FastAPI(title="portfolio-codebase", version="0.1.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


async def require_user(request: Request) -> dict[str, Any]:
    chat_auth = _import_chat_auth()
    return await chat_auth.require_user(request)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "codebase"})


@app.get("/api/codebase/snapshot")
async def api_snapshot(_user: dict = Depends(require_user)) -> JSONResponse:
    data = await _ensure_scan()
    return JSONResponse(data)


@app.get("/api/codebase/scan")
async def api_scan(_user: dict = Depends(require_user)) -> JSONResponse:
    # Same as snapshot today, kept distinct so future versions can diverge
    # (e.g., snapshot returns a precomputed materialized view, scan always
    # walks from scratch).
    data = await _ensure_scan()
    return JSONResponse(data)


@app.post("/api/codebase/refresh")
async def api_refresh(_user: dict = Depends(require_user)) -> JSONResponse:
    global _scan_cache, _scan_cache_at
    async with _scan_lock:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, scanner.scan_all, scanner.DEFAULT_REPOS)
        _scan_cache = data
        _scan_cache_at = time.time()
    return JSONResponse({"ok": True, "scanned_at": _scan_cache_at})


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    _user: dict = Depends(require_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user_email": _user.get("email", "unknown"),
        },
    )
