"""Chat service FastAPI app.

Phase 1: healthz, /api/me, session CRUD.
Phase 2 (initial): SSE message endpoint, attachments, title generation,
attachment-purge on session delete.
Phase 2 (Bug 1 fix): the SSE handler now persists the user message and an
assistant placeholder *before* iterating the model stream, then updates the
placeholder by ``seq`` on ``done`` / ``error`` / disconnect. A per-session
asyncio lock (held in ``post_message`` and across the streaming generator)
serialises concurrent writers so the read-modify-write race in
``storage.append_messages`` cannot drop turns.

Importable with no env vars set — JWKS fetching is lazy inside ``auth``,
``claude_runner.spawn_claude`` does not run until a message is posted, and
``markers.py``/``attachments.py`` have no import-time side effects.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import mimetypes
import os
import urllib.parse
import re
import tarfile
import tempfile
import time
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import account_router
import artifact_runner
import attachments
import chat_scheduler
import claude_runner
import docker
import haihub_runner
import image_gen
import local_runner
import tokenhub_runner
import mimo_runner
import storage
import user_container
from session_process import SessionProcessManager

# AUTH_MODE switch: "local" → email+password JWT via local_auth (chat.ald3.com
# + dev.ald3.com side). Anything else (default) → the existing CF Access JWT
# path in auth.py (wizerith.ai + dev.wizerith.ai stay on this). This single
# rebind makes every `Depends(require_user)` below pick up the right verifier
# without any per-route changes.
_AUTH_MODE = os.environ.get("AUTH_MODE", "").strip().lower()
if _AUTH_MODE == "local":
    from local_auth import require_local_user as require_user  # noqa: F401
else:
    from auth import require_user  # noqa: F401

logger = logging.getLogger("chat.app")

app = FastAPI(title="chat", version="0.2.0")


# When AUTH_MODE=local, mount the /api/auth/* routes and seed the DB at startup.
# When unset, this is completely skipped so chat-wizerith never imports/touches
# local_auth.py at runtime.
if _AUTH_MODE == "local":
    import local_auth
    import local_auth_routes
    app.include_router(local_auth_routes.router)

    @app.on_event("startup")
    async def _seed_local_auth_db() -> None:
        try:
            local_auth.init_db_and_seed()
        except Exception:
            logger.exception("local_auth DB init failed; auth endpoints may 500")


# ---------------------------------------------------------------------------
# Identity / role configuration.
#
# ``role`` on /api/me is consumed by the frontend Sidebar (Phase 2) to gate
# admin-only UI like the Terminal button. Resolution priority, in order:
#
#   1. ``sandbox_users.json`` next to this file, if present and the
#      authenticated email is keyed in it. JSON shape:
#          {"someone@example.com": "admin", "other@example.com": "user"}
#      The file is intentionally NOT shipped — it is an opt-in override
#      for sandbox / staging deployments. Missing file => fall through.
#      Malformed JSON => log a warning and fall through (do NOT 500 the
#      identity endpoint over a bad ops file).
#
#   2. ``FELIX_EMAIL`` env var: when the authed email matches it, role
#      is ``"admin"``; otherwise ``"user"``.
#
#   3. ``DEFAULT_FELIX_EMAIL`` constant fallback when the env var is
#      unset. Real deployments should set ``FELIX_EMAIL`` in the chat
#      service's environment (docker-compose / systemd unit) rather than
#      relying on this default.
# ---------------------------------------------------------------------------

DEFAULT_FELIX_EMAIL = "felix@ald3.com"
# Default admin set when ADMIN_EMAILS is unset. Includes both real admin
# accounts so a fresh deploy without ops env-vars still grants them admin
# (which routes their chat turns to the chat-host-shell container with full
# host visibility — see claude_runner._build_dispatch).
DEFAULT_ADMIN_EMAILS: tuple[str, ...] = (
    "victorchiu2003@gmail.com",
    "supzekai@gmail.com",
)
_SANDBOX_USERS_PATH = Path(__file__).resolve().parent / "sandbox_users.json"
_VALID_ROLES = ("admin", "user")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CreateSessionBody(BaseModel):
    title: str | None = None
    # "personal" (default) routes claude into the per-email container;
    # "shared" routes into the tenant's shared container, so coworkers
    # collaborating from different sessions see the same /workspace
    # filesystem. Only honored when the deployment has set
    # WIZERITH_SHARED_CONTAINER_NAME — otherwise the create endpoint
    # 400s with "shared workspace not enabled" so the frontend can
    # surface a clear message instead of silently falling back.
    workspace: str | None = None


class RenameSessionBody(BaseModel):
    """PATCH /api/sessions/{id} body.

    All fields optional; only fields explicitly supplied are written.
    Kept under the legacy class name so the public route signature
    stays stable — clients that historically POSTed ``{title}`` still
    work, and new clients can add starred/archived/folder.
    """
    title: str | None = None
    starred: bool | None = None
    archived: bool | None = None
    folder: str | None = None


# ---------------------------------------------------------------------------
# Model lineup + reasoning-effort control (2026-09-28).
#
# chat.wizerith.ai now leads with the TokenHub-hosted GLM-5.3 / Kimi K3 and
# no longer offers the Anthropic/claude models. Set CHAT_DEFAULT_MODEL on
# the chat-wizerith service to activate the new lineup: any model value
# that is not one of the non-claude aliases below (missing / "default" /
# a stale claude alias from a pre-change tab) is mapped to the default
# model instead of reaching the claude CLI. With the env unset the legacy
# behaviour (claude path, anthropic aliases honoured) is preserved — the
# pytest suite runs there, hermetically.
# ---------------------------------------------------------------------------
CHAT_DEFAULT_MODEL = os.environ.get("CHAT_DEFAULT_MODEL", "").strip()
# "mimo" / "mimo-flash" (MiMo V2.6 Pro / Flash) served since 2026-09-29 on
# the Xiaomi Token Plan key in ~/.mimo_key (see mimo_runner).
NON_CLAUDE_MODELS = frozenset({
    "glm", "kimi", "mimo", "mimo-flash", "qwen", "deepseek", "minimax",
    "gemma4-local",
})


def _normalize_model(model: str | None) -> str | None:
    """Map ``model`` onto the served lineup (new lineup only).

    Identity when CHAT_DEFAULT_MODEL is unset (legacy mode). With it set,
    known non-claude aliases pass through; everything else — including
    ``None``/``"default"`` (which used to mean "let the claude CLI pick")
    and stale claude aliases — becomes the default model.
    """
    if not CHAT_DEFAULT_MODEL:
        return model
    return model if model in NON_CLAUDE_MODELS else CHAT_DEFAULT_MODEL


# Per-model reasoning-effort levels, verified against each gateway: GLM,
# DeepSeek, Qwen and MiniMax reject unsupported values (so an invalid
# stored/POSTed level must never reach the payload), while Kimi's gateway
# accepts any string (its set is the conservative subset). Models without
# an entry (gemma4-local; claude aliases in legacy mode) offer no effort
# control — the frontend hides the selector and the backend drops the
# value.
EFFORT_LEVELS: dict[str, tuple[str, ...]] = {
    "glm": ("low", "high", "max"),
    "kimi": ("low", "medium", "high", "max"),
    # MiMo Token Plan: low/medium/high accepted, "max" -> HTTP 400 (2026-09-29).
    "mimo": ("low", "medium", "high"),
    "mimo-flash": ("low", "medium", "high"),
    "qwen": ("none", "low", "medium", "high"),
    "deepseek": ("none", "low", "medium", "high", "max"),
    "minimax": ("none", "low", "medium", "high"),
}


# Hidden-reasoning text persisted per assistant message (``meta.reasoning``)
# so the thinking block survives a reload. Display-only: how much of it the
# UI shows is a client setting (off / brief / full) and nothing here changes
# what is requested from the model. Capped because a 64k-token reasoning
# trace is ~250 KB and session files are read whole on every poll.
REASONING_PERSIST_CAP = 120_000
_REASONING_CAP_NOTE = "\n\n[thinking truncated for storage]"


def _capped_reasoning(parts: list[str]) -> str | None:
    """Join streamed reasoning deltas for persistence, or None if empty."""
    if not parts:
        return None
    text = "".join(parts)
    if not text.strip():
        return None
    if len(text) > REASONING_PERSIST_CAP:
        text = text[:REASONING_PERSIST_CAP] + _REASONING_CAP_NOTE
    return text


def _validated_effort(model: str | None, effort: str | None) -> str | None:
    """Effort level for ``model`` iff supported and in the level set."""
    if not effort:
        return None
    levels = EFFORT_LEVELS.get(model or "")
    if not levels:
        return None
    return effort if effort in levels else None


class SendMessageBody(BaseModel):
    text: str
    # Optional per-turn model override. Frontend passes one of
    # ``opus``/``sonnet``/``haiku``; anything else is ignored by
    # claude_runner._validated_model. Absence means "let the CLI pick".
    model: str | None = None
    # Optional per-turn reasoning-effort override (OpenAI-style
    # ``reasoning_effort`` on the TokenHub/haihub paths). Validated
    # against EFFORT_LEVELS for the effective model; anything else is
    # dropped and the provider default applies.
    effort: str | None = None
    # Per-turn mode. ``"chat"`` (default) routes to the claude worker;
    # ``"image"`` routes to the gemini image-generation worker. Anything
    # else is treated as ``"chat"`` so a stale client cannot break.
    mode: str | None = None


class ForkSessionBody(BaseModel):
    """POST /api/sessions/{id}/fork body.

    ``from_seq`` is the seq of the message the user wants to replace
    (typically a user message). The new session inherits messages
    ``seq < from_seq`` from the source; the next POST .../messages
    against the new session id sends ``text`` and the worker assembles
    a context preamble so claude continues coherently in a fresh
    underlying claude session.
    """
    from_seq: int
    text: str
    model: str | None = None


# ---------------------------------------------------------------------------
# Background-task tracking. We keep strong refs to scheduled title tasks so
# the event loop doesn't garbage-collect them mid-flight, AND so tests can
# await completion deterministically (see ``conftest._drain_background_tasks``).
# ---------------------------------------------------------------------------

_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_background(coro: Any) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_email(claims: dict[str, Any]) -> str:
    """Trust boundary: ``require_user`` already enforced presence + type.
    Normalised (strip + lower) so a differently-cased email from the IdP maps
    to the same user dir and passes the per-file ownership check."""
    return str(claims["email"]).strip().lower()


def _felix_email() -> str:
    """Return felix's email — env var ``FELIX_EMAIL`` or the module default.

    Read each call (not cached) so tests can ``monkeypatch.setenv`` without
    re-importing the module. The env-var read is one ``os.environ.get``;
    the cost is negligible for a /api/me call.
    """
    return os.environ.get("FELIX_EMAIL") or DEFAULT_FELIX_EMAIL


def _admin_emails() -> frozenset[str]:
    """Return the admin email allowlist.

    ``ADMIN_EMAILS`` env var (comma-separated) wins; falls back to the
    module-default tuple plus FELIX_EMAIL so single-address deployments
    keep working without setting the new env var. Lower-cased and
    de-duped to make matching robust to client-side casing variants.
    Read each call so tests can monkeypatch without reimport.
    """
    raw = os.environ.get("ADMIN_EMAILS")
    if raw:
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif os.environ.get("AUTH_MODE", "").strip().lower() == "local":
        # Fail closed for a local-auth tenant: the module defaults are the
        # ald3 personal addresses, and admin turns run in the host shell.
        return frozenset()
    else:
        items = list(DEFAULT_ADMIN_EMAILS)
    items.append(_felix_email())
    return frozenset(s.lower() for s in items)


def _load_sandbox_users() -> dict[str, str] | None:
    """Read ``sandbox_users.json`` if it exists, return its dict.

    Return values:
      * ``None`` — file absent (the production path), unreadable, not
        valid JSON, or not a JSON object. Caller should fall through to
        the env-var path. Errors are logged but never raised.
      * ``dict`` — the file parsed cleanly. May be empty (``{}``) if
        every entry was rejected by the per-entry role-validity filter;
        that is distinguishable from ``None`` and tells the caller "the
        file exists and is well-formed, but doesn't list this email" —
        which still routes to fall-through in ``_resolve_role`` for any
        email not keyed in the dict.

    The expected shape is ``{email: role}`` with role in ``_VALID_ROLES``;
    invalid roles for individual entries are dropped silently so one bad
    line cannot disqualify the whole file.
    """
    try:
        raw = _SANDBOX_USERS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "sandbox_users.json present but unreadable; falling through",
            exc_info=True,
        )
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "sandbox_users.json is not valid JSON; falling through",
            exc_info=True,
        )
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "sandbox_users.json is not a JSON object (got %s); falling through",
            type(parsed).__name__,
        )
        return None
    cleaned: dict[str, str] = {}
    for email, role in parsed.items():
        if isinstance(email, str) and isinstance(role, str) and role in _VALID_ROLES:
            cleaned[email] = role
    return cleaned


def _resolve_role(email: str) -> str:
    """Resolve the role for ``email``. Returns one of ``_VALID_ROLES``.

    See the ``Identity / role configuration`` block at module top for the
    full priority order. ``ADMIN_EMAILS`` is the admin allowlist; anyone
    listed there (or FELIX_EMAIL, for legacy compat) is admin unless
    ``sandbox_users.json`` overrides them. Match is case-insensitive.
    """
    sandbox = _load_sandbox_users()
    if sandbox is not None and email in sandbox:
        return sandbox[email]
    return "admin" if email.lower() in _admin_emails() else "user"


def _summary(session: dict[str, Any]) -> dict[str, Any]:
    """Project a session dict to the create/list response shape.

    Note the asymmetry vs ``GET /api/sessions/{id}`` and ``PATCH``: the
    brief specifies that ``POST`` returns only ``{id, title, created_at,
    updated_at}`` while ``GET`` returns the full session (including
    ``messages`` and ``claude_session_id``). This helper exists for
    that single projection. ``list_sessions`` already returns summaries
    from the storage layer, so it does not need to call this.
    """
    return {
        "id": session["id"],
        "title": session.get("title"),
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "workspace": session.get("workspace"),
    }


# ---------------------------------------------------------------------------
# Generated images (Gemini image-gen mode).
#
# Saved under a SEPARATE directory from user uploads so the per-turn
# attachments purge in the worker's finally doesn't wipe them. Each
# session gets its own subdirectory; on session delete we recursively
# remove it (see delete_session).
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402

# Image-gen worker writes <hex>.<ext>; claude artifacts (.png/.svg/.html/.csv
# /.json/.mmd) write arbitrary basenames. Keep both safe:
#   - no path separator (blocks traversal)
#   - no leading dot or `..` segments (blocks hidden / parent-dir tricks)
#   - reasonable length cap
# A second canonical-path startswith check at the route level is the
# defence-in-depth backstop in case this regex misses something.
# Any single path component the model may have used as a file name: no
# separators, no leading dot (hidden/`..`), no control characters, bounded
# length. Non-ASCII (Chinese titles), spaces and punctuation are all fine —
# the serve route still resolves the path and checks it stays inside the
# per-session dir. Links are percent-encoded by _scan_new_artifacts; the
# route sees the decoded name. (Was ASCII-only, which 404'd every artifact
# with a non-ASCII or spaced name even though the file was on disk.)
_GENERATED_FILENAME_RE = _re.compile(r"^(?!\.)[^/\\\x00-\x1f\x7f]{1,200}$")
_GENERATED_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".json": "application/json",
    ".mmd": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".pdf": "application/pdf",
    ".xml": "application/xml; charset=utf-8",
    ".xsl": "application/xml; charset=utf-8",
    ".xslt": "application/xml; charset=utf-8",
    # Source code: served as text/plain so browsers display rather than
    # download. The frontend syntax-highlights based on the extension.
    ".py": "text/x-python; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".jsx": "text/javascript; charset=utf-8",
    ".ts": "text/typescript; charset=utf-8",
    ".tsx": "text/typescript; charset=utf-8",
    ".go": "text/x-go; charset=utf-8",
    ".rs": "text/x-rust; charset=utf-8",
    ".java": "text/x-java; charset=utf-8",
    ".kt": "text/x-kotlin; charset=utf-8",
    ".swift": "text/x-swift; charset=utf-8",
    ".c": "text/x-c; charset=utf-8",
    ".h": "text/x-c; charset=utf-8",
    ".cpp": "text/x-c++; charset=utf-8",
    ".hpp": "text/x-c++; charset=utf-8",
    ".cs": "text/x-csharp; charset=utf-8",
    ".rb": "text/x-ruby; charset=utf-8",
    ".php": "text/x-php; charset=utf-8",
    ".pl": "text/x-perl; charset=utf-8",
    ".lua": "text/x-lua; charset=utf-8",
    ".sh": "text/x-shellscript; charset=utf-8",
    ".bash": "text/x-shellscript; charset=utf-8",
    ".zsh": "text/x-shellscript; charset=utf-8",
    ".fish": "text/x-shellscript; charset=utf-8",
    ".sql": "application/sql; charset=utf-8",
    ".r": "text/x-r; charset=utf-8",
    ".yaml": "text/yaml; charset=utf-8",
    ".yml": "text/yaml; charset=utf-8",
    ".toml": "text/x-toml; charset=utf-8",
    ".ini": "text/plain; charset=utf-8",
    ".cfg": "text/plain; charset=utf-8",
    ".dockerfile": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    # Additional code languages: served as text/plain so the inline
    # CodeBody renderer can syntax-highlight via its extension→language map.
    ".scala": "text/x-scala; charset=utf-8",
    ".dart": "text/x-dart; charset=utf-8",
    ".ex": "text/x-elixir; charset=utf-8",
    ".exs": "text/x-elixir; charset=utf-8",
    ".clj": "text/x-clojure; charset=utf-8",
    ".cljs": "text/x-clojure; charset=utf-8",
    ".hs": "text/x-haskell; charset=utf-8",
    ".zig": "text/plain; charset=utf-8",
    ".nim": "text/x-nim; charset=utf-8",
    ".jl": "text/x-julia; charset=utf-8",
    # Video: served with proper Content-Type so <video> can play natively
    # in the browser. .mkv/.avi may not be supported in all browsers but
    # the Content-Type is honest; the artifact still downloads cleanly.
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    # Audio: native <audio> playback.
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    # Office docs: download-only in the inline view (no in-browser
    # preview); model is told to also save a .pdf alongside for inline
    # viewing.
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    # Diagram source formats: .dot (Graphviz) and .puml (PlantUML) render
    # as syntax-highlighted source — no in-browser graph rendering yet.
    # .drawio is XML; .excalidraw is JSON; both render via the existing
    # XML/JSON bodies.
    ".dot": "text/vnd.graphviz; charset=utf-8",
    ".puml": "text/x-plantuml; charset=utf-8",
    ".drawio": "application/xml; charset=utf-8",
    ".excalidraw": "application/json; charset=utf-8",
    # Jupyter notebooks: parsed cell-by-cell in the inline viewer.
    ".ipynb": "application/x-ipynb+json; charset=utf-8",
    # 3D / CAD: text formats (.obj/.stl ASCII, .gltf JSON) get inline
    # source previews; binary .glb is download-only.
    ".obj": "text/plain; charset=utf-8",
    ".stl": "text/plain; charset=utf-8",
    ".gltf": "model/gltf+json",
    ".glb": "model/gltf-binary",
    # Geo: .geojson piggybacks on the JSON viewer; .kml on the XML viewer.
    ".geojson": "application/geo+json",
    ".kml": "application/vnd.google-earth.kml+xml; charset=utf-8",
    # Archives: download-only.
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    # Fonts: rendered with a sample-text preview using @font-face.
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    # Columnar data formats — quant workflows cache OHLCV here. Decoded
    # in the browser to a tabular preview using hyparquet (parquet) /
    # apache-arrow (feather/arrow IPC).
    ".parquet": "application/vnd.apache.parquet",
    ".feather": "application/vnd.apache.feather",
    ".arrow": "application/vnd.apache.arrow.file",
}


def _generated_root() -> Path:
    return Path(os.environ.get("CHAT_GENERATED_DIR", "/data/generated"))


def _session_generated_dir(session_id: str) -> Path:
    """Per-session dir for AI-generated images. Validated session_id only."""
    if not storage._is_valid_session_id(session_id):
        raise ValueError("session_id must be UUID-shaped")
    return _generated_root() / session_id


def _delete_generated_dir(session_id: str) -> None:
    import shutil
    try:
        d = _session_generated_dir(session_id)
    except ValueError:
        return
    shutil.rmtree(d, ignore_errors=True)


def _format_prior_history(messages: list[dict[str, Any]]) -> str:
    """Render a list of session messages as a human-readable transcript.

    Used as the context preamble for a fork-first-turn (new claude
    session, but the chat session has inherited messages from a fork).
    Skips empty assistant placeholders and tool-output marker spans
    that aren't useful as model context.
    """
    lines: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "assistant" and m.get("status") == "error":
            # Failure notices are not conversation; feeding them back teaches
            # the model to reproduce them.
            continue
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
    return "\n\n".join(lines)


# Stateless (OpenAI-compatible) runners get the session's own transcript on
# EVERY turn — haihub/tokenhub/local have no server-side session to --resume,
# so without this turn 2+ has no memory of turn 1 (see
# tests/test_api_history_continuity.py). Capped so a very long thread can't
# blow the provider's context window: keep the most recent tail.
_STATELESS_HISTORY_MAX_CHARS = int(
    os.environ.get("CHAT_STATELESS_HISTORY_MAX_CHARS", "120000")
)


def _stateless_history(messages: list[dict[str, Any]]) -> str | None:
    """Full prior transcript for a stateless runner, or None when empty."""
    text = _format_prior_history(messages)
    if not text:
        return None
    if len(text) > _STATELESS_HISTORY_MAX_CHARS:
        tail = text[-_STATELESS_HISTORY_MAX_CHARS:]
        # Cut at a turn boundary so the model never sees half a code fence
        # or a truncated "User:" line.
        at = tail.find("\n\nUser: ")
        if at != -1:
            tail = tail[at + 2:]
        text = (
            "[Earlier history truncated — only the most recent part of this "
            "conversation is shown.]\n\n" + tail
        )
    return text


def _sse_event(event_type: str, payload: dict[str, Any]) -> bytes:
    """Format one SSE frame.

    ``data:`` is a JSON-encoded payload so the client can ``JSON.parse``
    it directly. We always include the ``event:`` line so frontend
    consumers can dispatch on type without parsing every payload.
    """
    body = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {body}\n\n".encode("utf-8")


# Reuse storage's timestamp helper so transcript ts values use the same
# microsecond-precision ISO-8601 shape as session created_at/updated_at; this
# keeps the lexicographic sort in list_sessions() consistent across both.
# Phase 2 promoted the helper to a public ``storage.now_iso``; we import
# the public name directly here. (The legacy ``storage._now_iso`` alias
# is kept inside storage.py for any out-of-tree caller that still binds
# the underscore name; this module no longer uses it.)
from storage import now_iso  # noqa: E402


# ---------------------------------------------------------------------------
# Per-user container auto-eviction (overnight item 8).
#
# Daily asyncio loop. By default DRY-RUN: real evictions require both
# ``CHAT_USER_EVICTION_ENABLED=1`` in the chat env (gate inside the
# eviction module) and ``dry_run=False`` here. The chat container
# already mounts /var/run/docker.sock (see docker-compose.yml), so this
# scheduling path is the smallest possible change — no new infra.
# ---------------------------------------------------------------------------

_EVICTION_INTERVAL_SECONDS = 24 * 60 * 60
_eviction_task: asyncio.Task[Any] | None = None


async def _eviction_loop() -> None:
    """Sleep first, then evict, then loop. The leading sleep avoids
    running on every chat container restart."""
    # Local import: keep app startup independent of the eviction
    # module's availability — if it's missing from the image (or fails
    # to import), only the loop dies, not the whole chat service.
    import user_container_eviction
    while True:
        try:
            await asyncio.sleep(_EVICTION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        try:
            actions = user_container_eviction.evict_idle_containers(dry_run=False)
            logger.info(
                "user-container eviction pass: %d actions",
                len(actions),
                extra={"actions": [a.__dict__ for a in actions]},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logger.warning("eviction loop iteration failed: %r", exc)


@app.on_event("startup")
async def _start_eviction_task() -> None:
    global _eviction_task
    _eviction_task = asyncio.create_task(_eviction_loop())


# Set by the FIRST shutdown hook. A worker whose task is cancelled while this
# is True is dying with the process, not by user request: it must leave its
# placeholder at status='streaming' (and its uploads on disk) so the next boot
# re-runs the turn instead of writing it off as cancelled.
_shutting_down = False


@app.on_event("shutdown")
async def _flag_shutdown() -> None:
    global _shutting_down
    _shutting_down = True


_RECOVERY_NOTE = (
    "\n\n[System note: the backend restarted while answering this message, so "
    "this turn is being re-run automatically. Files or work from the first "
    "attempt may already exist in the workspace — check before redoing it.]"
)


async def _recover_turn(rec: dict[str, Any]) -> bool:
    """Re-run one interrupted turn on its existing assistant placeholder.

    Mirrors post_message's spawn: take the session lock, register a TurnRun
    under the SAME assistant seq (so the client's bubble fills in and a GET
    .../stream re-attaches to it), and hand off to _run_turn_worker with the
    user's default model. Returns True if a worker was spawned.
    """
    email, session_id, seq = rec["email"], rec["session_id"], rec["seq"]
    session = storage.get_session(email, session_id)
    if session is None or not isinstance(seq, int):
        return False
    msgs = session.get("messages") or []
    if not msgs or msgs[-1].get("seq") != seq or msgs[-1].get("status") != storage.ASSISTANT_STATUS_STREAMING:
        return False
    key = (email, session_id)
    lock = storage.get_session_lock(email, session_id)
    await lock.acquire()
    if key in _active_runs:
        lock.release()
        return False
    prior = [m for m in msgs[:-2]]
    try:
        _settings_blob = storage.get_settings(email)
    except Exception:
        _settings_blob = {}
    if not isinstance(_settings_blob, dict):
        _settings_blob = {}
    turn_cfg = rec.get("turn") if isinstance(rec.get("turn"), dict) else {}
    model = _normalize_model(turn_cfg.get("model") or _settings_blob.get("default_model"))
    effort = _validated_effort(model, turn_cfg.get("effort"))
    is_first_turn = not session.get("claude_initialized")
    if is_first_turn:
        try:
            storage.mark_claude_initialized(email, session_id)
        except Exception:
            pass
    prior_history = _format_prior_history(prior) if (is_first_turn and prior) else None
    run = _TurnRun(email=email, session_id=session_id, assistant_seq=seq)
    _active_runs[key] = run
    task = asyncio.create_task(
        _run_turn_worker(
            run=run,
            user_text=str(rec["user_text"]) + _RECOVERY_NOTE,
            claude_session_id=session["claude_session_id"],
            account=session.get("account"),
            role=session.get("role"),
            container=session.get("container"),
            is_first_turn=is_first_turn,
            title_was_empty=not session.get("title"),
            lock=lock,
            model=model,
            effort=effort,
            prior_history=prior_history,
            stateless_history=_stateless_history(prior),
        )
    )
    _active_tasks[key] = task

    def _cleanup(_t: asyncio.Task[None], _k: tuple[str, str] = key) -> None:
        if _active_runs.get(_k) is run:
            _active_runs.pop(_k, None)
        if _active_tasks.get(_k) is _t:
            _active_tasks.pop(_k, None)

    task.add_done_callback(_cleanup)
    logger.warning(
        "restart recovery: re-running interrupted turn %s seq=%s on model=%s",
        session_id, seq, model,
    )
    return True


async def _recover_interrupted_turns() -> tuple[int, int]:
    """Boot-time handling of assistant messages left at status='streaming'.

    A backend restart (deploy, crash, OOM) kills every in-flight turn. The
    user's message is already persisted, so a turn whose placeholder is the
    last message of its thread is simply re-run on the same bubble; anything
    else (a wake — the scheduler's lease re-fires it — or a placeholder that
    is no longer the tail) is flipped to error with a restart notice so the
    spinner clears. Returns (recovered, swept).
    """
    try:
        found = await asyncio.to_thread(storage.find_streaming_placeholders)
    except Exception as exc:  # noqa: BLE001
        logger.warning("restart recovery: scan failed: %r", exc)
        return (0, 0)
    keep = {
        (r["email"], r["session_id"], r["seq"]) for r in found if r["recoverable"]
    }
    try:
        swept = await asyncio.to_thread(storage.sweep_stale_streaming_messages, keep)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stale-streaming sweep failed at boot: %r", exc)
        swept = 0
    recovered = 0
    for rec in found:
        if not rec["recoverable"]:
            continue
        try:
            if await _recover_turn(rec):
                recovered += 1
        except Exception:
            logger.exception(
                "restart recovery failed for %s seq=%s", rec["session_id"], rec["seq"],
            )
    if recovered or swept:
        logger.warning(
            "restart recovery: re-ran %d interrupted turn(s), swept %d → error",
            recovered, swept,
        )
    return (recovered, swept)


@app.on_event("startup")
async def _sweep_stale_streaming() -> None:
    """Boot-time handling of assistant messages stuck at status='streaming':
    re-run the ones that can be (see _recover_interrupted_turns), write off
    the rest so the client's spinner clears.

    The worker's try/except persists a terminal status on every clean
    exit (complete / error / cancelled), so the only way a message ends
    up permanently 'streaming' is via a SIGKILL / OOM / container restart
    mid-turn — those events skip Python exception handlers entirely.
    Without this sweep the frontend's reconnect path sees status=
    'streaming' on page-load forever and the spinner never clears.
    """
    await _recover_interrupted_turns()


@app.on_event("startup")
async def _start_artifact_runner_gc() -> None:
    artifact_runner.install_gc_task(asyncio.get_running_loop())


# A1: idle-evict long-lived session processes. Stop event + task tracked so
# shutdown can drain them cleanly (and so tests don't leak the loop).
_session_evict_stop = asyncio.Event()
_session_evict_task: asyncio.Task[None] | None = None


@app.on_event("startup")
async def _start_session_eviction() -> None:
    if not _persistent_enabled():
        return
    global _session_evict_task
    # Evict a session's process after 15 min idle with no open turn and no
    # pending background task (never mid-wait — pending_bg gates it).
    _session_evict_task = asyncio.create_task(
        _session_mgr.run_eviction_loop(
            _session_evict_stop, interval=60.0, idle_grace=900.0
        )
    )


@app.on_event("shutdown")
async def _stop_session_processes() -> None:
    _session_evict_stop.set()
    if _session_evict_task is not None:
        _session_evict_task.cancel()
    try:
        await _session_mgr.aclose_all()
    except Exception:
        logger.exception("session-process aclose_all failed at shutdown")


@app.on_event("startup")
async def _start_schedule_tick() -> None:
    # Durable scheduled-wake firing. Runs REGARDLESS of the persistent-session
    # kill-switch: _fire_schedule fires via the live session process when
    # persistent is on, and via the per-turn run_turn path when it's off, so a
    # wake never silently no-ops just because CHAT_PERSISTENT_SESSIONS=0.
    # Survives restart because the store is a file; a wake armed before a
    # restart fires on the first tick after boot (catch-up collapses misses).
    global _schedule_tick_task
    _schedule_tick_stop.clear()
    _schedule_tick_task = asyncio.create_task(_schedule_tick_loop(interval=30.0))


@app.on_event("shutdown")
async def _stop_schedule_tick() -> None:
    _schedule_tick_stop.set()
    if _schedule_tick_task is not None:
        _schedule_tick_task.cancel()


@app.on_event("startup")
async def _heal_auth_proxies() -> None:
    # After a host reboot per-user containers come back up but the auth
    # proxy (a docker-exec'd background process) does not. Without this
    # heal, the first turn for each user fails with ECONNREFUSED on the
    # proxy port; ensure_user_container would only respawn it on the
    # *next* turn. Iterate matching containers once at boot so reboots
    # auto-heal. Best-effort: a single broken container must not block
    # startup.
    try:
        import docker  # type: ignore

        client = docker.from_env()
        containers = client.containers.list(all=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("auth-proxy heal: containers.list failed: %r", exc)
        return
    shared_name = user_container.shared_container_name()
    for c in containers:
        name = getattr(c, "name", "") or ""
        # Heal both per-user (prefix-matched) AND the tenant's shared
        # container if configured. Same docker-exec'd auth-proxy in both
        # cases — it dies with the in-container restart and the runner
        # would otherwise ECONNREFUSE on the first turn after reboot.
        if name.startswith(user_container.CONTAINER_NAME_PREFIX):
            pass
        elif shared_name and name == shared_name:
            pass
        else:
            continue
        try:
            await asyncio.to_thread(user_container.ensure_auth_proxy_running, name)
            logger.info("auth-proxy heal: %s ok", name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auth-proxy heal: %s failed: %r", name, exc)


@app.on_event("shutdown")
async def _stop_eviction_task() -> None:
    global _eviction_task
    if _eviction_task is None:
        return
    _eviction_task.cancel()
    try:
        await _eviction_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _eviction_task = None


# ---------------------------------------------------------------------------
# Public health (NOT auth-gated)
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "chat"}


# ---------------------------------------------------------------------------
# Identity echo
# ---------------------------------------------------------------------------

@app.get("/api/me")
async def me(claims: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    # ``role`` added in Phase 1 — see "Identity / role configuration" block above.
    # ``settings`` added so the frontend can apply theme / send_on_enter / etc.
    # before the first user input — bundling avoids a second request on page load.
    # ``shared_workspace_enabled`` is true iff this deployment has set
    # WIZERITH_SHARED_CONTAINER_NAME — the SPA hides the workspace toggle
    # entirely on tenants where shared mode is not provisioned.
    # ``max_message_bytes`` publishes the POST /messages text cap so the
    # composer can spill oversized text into a .txt attachment BEFORE
    # sending. Without it the SPA had to hardcode a guess, and a deployment
    # that overrode CHAT_MAX_MESSAGE_BYTES would silently 413 again.
    email = _user_email(claims)
    return {
        "email": email,
        "role": _resolve_role(email),
        "settings": storage.get_settings(email),
        "shared_workspace_enabled": bool(user_container.shared_container_name()),
        "max_message_bytes": MAX_MESSAGE_TEXT_BYTES,
    }


@app.get("/api/settings")
async def get_settings_endpoint(
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    return storage.get_settings(_user_email(claims))


@app.put("/api/settings")
async def put_settings_endpoint(
    body: dict[str, Any],
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return storage.update_settings(_user_email(claims), body)


# Cross-session memory: a freeform markdown blob the model reads in front
# of every turn and may rewrite at the end via the <memory_update> sentinel
# block (parsed in _run_turn_worker). The endpoints let the user inspect
# / hand-edit / clear memory from the Settings UI. Body for PUT is plain
# text on a "text" field (not a JSON object of settings) so we keep memory
# decoupled from the settings JSON blob.
class _MemoryBody(BaseModel):
    text: str


@app.get("/api/memory")
async def get_memory_endpoint(
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    return {"text": storage.get_memory(_user_email(claims))}


@app.put("/api/memory")
async def put_memory_endpoint(
    body: _MemoryBody,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    return {"text": storage.set_memory(_user_email(claims), body.text)}


# Filesystem picker: powers the @-autocomplete in the Composer. Returns
# children of ``prefix``'s parent dir filtered by basename prefix, like
# shell tab-completion. Sandboxed per role: admin sees /home/felix/...;
# user sees /workspace/... inside their per-user container (via docker
# exec, slow but only ~50ms per call). Blocks symlinks-out-of-root via
# strict prefix-startswith on the resolved path.
_FILE_PICKER_HIDDEN_DIRS = frozenset({
    "node_modules", "__pycache__", ".git", "venv", ".venv",
    ".nix-profile", ".cache", ".npm", ".cargo", ".local",
    "target", "build", "dist", ".pytest_cache", ".mypy_cache",
})


def _file_picker_root(role: str) -> str:
    return "/home/felix" if role == "admin" else "/workspace"


def _list_files_host(prefix: str, limit: int) -> list[dict[str, Any]]:
    """List host-filesystem children matching ``prefix``. Admin only."""
    root = "/home/felix"
    if not prefix:
        prefix = root + "/"
    if not prefix.startswith(root):
        return []
    if prefix.endswith("/"):
        directory, basename = prefix.rstrip("/") or "/", ""
    else:
        directory = os.path.dirname(prefix) or "/"
        basename = os.path.basename(prefix)
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    show_hidden = basename.startswith(".")
    out: list[dict[str, Any]] = []
    for name in entries:
        if not show_hidden and name.startswith("."):
            continue
        if name in _FILE_PICKER_HIDDEN_DIRS:
            continue
        if basename and not name.startswith(basename):
            continue
        full = os.path.join(directory, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        is_dir = bool(st.st_mode & 0o040000)  # S_IFDIR bit
        out.append({
            "path": full,
            "name": name,
            "is_dir": is_dir,
            "size": int(st.st_size) if not is_dir else 0,
        })
    out.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
    return out[:limit]


def _list_files_in_container(
    container_name: str, prefix: str, limit: int,
) -> list[dict[str, Any]]:
    """List files inside the per-user container's /workspace via docker exec."""
    root = "/workspace"
    if not prefix:
        prefix = root + "/"
    if not prefix.startswith(root):
        return []
    if prefix.endswith("/"):
        directory, basename = prefix.rstrip("/") or "/", ""
    else:
        directory = os.path.dirname(prefix) or "/"
        basename = os.path.basename(prefix)
    # find -maxdepth 1 -mindepth 1 -printf '%y\t%s\t%f\n' lists immediate
    # children with type/size/name. We post-filter in Python so the docker
    # exec args stay simple and we don't have to escape the basename.
    cmd = [
        "find", directory, "-maxdepth", "1", "-mindepth", "1",
        "-printf", "%y\\t%s\\t%f\\n",
    ]
    try:
        proc = user_container._docker_exec(
            container_name, cmd, user="1000:1000", check=False,
        )
    except Exception:
        logger.exception("file picker: docker exec failed for %s", container_name)
        return []
    if proc.returncode != 0:
        return []
    out: list[dict[str, Any]] = []
    show_hidden = basename.startswith(".")
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        ftype, size_str, name = parts
        if not show_hidden and name.startswith("."):
            continue
        if name in _FILE_PICKER_HIDDEN_DIRS:
            continue
        if basename and not name.startswith(basename):
            continue
        is_dir = ftype == "d"
        try:
            size = int(size_str)
        except ValueError:
            size = 0
        full = os.path.join(directory, name)
        out.append({
            "path": full,
            "name": name,
            "is_dir": is_dir,
            "size": size if not is_dir else 0,
        })
    out.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
    return out[:limit]


