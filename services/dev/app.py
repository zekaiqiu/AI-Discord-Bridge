"""dev-wizerith — browser Python IDE backend.

Sits at dev.wizerith.ai. Cloudflare Access stamps every request with a
JWT; we verify it (same JWKS / AUD as chat.wizerith.ai), map the email to
a per-user container provisioned by services.chat.user_container, and
expose:

  GET    /healthz                       — liveness
  GET    /api/me                        — { email, role }
  GET    /api/state                     — load IDE state from user's volume
  PUT    /api/state                     — save IDE state
  GET    /api/files?path=               — list a directory
  GET    /api/files/read?path=          — read a file
  PUT    /api/files/write               — atomic write
  POST   /api/files/mkdir               — mkdir -p
  POST   /api/files/rename              — atomic rename
  DELETE /api/files?path=               — rm -rf
  POST   /api/jobs/run                  — start a `python path` job; returns job_id
  GET    /api/jobs/{job_id}/stream      — SSE: replays buffer + streams live
  GET    /api/jobs                      — list active+recent jobs for this user
  DELETE /api/jobs/{job_id}             — kill a running job

All paths under /api require a valid CF Access JWT. The frontend SPA is
served from /static and / (index.html). The static bundle lives in
/app/static after the Dockerfile's COPY --from=frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import pathlib
import re
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Bind-mounted at /app/services/chat — see Dockerfile + docker-compose.yml.
from services.chat import auth as chat_auth
from services.chat import user_container as chat_user_container

# AUTH_MODE=local switches dev.ald3.com onto the cookie-JWT path issued by
# the chat container's local_auth (same .ald3.com cookie). Unset (the
# default, dev-wizerith) keeps the legacy CF Access flow below. This is the
# only place the switch shows up — the rest of require_user is unchanged.
_AUTH_MODE = os.environ.get("AUTH_MODE", "").strip().lower()
if _AUTH_MODE == "local":
    from services.chat import local_auth as chat_local_auth  # noqa: F401
else:
    chat_local_auth = None  # type: ignore[assignment]

import docker
import docker_exec
import file_ops
import lsp_bridge
import projects as projects_mod
import pty_bridge
import state as state_mod


logger = logging.getLogger("dev-wizerith")
logging.basicConfig(level=logging.INFO)

STATIC_DIR = pathlib.Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Auth dependency.
#
# Mirrors services.chat.auth.require_user but: (a) reuses the PyJWT path
# (verify_cf_access_jwt + email_from_claims) since dev-wizerith was built
# after the PyJWT migration and (b) caches the resolved {email, role} on
# request.state so handlers can skip a second pass.
# ---------------------------------------------------------------------------

CF_JWT_HEADER = chat_auth.JWT_HEADER_NAME
CF_JWT_COOKIE = "CF_Authorization"


async def require_user(request: Request) -> dict:
    # Local-auth path (chat.ald3.com + dev.ald3.com tenant). Reads the
    # shared .ald3.com cookie issued by chat's local_auth.set_auth_cookie.
    # Falls through to the CF Access path below when AUTH_MODE is unset
    # (dev-wizerith never enters this branch).
    if chat_local_auth is not None:
        token = request.cookies.get(chat_local_auth.COOKIE_NAME)
        if not token:
            authz = request.headers.get("Authorization", "")
            if authz.startswith("Bearer "):
                token = authz[len("Bearer "):].strip()
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="not authenticated",
            )
        # verify_jwt_token raises 401/403 itself on bad signature / non-
        # allowlisted email; let those propagate untouched so the client
        # gets the precise status code.
        claims = chat_local_auth.verify_jwt_token(token)
        email = claims["email"]
        role = chat_auth.resolve_role(email)
        workspace = _resolve_workspace(request)
        request.state.user_email = email
        request.state.user_role = role
        request.state.workspace = workspace
        return {"email": email, "role": role, "claims": claims, "workspace": workspace}

    token = request.headers.get(CF_JWT_HEADER) or request.cookies.get(CF_JWT_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing CF Access JWT",
        )
    try:
        claims = chat_auth.verify_cf_access_jwt(token)
        email = chat_auth.email_from_claims(claims)
    except chat_auth.JWTVerificationError as exc:
        # Log the reason locally — the surface is generic 401 to the client.
        logger.warning("JWT verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid CF Access JWT",
        )
    role = chat_auth.resolve_role(email)
    workspace = _resolve_workspace(request)
    request.state.user_email = email
    request.state.user_role = role
    request.state.workspace = workspace
    return {"email": email, "role": role, "claims": claims, "workspace": workspace}


async def _ws_authenticate(websocket: WebSocket) -> str | None:
    """Resolve the user's email for a WebSocket handshake, or close the
    socket and return None on failure.

    Mirrors `require_user`'s dual-path logic: local-auth cookie when
    AUTH_MODE=local (dev-wizerith, dev.ald3.com), CF Access JWT otherwise.
    Without this the WS endpoints below only check CF_Authorization and
    silently reject every local-auth handshake with 1008 / 403.
    """
    if chat_local_auth is not None:
        token = websocket.cookies.get(chat_local_auth.COOKIE_NAME)
        if not token:
            authz = websocket.headers.get("Authorization", "")
            if authz.startswith("Bearer "):
                token = authz[len("Bearer "):].strip()
        if not token:
            await websocket.close(code=1008, reason="missing auth cookie")
            return None
        try:
            claims = chat_local_auth.verify_jwt_token(token)
        except HTTPException as exc:
            logger.warning("WS local-auth failed: %s", exc.detail)
            await websocket.close(code=1008, reason="invalid auth")
            return None
        return claims["email"]

    token = (
        websocket.headers.get(CF_JWT_HEADER)
        or websocket.cookies.get(CF_JWT_COOKIE)
    )
    if not token:
        await websocket.close(code=1008, reason="missing CF Access JWT")
        return None
    try:
        claims = chat_auth.verify_cf_access_jwt(token)
        return chat_auth.email_from_claims(claims)
    except chat_auth.JWTVerificationError as exc:
        logger.warning("WS CF Access auth failed: %s", exc)
        await websocket.close(code=1008, reason="invalid CF Access JWT")
        return None


# ---------------------------------------------------------------------------
# Container resolution.
#
# Idempotent: chat may have provisioned it already; if so we get the same
# name back at zero cost. If it's gone (e.g. the operator `docker rm -f`'d
# it like I did the day FRED_API_KEY landed), this reprovisions on the
# fly — same auto-heal pattern as chat's app.py resume path.
# ---------------------------------------------------------------------------

# Cache key = (email, workspace) — same user has at most one personal
# container + one shared container, and both can be open simultaneously
# (the user's chat thread may be in shared mode while their IDE pane is
# in personal mode, or vice-versa). Cache them independently.
_ENSURE_CONTAINER_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_ENSURE_CONTAINER_TTL_S = 300.0


def _ensure_container_for(email: str, workspace: str = "personal") -> str:
    """Cached wrapper around chat_user_container.ensure_user_container /
    ensure_shared_container.

    The upstream call (`docker volume inspect/create` → `docker network inspect/create` →
    `docker container inspect` + idempotent setup) costs ~700ms per request even when
    the container has been up for days. The provisioning is deterministic-by-email
    (personal) or process-global (shared), so we memoise per-process for
    `_ENSURE_CONTAINER_TTL_S`. Cache misses + first call after process boot pay the
    full cost; subsequent ops within the same worker get the name for ~0 ms.

    Workspace routing:
    - "personal" (default) → `chat_user_container.ensure_user_container(email)`
    - "shared" → `chat_user_container.ensure_shared_container()` — same container
      for every user on the tenant. Refused with 400 if the tenant didn't set
      WIZERITH_SHARED_CONTAINER_NAME.

    Invalidation:
    - Time: TTL above caps staleness if the container is somehow destroyed externally.
    - On demand: `_invalidate_container_cache(email, workspace)` — called by the
      auto-heal retry path in `_wrap_fileop` when a docker exec fails with
      "No such container".
    """
    import time
    if workspace not in ("personal", "shared"):
        workspace = "personal"
    key = (email, workspace)
    cached = _ENSURE_CONTAINER_CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[1] < _ENSURE_CONTAINER_TTL_S:
        return cached[0]
    try:
        if workspace == "shared":
            name = chat_user_container.ensure_shared_container()
        else:
            name = chat_user_container.ensure_user_container(email)
    except getattr(chat_user_container, "SharedContainerNotConfigured", tuple()) as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="shared workspace is not enabled on this tenant",
        ) from exc
    except Exception as exc:
        logger.error("ensure container (email=%s workspace=%s) failed: %s", email, workspace, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="container provisioning failed",
        ) from exc
    _ENSURE_CONTAINER_CACHE[key] = (name, now)
    return name


def _ensure_container_for_user(user: dict) -> str:
    """Resolve the container name for the user dict produced by `require_user`.

    Convenience wrapper that picks up the `workspace` field the dependency
    stamps on (cookie / query → `personal` default).
    """
    return _ensure_container_for(user["email"], user.get("workspace", "personal"))


def _invalidate_container_cache(email: str, workspace: str = "personal") -> None:
    """Drop the cached container name and tear down any persistent exec
    channel pointing at the (about-to-be-stale) container name."""
    if workspace not in ("personal", "shared"):
        workspace = "personal"
    entry = _ENSURE_CONTAINER_CACHE.pop((email, workspace), None)
    if entry is not None:
        try:
            import exec_channel
            exec_channel.drop_channel(entry[0])
        except Exception:  # noqa: BLE001
            pass


def _resolve_workspace(request: Request) -> str:
    """Pick the workspace the request targets: query > cookie > 'personal'.

    The chat → dev deep-link sends `?workspace=...`, and the dev SPA's
    own toggle writes `chat_workspace=...` as a Domain=.<apex> cookie
    so a fresh browser tab inherits the toggle without a deep-link.

    `wizerith_workspace` is the legacy cookie name from when this code
    was wizerith-specific; still read here so existing sessions keep
    working while the new bundle propagates. Safe to remove after a
    couple of months.
    """
    q = request.query_params.get("workspace")
    if q in ("personal", "shared"):
        return q
    c = request.cookies.get("chat_workspace") or request.cookies.get("wizerith_workspace")
    if c in ("personal", "shared"):
        return c
    return "personal"


# ---------------------------------------------------------------------------
# App + routes.
# ---------------------------------------------------------------------------

app = FastAPI(title="dev-wizerith", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "dev-wizerith"}


@app.get("/api/debug-headers")
async def debug_headers(request: Request) -> dict:
    """Diagnostic: surface which CF-prefixed headers and cookies are arriving.

    Intentionally unauthenticated — the whole point is to diagnose auth
    failures. Returns only header *names* (not values, except for the CF
    Ray / Ipcountry which are publicly visible anyway). Cookies returned
    as names only. Strip this once auth is working.
    """
    cf_headers: dict[str, str] = {}
    other_headers: list[str] = []
    for name, value in request.headers.items():
        n = name.lower()
        if n.startswith("cf-") or n in ("x-forwarded-for", "x-forwarded-proto", "host"):
            # Mask the JWT value (long string) but show it's present.
            if "jwt" in n or "authorization" in n:
                cf_headers[name] = f"<present, {len(value)} chars>"
            else:
                cf_headers[name] = value
        else:
            other_headers.append(name)
    cookie_names = list(request.cookies.keys())
    return {
        "cf_and_proxy_headers": cf_headers,
        "other_header_names": sorted(other_headers),
        "cookie_names": cookie_names,
        "has_cf_access_jwt_header": "cf-access-jwt-assertion" in {h.lower() for h in request.headers.keys()},
        "has_cf_authorization_cookie": "CF_Authorization" in request.cookies,
    }


@app.get("/api/me")
async def api_me(user: dict = Depends(require_user)) -> dict:
    return {"email": user["email"], "role": user["role"]}


# --- IDE state ----------------------------------------------------------


class StatePayload(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/state")
async def api_state_get(user: dict = Depends(require_user)) -> dict:
    container = _ensure_container_for_user(user)
    try:
        return {"state": state_mod.load(container)}
    except docker_exec.DockerExecError as exc:
        raise HTTPException(status_code=500, detail=f"state load failed: {exc.stderr[:200]}")


@app.put("/api/state")
async def api_state_put(payload: StatePayload, user: dict = Depends(require_user)) -> dict:
    container = _ensure_container_for_user(user)
    try:
        state_mod.save(container, payload.state)
    except docker_exec.DockerExecError as exc:
        raise HTTPException(status_code=500, detail=f"state save failed: {exc.stderr[:200]}")
    return {"ok": True}


# --- Files --------------------------------------------------------------


def _wrap_fileop(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


def _do_user_fileop(user: dict, fn, *args, **kwargs):
    """Resolve the user's container, call fn(container, *args, **kwargs), retry once on stale-cache.

    The container name is memoised in `_ENSURE_CONTAINER_CACHE`. If the operator
    nukes a per-user container while a browser session is open, the first docker
    exec after that returns "No such container" — we invalidate the cache,
    re-provision, and retry once before bubbling the error.
    """
    container, result = _do_user_fileop_with_container(user, fn, *args, **kwargs)
    return result


def _do_user_fileop_with_container(user: dict, fn, *args, **kwargs):
    """Same as _do_user_fileop but returns (container, result) — used by mutating
    endpoints that need the container name to invalidate listing caches after.
    """
    email = user["email"]
    workspace = user.get("workspace", "personal")
    container = _ensure_container_for(email, workspace)
    try:
        return container, _wrap_fileop(fn, container, *args, **kwargs)
    except HTTPException as http_exc:
        if http_exc.status_code == 500 and "no such container" in str(http_exc.detail).lower():
            _invalidate_container_cache(email, workspace)
            container = _ensure_container_for(email, workspace)
            return container, _wrap_fileop(fn, container, *args, **kwargs)
        raise


@app.get("/api/files")
async def api_files_list(path: str = "", user: dict = Depends(require_user)) -> dict:
    return _do_user_fileop(user, file_ops.list_dir, path)


@app.get("/api/files/read")
async def api_files_read(path: str, user: dict = Depends(require_user)) -> dict:
    return _do_user_fileop(user, file_ops.read_file, path)


# Binary read path for non-text viewers (images, PDFs, spreadsheets, media).
# Content-Type is guessed from extension via stdlib `mimetypes`; falls back to
# application/octet-stream. Cached for an hour client-side — workspace files
# can change, but the same user editing in the same session won't notice the
# stale window; viewer remounts on tab-reopen via a cache-busting query param
# so explicit "reload after change" works.
@app.get("/api/files/raw")
async def api_files_raw(path: str, user: dict = Depends(require_user)) -> Response:
    container = _ensure_container_for_user(user)
    try:
        data, _size = await asyncio.to_thread(
            file_ops.read_file_bytes, container, path
        )
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    name = path.rsplit("/", 1)[-1]
    media_type, _enc = mimetypes.guess_type(name)
    if not media_type:
        media_type = "application/octet-stream"
    # For archive/binary types, force the browser to save without any
    # transformation by setting Content-Disposition: attachment. Inline
    # display (PDF / image / video) keeps the default inline behavior so
    # the in-IDE viewers still embed them.
    inline_types = (
        "image/", "video/", "audio/", "application/pdf",
        "text/", "application/json", "application/xml",
    )
    is_inline = any(media_type.startswith(p) for p in inline_types)
    headers: dict[str, str] = {"Cache-Control": "private, max-age=0, must-revalidate"}
    if not is_inline:
        # RFC 5987 encoding so non-ASCII filenames survive the header.
        # Legacy `filename=` is ASCII-only — strip anything outside printable
        # ASCII and escape `\` / `"` so the quoted-string form stays valid.
        from urllib.parse import quote
        safe_unicode = quote(name, safe="")
        legacy = "".join(c for c in name if 32 <= ord(c) < 127 and c not in ('"', "\\")) or "download"
        headers["Content-Disposition"] = (
            f'attachment; filename="{legacy}"; filename*=UTF-8\'\'{safe_unicode}'
        )
    return Response(
        content=data,
        media_type=media_type,
        headers=headers,
    )


class WriteBody(BaseModel):
    path: str
    content: str


@app.put("/api/files/write")
async def api_files_write(body: WriteBody, user: dict = Depends(require_user)) -> dict:
    container, result = _do_user_fileop_with_container(user, file_ops.write_file, body.path, body.content)
    file_ops.invalidate_listing(container, body.path)
    return result


class MkdirBody(BaseModel):
    path: str


@app.post("/api/files/mkdir")
async def api_files_mkdir(body: MkdirBody, user: dict = Depends(require_user)) -> dict:
    container, result = _do_user_fileop_with_container(user, file_ops.mkdir, body.path)
    file_ops.invalidate_listing(container, body.path)
    return result


class RenameBody(BaseModel):
    src: str
    dst: str


@app.post("/api/files/rename")
async def api_files_rename(body: RenameBody, user: dict = Depends(require_user)) -> dict:
    container, result = _do_user_fileop_with_container(user, file_ops.rename, body.src, body.dst)
    file_ops.invalidate_listing(container, body.src)
    file_ops.invalidate_listing(container, body.dst)
    return result


class CopyBody(BaseModel):
    src: str
    dst: str


@app.post("/api/files/copy")
async def api_files_copy(body: CopyBody, user: dict = Depends(require_user)) -> dict:
    container, result = _do_user_fileop_with_container(user, file_ops.copy, body.src, body.dst)
    file_ops.invalidate_listing(container, body.dst)
    return result


@app.delete("/api/files")
async def api_files_delete(path: str, user: dict = Depends(require_user)) -> dict:
    container, result = _do_user_fileop_with_container(user, file_ops.delete, path)
    file_ops.invalidate_listing(container, path)
    return result


class CompressBody(BaseModel):
    path: str


@app.post("/api/files/compress")
async def api_files_compress(body: CompressBody, user: dict = Depends(require_user)) -> dict:
    """Start an async compress job. Returns {id, src} immediately.

    Progress + completion are streamed over `GET /api/files/compress/{id}/stream`
    (SSE). The synchronous return is only metadata — listing invalidation
    happens once the SSE stream emits a terminal event.
    """
    email = user["email"]
    container = _ensure_container_for_user(user)
    try:
        job = file_ops.start_compress_job(email=email, container=container, src=body.path)
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"id": job.id, "src": job.src, "started_at": job.started_at}


@app.get("/api/files/compress/{job_id}/stream")
async def api_files_compress_stream(job_id: str, user: dict = Depends(require_user)) -> StreamingResponse:
    job = file_ops.compress_registry.get(job_id)
    if not job or job.email != user["email"]:
        raise HTTPException(status_code=404, detail="compress job not found")

    q = file_ops.subscribe_compress(job)

    async def event_stream():
        # Replay buffer first so a reconnecting subscriber sees full history.
        for evt in list(job.events):
            yield _sse(evt.get("event", "message"), evt)
        # If the job's already finished, close out — no point waiting for
        # the sentinel.
        if job.status != "running":
            file_ops.unsubscribe_compress(job, q)
            # Final snapshot so the client has the canonical state shape.
            yield _sse("snapshot", file_ops.compress_result_payload(job))
            # On `done`, also invalidate the listing cache so the next
            # /api/files call sees the new .zip.
            if job.status == "done" and job.dst:
                file_ops.invalidate_listing(job.container, job.dst)
            return
        try:
            while True:
                evt = await q.get()
                if evt is None:
                    break
                yield _sse(evt.get("event", "message"), evt)
            yield _sse("snapshot", file_ops.compress_result_payload(job))
            if job.status == "done" and job.dst:
                file_ops.invalidate_listing(job.container, job.dst)
        finally:
            file_ops.unsubscribe_compress(job, q)

    # `X-Accel-Buffering: no` disables intermediate buffering at any nginx-
    # style proxy (CF tunnel respects this), so progress events stream live
    # rather than landing all at once when the job finishes.
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache, no-transform"},
    )


@app.get("/api/files/search")
async def api_files_search(
    q: str, root: str = "/",
    user: dict = Depends(require_user),
) -> dict:
    """File-name search rooted at `root` (default `/`).

    Wraps file_ops.search — see there for caps + prune clauses.
    """
    return _do_user_fileop(user, file_ops.search, q, root)


# ---------------------------------------------------------------------------
# Drive-only endpoints (Recent / Starred / Trash) for drive.wizerith.ai.
#
# These all operate on the user's own /workspace tree — same container
# as /api/files. State (starred manifest, trash payloads) lives under
# /workspace/.drive-meta and /workspace/.drive-trash, both filtered out of
# normal listings via file_ops.HIDDEN_TOP_LEVEL.
# ---------------------------------------------------------------------------


@app.get("/api/files/recent")
async def api_files_recent(
    limit: int = 100,
    max_age_days: int = 30,
    user: dict = Depends(require_user),
) -> dict:
    """Workspace-wide list of most-recently-modified regular files."""
    return _do_user_fileop(user, file_ops.list_recent, limit, max_age_days)


@app.get("/api/files/starred")
async def api_files_starred(user: dict = Depends(require_user)) -> dict:
    """Items the user has pinned via the drive UI. Missing paths are
    silently evicted from the manifest so the view stays clean as the
    underlying files come and go."""
    return _do_user_fileop(user, file_ops.list_starred)


class StarBody(BaseModel):
    path: str


@app.post("/api/files/star")
async def api_files_star(body: StarBody, user: dict = Depends(require_user)) -> dict:
    return _do_user_fileop(user, file_ops.star, body.path)


@app.post("/api/files/unstar")
async def api_files_unstar(body: StarBody, user: dict = Depends(require_user)) -> dict:
    return _do_user_fileop(user, file_ops.unstar, body.path)


class TrashBody(BaseModel):
    path: str


@app.post("/api/files/trash")
async def api_files_trash(body: TrashBody, user: dict = Depends(require_user)) -> dict:
    """Soft-delete: move the path into /workspace/.drive-trash. The IDE's
    DELETE /api/files endpoint stays as a hard delete (used by the in-IDE
    file tree); drive's UI should call this instead so users can restore."""
    container, result = _do_user_fileop_with_container(user, file_ops.trash, body.path)
    # The source path is gone from its parent directory now, so any cached
    # listing of that parent must be evicted.
    file_ops.invalidate_listing(container, body.path)
    return result


