"""term-router — FastAPI app that bridges xterm.js to a docker exec session.

The flow (see brief): JWT in handshake header or cookie → verify → resolve
role → choose target container (admin: portfolio-ttyd; user: their per-email
container provisioned by services.chat.user_container) → docker exec a
shell → bridge stdin/stdout/resize over a WebSocket.

Why an `create_app(...)` factory: so tests can inject a fake docker client,
a fake jwt_verifier, and a fake ensure_container without monkeypatching
imports. The module-level `app = create_app()` keeps `uvicorn app:app`
working in production.

Trust posture: this service mounts /var/run/docker.sock. Compromise of
this process is equivalent to host root. Same trust model as
chat-host-shell — see docker-compose.yml for the corresponding mount.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import pathlib
import posixpath
import urllib.parse
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket
from fastapi import File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import file_ops
import pty_bridge

logger = logging.getLogger(__name__)

# --- Constants -----------------------------------------------------------

# Container name an admin WS lands in. Env-driven so each tenant's
# term-router can target its own admin shell (chat-wizerith already uses
# this pattern for the chat-host-shell container; we mirror it here).
# Default = the historical ald3 admin shell so existing deployments work
# without a compose change.
ADMIN_TTYD_CONTAINER = os.environ.get(
    "ADMIN_TTYD_CONTAINER",
    "portfolio-ttyd",
)
# Kept as separate constants — admin and user shells may diverge in a
# future phase (e.g. user containers gaining a tighter rcfile). Don't
# deduplicate into a single SHELL_ARGV without a brief change.
USER_SHELL_ARGV = ["bash", "-l"]
ADMIN_SHELL_ARGV = ["bash", "-l"]
STATIC_DIR = pathlib.Path(__file__).parent / "static"

# WebSocket close codes per RFC 6455 application range (4000-4999).
# We mirror HTTP status numbering (401/500) for operator legibility — when
# a code shows up in browser devtools, it reads at a glance.
_CLOSE_UNAUTHORIZED = 4401  # mirrors HTTP 401 — auth failed (no/bad/missing JWT)
_CLOSE_SERVER_ERROR = 4500  # mirrors HTTP 500 — server-side provisioning failure

_COOKIE_NAME = "CF_Authorization"

# Workspace selection (Phase: shared-workspace).
#
# The chat topbar lets a user switch between their personal per-email
# container and the tenant's shared container. The selection is
# persisted as a cookie so every WS handshake AND every /api/files/*
# call routes consistently without the JS having to re-thread it through
# every fetch URL. A ?workspace= query override exists too so the same
# JS can flip a single API call without touching the cookie.
_WORKSPACE_COOKIE_NAME = "chat_workspace"
# Legacy cookie name from a previous naming scheme; still read so old
# sessions don't lose their workspace selection mid-flight. Safe to
# remove after a couple of months once browsers have rolled over.
_WORKSPACE_COOKIE_NAME_LEGACY = "wizerith_workspace"
_WORKSPACE_PERSONAL = "personal"
_WORKSPACE_SHARED = "shared"


# --- JWT extraction ------------------------------------------------------

def _extract_jwt(websocket: WebSocket) -> Optional[str]:
    """Pull the CF Access JWT from a WS handshake.

    Header preferred (`Cf-Access-Jwt-Assertion`); cookie `CF_Authorization`
    is the documented fallback. Returns None when neither is present so
    the caller can close 4401 with a single code path.
    """
    # Headers are case-insensitive in Starlette's MutableHeaders.
    header_token = websocket.headers.get("cf-access-jwt-assertion")
    if header_token:
        return header_token
    cookie_token = websocket.cookies.get(_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    return None


def _extract_jwt_http(request: Request) -> Optional[str]:
    """Same precedence as the WS path, but for HTTP requests."""
    header_token = request.headers.get("cf-access-jwt-assertion")
    if header_token:
        return header_token
    cookie_token = request.cookies.get(_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    return None


def _normalize_path(raw: str) -> str:
    """Resolve `..` and `.` in a posix path string client-side, leaving
    server-side perms as the only authority. Refuses empty input.

    We don't pin paths under a chroot here because uid 1000 in the user
    container already can't escape its own filesystem perms — the chmod
    boundary is the real check. This just collapses ``a/b/../c`` → ``a/c``
    so user-typed breadcrumbs are tolerant.
    """
    if not raw:
        raw = "/"
    # posixpath.normpath collapses .. and . safely; ensure it stays absolute.
    if not raw.startswith("/"):
        raw = "/" + raw
    return posixpath.normpath(raw) or "/"


# --- App factory ---------------------------------------------------------

def _default_docker_client():
    """Lazy: don't touch the daemon at import time."""
    import docker  # local — same pattern as services.chat.user_container
    return docker.from_env()