@app.get("/api/files")
async def list_files_endpoint(
    prefix: str = "",
    limit: int = 50,
    workspace: str | None = None,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """List attachable files. The Composer passes the active session's
    workspace ("personal" / "shared") so the picker scopes to the same
    container the next turn will dispatch into; without this a user in a
    shared session would only see their personal /workspace.
    """
    email = _user_email(claims)
    role = _resolve_role(email)
    limit = max(1, min(200, limit))
    root = _file_picker_root(role)
    if role == "admin":
        items = _list_files_host(prefix, limit)
    else:
        if workspace == "shared":
            shared_name = user_container.shared_container_name()
            if not shared_name:
                # Shared mode requested on a tenant that never enabled it
                # — return an empty list rather than leaking the personal
                # container's view.
                return {"root": root, "items": []}
            container_name = shared_name
        else:
            try:
                container_name = user_container.container_name_for(email)
            except Exception:
                return {"root": root, "items": []}
        items = _list_files_in_container(container_name, prefix, limit)
    return {"root": root, "items": items}


# Matches a <memory_update>...</memory_update> block at the END of the
# assistant response (allowing trailing whitespace). DOTALL lets the inner
# content span newlines. Anchored to the end so a discussion of the
# memory_update protocol mid-conversation doesn't get mistakenly extracted.
# The closing tag accepts the abbreviated </memory> as well: the model
# frequently closes the block that way, and a strict </memory_update>-only
# match left those blocks unscrubbed so the raw tag leaked into the chat.
_MEMORY_UPDATE_RE = re.compile(
    r"\n*<memory_update>\s*\n?(.*?)\n?\s*</memory(?:_update)?>\s*\Z",
    re.DOTALL,
)
# Same block, anywhere in the text. A tool-loop reply is the join of every
# step's text, so a model that emitted the block in a middle step (before its
# last tool call) leaves it mid-reply; it must still be applied and stripped
# rather than shown raw to the user.
_MEMORY_UPDATE_ANY_RE = re.compile(
    r"[ \t]*\n*<memory_update>\s*\n?(.*?)\n?\s*</memory(?:_update)?>[ \t]*\n?",
    re.DOTALL,
)
# Strict closer first: a block that wraps a nested <memory>…</memory> would
# otherwise be cut at the inner closer and leak "</memory_update>" into the
# reply.
_MEMORY_UPDATE_STRICT_RE = re.compile(
    r"[ \t]*\n*<memory_update>\s*\n?(.*?)\n?\s*</memory_update>[ \t]*\n?",
    re.DOTALL,
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)


def _inside_code_fence(text: str, pos: int) -> bool:
    """True when ``pos`` falls inside a ``` / ~~~ fenced block — the model
    quoting the memory protocol (or cat-ing a file that contains the tag)
    must not overwrite the user's memory."""
    return sum(1 for _ in _FENCE_RE.finditer(text, 0, pos)) % 2 == 1


# ---------------------------------------------------------------------------
# Inline artifacts: files claude saves during a turn (plots, charts, CSV,
# JSON, HTML, etc.) get rendered inline beneath the assistant's response.
# Lifecycle:
#   1. Worker pre-turn: ensure the artifacts dir exists and is writable
#      from the dispatch claude is about to run under.
#   2. Worker tracks `turn_start_ts` when the run begins.
#   3. After claude's `done`, worker collects new files (mtime > start_ts)
#      from the dispatch's artifacts area and stages them under the
#      chat-side /data/generated/<sid>/ that the existing serving route
#      already exposes. Then appends one markdown line per artifact to the
#      assistant's text — `![](url)` for images so they render inline,
#      `[name](url)` for everything else (HTML/CSV/JSON/MMD) so the user
#      can click through.
#   4. Image-gen worker writes hex-named files to the same dir; a turn's
#      mtime filter ensures we don't double-pick those up across turns.
# ---------------------------------------------------------------------------

_ARTIFACT_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    ".html", ".htm",
    ".csv", ".tsv",
    ".json", ".mmd",
    ".xlsx", ".xls", ".ods",
    ".pdf",
    ".xml", ".xsl", ".xslt",
    # Source code: rendered with syntax-highlighting in the inline viewer.
    ".py", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".c", ".h", ".cpp", ".hpp", ".cs",
    ".rb", ".php", ".pl", ".lua",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".r", ".m",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".dockerfile",
    ".md", ".txt", ".log",
    ".scala", ".dart", ".ex", ".exs", ".clj", ".cljs",
    ".hs", ".zig", ".nim", ".jl",
    # Video / audio: native <video>/<audio> playback.
    ".mp4", ".mov", ".webm", ".mkv", ".avi",
    ".mp3", ".wav", ".flac", ".ogg", ".m4a",
    # Office docs: download-only inline (PDF export is the workaround for
    # in-page previewing).
    ".docx", ".doc", ".pptx", ".ppt",
    # Diagrams: .dot/.puml render as code; .drawio as XML; .excalidraw as JSON.
    ".dot", ".puml", ".drawio", ".excalidraw",
    # Jupyter notebooks: cell-by-cell inline viewer.
    ".ipynb",
    # 3D / CAD.
    ".obj", ".stl", ".gltf", ".glb",
    # Geo.
    ".geojson", ".kml",
    # Archives: download-only.
    ".zip", ".tar", ".gz",
    # Fonts: sample-text preview.
    ".ttf", ".otf", ".woff", ".woff2",
    # Columnar data formats: tabular preview in the browser.
    ".parquet", ".feather", ".arrow",
})
_INLINE_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
})