@app.get("/api/files/trash")
async def api_files_trash_list(user: dict = Depends(require_user)) -> dict:
    return _do_user_fileop(user, file_ops.list_trash)


class TrashIdBody(BaseModel):
    trash_id: str


@app.post("/api/files/trash/restore")
async def api_files_trash_restore(
    body: TrashIdBody, user: dict = Depends(require_user),
) -> dict:
    """Move a trashed item back to its original_path. If that path is now
    occupied, the item is restored with a ` (restored N)` suffix."""
    container, result = _do_user_fileop_with_container(user, file_ops.restore_from_trash, body.trash_id)
    # Restoration writes to the original parent — invalidate so the next
    # listing of the destination folder sees it.
    if result.get("restored_path"):
        file_ops.invalidate_listing(container, result["restored_path"])
    return result


@app.delete("/api/files/trash/item")
async def api_files_trash_purge(
    trash_id: str, user: dict = Depends(require_user),
) -> dict:
    """Permanently delete a single trash item."""
    return _do_user_fileop(user, file_ops.purge_trash_item, trash_id)


@app.delete("/api/files/trash")
async def api_files_trash_empty(user: dict = Depends(require_user)) -> dict:
    """Permanently delete every item in the trash."""
    return _do_user_fileop(user, file_ops.empty_trash)