def create_app(
    *,
    docker_client: Any = None,
    jwt_verifier: Optional[Callable[[str], dict]] = None,
    ensure_container: Optional[Callable[[str], str]] = None,
    ensure_shared: Optional[Callable[[], str]] = None,
    shared_enabled: Optional[Callable[[], bool]] = None,
) -> FastAPI:
    """Build the FastAPI app. All deps are injectable for tests.

    Defaults wire in:
      - docker_client: lazy `docker.from_env()` on first WS connection.
      - jwt_verifier: services.chat.auth.verify_cf_access_jwt
      - ensure_container: services.chat.user_container.ensure_user_container
      - ensure_shared: services.chat.user_container.ensure_shared_container
      - shared_enabled: services.chat.user_container.shared_container_name
    """
    # Lazy imports: keeps `import services.term_router.app` cheap, and lets
    # tests pass without a docker daemon on PATH.
    from services.chat import auth as chat_auth
    from services.chat import user_container as chat_user_container

    _verifier = jwt_verifier or chat_auth.verify_cf_access_jwt
    _ensure = ensure_container or chat_user_container.ensure_user_container
    _ensure_shared = ensure_shared or chat_user_container.ensure_shared_container
    _shared_enabled = shared_enabled or (
        lambda: bool(chat_user_container.shared_container_name())
    )

    app = FastAPI(title="term-router")

    # --- HTTP routes -----------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/")
    async def index() -> FileResponse:
        # The index pulls in versioned /static/{files,term}.js — but the
        # HTML itself is the entry point, so it must NEVER come from a
        # stale cache (otherwise a deploy that bumps the ?v= query is
        # invisible to clients still holding the old HTML). Cache-Control:
        # no-cache forces a revalidation on every load; the static assets
        # under /static can still be cached aggressively because their
        # URLs change when their contents change.
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # StaticFiles handles path-traversal protection internally (it
    # resolves under STATIC_DIR and rejects symlink escapes).
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )

    def _resolve_workspace_http(
        request: Request, *, query_override: Optional[str] = None
    ) -> str:
        """Pick the workspace for an HTTP request: query > cookie > personal.

        Untrusted input — clamped to the closed set {"personal", "shared"}
        and folded back to "personal" on anything else so a malformed
        cookie can't pin a user to an unprovisioned target.
        """
        raw = (query_override or "").strip().lower()
        if raw in (_WORKSPACE_PERSONAL, _WORKSPACE_SHARED):
            return raw
        cookie_val = (
            request.cookies.get(_WORKSPACE_COOKIE_NAME)
            or request.cookies.get(_WORKSPACE_COOKIE_NAME_LEGACY)
            or ""
        ).strip().lower()
        if cookie_val == _WORKSPACE_SHARED:
            return _WORKSPACE_SHARED
        return _WORKSPACE_PERSONAL

    def _resolve_workspace_ws(websocket: WebSocket) -> str:
        """Same as _resolve_workspace_http but reads from the WS handshake.

        Honors a ?workspace= query string on the WS URL too — the term JS
        toggles workspace by reconnecting with that query rather than
        having to wait for the cookie write to round-trip.
        """
        try:
            query_override = websocket.query_params.get("workspace")
        except Exception:
            query_override = None
        raw = (query_override or "").strip().lower()
        if raw in (_WORKSPACE_PERSONAL, _WORKSPACE_SHARED):
            return raw
        cookie_val = (
            websocket.cookies.get(_WORKSPACE_COOKIE_NAME)
            or websocket.cookies.get(_WORKSPACE_COOKIE_NAME_LEGACY)
            or ""
        ).strip().lower()
        if cookie_val == _WORKSPACE_SHARED:
            return _WORKSPACE_SHARED
        return _WORKSPACE_PERSONAL

    @app.get("/api/me")
    async def term_me(request: Request) -> JSONResponse:
        """Lightweight identity echo for the term JS.

        Returns the resolved email/role plus shared_workspace_enabled so
        the topbar can decide whether to render the workspace toggle. We
        intentionally don't error on bad/missing JWT — we return 401 so
        the JS can keep behaving (the actual gating still happens at /ws
        and /api/files/*).
        """
        token = _extract_jwt_http(request)
        if not token:
            raise HTTPException(status_code=401, detail="missing CF Access JWT")
        try:
            claims = _verifier(token)
        except chat_auth.JWTVerificationError as exc:
            raise HTTPException(status_code=401, detail="invalid JWT") from exc
        try:
            email = chat_auth.email_from_claims(claims)
        except chat_auth.JWTVerificationError as exc:
            raise HTTPException(status_code=401, detail="no email in JWT") from exc
        return JSONResponse({
            "email": email,
            "role": chat_auth.resolve_role(email),
            "shared_workspace_enabled": _shared_enabled(),
            "workspace": _resolve_workspace_http(request),
        })

    # --- File-system API -------------------------------------------------
    #
    # All five endpoints share the same auth/role/target resolution as
    # /ws, factored into _resolve_target. Filesystem ops run as uid
    # 1000:1000 inside the user's container — the same uid the bash
    # terminal runs as — so any path the user can read/write from `bash`
    # they can read/write from this UI, and nothing else. /var/claude-
    # runner/.claude/.credentials.json (mode 0400 owner uid 2000) stays
    # invisible. See file_ops.py for the security note.

    def _resolve_target(
        request: Request, *, workspace_override: Optional[str] = None
    ) -> tuple[str, str, str]:
        """Verify the JWT and return (email, role, container_name).

        Picks the container based on the resolved workspace (query >
        cookie > "personal"). Admin always lands in ADMIN_TTYD_CONTAINER
        regardless of workspace — the admin shell is the operator's host
        view and not affected by the user-facing workspace switch.

        Raises HTTPException(401) on auth failures, 500 on container
        provisioning errors. Both error shapes mirror the WS close codes
        (_CLOSE_UNAUTHORIZED / _CLOSE_SERVER_ERROR) so the two paths log
        and surface uniformly.
        """
        token = _extract_jwt_http(request)
        if not token:
            raise HTTPException(status_code=401, detail="missing CF Access JWT")
        try:
            claims = _verifier(token)
        except chat_auth.JWTVerificationError as exc:
            logger.info("JWT verification failed (file api): %s", exc)
            raise HTTPException(status_code=401, detail="invalid JWT") from exc
        try:
            email = chat_auth.email_from_claims(claims)
        except chat_auth.JWTVerificationError as exc:
            logger.info("email_from_claims failed (file api): %s", exc)
            raise HTTPException(status_code=401, detail="no email in JWT") from exc
        role = chat_auth.resolve_role(email)
        if role == "admin":
            return email, role, ADMIN_TTYD_CONTAINER
        workspace = _resolve_workspace_http(
            request, query_override=workspace_override
        )
        try:
            if workspace == _WORKSPACE_SHARED:
                if not _shared_enabled():
                    raise HTTPException(
                        status_code=400,
                        detail="shared workspace is not enabled on this tenant",
                    )
                container = _ensure_shared()
            else:
                container = _ensure(email)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "ensure_%s_container failed for %s (file api)",
                workspace, email,
            )
            raise HTTPException(
                status_code=500, detail="container provisioning failed"
            ) from exc
        return email, role, container

    def _client():
        return docker_client or _default_docker_client()

    @app.get("/api/files/list")
    async def api_list(
        request: Request,
        path: str = "/",
        workspace: Optional[str] = None,
    ) -> JSONResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        norm = _normalize_path(path)
        try:
            entries = await asyncio.to_thread(
                file_ops.list_dir, _client(), container, norm
            )
        except file_ops.FileOpError as exc:
            # Most often: ENOENT or EACCES from `find`. 404 covers both
            # cleanly enough for the UI; the stderr is in the body.
            return JSONResponse(
                {"path": norm, "error": exc.stderr.strip()},
                status_code=404,
            )
        return JSONResponse(
            {
                "path": norm,
                "entries": [
                    {
                        "name": e.name,
                        "type": e.type,
                        "size": e.size,
                        "mtime": e.mtime,
                    }
                    for e in entries
                ],
            }
        )

    @app.get("/api/files/download")
    async def api_download(
        request: Request,
        path: str,
        workspace: Optional[str] = None,
    ) -> StreamingResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        norm = _normalize_path(path)
        # Stat first so we can surface 404 / 403 before starting the
        # response stream (once headers are flushed we can't change the
        # status code, so a missing file would otherwise look like a
        # successful empty download).
        info = await asyncio.to_thread(
            file_ops.stat_path, _client(), container, norm
        )
        if info is None:
            raise HTTPException(status_code=404, detail="not found")
        if info.type == "d":
            raise HTTPException(status_code=400, detail="path is a directory")

        client = _client()

        def _gen():
            for chunk in file_ops.stream_download(client, container, norm):
                if chunk:
                    yield chunk

        filename = posixpath.basename(norm) or "file"
        # RFC 6266 filename* for unicode names; ascii fallback for legacy UAs.
        safe_ascii = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
        encoded = urllib.parse.quote(filename)
        disposition = (
            f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded}'
        )
        headers = {
            "Content-Disposition": disposition,
            "Content-Length": str(info.size),
        }
        return StreamingResponse(
            _gen(), media_type="application/octet-stream", headers=headers
        )

    # File viewer/editor backing routes. Three endpoints split by purpose:
    #   /api/files/read   — text content for the editor pane (capped at 8 MiB,
    #                        returns {content, truncated, encoding} JSON).
    #   /api/files/save   — write text content back. Body: {path, content}.
    #   /api/files/raw    — stream bytes inline (Content-Disposition: inline)
    #                        with a guessed Content-Type, used by <img>,
    #                        <video>, <audio>, <iframe> for PDFs, etc.
    # The existing /api/files/download stays as the explicit "save to disk"
    # path with attachment disposition; the viewer doesn't use it.

    # MIME guesses for inline rendering. mimetypes' default DB covers most
    # of what we need; the explicit overrides below pin a few that can
    # vary across systems or that we want a specific charset on.
    _INLINE_MIME_OVERRIDES: dict[str, str] = {
        ".md": "text/markdown; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".tsv": "text/tab-separated-values; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".log": "text/plain; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".yaml": "text/yaml; charset=utf-8",
        ".yml": "text/yaml; charset=utf-8",
        ".toml": "text/plain; charset=utf-8",
        ".py": "text/x-python; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".ts": "text/typescript; charset=utf-8",
        ".tsx": "text/typescript; charset=utf-8",
        ".jsx": "text/javascript; charset=utf-8",
        ".sh": "text/x-shellscript; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".xml": "text/xml; charset=utf-8",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".webm": "video/webm",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".ogv": "video/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
    }

    def _guess_mime(filename: str) -> str:
        ext = posixpath.splitext(filename)[1].lower()
        if ext in _INLINE_MIME_OVERRIDES:
            return _INLINE_MIME_OVERRIDES[ext]
        guess, _ = mimetypes.guess_type(filename)
        return guess or "application/octet-stream"

    @app.get("/api/files/read")
    async def api_read(
        request: Request,
        path: str,
        workspace: Optional[str] = None,
    ) -> JSONResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        norm = _normalize_path(path)
        info = await asyncio.to_thread(
            file_ops.stat_path, _client(), container, norm
        )
        if info is None:
            raise HTTPException(status_code=404, detail="not found")
        if info.type == "d":
            raise HTTPException(status_code=400, detail="path is a directory")
        try:
            data, truncated = await asyncio.to_thread(
                file_ops.read_file, _client(), container, norm,
            )
        except file_ops.FileOpError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        # Decode as UTF-8; if it fails the file is binary and the editor
        # shouldn't try to render it as text. Surface a 415 so the
        # frontend can fall back to the raw-stream renderer (image, pdf,
        # etc.) without UI flicker.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=415,
                detail="binary file; use /api/files/raw to view",
            )
        return JSONResponse({
            "path": norm,
            "size": info.size,
            "mime": _guess_mime(norm),
            "content": text,
            "truncated": truncated,
            "max_bytes": file_ops.MAX_READ_BYTES,
        })

    @app.post("/api/files/save")
    async def api_save(
        request: Request,
        workspace: Optional[str] = None,
    ) -> JSONResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        path = body.get("path")
        content = body.get("content")
        if not isinstance(path, str) or not path:
            raise HTTPException(status_code=400, detail="path is required")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        norm = _normalize_path(path)
        encoded = content.encode("utf-8")
        if len(encoded) > file_ops.MAX_READ_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"content exceeds {file_ops.MAX_READ_BYTES} bytes",
            )
        try:
            written = await asyncio.to_thread(
                file_ops.write_file, _client(), container, norm, encoded,
            )
        except file_ops.FileOpError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse({"path": norm, "bytes_written": written})

    @app.get("/api/files/raw")
    async def api_raw(
        request: Request,
        path: str,
        workspace: Optional[str] = None,
    ) -> StreamingResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        norm = _normalize_path(path)
        info = await asyncio.to_thread(
            file_ops.stat_path, _client(), container, norm
        )
        if info is None:
            raise HTTPException(status_code=404, detail="not found")
        if info.type == "d":
            raise HTTPException(status_code=400, detail="path is a directory")

        client = _client()

        def _gen():
            for chunk in file_ops.stream_download(client, container, norm):
                if chunk:
                    yield chunk

        filename = posixpath.basename(norm) or "file"
        media = _guess_mime(filename)
        # Inline disposition lets the browser render the response in-place
        # (img, video, iframe) rather than triggering a download. Keep the
        # filename* hint so a "Save Page" still uses the right name.
        encoded = urllib.parse.quote(filename)
        safe_ascii = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
        disposition = (
            f'inline; filename="{safe_ascii}"; filename*=UTF-8\'\'{encoded}'
        )
        headers = {
            "Content-Disposition": disposition,
            "Content-Length": str(info.size),
            # Browsers cache by URL; without a varying ETag/Last-Modified
            # an edit-then-reopen would still show the old content. Use
            # mtime+size as a cheap content-stamp.
            "Cache-Control": "no-cache, must-revalidate",
            "ETag": f'W/"{info.size}-{int(info.mtime)}"',
        }
        return StreamingResponse(_gen(), media_type=media, headers=headers)

    @app.post("/api/files/upload")
    async def api_upload(
        request: Request,
        path: str = Form(...),
        file: UploadFile = File(...),
        workspace: Optional[str] = None,
    ) -> JSONResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        # `path` is the destination DIRECTORY; the uploaded file lands at
        # path/<original-filename>. Mirrors typical drag-drop semantics.
        dest_dir = _normalize_path(path)
        filename = posixpath.basename(file.filename or "")
        if not filename or "/" in filename:
            raise HTTPException(status_code=400, detail="invalid filename")
        dest = posixpath.join(dest_dir, filename)

        # Stream the multipart body straight into docker exec stdin.
        # FastAPI's UploadFile gives us .read(size) which is async-safe;
        # we pump fixed-size chunks to avoid loading the whole file in
        # memory. Producer (async) puts chunks on a queue; consumer (sync,
        # running on a thread via asyncio.to_thread) drains the queue and
        # writes to the docker socket. Bounded queue gives backpressure
        # so a slow docker write throttles the upload at the HTTP edge.
        CHUNK = 1024 * 1024  # 1 MiB
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=4)

        async def _producer():
            try:
                while True:
                    chunk = await file.read(CHUNK)
                    if not chunk:
                        break
                    await queue.put(chunk)
            finally:
                await queue.put(None)

        def _sync_iter():
            while True:
                fut = asyncio.run_coroutine_threadsafe(queue.get(), loop)
                chunk = fut.result()
                if chunk is None:
                    return
                yield chunk

        producer_task = asyncio.create_task(_producer())
        try:
            total = await asyncio.to_thread(
                file_ops.stream_upload, _client(), container, dest, _sync_iter()
            )
        except file_ops.FileOpError as exc:
            # Drain producer so it doesn't dangle.
            producer_task.cancel()
            try:
                await producer_task
            except (asyncio.CancelledError, Exception):
                pass
            status = 413 if exc.exit_code == 413 else 400
            raise HTTPException(status_code=status, detail=exc.stderr or str(exc)) from exc
        await producer_task
        return JSONResponse({"path": dest, "bytes": total})

    @app.get("/api/files/search")
    async def api_search(
        request: Request,
        q: str,
        root: str = "/",
        workspace: Optional[str] = None,
    ) -> JSONResponse:
        """Filename substring search rooted at `root` (default `/`).

        Caps depth + result count, prunes common noise paths in
        file_ops.search_files. Always 200 on a clean exec; non-matches
        return an empty list. Failed exec (e.g. container gone) → 500.
        """
        _, _, container = _resolve_target(request, workspace_override=workspace)
        safe_root = _normalize_path(root) if root else "/"
        if not safe_root.startswith("/"):
            safe_root = "/"
        try:
            results = await asyncio.to_thread(
                file_ops.search_files, _client(), container, q, root=safe_root
            )
        except file_ops.FileOpError as exc:
            raise HTTPException(status_code=500, detail=exc.stderr.strip()) from exc
        return JSONResponse({"query": q, "root": safe_root, "results": results})

    @app.post("/api/files/mkdir")
    async def api_mkdir(
        request: Request,
        path: str = Form(...),
        workspace: Optional[str] = None,
    ) -> JSONResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        norm = _normalize_path(path)
        if norm in ("/", ""):
            raise HTTPException(status_code=400, detail="cannot create '/'")
        try:
            await asyncio.to_thread(file_ops.mkdir, _client(), container, norm)
        except file_ops.FileOpError as exc:
            raise HTTPException(status_code=400, detail=exc.stderr.strip()) from exc
        return JSONResponse({"path": norm, "ok": True})

    @app.post("/api/files/delete")
    async def api_delete(
        request: Request,
        path: str = Form(...),
        workspace: Optional[str] = None,
    ) -> JSONResponse:
        _, _, container = _resolve_target(request, workspace_override=workspace)
        norm = _normalize_path(path)
        # Refuse to delete the root or workspace root — these would brick
        # the container even if uid 1000 *could* manage it (it can't, but
        # the error message would be confusing). The caller can still
        # `rm -rf /workspace/*` from a terminal if they really want to.
        if norm in ("/", "", "/workspace", "/home", "/var", "/etc"):
            raise HTTPException(
                status_code=400, detail=f"refusing to delete {norm}"
            )
        try:
            await asyncio.to_thread(file_ops.delete, _client(), container, norm)
        except file_ops.FileOpError as exc:
            raise HTTPException(status_code=400, detail=exc.stderr.strip()) from exc
        return JSONResponse({"path": norm, "ok": True})

    # --- WebSocket terminal ---------------------------------------------

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        # Step 1: JWT.
        token = _extract_jwt(websocket)
        if not token:
            await websocket.close(code=_CLOSE_UNAUTHORIZED)
            return

        # Step 2: verify.
        try:
            claims = _verifier(token)
        except chat_auth.JWTVerificationError as exc:
            logger.info("JWT verification failed: %s", exc)
            await websocket.close(code=_CLOSE_UNAUTHORIZED)
            return

        # Step 3: email.
        try:
            email = chat_auth.email_from_claims(claims)
        except chat_auth.JWTVerificationError as exc:
            logger.info("email_from_claims failed: %s", exc)
            await websocket.close(code=_CLOSE_UNAUTHORIZED)
            return

        # Step 4: role → target.
        role = chat_auth.resolve_role(email)
        if role == "admin":
            target_container = ADMIN_TTYD_CONTAINER
            shell = ADMIN_SHELL_ARGV
        else:
            workspace = _resolve_workspace_ws(websocket)
            try:
                if workspace == _WORKSPACE_SHARED:
                    if not _shared_enabled():
                        # Tenant hasn't provisioned a shared container.
                        # 4400 = HTTP 400 mirror; the term JS surfaces
                        # the close code in the disconnect banner so an
                        # operator can spot it from devtools.
                        await websocket.close(code=4400)
                        return
                    target_container = _ensure_shared()
                else:
                    target_container = _ensure(email)
            except Exception:
                logger.exception(
                    "ensure_%s_container failed for %s",
                    workspace, email,
                )
                await websocket.close(code=_CLOSE_SERVER_ERROR)
                return
            shell = USER_SHELL_ARGV

        # Step 5: now we accept — past this point we own the connection.
        await websocket.accept()

        # Step 6: open the docker exec.
        client = docker_client or _default_docker_client()
        try:
            # docker-py's exec_create returns a dict on most versions but a
            # bare string id on older builds — keep the local name `raw_exec`
            # so the type-pivot below reads as "extract id from raw" rather
            # than "exec_create reused as both call and result".
            raw_exec = client.api.exec_create(
                target_container,
                cmd=shell,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
            )
            exec_id = raw_exec["Id"] if isinstance(raw_exec, dict) else raw_exec
            # tty=True at exec_start MUST match exec_create; otherwise the
            # daemon emits stdcopy-framed chunks (\x01\x00\x00\x00\x00\x00\x00<len>
            # before each payload) and xterm.js renders the framing bytes as
            # control-char garbage instead of shell output — UI shows
            # "connected" but no usable shell.
            exec_stream = client.api.exec_start(exec_id, socket=True, demux=False, tty=True)
            # Disable the read timeout that docker-py inherits from the
            # docker client (~30s by default). Otherwise an idle shell
            # raises TimeoutError out of _read_chunk after that window,
            # _exec_to_ws returns, and we cleanly close the WebSocket
            # with code 1000 — which presents to the user as a "graceful"
            # disconnect every 30s. The pty_bridge handles real EOF via
            # zero-byte reads; we don't need a deadline on top.
            inner_sock = getattr(exec_stream, "_sock", None)
            if inner_sock is not None:
                try:
                    inner_sock.settimeout(None)
                except OSError:
                    logger.debug("could not clear exec socket timeout", exc_info=True)
        except Exception:
            logger.exception(
                "exec_create/exec_start failed (container=%s, role=%s)",
                target_container, role,
            )
            await websocket.close(code=_CLOSE_SERVER_ERROR)
            return

        # Step 7: hand off both directions.
        try:
            await pty_bridge.bridge(
                websocket, exec_stream, exec_id, docker_client=client
            )
        finally:
            # Best-effort: try to close the exec stream and the ws.
            try:
                if hasattr(exec_stream, "close"):
                    exec_stream.close()
            except Exception:
                logger.debug("exec_stream close raised", exc_info=True)
            try:
                await websocket.close()
            except Exception:
                # Already closed; ignore.
                pass

    return app


# Module-level default app for `uvicorn app:app`.
app = create_app()