def _ensure_artifacts_dir(
    *, session_id: str, dispatch: str, container_name: str | None,
) -> None:
    """Idempotently create the per-session artifacts dir under whichever
    filesystem claude will see in this dispatch.

    Best-effort: a docker hiccup or mkdir failure logs and returns; the
    turn proceeds without inline artifacts (graceful degrade).
    """
    try:
        gen_dir = _session_generated_dir(session_id)
    except ValueError:
        return
    if dispatch in ("host", "local"):
        # Both reach the same on-host dir as the chat container's
        # /data/generated/<sid>. Creating from chat-side suffices.
        try:
            gen_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("ensure_artifacts_dir(chat-side) failed for %s", session_id)
        return
    if dispatch == "user" and container_name:
        # Per-user container needs the dir at /workspace/.artifacts/<sid>/.
        # Owned 1000:1000 so the user shell + claude (uid 1000 inside) can write.
        try:
            user_container._docker_exec(
                container_name,
                ["sh", "-c",
                 f"mkdir -p /workspace/.artifacts/{session_id} && "
                 f"chown 1000:1000 /workspace/.artifacts /workspace/.artifacts/{session_id}"],
                user="root",
                check=False,
            )
        except Exception:
            logger.exception(
                "ensure_artifacts_dir(user) failed for %s in %s",
                session_id, container_name,
            )


_ARTIFACT_TAR_MAX_BYTES = 512 * 1024 * 1024
_ARTIFACT_FILE_MAX_BYTES = 128 * 1024 * 1024