@app.post("/api/files/upload")
async def api_files_upload(
    request: Request,
    path: str = Form(""),
    file: UploadFile = File(...),
    user: dict = Depends(require_user),
) -> dict:
    """Upload a single file into <path>/<file.filename>.

    Buffered fully into memory (capped at file_ops.MAX_UPLOAD_BYTES = 100 MB)
    — fine for typical code / config / dataset uploads in an IDE. Multi-GB
    research datasets should still use term.wizerith.ai's chunked
    upload pipeline. Returns the canonical /workspace-relative path.

    `path` is the destination directory relative to /workspace and defaults
    to the workspace root. Starlette 1.0 treats an empty-string Form field
    as "missing" when the field is required, so we default to "" rather
    than Form(...) — the frontend always sends a `path` part, sometimes
    empty (root upload), sometimes a subdir (folder drop).
    """
    # Defense layer 1 — Content-Length pre-check. Reject obviously oversized
    # uploads before we buffer/spool the body. Multipart overhead for one
    # file part is a few hundred bytes; allow 64 KB headroom. A malicious
    # client that lies about Content-Length still hits the head -c cap in
    # the shell script below (defense layer 2 — the authoritative limit).
    cl_header = request.headers.get("content-length")
    if cl_header and cl_header.isdigit() and int(cl_header) > file_ops.MAX_UPLOAD_BYTES + 64 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds max upload size of {file_ops.MAX_UPLOAD_BYTES} bytes",
        )

    container = _ensure_container_for_user(user)

    # Filename sanitization. Strip any directory components — a drop-from-OS
    # filename like "subA/subB/foo.txt" should land at `<destDir>/foo.txt`,
    # not auto-create subA/subB. Drop "." and ".." (which posixpath.basename
    # leaves intact for those inputs).
    import posixpath as _pp
    raw_name = (file.filename or "").strip()
    safe_name = _pp.basename(raw_name)
    if safe_name in ("", ".", ".."):
        safe_name = "uploaded"

    dest_rel = _pp.join(path.lstrip("/"), safe_name)
    # _normalize raises FileOpError on path traversal / NUL / absolute paths;
    # surface a clean 4xx instead of letting it bubble up as a 500.
    try:
        abs_path = file_ops._normalize(dest_rel)
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    if abs_path == "/workspace":
        raise HTTPException(status_code=400, detail="cannot upload to the workspace root itself")

    import secrets
    parent = _pp.dirname(abs_path) or "/workspace"
    # Per-upload random suffix so concurrent uploads of the same destination
    # filename can't trample each other's temp files. A crashed upload
    # leaves an orphan tmp the user can rm via the terminal.
    tmp_path = f"{abs_path}.tmp.dev-wizerith.{secrets.token_hex(4)}"

    # Authoritative size cap: read at most MAX+1 bytes via `head -c`. If the
    # input is larger, the file ends up MAX+1 bytes — we detect that, delete
    # the tmp, and exit 2 → mapped to a 413 below. This is the layer that
    # protects against a malicious client lying about Content-Length.
    max_bytes = file_ops.MAX_UPLOAD_BYTES
    shquote = file_ops.shquote
    script = (
        f"set -e\n"
        f"mkdir -p {shquote(parent)}\n"
        f"head -c {max_bytes + 1} > {shquote(tmp_path)}\n"
        f"size=$(wc -c < {shquote(tmp_path)})\n"
        f'if [ "$size" -gt {max_bytes} ]; then\n'
        f"  rm -f {shquote(tmp_path)}\n"
        f"  echo 'file exceeds max upload size of {max_bytes} bytes' >&2\n"
        f"  exit 2\n"
        f"fi\n"
        f"mv {shquote(tmp_path)} {shquote(abs_path)}\n"
        f'printf "%s" "$size"\n'
    )

    # Stream from the UploadFile's underlying SpooledTemporaryFile straight
    # into `docker exec`'s stdin. Rollover() forces the spool to disk so it
    # has a real fd Popen can dup2 (small uploads stay in-memory until
    # rollover; this just nudges them onto disk). No Python-side copy of
    # the full payload — the OS handles the fd-to-fd transfer.
    #
    # Wrapped in asyncio.to_thread so the (blocking) subprocess.run call
    # doesn't stall the event loop for the ~3s a 100 MB upload takes —
    # other requests on this worker (terminal SSE, file list, etc.) keep
    # flowing in parallel.
    file.file.rollover()
    file.file.seek(0)
    try:
        rc, stdout, stderr = await asyncio.to_thread(
            docker_exec.run_exec_streaming,
            container,
            ["sh", "-c", script],
            stdin_fileobj=file.file,
            timeout=300.0,
        )
    except docker_exec.DockerExecError as exc:
        # Only TimeoutExpired raises here; other failures come back via rc.
        raise HTTPException(status_code=504, detail=f"upload timed out: {exc.stderr[:200]}")

    if rc == 2:
        # head -c oversize trip.
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds max upload size of {file_ops.MAX_UPLOAD_BYTES} bytes",
        )
    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail=f"upload failed: {stderr[:200].decode('utf-8', errors='replace')}",
        )

    try:
        size = int(stdout.strip() or 0)
    except ValueError:
        size = 0
    rel = abs_path[len("/workspace") + 1:] if abs_path.startswith("/workspace/") else abs_path
    # Drop cached listing for the destination dir so the new file shows up on
    # the next tree expansion without waiting out the 5 s TTL.
    file_ops.invalidate_listing(container, rel)
    return {"path": rel, "size": size}


@app.post("/api/files/upload-raw")
async def api_files_upload_raw(
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    # Alternate single-file upload that takes the file body as the raw POST
    # body (application/octet-stream) with destination + name in headers,
    # rather than multipart/form-data. Cloudflare Access on dev.wizerith.ai
    # rejects authenticated multipart POSTs ("Incoming request ended abruptly"
    # at the tunnel before caddy ever sees it); a raw octet-stream body
    # traverses the same path fine.
    cl_header = request.headers.get("content-length")
    if cl_header and cl_header.isdigit() and int(cl_header) > file_ops.MAX_UPLOAD_BYTES + 64 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds max upload size of {file_ops.MAX_UPLOAD_BYTES} bytes",
        )

    container = _ensure_container_for_user(user)

    import posixpath as _pp
    import urllib.parse as _up
    raw_name = _up.unquote((request.headers.get("x-filename") or "").strip())
    safe_name = _pp.basename(raw_name)
    if safe_name in ("", ".", ".."):
        safe_name = "uploaded"
    path = _up.unquote(request.headers.get("x-upload-path", "") or "")

    dest_rel = _pp.join(path.lstrip("/"), safe_name)
    try:
        abs_path = file_ops._normalize(dest_rel)
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    if abs_path == "/workspace":
        raise HTTPException(status_code=400, detail="cannot upload to the workspace root itself")

    import secrets, tempfile
    parent = _pp.dirname(abs_path) or "/workspace"
    tmp_path = f"{abs_path}.tmp.dev-wizerith.{secrets.token_hex(4)}"

    max_bytes = file_ops.MAX_UPLOAD_BYTES
    shquote = file_ops.shquote
    script = (
        f"set -e\n"
        f"mkdir -p {shquote(parent)}\n"
        f"head -c {max_bytes + 1} > {shquote(tmp_path)}\n"
        f"size=$(wc -c < {shquote(tmp_path)})\n"
        f'if [ "$size" -gt {max_bytes} ]; then\n'
        f"  rm -f {shquote(tmp_path)}\n"
        f"  echo 'file exceeds max upload size of {max_bytes} bytes' >&2\n"
        f"  exit 2\n"
        f"fi\n"
        f"mv {shquote(tmp_path)} {shquote(abs_path)}\n"
        f'printf "%s" "$size"\n'
    )

    # Spool the streaming request body to a temp file so we have a real fd
    # to hand to docker exec stdin. Bounded memory; ~3s for 100MB.
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    received = 0
    cap = max_bytes + 64 * 1024
    async for chunk in request.stream():
        if not chunk:
            continue
        received += len(chunk)
        if received > cap:
            spool.close()
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds max upload size of {max_bytes} bytes",
            )
        spool.write(chunk)
    spool.seek(0)

    try:
        rc, stdout, stderr = await asyncio.to_thread(
            docker_exec.run_exec_streaming,
            container,
            ["sh", "-c", script],
            stdin_fileobj=spool,
            timeout=300.0,
        )
    except docker_exec.DockerExecError as exc:
        raise HTTPException(status_code=504, detail=f"upload timed out: {exc.stderr[:200]}")
    finally:
        spool.close()

    if rc == 2:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds max upload size of {file_ops.MAX_UPLOAD_BYTES} bytes",
        )
    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail=f"upload failed: {stderr[:200].decode('utf-8', errors='replace')}",
        )

    try:
        size = int(stdout.strip() or 0)
    except ValueError:
        size = 0
    rel = abs_path[len("/workspace") + 1:] if abs_path.startswith("/workspace/") else abs_path
    file_ops.invalidate_listing(container, rel)
    return {"path": rel, "size": size}