def _collect_user_container_artifacts(
    *, session_id: str, container_name: str,
) -> None:
    """Pull /workspace/.artifacts/<sid>/ out of the per-user container and
    extract into the chat-side /data/generated/<sid>/ so the existing file
    server can hand them out. No-op if the dir is empty / missing.

    Path-traversal guard: extracted files must resolve inside gen_dir.
    """
    try:
        gen_dir = _session_generated_dir(session_id)
    except ValueError:
        return
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
    except Exception:
        logger.exception(
            "collect_artifacts: cannot reach container %s", container_name,
        )
        return
    src_path = f"/workspace/.artifacts/{session_id}"
    try:
        stream, _stat = container.get_archive(src_path)
    except docker.errors.NotFound:
        # claude didn't save anything to the artifacts dir — fine.
        return
    except Exception:
        logger.exception("get_archive failed for %s:%s", container_name, src_path)
        return
    # Bounded and off-heap: the dir is user-writable (uid 1000 in the user's
    # own sandbox), so a multi-GB file there must not be slurped into the
    # backend's memory (no mem limit on this container → host swap thrash).
    tmp = tempfile.TemporaryFile()
    total = 0
    for chunk in stream:
        total += len(chunk)
        if total > _ARTIFACT_TAR_MAX_BYTES:
            logger.warning(
                "collect_artifacts: %s:%s exceeds %d bytes; skipping",
                container_name, src_path, _ARTIFACT_TAR_MAX_BYTES,
            )
            tmp.close()
            return
        tmp.write(chunk)
    tmp.seek(0)
    gen_dir.mkdir(parents=True, exist_ok=True)
    gen_root = gen_dir.resolve()
    try:
        with tarfile.open(fileobj=tmp, mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                if member.size > _ARTIFACT_FILE_MAX_BYTES:
                    logger.warning(
                        "collect_artifacts: skipping %s (%d bytes > cap)",
                        member.name, member.size,
                    )
                    continue
                # get_archive wraps everything under <session_id>/. Strip
                # that prefix so files land directly in gen_dir.
                parts = member.name.split("/", 1)
                rel = parts[1] if len(parts) > 1 else parts[0]
                if not rel or rel.startswith(".") or "/" in rel:
                    # Skip nested dirs and dotfiles for now — keeps the
                    # served URL space flat and predictable.
                    continue
                dest = gen_dir / rel
                # Defence-in-depth: ensure the resolved path is inside gen_dir.
                try:
                    if not str(dest.resolve()).startswith(str(gen_root) + os.sep) \
                            and dest.resolve() != gen_root:
                        continue
                except Exception:
                    continue
                fp = tar.extractfile(member)
                if fp is None:
                    continue
                # If dest exists and the tar member is the same size + same
                # source mtime, this is a re-extract from a cumulative
                # /workspace/.artifacts/<sid>/ that grew this turn — skip
                # so we don't bump the destination's mtime. That mtime is
                # what _scan_new_artifacts uses to decide "new this turn";
                # bumping it on every extract would make every prior
                # artifact reappear in every subsequent message's footer.
                try:
                    src_mtime = float(member.mtime)
                except (TypeError, ValueError):
                    src_mtime = None
                if src_mtime is not None and dest.exists():
                    try:
                        st = dest.stat()
                        # Tolerance: tar stores mtime as int seconds; the
                        # extracted file's stored mtime is also int after
                        # os.utime, so an exact equality check is fine here.
                        if st.st_size == member.size and int(st.st_mtime) == int(src_mtime):
                            continue
                    except OSError:
                        pass
                try:
                    dest.write_bytes(fp.read())
                    os.chmod(dest, 0o640)
                    if src_mtime is not None:
                        # Stamp dest with the model-side mtime so future
                        # re-extracts of unchanged files match the skip
                        # check above, AND so _scan_new_artifacts'
                        # turn_start_ts filter correctly excludes prior
                        # turns' files from the new turn's footer.
                        os.utime(dest, (src_mtime, src_mtime))
                except OSError:
                    logger.exception("write artifact %s failed", dest)
    except tarfile.TarError:
        logger.exception("artifact tar extraction failed for %s", container_name)


_IMAGE_REQUEST_PREFIX = "_image_request_"


async def _process_image_requests(*, session_id: str) -> None:
    """Drive Gemini for every ``_image_request_*.json`` marker the model
    dropped under /data/generated/<sid>/ this turn.

    Each marker file is a JSON object ``{"prompt": str, "filename":
    str}``. We POST the prompt to Gemini, write the returned image bytes
    to ``filename`` (in the same dir), then delete the marker. This is
    how the model autonomously generates AI images — it can't reach the
    Gemini API directly from a per-user container (no key, no egress)
    and shouldn't have the key on host either, so the chat backend is
    the only process that holds GEMINI_API_KEY. The marker protocol
    works uniformly across host / user / local dispatches because it
    only requires file write — same as any other artifact.

    Errors are logged and the marker is replaced with a sibling
    ``<filename>.error`` text file so the user sees what went wrong
    instead of silent nothing.
    """
    try:
        gen_dir = _session_generated_dir(session_id)
    except ValueError:
        return
    if not gen_dir.is_dir():
        return
    for p in sorted(gen_dir.iterdir()):
        if not p.is_file() or not p.name.startswith(_IMAGE_REQUEST_PREFIX):
            continue
        if p.suffix.lower() != ".json":
            continue
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("malformed image-request marker %s", p)
            try:
                p.unlink()
            except OSError:
                pass
            continue
        prompt = (spec.get("prompt") or "").strip() if isinstance(spec, dict) else ""
        filename = (spec.get("filename") or "").strip() if isinstance(spec, dict) else ""
        # Sanitise the requested filename: must match the same strict
        # regex as image_gen-mode worker output (no traversal, no hidden
        # files, capped length). Default to a hex-named .png when the
        # model omits/mangles it.
        if not filename or not _GENERATED_FILENAME_RE.match(filename):
            filename = f"genimg_{p.stem.removeprefix(_IMAGE_REQUEST_PREFIX)}.png"
        ext = Path(filename).suffix.lower()
        if ext not in _INLINE_IMAGE_EXTENSIONS:
            # Force a sane image extension; gemini returns PNG/JPEG.
            filename = Path(filename).stem + ".png"
        dest = gen_dir / filename
        try:
            img_bytes, _mime = await image_gen.generate_image(prompt)
            dest.write_bytes(img_bytes)
            try:
                os.chmod(dest, 0o640)
            except OSError:
                pass
        except Exception as e:  # ImageGenError or transport
            logger.warning(
                "image-gen request failed for session %s: %s", session_id, e,
            )
            err_path = gen_dir / (filename + ".error")
            try:
                err_path.write_text(f"image generation failed: {e}\n", encoding="utf-8")
            except OSError:
                pass
        finally:
            try:
                p.unlink()
            except OSError:
                pass


_SCHEDULE_REQUEST_PREFIX = "_schedule_request_"


async def _process_schedule_requests(*, session_id: str, email: str) -> int:
    """Register a durable wake for every ``_schedule_request_*.json`` marker the
    model dropped under /data/generated/<sid>/ this turn. Returns how many were
    registered (so the caller can notify the UI only when something changed).

    Mirrors ``_process_image_requests``: same drop-a-JSON-file protocol (works
    uniformly across host / user / local dispatch because it only needs a file
    write), drained post-turn, marker deleted after handling. Each marker is
    ``{"prompt": str, "in"|"at"|"every": str, "note"?: str}``. On a bad marker
    we leave a sibling ``.error`` so the failure is visible instead of silent.
    """
    registered = 0
    try:
        gen_dir = _session_generated_dir(session_id)
    except ValueError:
        return 0
    if not gen_dir.is_dir():
        return 0
    for p in sorted(gen_dir.iterdir()):
        if not p.is_file() or not p.name.startswith(_SCHEDULE_REQUEST_PREFIX):
            continue
        if p.suffix.lower() != ".json":
            continue
        err: str | None = None
        try:
            spec = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(spec, dict):
                raise ValueError("marker must be a JSON object")
            prompt = (spec.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("marker needs a non-empty 'prompt'")
            note = spec.get("note")
            delay = chat_scheduler.parse_duration(spec.get("in")) if spec.get("in") is not None else None
            at_epoch = chat_scheduler.parse_at(spec.get("at")) if spec.get("at") is not None else None
            every = chat_scheduler.parse_duration(spec.get("every")) if spec.get("every") is not None else None
            if delay is None and at_epoch is None and every is None:
                raise ValueError("marker needs one of 'in', 'at', or 'every'")
            rec = chat_scheduler.register(
                email, session_id, prompt,
                delay_seconds=delay, at_epoch=at_epoch, every_seconds=every,
                note=note,
            )
            registered += 1
            logger.info(
                "scheduled wake %s for %s session=%s next_fire=%s",
                rec["id"], email, session_id, rec["next_fire"],
            )
        except Exception as e:  # noqa: BLE001 — malformed marker / cap hit
            err = str(e)
            logger.warning(
                "schedule-request marker failed for session %s: %s", session_id, e,
            )
        finally:
            if err is not None:
                try:
                    (gen_dir / (p.stem + ".schedule_error")).write_text(
                        f"could not schedule wake: {err}\n", encoding="utf-8",
                    )
                except OSError:
                    pass
            try:
                p.unlink()
            except OSError:
                pass
    return registered


def _scan_new_artifacts(
    *, session_id: str, since_ts: float,
) -> list[dict[str, Any]]:
    """Return artifact descriptors for files in /data/generated/<sid>/ with
    mtime > since_ts and a recognised extension. Skips dotfiles and
    internal control files (image-request markers)."""
    try:
        gen_dir = _session_generated_dir(session_id)
    except ValueError:
        return []
    if not gen_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(gen_dir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.name.startswith(_IMAGE_REQUEST_PREFIX):
            # Internal marker for autonomous image-gen — never surface as
            # an artifact; the post-turn processor handles it.
            continue
        if p.name.startswith(_SCHEDULE_REQUEST_PREFIX):
            # Internal marker for a scheduled wake — handled + deleted by
            # _process_schedule_requests before this scan; guard anyway.
            continue
        ext = p.suffix.lower()
        # Surface ANY generated file back to the user regardless of format —
        # the serve route handles unknown extensions (octet-stream download),
        # so the model can send back any file type. Only skip internal scratch
        # (failed image-gen markers); real internal markers are prefix-skipped
        # above. (_ARTIFACT_EXTENSIONS is retained for inline-preview hints.)
        if ext in (".error", ".partial", ".tmp"):
            continue
        try:
            mtime = p.stat().st_mtime
            size = p.stat().st_size
        except OSError:
            continue
        # get_archive truncates mtimes to whole seconds; compare against the
        # floor so a file written in the turn's first second is not dropped.
        if mtime < math.floor(since_ts):
            continue
        out.append({
            "filename": p.name,
            "ext": ext,
            "size": size,
            "url": (
                f"/api/sessions/{session_id}/generated/"
                + urllib.parse.quote(p.name, safe="")
            ),
        })
    return out


def _format_artifacts_markdown(artifacts: list[dict[str, Any]]) -> str:
    """Build the markdown tail to append to the assistant's response.

    Image extensions render inline as ``![filename](url)``. Everything
    else gets a clickable link with a small "(<size>)" suffix so the
    user knows whether to expect a 2KB CSV or a 5MB HTML report.
    """
    if not artifacts:
        return ""
    lines: list[str] = ["", "", "---", "**Artifacts**"]
    for a in artifacts:
        url = a["url"]
        name = a["filename"]
        if a["ext"] in _INLINE_IMAGE_EXTENSIONS:
            lines.append(f"![{name}]({url})")
        else:
            human_size = (
                f"{a['size'] / 1024:.1f} KB" if a["size"] >= 1024 else f"{a['size']} B"
            )
            lines.append(f"- [{name}]({url}) ({human_size})")
    return "\n".join(lines) + "\n"


def _extract_memory_update(text: str) -> tuple[str, str | None]:
    """Return (scrubbed_text, new_memory_or_None).

    If the text ends with a <memory_update>...</memory_update> block,
    extract the inner content and strip the block from the response.
    Otherwise return the input unchanged with None.
    """
    if not text:
        return text, None
    # Per block: prefer the strict </memory_update> closer when one exists at
    # that position (a nested <memory>…</memory> wrapper would otherwise cut
    # the block short); fall back to the abbreviated closer.
    matches = []
    pos = 0
    while True:
        m = _MEMORY_UPDATE_ANY_RE.search(text, pos)
        if m is None:
            break
        strict = _MEMORY_UPDATE_STRICT_RE.match(text, m.start())
        chosen = strict if strict is not None else m
        pos = chosen.end()
        if not _inside_code_fence(text, chosen.start()):
            matches.append(chosen)
    if not matches:
        return text, None
    # Last non-empty block wins (the model's final view of memory); every
    # matched block is stripped from what the user sees. An empty block is
    # "nothing to record", never "wipe memory".
    new_memory: str | None = None
    for m in reversed(matches):
        if m.group(1).strip():
            new_memory = m.group(1).strip()
            break
    scrubbed = text
    for m in reversed(matches):
        scrubbed = scrubbed[: m.start()] + scrubbed[m.end():]
    return scrubbed.rstrip(), new_memory


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def list_sessions(
    workspace: str | None = None,
    claims: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """List the user's sessions, optionally scoped to a workspace.

    `?workspace=personal` and `?workspace=shared` filter to that bucket;
    no param returns every session (preserves the old contract for
    callers that don't pass it). Sessions with no `workspace` field are
    treated as personal — they pre-date the toggle.
    """
    if workspace is not None and workspace not in ("personal", "shared", "admin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace must be 'personal', 'shared', or 'admin'",
        )
    return storage.list_sessions(_user_email(claims), workspace=workspace)


@app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionBody | None = None,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    title = body.title if body is not None else None
    # Pick which Claude account this session lives on. Sessions are
    # locked per-account for their lifetime — claude's --resume <uuid>
    # requires the session UUID to be on the same account that originally
    # created it, so a transparent mid-session switch would break
    # conversation continuity. New sessions get whichever account in the
    # pool has the most headroom; a saturated pool falls back to None
    # (interpreted as "main") so a session can still be created when
    # quotas are tight — the user just sees the rate-limit naturally.
    account_name: str | None = None
    try:
        choice = account_router.pick()
        account_name = choice.name
    except account_router.NoAccountsAvailable:
        # No usable pooled account. Leave the session unpinned: the runner
        # resolves an account per turn, and populate_credentials refuses to
        # ship the operator's primary login into a per-user container.
        account_name = None
    except Exception:
        logger.exception("account_router.pick failed; leaving session unpinned")
        account_name = None
    # Lock the role at create time so dispatch (admin → chat-host-shell vs
    # user → per-user container) is stable for the session's lifetime.
    email = _user_email(claims)
    role = _resolve_role(email)
    # Multi-user wiring: every non-admin email gets a per-user docker
    # container provisioned at session-create time, and the resolved
    # container name is persisted on the session JSON. The worker reads
    # session["container"] on every turn and dispatches the claude
    # invocation into it via `docker exec` (see
    # claude_runner.spawn_claude dispatch="user"). Admin sessions skip
    # provisioning entirely — they keep landing in chat-host-shell as
    # before.
    # Workspace selection — defaults to "personal" (the historical
    # per-email container). "shared" routes into the tenant's shared
    # container, but only when the deployment has explicitly opted in
    # via WIZERITH_SHARED_CONTAINER_NAME. We 400 (not silently fall
    # back) so a wizerith-mode SPA pointed at an ald3-mode backend
    # surfaces the misconfiguration rather than dropping the user into
    # a personal container they didn't ask for.
    requested_workspace = (body.workspace if body is not None else None) or "personal"
    if requested_workspace not in ("personal", "shared", "admin"):
        raise HTTPException(
            status_code=400,
            detail="workspace must be 'personal', 'shared', or 'admin'",
        )
    if requested_workspace == "shared" and not user_container.shared_container_name():
        raise HTTPException(
            status_code=400,
            detail="shared workspace is not enabled on this tenant",
        )

    container_name: str | None = None
    if role == "user":
        try:
            if requested_workspace == "shared":
                container_name = await asyncio.to_thread(user_container.ensure_shared_container)
                # Record the shared container's PREFERRED account, resolved
                # to a usable one — refresh_credentials_if_stale re-resolves
                # on every turn, so this is a label for consistency, not a
                # hard pin (a saturated shared account no longer wedges the
                # workspace) — see user_container.resolve_usable_account.
                account_name = user_container.resolve_usable_account(
                    user_container.shared_container_account()
                )
            else:
                container_name = await asyncio.to_thread(user_container.ensure_user_container, email)
        except user_container.SharedContainerNotConfigured as exc:
            # The env-disabled case is already caught above; this is the
            # belt-and-braces re-raise path if env is unset between the
            # check and the call.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            # Don't crash session creation on a docker hiccup — log loud
            # and let the runner fall back to in-chat-container dispatch
            # for this turn (the soft-fallback in claude_runner.run_turn
            # treats role="user" with no container as legacy local).
            logger.exception(
                "ensure_%s_container failed for %s; falling back to local dispatch",
                "shared" if requested_workspace == "shared" else "user",
                email,
            )
            container_name = None
    session = storage.create_session(
        email,
        title=title,
        account=account_name,
        role=role,
        container=container_name,
        workspace=requested_workspace,
    )
    return _summary(session)


@app.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    session = storage.get_session(_user_email(claims), session_id)
    if session is None:
        # Cross-email is funneled through the same 404 to avoid existence leak.
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.patch("/api/sessions/{session_id}")
async def patch_session(
    session_id: str,
    body: RenameSessionBody,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Patch user-controlled session metadata.

    All body fields optional; only fields explicitly set are written.
    Backwards-compatible with the legacy "title-only" rename use.
    """
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.starred is not None:
        fields["starred"] = body.starred
    if body.archived is not None:
        fields["archived"] = body.archived
    if body.folder is not None:
        fields["folder"] = body.folder
    if not fields:
        raise HTTPException(status_code=400, detail="no patchable fields supplied")
    session = storage.update_session_metadata(
        _user_email(claims), session_id, **fields,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@app.get("/api/sessions/{session_id}/head")
async def session_head(
    session_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Cheap change-detector for one session: the fields a client needs to
    decide whether to refetch, and nothing else.

    Exists for the SPA's idle poll, which watches for turns the client did
    not start — i.e. scheduled wakes, which stream server-side with no
    subscriber attached. That poll needs to run forever in every open tab, so
    the two obvious sources are both wrong for it: ``GET /sessions/{id}``
    ships the session's ENTIRE message array (hundreds of KB on a long
    thread) every tick, and ``GET /sessions`` parses every session file the
    user owns. This reads one file and returns ~100 bytes; the full fetch
    happens only once ``updated_at`` actually moves.

    404s a session the caller doesn't own, matching the sibling endpoints —
    no existence leak.
    """
    email = _user_email(claims)
    session = storage.get_session(email, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    msgs = session.get("messages") or []
    last = msgs[-1] if msgs else None
    return {
        "id": session_id,
        "updated_at": session.get("updated_at"),
        "message_count": len(msgs),
        # Lets the client tell "a turn is running" from "a turn finished"
        # without pulling the messages.
        "last_role": (last or {}).get("role"),
        "last_status": (last or {}).get("status"),
    }


@app.get("/api/sessions/{session_id}/schedules")
async def list_session_schedules(
    session_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Pending scheduled wakes for this session (drives the composer clock
    indicator). 404s a session the caller doesn't own to avoid existence leak."""
    email = _user_email(claims)
    if storage.get_session(email, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return chat_scheduler.list_for(email, session_id)


@app.delete(
    "/api/sessions/{session_id}/schedules/{sched_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_session_schedule(
    session_id: str,
    sched_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> Response:
    email = _user_email(claims)
    if storage.get_session(email, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not chat_scheduler.cancel(email, sched_id):
        raise HTTPException(status_code=404, detail="schedule not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/search")
async def search_sessions(
    q: str = "",
    claims: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Brute-force full-text search across the caller's sessions.

    Returns up to 50 summaries (same shape as the list endpoint plus a
    ``snippet`` string). Empty/whitespace ``q`` returns an empty list.
    """
    return storage.search_sessions(_user_email(claims), q)


@app.get("/api/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    format: str = "md",
    claims: dict[str, Any] = Depends(require_user),
) -> Response:
    """Download a session as Markdown or JSON.

    ``format`` is ``md`` (default) or ``json``. Returns 404 if missing
    or owned by another email; 400 on unknown format.
    """
    email = _user_email(claims)
    if format not in ("md", "json"):
        raise HTTPException(status_code=400, detail="format must be 'md' or 'json'")
    session = storage.get_session(email, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    raw_title = (session.get("title") or "session").strip() or "session"
    # Header values are latin-1: keep an ASCII slug for ``filename=`` and
    # carry the real (e.g. Chinese) title in RFC 5987 ``filename*=``. The old
    # slug kept any isalnum() char, so a CJK title 500'd the export.
    title_slug = "".join(
        c if (c.isascii() and c.isalnum()) or c in "-_" else "-" for c in raw_title.lower()
    ).strip("-") or "session"
    def _disposition(ext: str) -> str:
        return (
            f'attachment; filename="{title_slug}.{ext}"; '
            f"filename*=UTF-8''{urllib.parse.quote(raw_title, safe='')}.{ext}"
        )
    if format == "json":
        body = json.dumps(session, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": _disposition("json"),
            },
        )
    md = storage.export_session_markdown(email, session_id) or ""
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": _disposition("md"),
        },
    )


@app.post("/api/sessions/{session_id}/fork", status_code=status.HTTP_201_CREATED)
async def fork_session(
    session_id: str,
    body: ForkSessionBody,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Create a new session seeded with messages[seq < from_seq] from src.

    The caller is expected to immediately POST the new ``text`` to
    ``/api/sessions/{new_id}/messages``. We don't drive that turn from
    here — keeping the fork endpoint small means the message-send path
    (with all its rate limits, locking, and SSE plumbing) stays the
    single entry point for new turns. Returns the new session summary
    so the client can switch to it before sending.
    """
    email = _user_email(claims)
    if not isinstance(body.text, str) or not body.text.strip():
        raise HTTPException(status_code=400, detail="text must be a non-empty string")
    if body.from_seq < 0:
        raise HTTPException(status_code=400, detail="from_seq must be >= 0")
    src = storage.get_session(email, session_id)
    if src is None:
        raise HTTPException(status_code=404, detail="session not found")
    new = storage.fork_session_seed(
        email, session_id, body.from_seq,
    )
    if new is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _summary(new)


@app.delete("/api/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> Response:
    email = _user_email(claims)
    key = (email, session_id)
    run = _active_runs.get(key)
    if run is not None:
        # Stop the in-flight turn so it cannot keep executing tools and
        # recreate the generated dir after we purge it.
        run.cancel_requested = True
        gen = getattr(run, "_gen", None)
        if gen is not None:
            try:
                await gen.aclose()
            except Exception:
                pass
        task = _active_tasks.get(key)
        if task is not None and not task.done():
            task.cancel()
    ok = storage.delete_session(email, session_id)
    if not ok:
        # Storage refused (missing or cross-email). Do NOT touch the
        # attachments dir in this case — purging would reveal that some
        # other user owned the session.
        raise HTTPException(status_code=404, detail="session not found")
    # Storage delete succeeded; purge attachments after, idempotently.
    attachments.delete_attachments_dir(session_id)
    attachments.delete_preview_dir(session_id)
    _delete_generated_dir(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Messages (SSE) + attachments + agent lifecycle (Phase 2 + Phase 4).
#
# Phase 4 (Bug 3 fix) execution model:
#
#   * The agent run is an asyncio.Task spawned by ``post_message`` and
#     OWNED BY THE SERVER for the lifetime of that task. Closing the SSE
#     response, the tab, the browser, or losing the network does NOT
#     cancel it. Only an explicit POST to /sessions/{id}/cancel does.
#
#   * The task writes deltas/tool events to its TurnRun (in-memory event
#     log + queue per subscriber) and persists the final assistant
#     content + status (complete | error | cancelled) on terminal.
#
#   * The SSE response body is a *subscriber*: it copies the run's
#     event log on attach, then drains live events from a per-subscriber
#     queue. On disconnect the subscriber is removed; the worker keeps
#     running.
#
#   * Reconnect is GET /sessions/{id}/stream. It attaches a new
#     subscriber to the active run if any (replays event log, then
#     drains live tail), or emits a single ``done`` (or whatever the
#     terminal was) immediately if the run already finished.
#
#   * The per-session lock from Phase 2 is now held by the worker for
#     the lifetime of the run — not by the request handler. This keeps
#     "at most one in-flight turn per session" intact. A second
#     ``post_message`` arriving while a run is active is rejected with
#     HTTP 409 (a documented choice; see CHANGES.md).
# ---------------------------------------------------------------------------


# ===========================================================================
# TurnRun: per-active-run state shared between worker task and subscribers.
# ===========================================================================

class _TurnRun:
    """In-memory state for one active agent run.

    Lifecycle:
      * Created by ``post_message`` AFTER user-message + placeholder are
        durably persisted. Inserted into ``_active_runs`` keyed by
        ``(email, session_id)`` while the worker holds the per-session
        lock.
      * Worker task pushes events via ``emit(...)``; subscribers attach
        via ``attach()`` and detach via ``detach()``.
      * On terminal (done | error | cancelled), worker calls
        ``finalize(...)``; ``done_event`` is set. Subscribers drain their
        queues and exit.
      * Worker's ``finally`` removes the run from ``_active_runs`` and
        releases the per-session lock. After this point any reconnect
        sees no active run and falls back on the durable history.

    The ``events`` log is the canonical replay source: it is appended to
    on every ``emit()`` and read by ``attach()`` to bootstrap a fresh
    subscriber. The per-subscriber queue is the live-tail channel.
    """

    # Sentinel pushed into a subscriber's queue to signal "no more events
    # are coming for you, exit the loop". Distinguished from real event
    # dicts by identity check.
    _SENTINEL: dict[str, Any] = {"_eof": True}

    def __init__(self, *, email: str, session_id: str, assistant_seq: int) -> None:
        self.email = email
        self.session_id = session_id
        self.assistant_seq = assistant_seq
        self.events: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # Serialises emit + attach so a subscriber's snapshot+register pair
        # is atomic with respect to live events landing in the meantime.
        self._lock = asyncio.Lock()
        self.done_event = asyncio.Event()
        self.terminal: dict[str, Any] | None = None
        self.cancel_requested = False

    async def emit(self, event: dict[str, Any]) -> None:
        """Append an event to the log and broadcast to every subscriber."""
        async with self._lock:
            self.events.append(event)
            for q in self._subscribers:
                # put_nowait is safe: queues are unbounded.
                q.put_nowait(event)

    async def finalize(self, terminal: dict[str, Any]) -> None:
        """Record the terminal event and wake up subscribers' drain loops.

        ``terminal`` is also appended to the event log via emit() so
        late-arriving subscribers replay it. After finalize() the worker
        will not call emit() again.
        """
        await self.emit(terminal)
        async with self._lock:
            self.terminal = terminal
            # Wake every subscriber's loop so it can exit cleanly.
            for q in self._subscribers:
                q.put_nowait(self._SENTINEL)
        self.done_event.set()

    async def attach(self) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]:
        """Return (replay-snapshot, live-queue) for a new subscriber.

        The snapshot+register pair is performed under ``self._lock`` so
        no event can land "between" the snapshot and the subscribe.
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            snapshot = list(self.events)
            self._subscribers.add(q)
            # If the run already finalised, push the sentinel immediately
            # so the consumer's drain loop doesn't block forever.
            if self.terminal is not None:
                q.put_nowait(self._SENTINEL)
        return snapshot, q

    async def detach(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber. Called by the SSE response on disconnect.

        Idempotent: removing a queue that's already gone is a no-op.
        """
        async with self._lock:
            self._subscribers.discard(q)


# Module-global registry of active runs, keyed by (email, session_id).
# Lifetime: created by post_message, removed by the worker's finally.
# The set is small (one entry per concurrently-running turn) so a dict
# keyed by tuple is plenty.
_active_runs: dict[tuple[str, str], _TurnRun] = {}

# Module-global registry of active worker tasks, mirroring _active_runs.
# Kept separately so tests (and the cancel endpoint) can introspect /
# await the task, and so a cancel can call task.cancel() if the worker
# is stuck on a non-cooperative await. The shape is intentional: the
# brief asks for "no zombie tasks after terminal states", which means
# this dict must be empty after every run finishes.
_active_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}


# ---------------------------------------------------------------------------
# Test-only introspection. The leading underscore + the pragma keep these
# off the production API surface (no ``__all__`` export, no ``@app.get``
# binding); ``# pragma: no cover`` flags them to coverage tools as
# instrumentation rather than reachable production paths. Production code
# observes run state via the cancel / stream endpoints, not these dicts.
# ---------------------------------------------------------------------------
def _get_active_runs_snapshot() -> dict[tuple[str, str], _TurnRun]:  # pragma: no cover
    """Test-only accessor: a shallow copy of the active-runs dict.

    Used by ``tests/test_agent_lifecycle.py`` to verify the "no zombie
    tasks" invariant after each terminal state. Do NOT call from
    production code paths — the dict's identity is an implementation
    detail.
    """
    return dict(_active_runs)


def _get_active_tasks_snapshot() -> dict[tuple[str, str], asyncio.Task[None]]:  # pragma: no cover
    """Test-only accessor mirroring ``_get_active_runs_snapshot``."""
    return dict(_active_tasks)


# ===========================================================================
# Title task — unchanged from Phase 2 in behavior; relocated below the
# TurnRun definitions so the file flows top-to-bottom from data structures
# to the request handlers.
# ===========================================================================

async def _title_task(
    email: str,
    session_id: str,
    claude_session_id: str,
    user_text: str,
    assistant_text: str = "",
) -> None:
    """Background coroutine: generate a title and persist it. Silent on failure.

    The task can be spawned BEFORE the assistant has produced a reply
    (Phase-4 worker entry path: title runs in parallel with the main
    turn so it shows up in the sidebar quickly) — in which case
    ``assistant_text`` is empty. ``generate_title`` handles that case
    by titling from the user prompt alone.
    """
    try:
        title = await claude_runner.generate_title(
            claude_session_id=claude_session_id,
            first_user_msg=user_text,
            first_assistant_msg=assistant_text,
        )
        if not title:
            # generate_title is best-effort and returns "" on ANY failure
            # (60s timeout, overload/529, the title subprocess being starved
            # by a heavy concurrent main turn, JSON/runner errors). Before
            # this fallback, that left the session PERMANENTLY untitled, since
            # the title task only fired on the first turn and never retried.
            # Derive a title from the first user message so the sidebar always
            # has something. set_title is set-once, so a later successful
            # generate_title (see the retry gate in the worker) is never
            # clobbered by this fallback.
            title = claude_runner._normalise_title(user_text)
        if title:
            storage.set_title(email, session_id, title)
    except Exception:  # pragma: no cover - defensive
        logger.exception("title task failed for session %s", session_id)


# ===========================================================================
# Worker: drives the agent run. Runs as an asyncio.Task; OWNS the per-session
# lock for the lifetime of the run.
# ===========================================================================

# ===========================================================================
# A1: persistent streaming-input sessions (claude.ai turn model).
#
# When enabled (default ON; CHAT_PERSISTENT_SESSIONS=0 to disable), each chat
# session keeps ONE long-lived `claude --input-format stream-json` process open
# across turns, so a backgrounded task auto-surfaces and the agent reports back
# with no new user message. Verified live against a real per-user container.
# ===========================================================================
def _is_api_model(model: str | None) -> bool:
    """True for the OpenAI-compatible (stateless) runners: TokenHub glm/kimi,
    Xiaomi mimo, haihub qwen/deepseek/minimax, the home-GPU local model."""
    return bool(
        local_runner.is_local_model(model)
        or haihub_runner.is_haihub_model(model)
        or tokenhub_runner.is_tokenhub_model(model)
        or mimo_runner.is_mimo_model(model)
    )


def _api_attachment_preamble(attachments_dir: Path, tool_path: str) -> str:
    """Name the user's uploaded files (and where the run_bash tool can read
    them) at the top of the prompt. The claude path does the same with a
    Read-tool preamble; API models only have run_bash, so the wording
    points at shell/python readers instead."""
    try:
        files = sorted(p for p in attachments_dir.iterdir() if p.is_file())
    except OSError:
        return ""
    if not files:
        return ""
    lines = []
    for p in files:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        lines.append(f"  - {tool_path}/{p.name}  ({mime}, {size:,} bytes)")
    return (
        "[The user attached the following file(s). Read them with the "
        "run_bash tool before answering (cat/head for text, python for "
        "spreadsheets and data files, pdftotext or python for PDFs) — do not "
        "answer without reading them.]\n"
        + "\n".join(lines)
        + "\n\n"
    )


async def _api_model_turn_gen(
    *,
    model: str,
    user_text: str,
    role: str | None,
    container: str | None,
    session_id: str,
    prior_history: str | None,
    persona: str | None,
    memory: str | None,
    output_language: str,
    effort: str | None,
    attachments_dir: Path | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """One turn on a stateless API model, with the same tool/artifact
    dispatch for user turns AND scheduled wakes.

    Tools (run_bash) target the session's per-user container when it has
    one; admin sessions have none, so their tools run in the tenant
    host-shell container instead (parity with the claude path's "host"
    dispatch: HOME=/home/felix, artifacts in the tenant host dir that maps
    chat-side to /data/generated/<sid>).
    """
    runner = (
        local_runner if local_runner.is_local_model(model)
        else tokenhub_runner if tokenhub_runner.is_tokenhub_model(model)
        else mimo_runner if mimo_runner.is_mimo_model(model)
        else haihub_runner
    )
    tool_container = container
    tool_workdir = "/workspace"
    tool_home = "/workspace"
    tool_artifacts: str | None = None
    if not tool_container and role == "admin":
        tool_container = claude_runner.host_shell_container()
        tool_workdir = "/home/felix"
        tool_home = "/home/felix"
        tool_artifacts = claude_runner.artifacts_path_for("host", session_id)
    # Uploads: stage/translate the chat-side attachments dir to a path the
    # tool container can open (per-user: docker put_archive into
    # /workspace/.attachments/<sid>; admin host-shell: the bind-mounted host
    # dir) and tell the model the files exist. Without this, uploads were
    # silently invisible on the API-model path.
    if attachments_dir is not None and tool_container:
        dispatch = "user" if container else "host"
        try:
            # put_archive + exec into the container: keep it off the event loop.
            tool_attach = await asyncio.to_thread(
                claude_runner._claude_attachments_path,
                attachments_dir, dispatch=dispatch, container_name=container,
            )
        except Exception:
            logger.exception("attachment staging failed for %s", session_id)
            tool_attach = None
        if tool_attach:
            user_text = _api_attachment_preamble(attachments_dir, tool_attach) + user_text
    return runner.run_turn(
        prompt=user_text,
        model=model,
        prior_history=prior_history,
        persona=persona,
        memory=memory,
        output_language=output_language,
        container=tool_container,
        chat_session_id=session_id,
        effort=effort,
        tool_workdir=tool_workdir,
        tool_home=tool_home,
        artifacts_path=tool_artifacts,
    )


def _persistent_enabled() -> bool:
    """A1 persistent sessions on? Default ON; ``CHAT_PERSISTENT_SESSIONS=0``
    disables. Read per-call so conftest/tests can gate it without import-order
    games (and so an operator can flip it via env + restart, no rebuild)."""
    return os.environ.get("CHAT_PERSISTENT_SESSIONS", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


_session_mgr = SessionProcessManager()


# The SPA appends this exact line to the bubble when it is attached and sees a
# live ``error`` event. Persisting the identical string means a client that WAS
# attached ends up with byte-identical content to the server snapshot, so the
# tail merge in App.tsx is a no-op rather than a duplicate.
_ERROR_NOTICE = "\n\n_\u26a0 generation failed: {message}_"


def _scrub_partial(partial: str) -> str:
    """A cancelled/errored turn keeps its partial text but never a raw
    <memory_update> block (it would show in the thread and re-enter history)."""
    scrubbed, _ = _extract_memory_update(partial or "")
    return scrubbed


def _with_error_notice(partial: str, message: str) -> str:
    """Fold a turn's failure reason into the persisted assistant content.

    Before this, a failed turn persisted ``content=partial`` (almost always
    "") and the reason existed ONLY on the SSE ``error`` event. That is fine
    for a user-initiated turn — the sender is attached and sees it — but a
    SCHEDULED WAKE fires with no subscriber at all, so the reason was
    discarded and the thread was left holding a permanently blank bubble with
    no hint that anything had gone wrong. Nine such bubbles were sitting in
    live sessions when this was written.
    """
    reason = (message or "").strip() or "unknown error"
    return (partial or "") + _ERROR_NOTICE.format(message=reason)


async def _consume_into_run(
    run: _TurnRun,
    gen: AsyncIterator[dict[str, Any]],
    full_text_parts: list[str],
    *,
    container: str | None,
    artifacts_dispatch: str,
    turn_start_ts: float,
    memory_at_start: str | None = None,
) -> str:
    """Consume one normalised event stream into ``run``: stream deltas/tool
    events, and on terminal persist the assistant message + finalize.

    Shared by the user turn AND the CLI-initiated auto-continuation turn so the
    persist/finalize/artifacts/memory logic can't drift between them.
    ``full_text_parts`` is owned by the CALLER so a cancel mid-stream can
    persist the partial. Does NOT touch the registry/lock — that's the caller's
    ``finally``. May raise (CancelledError / unexpected) — the caller's except
    blocks finalise those, exactly as the legacy single-path worker did.

    Returns the terminal status it persisted — ``"complete"``, ``"error"`` or
    ``"cancelled"``. A failing turn does NOT raise (run_turn folds claude
    failures into an ``error`` EVENT), so callers that need to know whether the
    turn actually produced anything must read this; ``_fire_schedule`` does, to
    re-arm a wake whose turn died."""
    email = run.email
    session_id = run.session_id
    assistant_seq = run.assistant_seq
    reasoning_parts: list[str] = []
    async for event in gen:
        if run.cancel_requested:
            break
        et = event.get("type")
        if et == "delta":
            chunk = event.get("text") or ""
            if chunk:
                full_text_parts.append(chunk)
            await run.emit({"type": "delta", "text": chunk})
        elif et == "reasoning":
            # Hidden reasoning: streamed for display, persisted on the
            # message meta at ``done``. Never joins full_text_parts.
            rchunk = event.get("text") or ""
            if rchunk:
                reasoning_parts.append(rchunk)
                await run.emit({"type": "reasoning", "text": rchunk})
        elif et == "tool_start":
            await run.emit({
                "type": "tool_start",
                "name": event.get("name", "tool"),
                "input_summary": event.get("input_summary", ""),
            })
        elif et == "tool_end":
            await run.emit({
                "type": "tool_end",
                "name": event.get("name", "tool"),
            })
        elif et == "done":
            assistant_text = event.get("full_text") or "".join(full_text_parts)
            turn_meta = event.get("meta") if isinstance(event.get("meta"), dict) else None
            _reasoning_text = _capped_reasoning(reasoning_parts)
            if _reasoning_text is not None:
                turn_meta = dict(turn_meta or {})
                turn_meta["reasoning"] = _reasoning_text
            assistant_text, new_memory = _extract_memory_update(assistant_text)
            if new_memory is not None:
                try:
                    storage.set_memory_merged(email, new_memory, memory_at_start)
                except Exception:
                    logger.exception(
                        "set_memory failed for %s after turn complete", email,
                    )
            if artifacts_dispatch == "user" and container:
                await asyncio.to_thread(
                    _collect_user_container_artifacts,
                    session_id=session_id, container_name=container,
                )
            try:
                await _process_image_requests(session_id=session_id)
            except Exception:
                logger.exception(
                    "image-request processing failed for %s", session_id,
                )
            try:
                _scheduled_n = await _process_schedule_requests(
                    session_id=session_id, email=email,
                )
            except Exception:
                logger.exception(
                    "schedule-request processing failed for %s", session_id,
                )
                _scheduled_n = 0
            artifacts = _scan_new_artifacts(
                session_id=session_id, since_ts=turn_start_ts,
            )
            artifact_md = _format_artifacts_markdown(artifacts)
            if artifact_md:
                assistant_text = assistant_text.rstrip() + "\n" + artifact_md
                await run.emit({"type": "delta", "text": "\n" + artifact_md})
            try:
                storage.update_assistant_message(
                    email, session_id, assistant_seq,
                    content=assistant_text,
                    status=storage.ASSISTANT_STATUS_COMPLETE,
                    meta=turn_meta,
                )
            except Exception:
                logger.exception(
                    "update_assistant_message(complete) failed for %s seq=%s",
                    session_id, assistant_seq,
                )
                await run.finalize({"type": "error", "message": "persistence failed"})
                return "error"
            if new_memory is not None:
                await run.emit({"type": "memory_updated"})
            if _scheduled_n:
                await run.emit({"type": "schedules_updated"})
            await run.finalize({"type": "done", "full_text": assistant_text, "meta": turn_meta})
            return "complete"
        elif et == "error":
            partial = "".join(full_text_parts)
            err_message = event.get("message", "error")
            try:
                storage.update_assistant_message(
                    email, session_id, assistant_seq,
                    content=_with_error_notice(_scrub_partial(partial), err_message),
                    status=storage.ASSISTANT_STATUS_ERROR,
                )
            except Exception:
                logger.exception(
                    "update_assistant_message(error) failed for %s seq=%s",
                    session_id, assistant_seq,
                )
            await run.finalize({"type": "error", "message": err_message})
            return "error"
        else:
            continue

    # Fell out of the loop with no terminal event: either the consumer
    # cancelled mid-stream, or claude's stream just stopped.
    partial = "".join(full_text_parts)
    if run.cancel_requested:
        try:
            storage.update_assistant_message(
                email, session_id, assistant_seq,
                content=_scrub_partial(partial),
                status=storage.ASSISTANT_STATUS_CANCELLED,
            )
        except Exception:
            logger.exception(
                "update_assistant_message(cancelled) failed for %s seq=%s",
                session_id, assistant_seq,
            )
        await run.finalize({"type": "cancelled", "full_text": partial})
        return "cancelled"
    try:
        storage.update_assistant_message(
            email, session_id, assistant_seq,
            content=_with_error_notice(
                partial, "stream ended without terminal event"),
            status=storage.ASSISTANT_STATUS_ERROR,
        )
    except Exception:
        logger.exception(
            "update_assistant_message(no-terminal) failed for %s seq=%s",
            session_id, assistant_seq,
        )
    await run.finalize({
        "type": "error",
        "message": "stream ended without terminal event",
    })
    return "error"


async def _on_auto_turn(key: tuple[str, str], turn: Any) -> None:
    """Handle a CLI-initiated continuation turn (a background task reported
    back): open a NEW assistant message + ``_TurnRun``, register it so a live
    SSE / reconnect attaches, and drive it through ``_consume_into_run``. The
    session process is shared and serialises turns, so at most one of these runs
    at a time per session."""
    email, session_id = key
    gen = claude_runner.normalize_session_turn(turn, model=None)
    try:
        session, assistant_seq = storage.append_assistant_placeholder(email, session_id)
    except Exception:
        logger.exception("auto-turn: append_assistant_placeholder failed for %s", session_id)
        return
    if session is None or assistant_seq is None:
        return
    role = session.get("role")
    container = session.get("container")
    if container:
        artifacts_dispatch = "user"
    elif role == "admin":
        artifacts_dispatch = "host"
    else:
        artifacts_dispatch = "local"
    try:
        _ensure_artifacts_dir(
            session_id=session_id, dispatch=artifacts_dispatch, container_name=container,
        )
    except Exception:
        logger.exception("auto-turn: _ensure_artifacts_dir failed for %s", session_id)
    run = _TurnRun(email=email, session_id=session_id, assistant_seq=assistant_seq)
    _active_runs[key] = run
    full_text_parts: list[str] = []
    try:
        await _consume_into_run(
            run, gen, full_text_parts,
            container=container, artifacts_dispatch=artifacts_dispatch,
            turn_start_ts=time.time(),
        )
    except asyncio.CancelledError:
        partial = "".join(full_text_parts)
        try:
            storage.update_assistant_message(
                email, session_id, assistant_seq,
                content=partial, status=storage.ASSISTANT_STATUS_CANCELLED,
            )
        except Exception:
            pass
        try:
            await run.finalize({"type": "cancelled", "full_text": partial})
        except Exception:
            pass
    except Exception as exc:
        logger.exception("auto-turn consume crashed for %s", session_id)
        partial = "".join(full_text_parts)
        try:
            storage.update_assistant_message(
                email, session_id, assistant_seq,
                content=_with_error_notice(_scrub_partial(partial), f"auto-turn crashed: {exc!s}"),
                status=storage.ASSISTANT_STATUS_ERROR,
            )
        except Exception:
            pass
        try:
            await run.finalize({"type": "error", "message": f"auto-turn crashed: {exc!s}"})
        except Exception:
            pass
    finally:
        # Identity-guarded: never pop a newer run that replaced ours.
        if _active_runs.get(key) is run:
            _active_runs.pop(key, None)


# A wake whose turn FAILS must not vanish. ``claim_due`` has already dropped
# the one-shot by the time we fire, and run_turn folds claude failures into an
# error event rather than raising, so without an explicit re-arm a failed wake
# is lost with no retry and no trace beyond an empty error message in the
# thread. That is exactly how two wakes died on 2026-08-20 when their account
# hit a 300s cooldown. Backoff widens and is bounded, so a session that is
# permanently broken stops rather than retrying forever; the total window
# (~52 min) comfortably covers a rate-limit cooldown.
_WAKE_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (120.0, 300.0, 900.0, 1800.0)


def _requeue_failed_wake(rec: dict[str, Any], reason: str) -> None:
    """Re-arm a one-shot wake whose fire produced no assistant message.

    Recurring wakes are skipped: ``claim_due`` already re-armed those to their
    next interval, so requeueing would double-fire them. Never raises."""
    try:
        if rec.get("interval_seconds"):
            return
        try:
            attempt = int(rec.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        sched_id = rec.get("id")
        if attempt >= len(_WAKE_RETRY_BACKOFF_SECONDS):
            logger.error(
                "scheduled wake %s GIVING UP after %s attempts (%s); "
                "session=%s will not be woken",
                sched_id, attempt, reason, rec.get("session_id"),
            )
            return
        delay = _WAKE_RETRY_BACKOFF_SECONDS[attempt]
        chat_scheduler.requeue(
            rec.get("email"), rec.get("session_id"), rec.get("prompt") or "",
            delay_seconds=delay, note=rec.get("note"), attempt=attempt + 1,
        )
        logger.warning(
            "scheduled wake %s failed (%s); re-armed in %.0fs (attempt %s/%s)",
            sched_id, reason, delay, attempt + 1,
            len(_WAKE_RETRY_BACKOFF_SECONDS),
        )
    except Exception:
        logger.exception("scheduled wake re-arm failed for %s", rec.get("id"))


async def _fire_schedule(rec: dict[str, Any]) -> None:
    """Fire one due wake and then release the durable lease on it.

    ``claim_due`` leases a one-shot rather than deleting it, so the record
    survives a crash/restart that happens mid-fire. Completing here — after
    EVERY terminal outcome, including a permanent drop and a retry re-armed
    as a new record — is what stops it firing a second time. If this task is
    killed before the ``finally`` runs, the lease lapses and the wake fires
    again on a later tick, which is exactly the intent."""
    try:
        await _fire_schedule_locked(rec)
    finally:
        try:
            await asyncio.to_thread(chat_scheduler.complete, rec.get("id"))
        except Exception:
            logger.exception(
                "scheduled wake %s: lease release failed", rec.get("id"))


async def _fire_schedule_locked(rec: dict[str, Any]) -> None:
    """Fire one due scheduled wake: respawn/--resume the session's process,
    inject the stored instruction as a turn, and stream the reply into the
    thread — exactly the persist/consume path ``_on_auto_turn`` uses, so a
    wake message is indistinguishable from any other assistant turn (it just
    appears in the thread). NEVER raises; runs as its own task.

    Concurrency: we take the per-session lock before registering a run, so a
    wake and a live user turn can't both drive the same session. If a user
    turn holds the lock, ``acquire()`` simply waits and the wake fires the
    moment that turn finishes (a few seconds late, never dropped)."""
    email = rec.get("email")
    session_id = rec.get("session_id")
    prompt = (rec.get("prompt") or "").strip()
    sched_id = rec.get("id")
    if not email or not session_id or not prompt:
        return
    session = storage.get_session(email, session_id)
    if session is None:
        # The thread was deleted after the wake was armed. A recurring wake
        # would otherwise re-arm forever (claim_due bumps next_fire on every
        # fire), so remove the record instead of just dropping this firing.
        logger.warning("scheduled wake %s: session gone, cancelling", sched_id)
        try:
            chat_scheduler.cancel(email, sched_id)
        except Exception:
            logger.exception("scheduled wake %s: cancel failed", sched_id)
        return
    key = (email, session_id)
    lock = storage.get_session_lock(email, session_id)
    await lock.acquire()
    try:
        if key in _active_runs:
            # An auto-continuation turn slipped in under the lock window.
            # Counted as an attempt so a permanently busy session eventually
            # gives up instead of re-arming every two minutes forever.
            _requeue_failed_wake(rec, "run already active")
            logger.info("scheduled wake %s: run already active, requeued", sched_id)
            return
        # The snapshot above was taken before we waited for the lock; a turn
        # may have completed meanwhile (claude_initialized flipped, messages
        # appended). Everything below must see the current state.
        session = storage.get_session(email, session_id) or session
        role = session.get("role")
        container = session.get("container")
        account = session.get("account")
        claude_session_id = session.get("claude_session_id")
        if not claude_session_id:
            return
        dispatch = claude_runner.resolve_dispatch(role, container)
        if dispatch == "user-fail-closed":
            logger.warning(
                "scheduled wake %s: role=user session has no container; dropping",
                sched_id,
            )
            return
        is_first_turn = not session.get("claude_initialized")
        if container:
            artifacts_dispatch = "user"
        elif role == "admin":
            artifacts_dispatch = "host"
        else:
            artifacts_dispatch = "local"
        try:
            _ensure_artifacts_dir(
                session_id=session_id, dispatch=artifacts_dispatch,
                container_name=container,
            )
        except Exception:
            logger.exception("scheduled wake: ensure_artifacts_dir failed for %s", session_id)
        _settings_blob: dict[str, Any] = {}
        try:
            _settings_blob = storage.get_settings(email)
            persona = _settings_blob.get("persona") or None
            output_language = _settings_blob.get("output_language") or "en"
        except Exception:
            persona, output_language = None, "en"
        try:
            memory = storage.get_memory(email)
        except Exception:
            memory = ""
        artifacts_path = claude_runner.artifacts_path_for(dispatch, session_id)
        # A wake has no per-message model pick, so it runs on the user's
        # default model — the same one a fresh tab would send. Before this,
        # wakes always went to the claude CLI even when the lineup default is
        # a TokenHub/haihub model, which (with the pool accounts dead) made
        # every wake error out.
        wake_model = _normalize_model(
            _settings_blob.get("default_model") if isinstance(_settings_blob, dict) else None
        )
        recurring = bool(rec.get("interval_seconds"))
        wake_text = (
            "[Scheduled wake — this turn was triggered by a timer you set "
            "earlier, NOT by the user typing now. Carry out the instruction "
            "below and address the user directly; your reply posts to this "
            "thread whether or not they're currently watching"
            + (", and this wake will repeat on its interval until cancelled"
               if recurring else "")
            + ". Your instruction to yourself was:]\n\n" + prompt
        )
        # Fire path depends on whether the persistent streaming-session model
        # is enabled. When it's OFF (CHAT_PERSISTENT_SESSIONS=0 kill-switch),
        # we MUST still fire — via the same per-turn run_turn path normal turns
        # use today — otherwise the wake silently no-ops. When it's ON, inject
        # into the live/respawned session process.
        is_persistent = _persistent_enabled()
        effective_prompt: str | None = None
        argv = env = None
        if is_persistent:
            effective_prompt = claude_runner.build_streaming_prompt(
                wake_text, prior_history=None, persona=persona,
                output_language=output_language, memory=memory,
                artifacts_path=artifacts_path,
            )
            try:
                argv, env = claude_runner.make_streaming_args_env(
                    claude_session_id, is_first_turn=is_first_turn,
                    attachments_dir=None, model=None, role=role, container=container,
                    account=account, user_email=email,
                )
            except Exception:
                logger.exception("scheduled wake %s: make_streaming_args_env failed", sched_id)
                _requeue_failed_wake(rec, "make_streaming_args_env failed")
                return
        try:
            # via="wake" so the SPA can label the bubble. A wake turn has no
            # user message in front of it, so without this the reply renders
            # as the assistant spontaneously speaking.
            sess2, assistant_seq = storage.append_assistant_placeholder(
                email, session_id, via="wake",
            )
        except Exception:
            logger.exception("scheduled wake: placeholder append failed for %s", session_id)
            _requeue_failed_wake(rec, "placeholder append failed")
            return
        if sess2 is None or assistant_seq is None:
            _requeue_failed_wake(rec, "placeholder append returned nothing")
            return
        if is_first_turn:
            try:
                storage.mark_claude_initialized(email, session_id)
            except Exception:
                pass
        run = _TurnRun(email=email, session_id=session_id, assistant_seq=assistant_seq)
        _active_runs[key] = run
        full_text_parts: list[str] = []
        try:
            if is_persistent:
                proc = await _session_mgr.get_or_create(key, lambda: (argv, env), _on_auto_turn)
                user_turn = await proc.send_user(effective_prompt)
                gen = claude_runner.normalize_session_turn(user_turn, model=None)
            elif _is_api_model(wake_model):
                # Stateless API model: same dispatch as a typed turn, with the
                # thread's transcript (everything before the placeholder we
                # just appended) as history so the wake knows what it is
                # following up on.
                gen = await _api_model_turn_gen(
                    model=wake_model,
                    user_text=wake_text,
                    role=role,
                    container=container,
                    session_id=session_id,
                    prior_history=_stateless_history(session.get("messages") or []),
                    persona=persona,
                    memory=memory,
                    output_language=output_language,
                    effort=None,
                )
            else:
                # Per-turn path: run_turn wraps the raw instruction with the
                # artifacts/persona/memory blocks itself and --resumes the
                # session, so pass wake_text UNWRAPPED (not build_streaming_prompt).
                gen = claude_runner.run_turn(
                    claude_session_id=claude_session_id,
                    prompt=wake_text,
                    is_first_turn=is_first_turn,
                    account=account, role=role, container=container,
                    model=None, persona=persona, memory=memory,
                    chat_session_id=session_id,
                    output_language=output_language, user_email=email,
                )
            run._gen = gen  # type: ignore[attr-defined]  # so /cancel can close it
            status = await _consume_into_run(
                run, gen, full_text_parts,
                container=container, artifacts_dispatch=artifacts_dispatch,
                turn_start_ts=time.time(), memory_at_start=memory,
            )
            if status == "error":
                # The turn died without producing a message (account cooldown,
                # stale resume, API failure). run_turn swallowed it into an
                # error event, so this is the ONLY place that can save the wake.
                _requeue_failed_wake(rec, "turn ended in error")
            else:
                logger.info(
                    "scheduled wake %s fired for %s session=%s (persistent=%s)",
                    sched_id, email, session_id, is_persistent,
                )
        except Exception as exc:
            logger.exception("scheduled wake %s consume crashed for %s", sched_id, session_id)
            _requeue_failed_wake(rec, f"fire crashed: {exc!s}")
            partial = "".join(full_text_parts)
            try:
                storage.update_assistant_message(
                    email, session_id, assistant_seq,
                    content=_with_error_notice(
                        partial, f"scheduled wake crashed: {exc!s}"),
                    status=storage.ASSISTANT_STATUS_ERROR,
                )
            except Exception:
                pass
            try:
                await run.finalize({"type": "error", "message": f"scheduled wake crashed: {exc!s}"})
            except Exception:
                pass
        finally:
            if _active_runs.get(key) is run:
                _active_runs.pop(key, None)
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


_schedule_tick_stop = asyncio.Event()
_schedule_tick_task: asyncio.Task[None] | None = None


async def _schedule_tick_loop(*, interval: float = 30.0) -> None:
    """Poll the durable schedule store and fire due wakes. Each fire runs as
    its own task so one slow/locked session can't hold up the others."""
    while not _schedule_tick_stop.is_set():
        try:
            await asyncio.wait_for(_schedule_tick_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if _schedule_tick_stop.is_set():
            break
        try:
            due = await asyncio.to_thread(chat_scheduler.claim_due)
        except Exception:
            logger.exception("schedule tick: claim_due failed")
            continue
        for rec in due:
            try:
                _spawn_background(_fire_schedule(rec))
            except Exception:
                logger.exception("schedule tick: spawn fire failed for %s", rec.get("id"))


async def _run_turn_worker(
    *,
    run: _TurnRun,
    user_text: str,
    claude_session_id: str,
    is_first_turn: bool,
    title_was_empty: bool,
    lock: asyncio.Lock,
    account: str | None = None,
    role: str | None = None,
    container: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    prior_history: str | None = None,
    stateless_history: str | None = None,
) -> None:
    """Drive one agent turn to terminal. NEVER raises; always finalises run.

    ``prior_history`` is the fork-first-turn preamble (claude CLI path only —
    ordinary turns there ``--resume`` and must NOT get a transcript replay).
    ``stateless_history`` is this session's full prior transcript, used by
    the OpenAI-compatible runners on every turn because they hold no
    server-side session state.

    Contract:
      * On entry the worker already holds ``lock`` (acquired by
        ``post_message`` and handed off). The worker is responsible for
        releasing it in ``finally``.
      * On entry ``run`` is registered in ``_active_runs[key]`` and the
        task is registered in ``_active_tasks[key]``. The worker removes
        both in ``finally``.
      * Emits ``delta`` / ``tool_start`` / ``tool_end`` events through
        ``run.emit()`` as the model produces them.
      * Persists the assistant placeholder's final content + status on
        terminal (``complete`` | ``error`` | ``cancelled``).
      * Schedules the title-generation background task on the first
        successful ``done`` of an unnamed session.

    Cancellation: the cancel endpoint sets ``run.cancel_requested = True``
    and calls ``aclose()`` on the underlying claude generator. The
    worker checks the flag at every event boundary AND will exit on
    StopAsyncIteration when the gen is closed.
    """
    email = run.email
    session_id = run.session_id
    assistant_seq = run.assistant_seq
    full_text_parts: list[str] = []
    saw_terminal = False

    # Look up attachments dir the same way Phase 2 did; logic unchanged.
    try:
        attachments_dir = (
            attachments.session_attachments_dir(session_id)
            if attachments.has_attachments(session_id)
            else None
        )
    except Exception:
        # session_attachments_dir validates session_id shape; if it
        # rejects somehow at this stage we still want a clean terminal.
        attachments_dir = None

    # Hold a reference to the inner generator so the cancel handler can
    # close it from the outside. Stored on the run for the cancel
    # endpoint to find.
    #
    # The ``| None`` is genuinely reachable: if ``run_turn(...)`` raises
    # synchronously (e.g. an import-time misconfiguration in
    # claude_runner), ``gen`` stays None and the ``finally`` below
    # needs the guard to avoid a NameError-by-attribute on aclose.
    gen: AsyncIterator[dict[str, Any]] | None = None

    # Kick off title generation in PARALLEL with the run rather than
    # after `done`. The title call uses a fresh, independent claude
    # session id so it doesn't contend with the main turn's subprocess;
    # it typically returns within a few seconds, well before the user's
    # full response, so the sidebar's title appears as soon as the
    # title call completes instead of waiting for the (potentially
    # long) main response. (The post-done spawn below is gone — kicked
    # to the side so we don't double-fire.)
    #
    # Gate on title_was_empty ALONE (not is_first_turn): if the first turn's
    # title attempt failed AND the fallback somehow didn't land (e.g. the task
    # never ran), a later turn self-heals instead of staying untitled forever.
    # set_title is set-once, so once a title exists this is a cheap no-op
    # (title_was_empty is False) and an already-titled session never re-fires.
    if title_was_empty:
        _spawn_background(
            _title_task(
                email=email,
                session_id=session_id,
                claude_session_id=claude_session_id,
                user_text=user_text.replace(_RECOVERY_NOTE, ""),
            )
        )

    # User-level settings that influence the turn (persona is the only
    # one used backend-side; default_model/send_on_enter/etc. are
    # frontend-only). Read at turn time, not session-create time, so a
    # mid-session persona update takes effect on the next turn.
    persona: str | None = None
    output_language: str = "en"
    try:
        _settings_blob = storage.get_settings(email)
        persona = _settings_blob.get("persona") or None
        # ui_language is purely a frontend concern (drives the i18n
        # context for sidebar / composer / settings labels). Only the
        # output_language matters here — that's what the model uses.
        output_language = _settings_blob.get("output_language") or "en"
    except Exception:
        persona = None
        output_language = "en"
    # Cross-session memory. Always pass a string (even ""), so the runner
    # always emits the [Cross-session memory ...] protocol block — that's
    # what teaches the model the <memory_update> tag, including in the
    # cold-start "memory is empty, start filling it" case.
    memory: str = ""
    try:
        memory = storage.get_memory(email)
    except Exception:
        logger.exception("get_memory failed for %s", email)
        memory = ""
    memory_at_start: str | None = memory
    # Inline artifacts. Pre-create the per-session dir so claude doesn't
    # have to mkdir before its first save, and stamp the start time so we
    # can scan for files NEW to this turn (skipping older image-gen output).
    # Dispatch decision mirrors the one inside run_turn: admin → host,
    # role=user with container → user, else local.
    if role == "admin":
        _artifacts_dispatch = "host"
    elif role == "user" and container:
        _artifacts_dispatch = "user"
    else:
        _artifacts_dispatch = "local"
    _ensure_artifacts_dir(
        session_id=session_id,
        dispatch=_artifacts_dispatch,
        container_name=container,
    )
    # New-lineup normalization (identity in legacy mode): missing /
    # "default" / stale claude aliases map to the default non-claude model
    # so the claude CLI path is never reached by a stale client.
    model = _normalize_model(model)
    # Drop effort values the effective model doesn't support — junk from a
    # stale tab or a hand-crafted POST must never reach the provider.
    effort = _validated_effort(model, effort)
    turn_start_ts = time.time()

    try:
        if _is_api_model(model):
            # Non-Claude OpenAI-compatible models: the TokenHub-hosted
            # glm-5.3 / kimi-k3, the haihub-hosted qwen/deepseek/minimax and
            # the home-GPU "gemma4-local" (LM Studio). These do NOT use the
            # claude CLI / persistent-session path; they stream via their own
            # runner with the SAME normalized event contract the consume
            # loop below expects. Being stateless they get this session's
            # full prior transcript every turn (stateless_history).
            gen = await _api_model_turn_gen(
                model=model,
                user_text=user_text,
                role=role,
                container=container,
                session_id=session_id,
                prior_history=(
                    stateless_history if stateless_history is not None
                    else prior_history
                ),
                persona=persona,
                memory=memory,
                output_language=output_language,
                effort=effort,
                attachments_dir=attachments_dir,
            )
        elif _persistent_enabled():
            # A1: route the user turn through the session's long-lived
            # streaming process, so a task it backgrounds can auto-surface and
            # the agent reports back (via _on_auto_turn) with no new user turn.
            dispatch = claude_runner.resolve_dispatch(role, container)
            if dispatch == "user-fail-closed":
                await run.finalize({
                    "type": "error",
                    "message": "user session has no provisioned container",
                })
                return
            artifacts_path = claude_runner.artifacts_path_for(dispatch, session_id)
            effective_prompt = claude_runner.build_streaming_prompt(
                user_text,
                prior_history=prior_history,
                persona=persona,
                output_language=output_language,
                memory=memory,
                artifacts_path=artifacts_path,
            )
            argv, env = claude_runner.make_streaming_args_env(
                claude_session_id,
                is_first_turn=is_first_turn,
                attachments_dir=attachments_dir,
                model=model,
                role=role,
                container=container,
                account=account,
                user_email=email,
            )
            proc = await _session_mgr.get_or_create(
                (email, session_id), lambda: (argv, env), _on_auto_turn,
            )
            user_turn = await proc.send_user(effective_prompt)
            gen = claude_runner.normalize_session_turn(user_turn, model=model)
        else:
            gen = claude_runner.run_turn(
                claude_session_id=claude_session_id,
                prompt=user_text,
                attachments_dir=attachments_dir,
                is_first_turn=is_first_turn,
                account=account,
                role=role,
                container=container,
                model=model,
                prior_history=prior_history,
                persona=persona,
                memory=memory,
                chat_session_id=session_id,
                output_language=output_language,
                user_email=email,
            )
        # Stash on the run so cancel can aclose() it.  This is read by the
        # cancel handler under no lock — assignment is atomic.
        run._gen = gen  # type: ignore[attr-defined]

        reasoning_parts: list[str] = []
        async for event in gen:
            # Honour an explicit cancel BEFORE forwarding the event.  The
            # cancel handler also aclose()s the gen, so we usually exit
            # via StopAsyncIteration anyway; this is defence-in-depth.
            if run.cancel_requested:
                break

            et = event.get("type")
            if et == "delta":
                chunk = event.get("text") or ""
                if chunk:
                    full_text_parts.append(chunk)
                await run.emit({"type": "delta", "text": chunk})
            elif et == "reasoning":
                # Hidden reasoning: streamed for display, persisted on the
                # message meta at ``done``. Never joins full_text_parts.
                rchunk = event.get("text") or ""
                if rchunk:
                    reasoning_parts.append(rchunk)
                    await run.emit({"type": "reasoning", "text": rchunk})
            elif et == "tool_start":
                await run.emit({
                    "type": "tool_start",
                    "name": event.get("name", "tool"),
                    "input_summary": event.get("input_summary", ""),
                })
            elif et == "tool_end":
                await run.emit({
                    "type": "tool_end",
                    "name": event.get("name", "tool"),
                })
            elif et == "done":
                assistant_text = event.get("full_text") or "".join(full_text_parts)
                turn_meta = event.get("meta") if isinstance(event.get("meta"), dict) else None
                _reasoning_text = _capped_reasoning(reasoning_parts)
                if _reasoning_text is not None:
                    turn_meta = dict(turn_meta or {})
                    turn_meta["reasoning"] = _reasoning_text
                # If the model emitted a <memory_update> sentinel at the
                # end of its response, persist the new memory and strip
                # the block from what we save / show. Memory updates only
                # land on a clean ``done`` — error/cancel paths skip this
                # so partial blocks don't corrupt memory.
                assistant_text, new_memory = _extract_memory_update(assistant_text)
                if new_memory is not None:
                    try:
                        storage.set_memory_merged(email, new_memory, memory_at_start)
                    except Exception:
                        logger.exception(
                            "set_memory failed for %s after turn complete", email,
                        )
                # Collect inline artifacts. For user dispatch we first
                # docker-cp /workspace/.artifacts/<sid>/ out of the per-user
                # container into chat-side /data/generated/<sid>/, then
                # scan that dir for files newer than turn_start_ts. The
                # appended markdown is sent to the client as a final delta
                # AND embedded in the persisted assistant message so a
                # session reload still shows the artifacts.
                if _artifacts_dispatch == "user" and container:
                    await asyncio.to_thread(
                        _collect_user_container_artifacts,
                        session_id=session_id, container_name=container,
                    )
                # Resolve any autonomous image-gen requests the model
                # dropped during this turn. Runs AFTER the user-container
                # collection (so markers tar-streamed out have a chance
                # to land chat-side first) and BEFORE the artifact scan
                # (so the produced .png is included in this turn's
                # artifact footer).
                try:
                    await _process_image_requests(session_id=session_id)
                except Exception:
                    logger.exception(
                        "image-request processing failed for %s", session_id,
                    )
                try:
                    _scheduled_n = await _process_schedule_requests(
                        session_id=session_id, email=email,
                    )
                except Exception:
                    logger.exception(
                        "schedule-request processing failed for %s", session_id,
                    )
                    _scheduled_n = 0
                artifacts = _scan_new_artifacts(
                    session_id=session_id, since_ts=turn_start_ts,
                )
                artifact_md = _format_artifacts_markdown(artifacts)
                if artifact_md:
                    assistant_text = assistant_text.rstrip() + "\n" + artifact_md
                    # Stream the appended markdown so the live UI updates
                    # without a refresh. Persisting the augmented text
                    # below covers the "open this session later" path.
                    await run.emit({"type": "delta", "text": "\n" + artifact_md})
                try:
                    storage.update_assistant_message(
                        email, session_id, assistant_seq,
                        content=assistant_text,
                        status=storage.ASSISTANT_STATUS_COMPLETE,
                        meta=turn_meta,
                    )
                except Exception:
                    logger.exception(
                        "update_assistant_message(complete) failed for %s seq=%s",
                        session_id, assistant_seq,
                    )
                    await run.finalize({"type": "error", "message": "persistence failed"})
                    saw_terminal = True
                    return
                # Notify the client that memory was rewritten this turn so
                # an open Settings panel can refresh without polling. Sent
                # before ``done`` so the frontend handles them in order.
                if new_memory is not None:
                    await run.emit({"type": "memory_updated"})
                if _scheduled_n:
                    await run.emit({"type": "schedules_updated"})
                await run.finalize({"type": "done", "full_text": assistant_text, "meta": turn_meta})
                saw_terminal = True
                # Title task already kicked off at worker entry (spawned
                # in parallel with the main turn). Nothing to schedule
                # here.
                return
            elif et == "error":
                partial = "".join(full_text_parts)
                err_message = event.get("message", "error")
                try:
                    storage.update_assistant_message(
                        email, session_id, assistant_seq,
                        content=_with_error_notice(_scrub_partial(partial), err_message),
                        status=storage.ASSISTANT_STATUS_ERROR,
                    )
                except Exception:
                    logger.exception(
                        "update_assistant_message(error) failed for %s seq=%s",
                        session_id, assistant_seq,
                    )
                await run.finalize({"type": "error", "message": err_message})
                saw_terminal = True
                return
            else:
                # Unknown event — ignore. ``run_turn`` is the only producer.
                continue

        # Loop ended without a terminal event. This happens when the
        # cancel handler aclose()s the gen, or when the runner contract
        # is violated (shouldn't happen in production, but we defend).
        if not saw_terminal:
            partial = "".join(full_text_parts)
            if run.cancel_requested:
                try:
                    storage.update_assistant_message(
                        email, session_id, assistant_seq,
                        content=_scrub_partial(partial),
                        status=storage.ASSISTANT_STATUS_CANCELLED,
                    )
                except Exception:
                    logger.exception(
                        "update_assistant_message(cancelled) failed for %s seq=%s",
                        session_id, assistant_seq,
                    )
                await run.finalize({"type": "cancelled", "full_text": partial})
            else:
                try:
                    storage.update_assistant_message(
                        email, session_id, assistant_seq,
                        content=_with_error_notice(
                            partial, "stream ended without terminal event"),
                        status=storage.ASSISTANT_STATUS_ERROR,
                    )
                except Exception:
                    logger.exception(
                        "update_assistant_message(no-terminal) failed for %s seq=%s",
                        session_id, assistant_seq,
                    )
                await run.finalize({
                    "type": "error",
                    "message": "stream ended without terminal event",
                })
    except asyncio.CancelledError:
        # Task-level cancellation (the cancel endpoint may resort to
        # task.cancel() if aclose() is too slow). Treat as user cancel —
        # unless the PROCESS is shutting down: then leave the placeholder
        # at 'streaming' so the next boot re-runs this turn.
        partial = "".join(full_text_parts)
        if _shutting_down:
            logger.warning(
                "shutdown: leaving turn %s seq=%s for restart recovery",
                session_id, assistant_seq,
            )
        else:
            try:
                storage.update_assistant_message(
                    email, session_id, assistant_seq,
                    content=_scrub_partial(partial),
                    status=storage.ASSISTANT_STATUS_CANCELLED,
                )
            except Exception:
                logger.exception(
                    "update_assistant_message(cancelled-via-task-cancel) failed for %s seq=%s",
                    session_id, assistant_seq,
                )
        # finalize() emits the terminal even though we were cancelled;
        # subscribers need it.
        try:
            await run.finalize({"type": "cancelled", "full_text": partial})
        except Exception:
            pass
        # Do NOT re-raise — we want clean teardown via the finally below.
    except Exception as exc:
        # Unexpected exception inside the worker. Don't let it propagate
        # as an unhandled task error; persist + finalise as ``error``.
        logger.exception("worker for %s seq=%s crashed", session_id, assistant_seq)
        partial = "".join(full_text_parts)
        try:
            storage.update_assistant_message(
                email, session_id, assistant_seq,
                content=_with_error_notice(_scrub_partial(partial), f"worker crashed: {exc!s}"),
                status=storage.ASSISTANT_STATUS_ERROR,
            )
        except Exception:
            pass
        try:
            await run.finalize({"type": "error", "message": f"worker crashed: {exc!s}"})
        except Exception:
            pass
    finally:
        # Always: drop registry entries and release the lock so the next
        # turn for this session can start. Order matters: drop registry
        # FIRST so a new post_message arriving right after sees no active
        # run, THEN release the lock so it can proceed.
        key = (email, session_id)
        # Identity-guarded: a CLI-initiated auto-continuation turn (A1) may have
        # already registered its own run under this key right after our `done`;
        # don't pop it.
        # Close the generator and purge this turn's uploads BEFORE the lock is
        # released: both await, and a post_message waiting on the lock used to
        # slip in between, snapshot fresh uploads for turn N+1 — which the
        # purge below then deleted (chips shown, files never reached the model).
        if gen is not None:
            try:
                await gen.aclose()
            except Exception:
                pass
        # Purge the per-session attachments dir on every terminal so files
        # uploaded for THIS turn don't silently re-enter the model context on
        # the next turn. Keep them if we are dying with the process: the
        # re-run after restart needs them.
        if not _shutting_down:
            try:
                attachments.delete_attachments_dir(session_id)
            except Exception:  # pragma: no cover — best-effort
                logger.exception(
                    "attachments cleanup failed for %s", session_id,
                )
        if _active_runs.get(key) is run:
            _active_runs.pop(key, None)
        if _active_tasks.get(key) is asyncio.current_task():
            _active_tasks.pop(key, None)
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass


# ===========================================================================
# Image worker: parallel to _run_turn_worker but routes the prompt through
# Gemini and persists the resulting PNG. Same TurnRun + lock-handoff
# protocol so SSE subscribers and the cancel endpoint behave identically
# to a chat turn from the outside.
# ===========================================================================

import uuid as _uuid_for_imgname  # noqa: E402


def _persist_turn_error(email: str, session_id: str, seq: int, message: str) -> None:
    """Best-effort: mark the placeholder as errored so it never lingers at
    'streaming' (which reads as a hung turn and would be re-run at boot)."""
    try:
        storage.update_assistant_message(
            email, session_id, seq,
            content=_with_error_notice("", message),
            status=storage.ASSISTANT_STATUS_ERROR,
        )
    except Exception:
        logger.exception("persist_turn_error failed for %s seq=%s", session_id, seq)


async def _run_image_worker(
    *,
    run: _TurnRun,
    user_text: str,
    lock: asyncio.Lock,
) -> None:
    """Generate an image with Gemini, persist it, finalise the run."""
    email = run.email
    session_id = run.session_id
    assistant_seq = run.assistant_seq
    prompt = user_text.strip()

    try:
        try:
            png_bytes, mime = await image_gen.generate_image(prompt)
        except image_gen.ImageGenError as exc:
            err_msg = str(exc)
            try:
                storage.update_assistant_message(
                    email, session_id, assistant_seq,
                    content=f"⚠ image generation failed: {err_msg}",
                    status=storage.ASSISTANT_STATUS_ERROR,
                )
            except Exception:
                logger.exception(
                    "update_assistant_message(image-error) failed for %s seq=%s",
                    session_id, assistant_seq,
                )
            await run.finalize({"type": "error", "message": err_msg})
            return
        except Exception as exc:  # defence: never let the worker raise
            logger.exception("image worker crashed for %s seq=%s", session_id, assistant_seq)
            _persist_turn_error(email, session_id, assistant_seq, f"image worker crashed: {exc!s}")
            await run.finalize({"type": "error", "message": f"image worker crashed: {exc!s}"})
            return

        ext = "png"
        if mime == "image/jpeg":
            ext = "jpg"
        elif mime == "image/webp":
            ext = "webp"
        gen_dir = _session_generated_dir(session_id)
        try:
            gen_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
        except Exception:
            logger.exception("could not create generated dir for %s", session_id)
            _persist_turn_error(email, session_id, assistant_seq, "image persist failed")
            await run.finalize({"type": "error", "message": "image persist failed"})
            return
        filename = f"{_uuid_for_imgname.uuid4().hex[:16]}.{ext}"
        path = gen_dir / filename
        try:
            path.write_bytes(png_bytes)
            os.chmod(path, 0o640)
        except Exception:
            logger.exception("could not write generated image for %s", session_id)
            _persist_turn_error(email, session_id, assistant_seq, "image persist failed")
            await run.finalize({"type": "error", "message": "image persist failed"})
            return

        url = f"/api/sessions/{session_id}/generated/{filename}"
        # Markdown image. Frontend renders via the existing react-markdown
        # pipeline; ![alt](url) lands as a styled <img>. The alt text is
        # the truncated prompt so screen-readers / hover-titles still
        # describe the image meaningfully.
        alt = prompt.replace("\n", " ").strip()
        if len(alt) > 120:
            alt = alt[:117] + "…"
        markdown = f"![{alt}]({url})"
        try:
            storage.update_assistant_message(
                email, session_id, assistant_seq,
                content=markdown,
                status=storage.ASSISTANT_STATUS_COMPLETE,
            )
        except Exception:
            logger.exception(
                "update_assistant_message(image-done) failed for %s seq=%s",
                session_id, assistant_seq,
            )
            await run.finalize({"type": "error", "message": "persistence failed"})
            return
        await run.finalize({"type": "done", "full_text": markdown})
    except asyncio.CancelledError:
        # Stop during image generation: persist + finalise like the chat
        # worker does, or the SSE never closes and the bubble spins forever.
        try:
            storage.update_assistant_message(
                email, session_id, assistant_seq,
                content="", status=storage.ASSISTANT_STATUS_CANCELLED,
            )
        except Exception:
            logger.exception("update_assistant_message(image-cancel) failed for %s", session_id)
        try:
            await run.finalize({"type": "cancelled", "full_text": ""})
        except Exception:
            pass
    finally:
        key = (email, session_id)
        # Identity-guarded: a CLI-initiated auto-continuation turn (A1) may have
        # already registered its own run under this key right after our `done`;
        # don't pop it.
        if _active_runs.get(key) is run:
            _active_runs.pop(key, None)
        if _active_tasks.get(key) is asyncio.current_task():
            _active_tasks.pop(key, None)
        if lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass


# ===========================================================================
# SSE subscriber: consumes one TurnRun's event stream and yields SSE bytes.
# Used by both POST .../messages (the original SSE response) and the new
# GET .../stream reconnect endpoint.
# ===========================================================================

def _strip_type_key(evt: dict[str, Any]) -> dict[str, Any]:
    """Return ``evt`` with the ``type`` key dropped.

    The SSE wire format puts the event type on the ``event:`` line and
    every other field on the ``data:`` JSON; this helper produces the
    ``data:`` payload from a TurnRun event dict. Module-level so the
    function isn't re-created on every ``_sse_subscribe`` invocation
    (each subscribe is one HTTP response — for an idle service the cost
    is irrelevant, but the closure-style nested def signalled
    misleading per-call state to readers).
    """
    return {k: v for k, v in evt.items() if k != "type"}


async def _sse_subscribe(run: _TurnRun) -> AsyncIterator[bytes]:
    """Subscribe to ``run`` and yield SSE-framed bytes until terminal.

    Replay semantics: every event the run has emitted SO FAR is yielded
    first, in order, then live events drain from the per-subscriber queue
    until the sentinel arrives. This means a client that connects mid-run
    sees the full event log of the run-to-date AND every live event,
    with no duplicates and no gaps.

    Disconnect: when the SSE response is cancelled by Starlette (client
    closed the connection), this generator's finally detaches the
    subscriber. The run keeps going.
    """
    snapshot, q = await run.attach()
    try:
        # Replay phase: every event the run has emitted before we
        # attached. The same events are NOT in the live queue (attach()
        # snapshot+register is atomic), so this is the only place a
        # subscriber sees them.
        for evt in snapshot:
            yield _sse_event(evt["type"], _strip_type_key(evt))
        # Live phase: drain the queue until sentinel.
        while True:
            evt = await q.get()
            if evt is _TurnRun._SENTINEL:
                return
            yield _sse_event(evt["type"], _strip_type_key(evt))
    finally:
        await run.detach(q)


async def _with_keepalive(
    gen: AsyncIterator[bytes],
    interval: float = 20.0,
) -> AsyncIterator[bytes]:
    """Wrap an async byte iterator so it emits an SSE-comment keep-alive
    every ``interval`` seconds during idle gaps in the underlying stream.

    Cloudflare's proxy idle timeout is ~100s; claude turns that involve
    long-running tools (Read on a large file, Bash, Edit) can sit silent
    for tens of seconds at a time even when the worker is happily waiting
    on a subprocess. Without keep-alives the front-end fetch sees the TCP
    connection cut by Cloudflare and reports "Stream failed: load failed".
    Keep-alive frames are SSE comments (lines starting with ``:``) which
    compliant clients ignore.

    Care taken: the underlying iterator's ``__anext__`` is held across
    timeout cycles via a long-lived task, so we never cancel mid-event.
    """
    iterator = gen.__aiter__()
    pending: asyncio.Task[bytes] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if pending in done:
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield chunk
            else:
                yield b": keep-alive\n\n"
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


# ===========================================================================
# HTTP handlers: POST .../messages, POST .../cancel, GET .../stream.
# ===========================================================================

# ---------------------------------------------------------------------------
# Phase 5 (EB-M3 fix — audit/extra_bugs.md): cheap server-side limits on
# the message-send path. Both responses are HTTP error codes that the
# SPA's ApiError handler already surfaces as a banner; no client change
# is required for these to take effect.
#
#   * MAX_MESSAGE_TEXT_BYTES — body-size cap returning 413. 64 KiB is
#     ~16k tokens of english prose — comfortably more than any normal
#     interactive turn, well below the input-token caps real claude
#     deployments enforce.
#   * MAX_INFLIGHT_PER_USER — per-user concurrency cap returning 429.
#     The Phase 4 per-session 409 already prevents a single session from
#     spawning multiple workers; this caps the user's TOTAL across
#     sessions so one client opening a dozen tabs cannot saturate the
#     host.
#
# Both are tunable via env vars so a deployer can dial them up without
# a code change.  Defaults are intentionally tight — Phase 6 may raise
# them after observing real traffic.
# ---------------------------------------------------------------------------
# Both constants are READ AT IMPORT TIME from the environment. Tests
# that need a different value MUST use ``monkeypatch.setattr(app,
# "<NAME>", value)`` rather than ``monkeypatch.setenv(...)`` — once the
# module has been imported the env-var read has already happened, so
# ``setenv`` won't take effect. Production deployers set the env vars
# in the systemd / docker-compose unit before the process starts.
MAX_MESSAGE_TEXT_BYTES = int(os.environ.get("CHAT_MAX_MESSAGE_BYTES", 64 * 1024))
MAX_INFLIGHT_PER_USER = int(os.environ.get("CHAT_MAX_INFLIGHT_PER_USER", 3))

# How long POST /messages waits for the per-session lock before answering
# 409. Sized to cover the worker's teardown handoff (finalize -> pop
# _active_runs -> release lock), which the interrupt-then-send path races,
# while staying far below any edge/proxy request timeout.
SESSION_LOCK_WAIT_SEC = float(os.environ.get("CHAT_SESSION_LOCK_WAIT_SEC", 8))


def _count_inflight_for_email(email: str) -> int:
    """Number of currently-active runs owned by ``email``.

    Reads ``_active_runs`` directly. Cheap (the dict is small — bounded
    by total in-flight turns across the process). Used only by the
    rate-limit guard in post_message.
    """
    return sum(1 for (e, _sid) in _active_runs.keys() if e == email)


@app.post("/api/sessions/{session_id}/messages")
async def post_message(
    session_id: str,
    body: SendMessageBody,
    request: Request,
    claims: dict[str, Any] = Depends(require_user),
) -> StreamingResponse:
    """Start (or reject) one agent turn and return an SSE subscriber.

    Phase 4 ordering:
      1. 404 / 400 guards.
      2. Acquire the per-session lock briefly to atomically check
         ``_active_runs[key]``. If a run is already in flight, release
         the lock and return 409 (the documented contract — see
         CHANGES.md).
      3. Persist the user message + an assistant placeholder
         (``status="streaming"``).
      4. Construct the TurnRun, register it in ``_active_runs[key]``.
      5. Spawn the worker task with the lock HANDED OFF — the worker's
         ``finally`` releases it. ``post_message`` itself does NOT
         release the lock; control transfer is explicit so a missed
         release is a code-search-able bug.
      6. Return an SSE response that subscribes to the run.

    The SSE response's lifetime is INDEPENDENT of the worker. Closing
    the response unsubscribes the listener; the run keeps going. This
    is the Bug 3 fix.
    """
    email = _user_email(claims)
    session = storage.get_session(email, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Auto-heal legacy sessions before dispatch. Three cases:
    #   (a) role unset on a session created pre-role-locking → resolve from
    #       email and persist, so the runner's role-based dispatch picks the
    #       right path instead of falling into legacy-local (which would
    #       leak /home/felix for non-admin emails).
    #   (b) role=user but container unset (provisioning failed at create or
    #       session pre-dates multi-user-containers) → call ensure_user_container
    #       now so the worker dispatches into the per-user container instead
    #       of the chat container itself.
    #   (c) role=user and container name persisted but the actual docker
    #       container is gone (operator-initiated rm, daemon restart with
    #       lost state, eviction race) → ensure_user_container reprovisions
    #       it idempotently. Without this, the runner would docker-exec
    #       into a missing container and the turn would error out with
    #       "No such container: <name>" — a UX regression for the user
    #       since the persisted name itself was correct.
    # Both paths fail closed in the runner if heal-on-resume fails for any
    # reason — see claude_runner.run_turn role=="user" and not container.
    healed_fields: dict[str, Any] = {}
    if session.get("role") is None:
        healed_fields["role"] = _resolve_role(email)
    if not session.get("account"):
        try:
            healed_fields["account"] = account_router.pick().name
        except Exception:
            # Saturated/dead pool: the glm/kimi path does not need one, and a
            # 500 here would block the turn for nothing.
            pass
    effective_role = healed_fields.get("role", session.get("role"))
    # Workspace defaults to "personal" for legacy sessions that pre-date
    # the shared-workspace toggle. Shared sessions whose container name
    # was lost (e.g. JSON hand-edit, post-restart provisioning failure)
    # heal back into the shared container so the user doesn't silently
    # land in a private workspace mid-conversation.
    effective_workspace = (
        session.get("workspace") or "personal"
    )
    if effective_role == "user":
        persisted_container = session.get("container")
        needs_heal = not persisted_container
        if persisted_container and not needs_heal:
            try:
                await asyncio.to_thread(lambda: docker.from_env().containers.get(persisted_container))
            except docker.errors.NotFound:
                needs_heal = True
            except Exception:
                # Daemon hiccup or transient error; fall through to the
                # runner which will surface a useful message rather than
                # us silently respawning. ensure_user_container is also
                # idempotent so the runner-side path can re-attempt.
                pass
        if needs_heal:
            try:
                if effective_workspace == "shared":
                    healed_fields["container"] = await asyncio.to_thread(user_container.ensure_shared_container)
                    # Match create_session: record the shared container's
                    # preferred account resolved to a usable one (per-turn
                    # re-resolution makes this a label, not a hard pin).
                    healed_fields["account"] = user_container.resolve_usable_account(
                        user_container.shared_container_account()
                    )
                else:
                    healed_fields["container"] = await asyncio.to_thread(user_container.ensure_user_container, email)
            except Exception:
                logger.exception(
                    "ensure_%s_container failed for legacy session %s; runner will fail closed",
                    "shared" if effective_workspace == "shared" else "user",
                    session_id,
                )
    if healed_fields:
        updated = storage.backfill_session_meta(email, session_id, **healed_fields)
        if updated is not None:
            session = updated
    if not isinstance(body.text, str):
        raise HTTPException(status_code=400, detail="text must be a string")
    if not body.text.strip():
        # An attachments-only turn is valid in chat mode: the runner injects
        # an attachment preamble (see claude_runner._attachment_preamble) so
        # the model still has actionable input. Image generation genuinely
        # needs a text prompt, and a turn with neither text nor files is
        # meaningless — reject those.
        _attachments_only_ok = body.mode != "image"
        if _attachments_only_ok:
            try:
                _attachments_only_ok = attachments.has_attachments(session_id)
            except Exception:
                _attachments_only_ok = False
        if not _attachments_only_ok:
            raise HTTPException(
                status_code=400,
                detail="text must be a non-empty string (or attach a file)",
            )
    # EB-M3: body-size cap. 413 Payload Too Large is the documented
    # status for over-sized request bodies; the SPA's ApiError handler
    # surfaces the detail string in the error banner.
    text_bytes = len(body.text.encode("utf-8"))
    if text_bytes > MAX_MESSAGE_TEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"message text exceeds {MAX_MESSAGE_TEXT_BYTES} bytes "
                f"(got {text_bytes})"
            ),
        )
    # EB-M3: per-user concurrency cap. 429 Too Many Requests. We check
    # BEFORE acquiring the per-session lock so a rate-limited caller
    # doesn't queue up behind their own running turn.
    if _count_inflight_for_email(email) >= MAX_INFLIGHT_PER_USER:
        raise HTTPException(
            status_code=429,
            detail=(
                f"user has too many in-flight turns (max "
                f"{MAX_INFLIGHT_PER_USER})"
            ),
        )

    user_text = body.text
    user_model = body.model
    user_effort = body.effort
    user_mode = "image" if body.mode == "image" else "chat"
    # Snapshot pre-append message count so we can detect fork-first-turn
    # (is_first_turn AND prior messages exist) and assemble a context
    # preamble for the worker. For non-forked sessions this is 0 on the
    # first turn and >0 on every later turn, but later turns also have
    # claude_initialized=True, so the AND keeps the preamble out of
    # ordinary --resume invocations.
    prior_messages_count = len(session.get("messages") or [])
    key = (email, session_id)
    lock = storage.get_session_lock(email, session_id)

    # Bounded lock wait. The worker holds this lock for the WHOLE lifetime
    # of its run, and it pops ``_active_runs`` before releasing — so a
    # plain ``await lock.acquire()`` here does not 409 while a turn is in
    # flight, it BLOCKS until that turn finishes. The documented contract
    # (see the Phase 4 block above) is 409, and the block was a silent
    # deviation with a real failure mode: agent turns routinely run for
    # minutes, the response headers can't be sent until the lock is won,
    # and the edge (Cloudflare, 100s) times the request out long before
    # that. The client then saw a transport failure for a message the
    # server had never even read — which is how a sent message could
    # vanish mid-response. Wait only long enough to ride out the handoff
    # window of a turn that is already retiring, then answer 409 and let
    # the client decide (cancel-and-retry).
    try:
        await asyncio.wait_for(lock.acquire(), timeout=SESSION_LOCK_WAIT_SEC)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=409,
            detail="another turn is already in flight for this session",
        )
    try:
        if key in _active_runs:
            # 409: the per-session at-most-one-turn invariant from
            # Phase 2 is preserved. Documented contract: clients must
            # cancel the in-flight turn (POST .../cancel) or wait for
            # it to finish before sending another.
            lock.release()
            raise HTTPException(
                status_code=409,
                detail="another turn is already in flight for this session",
            )
        # Snapshot the uploaded attachments BEFORE the user message is
        # persisted so the metadata lands on the user-message JSON and the
        # bytes survive the per-turn purge. The worker's finally calls
        # ``delete_attachments_dir`` after each turn so the model can't
        # silently re-ingest stale uploads on later turns; the preview dir
        # is a separate, seq-keyed tree that survives so the SPA can
        # render the historical bubble like claude.ai.
        attachments_meta_for_user: list[dict[str, Any]] = []
        try:
            if attachments.has_attachments(session_id):
                src_dir = attachments.session_attachments_dir(session_id)
                for entry in sorted(src_dir.iterdir()):
                    if not entry.is_file():
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    ext = entry.suffix.lower()
                    mime = _GENERATED_MEDIA_TYPES.get(
                        ext, "application/octet-stream",
                    )
                    attachments_meta_for_user.append({
                        "filename": entry.name,
                        "size": size,
                        "mime": mime,
                    })
        except Exception:
            logger.exception(
                "attachment snapshot failed for %s; user msg will lack chips",
                session_id,
            )
            attachments_meta_for_user = []

        # Persist user message + assistant placeholder. Both writes
        # happen under the lock, same as Phase 2.
        try:
            session, _user_seq = storage.append_user_message(
                email, session_id, user_text,
                attachments=attachments_meta_for_user or None,
                turn={"mode": user_mode, "model": user_model, "effort": user_effort},
            )
            # Stash bytes to the per-seq preview dir so the SPA can render
            # the attachments on reload. Best-effort: a copy failure
            # downgrades to "chip without preview" but the turn still runs.
            if attachments_meta_for_user and isinstance(_user_seq, int):
                try:
                    attachments.stash_previews_for_seq(session_id, _user_seq)
                except Exception:
                    logger.exception(
                        "preview-stash failed for %s seq=%s",
                        session_id, _user_seq,
                    )
            if session is None:
                lock.release()
                raise HTTPException(status_code=404, detail="session not found")
            session, assistant_seq = storage.append_assistant_placeholder(
                email, session_id,
            )
            if session is None or assistant_seq is None:
                lock.release()
                raise HTTPException(status_code=404, detail="session not found")
        except HTTPException:
            raise
        except Exception:
            lock.release()
            logger.exception(
                "failed to persist user message / placeholder for %s",
                session_id,
            )
            raise HTTPException(status_code=500, detail="persistence failed")

        # First-turn detection: NOT len(messages)==N. claude registers a
        # session UUID the moment we spawn the subprocess, even if our
        # turn errors out before any messages get persisted (and several
        # bug-fix cycles had exactly that shape). If we then see "no
        # claude_initialized" on retry but messages count is plausible
        # for first-turn, we'd incorrectly use --session-id again and
        # claude says "Session ID … is already in use." The
        # claude_initialized flag flips at FIRST SPAWN, not first
        # successful turn — see storage.mark_claude_initialized.
        is_first_turn = not session.get("claude_initialized")
        title_was_empty = not session.get("title")
        if is_first_turn:
            # Mark before spawning the worker, so even a mid-stream crash
            # doesn't leave us in the bad state next time.
            try:
                storage.mark_claude_initialized(email, session_id)
            except Exception:
                logger.exception(
                    "mark_claude_initialized failed for %s", session_id,
                )

        run = _TurnRun(
            email=email,
            session_id=session_id,
            assistant_seq=assistant_seq,
        )
        _active_runs[key] = run
        # Lock is NOT released here. Ownership transfers to the worker;
        # the worker's ``finally`` calls ``lock.release()`` exactly once.
        #
        # TODO(phase-5+): if a multi-process / cross-worker model is
        # introduced, this in-process lock-handoff pattern needs to be
        # replaced with a cross-process primitive (file flock, redis
        # SETNX, etc.) and the ``_active_runs`` registry made cluster-
        # global.  Same forward seam Phase 2 left for Phase 4 on
        # ``_stream_turn_with_lock``, now retired.
    except HTTPException:
        # Lock state on this path: released above before raise (each
        # raise branch above releases first). Re-raise unchanged.
        raise

    # Compute the prior-history preamble for fork-first-turn cases.
    # session["messages"] now includes the just-appended user msg +
    # placeholder; we slice off everything before those.
    prior_history: str | None = None
    stateless_history: str | None = None
    # ``session`` is the post-append snapshot (…, user msg, placeholder), so
    # everything before the last two entries is the prior transcript — read
    # fresh rather than from the pre-lock count, which missed a wake that
    # finished while we waited for the lock.
    prior = (session.get("messages") or [])[:-2]
    if prior:
        # Stateless runners (glm/kimi/qwen/...) need the whole transcript on
        # every turn; the claude path only wants it on a fork's first turn.
        stateless_history = _stateless_history(prior)
        if is_first_turn:
            prior_history = _format_prior_history(prior)

    # Spawn the worker. From this point onward the run is "live".
    if user_mode == "image":
        task = asyncio.create_task(
            _run_image_worker(
                run=run,
                user_text=user_text,
                lock=lock,
            )
        )
    else:
        task = asyncio.create_task(
            _run_turn_worker(
                run=run,
                user_text=user_text,
                claude_session_id=session["claude_session_id"],
                # account/role/container/etc. — see _run_turn_worker docstring.
                account=session.get("account"),
                role=session.get("role"),
                container=session.get("container"),
                is_first_turn=is_first_turn,
                title_was_empty=title_was_empty,
                lock=lock,
                model=user_model,
                effort=user_effort,
                prior_history=prior_history,
                stateless_history=stateless_history,
            )
        )
    _active_tasks[key] = task
    # Strong ref via _active_tasks; also discard via add_done_callback so
    # the registry can never leak even if the worker's finally somehow
    # bypasses the pop.  Defence in depth.
    def _cleanup_on_task_done(_t: asyncio.Task[None], _k: tuple[str, str] = key) -> None:
        if _active_runs.get(_k) is run:
            _active_runs.pop(_k, None)
        if _active_tasks.get(_k) is _t:
            _active_tasks.pop(_k, None)
    task.add_done_callback(_cleanup_on_task_done)

    return StreamingResponse(
        _with_keepalive(_sse_subscribe(run)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/sessions/{session_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    session_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Explicit user cancel for the in-flight turn on this session.

    Returns 202 with ``{"cancelled": True}`` if a run was active and a
    cancel was issued; 404 if the session doesn't exist or isn't owned;
    409 with ``{"cancelled": False, "reason": "no active run"}`` if
    there's nothing to cancel (the run already finished).

    The cancel does the following, in order:
      1. Set ``run.cancel_requested = True`` so the worker's loop
         predicate notices on its next iteration.
      2. ``aclose()`` the underlying claude generator. This makes the
         worker's ``async for`` loop terminate via StopAsyncIteration
         and fall into the post-loop "no terminal" branch, which writes
         status=cancelled and finalises the run.
      3. As a last resort (e.g., the worker is wedged on a non-claude
         await), call ``task.cancel()``. The worker catches
         CancelledError in an outer except and still finalises cleanly.

    The endpoint returns AS SOON AS the cancel has been signalled — it
    does NOT await the worker's termination, since that could block on
    the SIGTERM timeout in spawn_claude. Tests that need to assert
    cleanup completed should poll ``_get_active_runs_snapshot`` or
    await the task directly.
    """
    email = _user_email(claims)
    # Ownership check via the storage layer's existence-and-cross-email
    # 404 funnel; same shape as every other session-scoped endpoint.
    if storage.get_session(email, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")

    key = (email, session_id)
    run = _active_runs.get(key)
    if run is None:
        # Nothing to cancel.  Use 409 here so a client retrying cancel
        # after the run already finished gets a distinguishable code
        # from "session not found".
        raise HTTPException(
            status_code=409,
            detail="no active run for this session",
        )

    run.cancel_requested = True
    # Try a graceful aclose first.
    gen = getattr(run, "_gen", None)
    if gen is not None:
        try:
            await gen.aclose()
        except Exception:
            pass
    # Belt-and-braces: if the task is somehow stuck outside the gen
    # iteration, hit it with task.cancel().  The worker catches
    # CancelledError and still finalises cleanly.
    task = _active_tasks.get(key)
    if task is not None and not task.done():
        task.cancel()

    return {"cancelled": True}


@app.get("/api/sessions/{session_id}/stream")
async def stream_session(
    session_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> StreamingResponse:
    """Subscribe to the in-flight run on this session, if any.

    Used by the frontend to reconnect after disconnect:
      * If a run is active, attach a fresh subscriber that replays the
        run's event log and then drains live events to terminal.
      * If no run is active (the run already finished), respond with
        an empty-but-valid SSE body and 204-equivalent ``done`` shape:
        we yield a single ``done`` event whose ``full_text`` is empty.
        The client's load path (GET /api/sessions/{id}) is the
        authoritative source for the final assistant message; the
        client uses ``seq`` to dedupe.

    The empty-`done` shape is intentional: the client always closes the
    SSE on ``done``, so the post-run-finished code path uses the same
    drain loop as the active-run path. No special "no run" 204 case.
    """
    email = _user_email(claims)
    if storage.get_session(email, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")

    key = (email, session_id)
    run = _active_runs.get(key)

    async def _emit_done_only() -> AsyncIterator[bytes]:
        yield _sse_event("done", {"full_text": ""})

    body = _sse_subscribe(run) if run is not None else _emit_done_only()
    return StreamingResponse(
        _with_keepalive(body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/sessions/{session_id}/attachments")
async def post_attachments(
    session_id: str,
    files: list[UploadFile] = File(...),
    claims: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    email = _user_email(claims)
    # Verify ownership before doing any disk work — and use the same
    # 404-on-cross-email convention as the rest of the API.
    session = storage.get_session(email, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return await attachments.save_uploads(session_id, files)
    except attachments.AttachmentError as err:
        raise HTTPException(status_code=err.status_code, detail=str(err))


@app.get("/api/sessions/{session_id}/attachment_previews/{seq}/{filename}")
async def get_attachment_preview(
    session_id: str,
    seq: int,
    filename: str,
    request: Request,
    claims: dict[str, Any] = Depends(require_user),
) -> Response:
    """Serve a user-uploaded attachment from the per-seq preview dir.

    The preview tree is populated by ``post_message`` immediately after the
    user message is appended, and survives the per-turn attachments-dir
    purge that wipes the model-facing copies. Auth + ownership gating
    mirrors the generated-image route.
    """
    email = _user_email(claims)
    if storage.get_session(email, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    if seq < 0:
        raise HTTPException(status_code=404, detail="not found")
    if not _GENERATED_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="not found")
    try:
        base = attachments.session_preview_dir(session_id, seq)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    path = base / filename
    try:
        canonical = path.resolve(strict=True)
        canonical.relative_to(base.resolve())
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    ext = path.suffix.lower()
    media = _GENERATED_MEDIA_TYPES.get(ext, "application/octet-stream")

    stat = canonical.stat()
    etag = f'W/"{stat.st_mtime_ns}-{stat.st_size}"'
    last_modified = formatdate(stat.st_mtime, usegmt=True)
    cache_headers = {
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    inm = request.headers.get("if-none-match")
    if inm and etag in {tag.strip() for tag in inm.split(",")}:
        return Response(status_code=304, headers=cache_headers)
    return FileResponse(path, media_type=media, headers=cache_headers)


@app.get("/api/sessions/{session_id}/generated/{filename}")
async def get_generated_image(
    session_id: str,
    filename: str,
    request: Request,
    claims: dict[str, Any] = Depends(require_user),
) -> Response:
    """Serve an image previously generated by the gemini image worker.

    Auth-gated by the same JWT path as the rest of the session API. Both
    parameters are validated before they reach the filesystem: session_id
    must be UUID-shaped, filename must match the strict ``<hex>.<ext>``
    pattern the worker writes (no traversal, no hidden files).
    """
    email = _user_email(claims)
    if storage.get_session(email, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not _GENERATED_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="not found")
    try:
        gen_dir = _session_generated_dir(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    path = gen_dir / filename
    # Belt-and-braces against path traversal: refuse if the resolved path
    # escapes the per-session dir. The regex already blocks `/` and leading
    # dots, but resolving symlinks here catches a malicious symlink that
    # could have been placed by a turn output.
    try:
        canonical = path.resolve(strict=True)
        canonical.relative_to(gen_dir.resolve())
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    ext = path.suffix.lower()
    media = _GENERATED_MEDIA_TYPES.get(ext, "application/octet-stream")

    # The same filename gets overwritten across turns when the model
    # regenerates an artifact (e.g. foo.pdf rewritten on turn 5). The URL
    # doesn't change, so without revalidation the browser keeps serving
    # the turn-1 bytes from its HTTP cache. Pin the cache key to the
    # file's mtime+size and force the browser to revalidate on every
    # fetch — 304s carry no body so the cost is one cheap round-trip.
    stat = canonical.stat()
    etag = f'W/"{stat.st_mtime_ns}-{stat.st_size}"'
    last_modified = formatdate(stat.st_mtime, usegmt=True)
    cache_headers = {
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "private, no-cache, must-revalidate",
    }

    inm = request.headers.get("if-none-match")
    if inm and etag in {tag.strip() for tag in inm.split(",")}:
        return Response(status_code=304, headers=cache_headers)
    ims = request.headers.get("if-modified-since")
    if ims:
        try:
            ims_dt = parsedate_to_datetime(ims)
        except (TypeError, ValueError):
            ims_dt = None
        if ims_dt is not None and int(stat.st_mtime) <= int(ims_dt.timestamp()):
            return Response(status_code=304, headers=cache_headers)

    return FileResponse(path, media_type=media, headers=cache_headers)


# ---------------------------------------------------------------------------
# Artifact Run (Python execution in the user's container).
# ---------------------------------------------------------------------------

class RunArtifactBody(BaseModel):
    filename: str
    source: str = "generated"  # "generated" | "attachment"


def _resolve_artifact_bytes(session_id: str, source: str, filename: str) -> bytes:
    """Read the source bytes for a runnable artifact (.py / .c / .cpp …;
    see artifact_runner.RUNNABLE_EXTENSIONS). Resolves from either
    /data/generated/<sid>/ (LLM-written, default) or /data/attachments/<sid>/
    (user-uploaded). The same hard validations apply to both — no traversal,
    no hidden files."""
    if not _GENERATED_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    if not filename.lower().endswith(artifact_runner.RUNNABLE_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail="only .py / .c / .cpp artifacts can be run",
        )
    if source == "attachment":
        try:
            base = attachments.session_attachments_dir(session_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid session id")
    else:
        try:
            base = _session_generated_dir(session_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid session id")
    path = base / filename
    try:
        canonical = path.resolve(strict=True)
        canonical.relative_to(base.resolve())
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        with open(canonical, "rb") as f:
            return f.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"read failed: {exc}")


@app.post("/api/sessions/{session_id}/runs")
async def create_artifact_run(
    session_id: str,
    body: RunArtifactBody,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Start a Python run on the user's container; returns the run id.

    The artifact bytes are loaded server-side (the request body is just
    a reference — no script content over the wire) and shipped into the
    container via the runner. Returns immediately; subscribe to events
    at GET /api/sessions/{sid}/runs/{rid}/stream.
    """
    email = _user_email(claims)
    if storage.get_session(email, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    script_bytes = _resolve_artifact_bytes(session_id, body.source, body.filename)
    try:
        container = user_container.ensure_user_container(email)
    except Exception as exc:
        logger.error("ensure_user_container(%s) failed: %s", email, exc)
        raise HTTPException(status_code=500, detail="container provisioning failed")
    try:
        run = await artifact_runner.start_run(
            email=email,
            session_id=session_id,
            container=container,
            filename=body.filename,
            script_bytes=script_bytes,
        )
    except artifact_runner.RunError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {
        "run_id": run.id,
        "status": run.status,
        "started_at": run.started_at,
        "stream_url": f"/api/sessions/{session_id}/runs/{run.id}/stream",
    }


def _authorize_run(run: "artifact_runner.Run", session_id: str, claims: dict[str, Any]) -> None:
    """Refuse to surface a run that doesn't belong to the requesting user
    or session. The run id is unguessable but defense in depth — every
    endpoint that touches a run goes through this gate."""
    if run.email != _user_email(claims):
        raise HTTPException(status_code=404, detail="run not found")
    if run.session_id != session_id:
        raise HTTPException(status_code=404, detail="run not found")


@app.get("/api/sessions/{session_id}/runs/{run_id}/stream")
async def stream_artifact_run(
    session_id: str,
    run_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> StreamingResponse:
    """SSE stream of stdout/stderr/media/done events for a run.

    Re-attachable: the subscriber gets the full event history first,
    then live events until the run terminates. Mirrors the chat
    /messages stream's keepalive shape.
    """
    run = artifact_runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    _authorize_run(run, session_id, claims)

    async def body_gen() -> AsyncIterator[bytes]:
        async for evt in run.subscribe():
            kind = evt.get("kind", "msg")
            # `kind` and `ts` are duplicated as event-type + payload field
            # for convenience on the client.
            yield _sse_event(kind, evt)

    return StreamingResponse(
        body_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/sessions/{session_id}/runs/{run_id}/cancel")
async def cancel_artifact_run(
    session_id: str,
    run_id: str,
    claims: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    run = artifact_runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    _authorize_run(run, session_id, claims)
    await run.cancel()
    return {"run_id": run_id, "status": run.status}


@app.get("/api/sessions/{session_id}/runs/{run_id}/media/{filename}")
async def get_artifact_run_media(
    session_id: str,
    run_id: str,
    filename: str,
    claims: dict[str, Any] = Depends(require_user),
) -> Response:
    """Serve a media file produced by an artifact run.

    The bytes live inside the user's container at /tmp/wizerith-runs/...;
    we cat them out via docker exec each time the URL is fetched. Cheap
    (≤25 MB cap per file) and avoids materializing media on the chat host's
    disk. Cached for an hour client-side — media is stable for the run's
    lifetime, and the URL is per-run-id so it can't collide.
    """
    run = artifact_runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    _authorize_run(run, session_id, claims)
    try:
        data, mime = await artifact_runner.read_media_bytes(run, filename)
    except artifact_runner.RunError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# Frontend SPA serving (Phase 3).
#
# Layout: Vite emits the built SPA into ``services/chat/static/``. We resolve
# that directory relative to this file so the resolution works whether the
# tests run from the service dir, the workspace root, or inside the container
# at /app.
#
# Two modes:
#   * Built (``static/index.html`` present): assets are served from disk;
#     anything not matching a real file and not under /api or /healthz falls
#     back to ``index.html`` so client-side routing works.
#   * Not built (``static/`` absent or empty): GET / returns a placeholder
#     JSON body. Lets the test suite run without a node toolchain in CI; the
#     SPA-fallback test for non-built mode asserts on this placeholder.
#
# Implementation note: rather than mounting ``StaticFiles`` at ``/`` (which
# requires careful ordering vs other routes), we use a single catch-all GET
# route. It is appended to the file so it lives strictly after every
# ``@app.get("/api/...")`` registration, which means FastAPI matches the
# specific routes first and only falls through here when nothing else hit.
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

# These paths are owned by the API and must NEVER fall through to the SPA.
# The catch-all guards on these lists explicitly so a SPA fallback can't
# shadow a yet-to-be-registered API route added in a later phase. We split
# into two: ``_API_PREFIXES`` matches via str.startswith, ``_API_EXACT``
# matches via equality (httpx normalises ``/api/sessions/..`` to bare
# ``/api`` on the way out, which won't match ``"api/"`` as a prefix).
_API_PREFIXES = ("api/",)
_API_EXACT = ("api", "healthz")


def _path_is_safe_under_static(rel_path: str) -> Path | None:
    """Return the resolved on-disk path iff ``rel_path`` is a real file under
    ``_STATIC_DIR``. Returns None for traversal attempts, missing files, and
    directories.

    Defence-in-depth: even though Vite never emits ``..`` in asset paths,
    the URL is attacker-controlled, and ``Path.resolve()`` lets us reject
    anything outside the static dir without trusting string manipulation.
    """
    if not rel_path:
        return None
    candidate = (_STATIC_DIR / rel_path).resolve()
    try:
        candidate.relative_to(_STATIC_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_or_static(full_path: str) -> Response:
    """Serve static assets, fall back to index.html for SPA routes.

    Order of resolution:
      1. Paths under reserved API prefixes are rejected here with 404 so a
         missing API endpoint surfaces honestly rather than as the SPA.
      2. If a real file exists at ``static/<path>`` (e.g. ``assets/index-abc.js``
         or ``favicon.ico``), serve it.
      3. If the SPA is built, return ``static/index.html`` so client routing
         can take over.
      4. If the SPA is not built, return a small JSON placeholder so the
         service is still useful (and testable) without a node toolchain.
    """
    # /api/* and /healthz are FastAPI-routed. If we got here, the request
    # didn't match any registered route — so it's a genuine 404, not an SPA
    # route. The two checks below stay in sync with the constants above.
    if full_path in _API_EXACT or any(full_path.startswith(p) for p in _API_PREFIXES):
        raise HTTPException(status_code=404, detail="not found")

    # 2. Real static asset on disk?
    if _STATIC_DIR.is_dir():
        rel = full_path[len("static/"):] if full_path.startswith("static/") else full_path
        target = _path_is_safe_under_static(rel) if rel else None
        if target is not None:
            # Hashed bundles under /assets/ are content-addressed (Vite
            # appends an 8-char hash) so they're safe to cache forever.
            # Anything else is an unhashed top-level asset (favicon, robots,
            # etc.) — cache briefly, but require revalidation.
            if rel.startswith("assets/"):
                headers = {"Cache-Control": "public, max-age=31536000, immutable"}
            else:
                headers = {"Cache-Control": "public, max-age=300, must-revalidate"}
            return FileResponse(target, headers=headers)

    # 3. SPA fallback when built. index.html itself MUST NOT be cached —
    # a stale copy referencing a deleted hashed bundle would brick the
    # frontend on the next deploy. `no-cache` allows the browser to keep
    # a copy but forces revalidation against the origin every time, which
    # is cheap (1.4 KB body) and survives Cloudflare in front.
    if _INDEX_HTML.is_file():
        return FileResponse(
            _INDEX_HTML,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    # 4. Pre-build placeholder. Tests rely on this exact body shape.
    return JSONResponse({"ok": True, "frontend": "not built"})