class NewProjectBody(BaseModel):
    name: str
    parent: str = ""               # workspace-relative parent dir; default = /workspace root
    # PyCharm-style new-project knobs.
    template: str = "script"       # one of TEMPLATES keys: empty | script | module | quant | fastapi
    with_venv: bool = True         # create .venv via `uv venv`
    base_interpreter: Optional[str] = None  # python binary to seed the venv; defaults to uv's own choice
    packages: list[str] = Field(default_factory=list)  # extra packages to pip-install into the venv
    init_git: bool = True
    create_main: bool = True       # write the template's main file (if any)
    create_readme: bool = True
    create_gitignore: bool = True
    description: str = ""


@app.post("/api/projects/new")
async def api_projects_new(body: NewProjectBody, user: dict = Depends(require_user)) -> dict:
    """PyCharm-style project bootstrap.

    Sequence (each step is best-effort beyond the directory + pyproject;
    the response carries a `warnings` list so the UI can surface failures
    without rolling back the whole project):

      1. mkdir project dir
      2. write pyproject.toml (template-tunable deps + python pin)
      3. write template files (main.py / module/__init__.py / fastapi / quant)
      4. write README.md (if create_readme)
      5. write .gitignore (if create_gitignore)
      6. create .venv via `uv venv` (if with_venv); honors base_interpreter
      7. pip-install packages = template.packages + body.packages
         (into the new venv if it exists, otherwise system pip — skip
         if neither makes sense)
      8. git init (if init_git)
      9. write .wizerith/project.json with the venv's python as interpreter
    """
    container = _ensure_container_for_user(user)
    name = (body.name or "").strip()
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="project name must be non-empty, no '/', no leading '.'")
    template = body.template if body.template in projects_mod.TEMPLATES else "script"
    parent_rel = (body.parent or "").strip().lstrip("/")
    proj_rel = (parent_rel + "/" + name).lstrip("/") if parent_rel else name
    slug = projects_mod.slugify(name)

    # Validate each user-supplied package name against a permissive PEP-503-ish
    # pattern. Without this, a name like `requests"; evil = "1` would inject
    # extra TOML fields into pyproject.toml (the field is built by f-string
    # interpolation below). PEP 503 allows letters, digits, ., -, _; we also
    # accept the common pip extras / version specifiers (`==`, `>=`, `<=`,
    # `~=`, `>`, `<`, `[`, `]`, `,`, ` `) so users can pin versions.
    _PKG_RE = re.compile(r"^[A-Za-z0-9_.\-+\[\]=<>~!,\s]+$")
    bad_pkgs = [p for p in (body.packages or []) if not p or not _PKG_RE.match(p)]
    if bad_pkgs:
        raise HTTPException(
            status_code=400,
            detail=f"invalid package name(s): {', '.join(bad_pkgs[:5])}",
        )

    warnings: list[str] = []

    from file_ops import _normalize, shquote  # noqa: SLF001 — stable

    def _toml_escape(s: str) -> str:
        # Minimal TOML basic-string escape: backslash → \\, double-quote → \",
        # CR/LF → space (TOML basic strings can't contain literal newlines).
        # Used for free-text user input that lands inside `"..."` in the
        # generated pyproject.toml — without escaping, a `"` or newline in
        # `description` would break the file or inject extra fields.
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")

    try:
        # 1. Directory.
        _wrap_fileop(file_ops.mkdir, container, proj_rel)

        # 2. pyproject.toml.
        pyproj_deps = ", ".join(
            f'"{_toml_escape(p)}"' for p in (projects_mod.TEMPLATES[template]["packages"] + list(body.packages))
        )
        pyproj = (
            f'[project]\n'
            f'name = "{_toml_escape(slug)}"\n'
            f'version = "0.1.0"\n'
            f'description = "{_toml_escape(body.description)}"\n'
            f'requires-python = ">=3.10"\n'
            f'dependencies = [{pyproj_deps}]\n'
        )
        _wrap_fileop(file_ops.write_file, container, proj_rel + "/pyproject.toml", pyproj)

        # 3. Template files (with {name}, {slug} substitutions).
        tmpl = projects_mod.TEMPLATES[template]
        main_rel_in_proj: Optional[str] = None
        if body.create_main:
            for rel_path_tmpl, content_tmpl in tmpl["files"].items():
                rel_path = rel_path_tmpl.format(slug=slug, name=name)
                content = content_tmpl.format(slug=slug, name=name)
                _wrap_fileop(file_ops.write_file, container, proj_rel + "/" + rel_path, content)
            if tmpl["main_file"]:
                main_rel_in_proj = tmpl["main_file"].format(slug=slug, name=name)

        # 4. README.md
        if body.create_readme:
            readme = (
                f"# {name}\n\n"
                f"{body.description or 'TODO: describe this project.'}\n\n"
                f"## Quickstart\n\n"
                f"```bash\n"
                f"# activate the venv\n"
                f"source .venv/bin/activate\n\n"
                f"# run the entry point\n"
                f"python {main_rel_in_proj or 'main.py'}\n"
                f"```\n"
            )
            _wrap_fileop(file_ops.write_file, container, proj_rel + "/README.md", readme)

        # 5. .gitignore
        if body.create_gitignore:
            _wrap_fileop(file_ops.write_file, container, proj_rel + "/.gitignore", projects_mod.PYTHON_GITIGNORE)

        # 6. venv
        abs_proj = _normalize(proj_rel)
        uv_path = "/home/linuxbrew/.linuxbrew/bin/uv"
        venv_python: Optional[str] = None
        if body.with_venv:
            venv_cmd = f"cd {shquote(abs_proj)} && {uv_path} venv .venv"
            if body.base_interpreter:
                venv_cmd += f" --python {shquote(body.base_interpreter)}"
            venv_cmd += " 2>&1"
            try:
                docker_exec.run_exec(container, ["sh", "-c", venv_cmd], timeout=90.0)
                venv_python = abs_proj + "/.venv/bin/python"
            except docker_exec.DockerExecError as exc:
                msg = exc.stderr[:200] if exc.stderr else "uv venv failed"
                warnings.append(f"venv: {msg}")
                logger.warning("uv venv failed in %s: %s", abs_proj, msg)

        # 7. Install packages
        all_pkgs = projects_mod.TEMPLATES[template]["packages"] + list(body.packages)
        if all_pkgs and venv_python:
            pip_cmd = (
                f"{uv_path} pip install --python {shquote(venv_python)} "
                + " ".join(shquote(p) for p in all_pkgs)
            )
            try:
                docker_exec.run_exec(container, ["sh", "-c", pip_cmd], timeout=300.0)
            except docker_exec.DockerExecError as exc:
                warnings.append(f"pip install: {exc.stderr[:200] if exc.stderr else 'failed'}")

        # 8. git init
        if body.init_git:
            git_cmd = (
                f"cd {shquote(abs_proj)} && "
                f"git init -q && "
                f"git add -A && "
                f"git -c user.email=dev@wizerith -c user.name=dev "
                f"commit -q -m 'Initial commit from dev.wizerith.ai' || true"
            )
            try:
                docker_exec.run_exec(container, ["sh", "-c", git_cmd], timeout=30.0)
            except docker_exec.DockerExecError as exc:
                warnings.append(f"git init: {exc.stderr[:200] if exc.stderr else 'failed'}")

        # 9. Write per-project config so the IDE remembers the chosen
        #    interpreter for this project.
        try:
            cfg = dict(projects_mod.DEFAULT_PROJECT_CONFIG)
            if venv_python:
                cfg["interpreter"] = venv_python
            elif body.base_interpreter:
                cfg["interpreter"] = body.base_interpreter
            projects_mod.save_project_config(container, proj_rel, cfg)
        except docker_exec.DockerExecError as exc:
            warnings.append(f"save config: {exc.stderr[:200] if exc.stderr else 'failed'}")
    except HTTPException:
        raise
    return {
        "path": proj_rel,
        "slug": slug,
        "template": template,
        "main_file": main_rel_in_proj,
        "venv_python": venv_python,
        "warnings": warnings,
    }


# --- Interpreters + project discovery + per-project config -------------


@app.get("/api/interpreters")
async def api_interpreters(user: dict = Depends(require_user)) -> dict:
    container = _ensure_container_for_user(user)
    return {"interpreters": projects_mod.discover_interpreters(container)}


@app.get("/api/projects")
async def api_projects_list(user: dict = Depends(require_user)) -> dict:
    container = _ensure_container_for_user(user)
    return {"projects": projects_mod.discover_projects(container)}


@app.get("/api/projects/config")
async def api_project_config_get(
    path: str, user: dict = Depends(require_user),
) -> dict:
    container = _ensure_container_for_user(user)
    return {"path": path, "config": projects_mod.load_project_config(container, path)}


class ProjectConfigBody(BaseModel):
    path: str
    config: dict[str, Any]


@app.put("/api/projects/config")
async def api_project_config_put(
    body: ProjectConfigBody, user: dict = Depends(require_user),
) -> dict:
    container = _ensure_container_for_user(user)
    try:
        projects_mod.save_project_config(container, body.path, body.config)
    except docker_exec.DockerExecError as exc:
        raise HTTPException(status_code=500, detail=f"save failed: {exc.stderr[:200]}")
    return {"ok": True}


# --- Jobs ---------------------------------------------------------------


class RunBody(BaseModel):
    path: str
    interpreter: str = "/usr/local/bin/python3"
    args: list[str] = Field(default_factory=list)
    cwd: Optional[str] = None  # relative to /workspace; defaults to the script's dir


@app.post("/api/jobs/run")
async def api_jobs_run(body: RunBody, user: dict = Depends(require_user)) -> dict:
    container = _ensure_container_for_user(user)
    # Validate the script path lives under /workspace (uses the same
    # normalization as file ops, so identical guarantees).
    try:
        abs_path = file_ops._normalize(body.path)  # noqa: SLF001 — private but stable
    except file_ops.FileOpError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if body.cwd is not None:
        try:
            abs_cwd = file_ops._normalize(body.cwd)
        except file_ops.FileOpError as exc:
            raise HTTPException(status_code=400, detail=f"bad cwd: {exc}")
    else:
        # Default: run from the directory the script lives in (so relative
        # imports & file IO behave the way the user expects).
        import posixpath
        abs_cwd = posixpath.dirname(abs_path) or "/workspace"

    cmd = [body.interpreter, abs_path, *body.args]
    job = await docker_exec.start_job(
        email=user["email"],
        container=container,
        cmd=cmd,
        workdir=abs_cwd,
    )
    return {
        "job_id": job.job_id,
        "started_at": job.started_at,
        "argv": cmd,
        "cwd": abs_cwd,
    }


@app.get("/api/jobs")
async def api_jobs_list(user: dict = Depends(require_user)) -> dict:
    jobs = docker_exec.registry.list_for(user["email"])
    return {
        "jobs": [
            {
                "job_id": j.job_id,
                "status": j.status,
                "exit_code": j.exit_code,
                "started_at": j.started_at,
                "argv": j.argv,
            }
            for j in sorted(jobs, key=lambda j: j.started_at, reverse=True)
        ]
    }


@app.get("/api/jobs/{job_id}/stream")
async def api_jobs_stream(job_id: str, user: dict = Depends(require_user)) -> StreamingResponse:
    job = docker_exec.registry.get(job_id)
    if not job or job.email != user["email"]:
        raise HTTPException(status_code=404, detail="job not found")

    q = docker_exec.subscribe(job)

    async def event_stream():
        # Replay the buffer first so reconnecting clients see history.
        for line in list(job.buffer):
            yield _sse(line.kind, {"text": line.text, "ts": line.ts})
        if job.status != "running":
            # Already done; close out.
            yield _sse(
                "status",
                {"status": job.status, "exit_code": job.exit_code},
            )
            return
        # Live: pull from the per-subscriber queue until sentinel.
        try:
            while True:
                line = await q.get()
                if line is None:
                    break
                yield _sse(line.kind, {"text": line.text, "ts": line.ts})
            yield _sse(
                "status",
                {"status": job.status, "exit_code": job.exit_code},
            )
        finally:
            docker_exec.unsubscribe(job, q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- LSP (pyright) ------------------------------------------------------


@app.websocket("/api/lsp/pyright")
async def lsp_pyright(websocket: WebSocket) -> None:
    """WebSocket ↔ pyright-langserver bridge.

    Auth goes through `_ws_authenticate`, which picks the local-auth cookie
    path when AUTH_MODE=local and the CF Access JWT path otherwise — same
    dual-path logic as the HTTP `require_user` dependency.
    """
    email = await _ws_authenticate(websocket)
    if email is None:
        return

    try:
        container = chat_user_container.ensure_user_container(email)
    except Exception as exc:
        logger.error("LSP container provisioning failed for %s: %s", email, exc)
        await websocket.close(code=1011, reason="container provisioning failed")
        return

    await websocket.accept()
    try:
        await lsp_bridge.ensure_pyright(container)
    except Exception as exc:
        logger.error("pyright install failed for %s: %s", email, exc)
        try:
            await websocket.send_text(
                '{"jsonrpc":"2.0","method":"window/showMessage",'
                '"params":{"type":1,"message":"pyright install failed; '
                'LSP unavailable"}}'
            )
        except Exception:
            pass
        await websocket.close(code=1011, reason="pyright install failed")
        return

    session = lsp_bridge.LspSession(container)
    try:
        await session.spawn()
    except Exception as exc:
        logger.error("pyright spawn failed for %s: %s", email, exc)
        await websocket.close(code=1011, reason="pyright spawn failed")
        return
    await _lsp_pump(session, websocket)


async def _lsp_pump(session: "lsp_bridge.LspSession", websocket: WebSocket) -> None:
    """Run the three pyright pumps concurrently until any one completes,
    then tear down. Extracted from lsp_pyright so the endpoint body stays
    linear and the terminal endpoint can follow directly below.
    """
    tasks = [
        asyncio.create_task(lsp_bridge.pump_ws_to_proc(session, websocket)),
        asyncio.create_task(lsp_bridge.pump_proc_to_ws(session, websocket)),
        asyncio.create_task(lsp_bridge.drain_stderr(session)),
    ]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        await session.stop()
        try:
            await websocket.close()
        except Exception:
            pass


# --- Terminal (interactive bash) ----------------------------------------


# uid 1000 inside the per-user container, same exec identity as docker_exec /
# lsp_bridge. The interactive shell needs a real PTY (tty=True), a
# matching home dir, and a login env so .bashrc / .nix-profile / PATH
# get sourced exactly like a term.wizerith.ai session.
_TERMINAL_SHELL = ["bash", "-l"]


@app.websocket("/api/terminal")
async def terminal_ws(websocket: WebSocket) -> None:
    """WebSocket ↔ docker-exec PTY bridge for an interactive `bash -l`
    inside the user's per-user container.

    Frame contract (mirrors term.wizerith.ai so the same client patterns
    just work):
      - text frame: either a JSON envelope `{"type":"resize","cols":N,"rows":M}`
        or `{"type":"ping"}` keepalive; anything else is forwarded as
        UTF-8 stdin bytes (xterm.js's onData hot path).
      - binary frame: raw stdin bytes.
      - server → client: always binary (raw stdout/stderr).
    """
    email = await _ws_authenticate(websocket)
    if email is None:
        return

    try:
        container = chat_user_container.ensure_user_container(email)
    except Exception as exc:
        logger.error("terminal container provisioning failed for %s: %s", email, exc)
        await websocket.close(code=1011, reason="container provisioning failed")
        return

    await websocket.accept()

    client = docker.from_env()
    try:
        raw_exec = client.api.exec_create(
            container,
            cmd=_TERMINAL_SHELL,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            # uid:gid 1000 = `app` inside the per-user container — same
            # identity the file ops + run-job paths use.
            user="1000:1000",
            workdir="/workspace",
            environment={
                "TERM": "xterm-256color",
                "HOME": "/workspace",
                "COLORTERM": "truecolor",
            },
        )
        exec_id = raw_exec["Id"] if isinstance(raw_exec, dict) else raw_exec
        # tty=True at exec_start MUST match exec_create — without it the
        # daemon emits stdcopy-framed chunks and xterm renders the framing
        # bytes as control-char garbage. Same gotcha called out in
        # term-router/app.py near the equivalent call site.
        exec_stream = client.api.exec_start(
            exec_id, socket=True, demux=False, tty=True,
        )
        # Clear the docker-py default read timeout so idle shells don't
        # masquerade as EOF every ~30 s. pty_bridge handles real EOF via
        # zero-byte reads.
        inner_sock = getattr(exec_stream, "_sock", None)
        if inner_sock is not None:
            try:
                inner_sock.settimeout(None)
            except OSError:
                logger.debug("could not clear exec socket timeout", exc_info=True)
    except Exception:
        logger.exception("terminal exec_create/exec_start failed for %s", email)
        try:
            await websocket.close(code=1011, reason="exec failed")
        except Exception:
            pass
        return

    try:
        await pty_bridge.bridge(
            websocket, exec_stream, exec_id, docker_client=client,
        )
    except Exception:
        logger.exception("terminal pty_bridge errored for %s", email)
    finally:
        try:
            if hasattr(exec_stream, "close"):
                exec_stream.close()
        except Exception:
            logger.debug("exec_stream close raised", exc_info=True)
        try:
            await websocket.close()
        except Exception:
            pass


@app.delete("/api/jobs/{job_id}")
async def api_jobs_kill(job_id: str, user: dict = Depends(require_user)) -> dict:
    job = docker_exec.registry.get(job_id)
    if not job or job.email != user["email"]:
        raise HTTPException(status_code=404, detail="job not found")
    killed = await docker_exec.kill_job(job)
    return {"killed": killed, "status": job.status}


# ---------------------------------------------------------------------------
# SSE helper.
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


# ---------------------------------------------------------------------------
# Static SPA. Mounted last so /api/* routes win.
# ---------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    # /assets/* are emitted by Vite with a content-hashed name and can be
    # cached aggressively. The root /index.html and /static/* fall through
    # to short caching since they change on every deploy.
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets"), check_dir=False), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        # Browser is hitting the SPA for the first time. CF Access fronts
        # this route so any unauthenticated GET / never reaches us — by the
        # time the browser is at this endpoint the user is already in the
        # CF identity. We don't need to gate index.html itself; the SPA's
        # /api/me probe will 401 if something's off, and the user gets a
        # clear error on the page.
        return FileResponse(str(STATIC_DIR / "index.html"))

    # SPA history fallback — any non-/api path that doesn't match a file
    # returns index.html so client-side routing works. We restrict to
    # method=GET and refuse anything matching /api/ explicitly.
    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> Response:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(STATIC_DIR / "index.html"))


# Periodic GC of finished jobs so memory stays bounded on a long-running
# uvicorn process.
@app.on_event("startup")
async def _start_gc():
    async def loop():
        while True:
            try:
                removed = await docker_exec.registry.gc()
                if removed:
                    logger.info("gc reaped %d finished jobs", removed)
            except Exception as exc:
                logger.warning("gc loop error: %s", exc)
            await asyncio.sleep(60)

    asyncio.create_task(loop())
