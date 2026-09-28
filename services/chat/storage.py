"""Filesystem-backed session storage.

Sessions are stored as one JSON file per session, namespaced by an email-derived
slug directory:

    <CHAT_SESSIONS_DIR>/<email-slug>/<session-id>.json

Phase 2 (this revision) adds per-message ``seq`` ordering and per-session
async serialisation so concurrent writers cannot clobber each other, and
exposes finer-grained mutators (``append_user_message``,
``append_assistant_placeholder``, ``update_assistant_message``) that the SSE
handler uses to persist a turn incrementally rather than only at ``done``.
The original ``append_messages`` is preserved for batch use; it now also
assigns ``seq`` values and routes through the same lock.

This module deliberately has no FastAPI imports — it's pure data plumbing so
unit tests and a future CLI can call it directly. The async lock helper
(``get_session_lock``) lives here only because the locks must be keyed on
the same identity (email, session_id) the storage functions key on; the
app layer holds the lock around its own read-modify-write composites
(e.g. "append assistant placeholder, stream, then update by seq").

Security note: ``session_id`` arrives from an HTTP path parameter, so every
public API that consumes it routes through ``_is_valid_session_id`` *before*
that value can reach ``os.path.join``. We only ever produce ids via
``uuid.uuid4()``, so a strict UUID-shape regex is sufficient — anything else
is by definition attacker-supplied (e.g. ``..`` or a URL-decoded ``../etc``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

# Permissions: directories 0750 (owner rwx, group rx), files 0640
# (owner rw, group r). Matches the brief; group readability lets a sidecar
# (e.g. backup container in same gid) inspect without giving world access.
_DIR_MODE = 0o750
_FILE_MODE = 0o640

_SLUG_UNSAFE_CHARS = re.compile(r"[^a-z0-9._-]")

# UUID v4 string shape: 8-4-4-4-12 hex chars. We tolerate upper/lower case
# because some clients normalise differently. Anything else (including the
# literal ``..``, a URL-decoded ``../etc``, or a Windows path) is rejected.
_VALID_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# Status values for an assistant message.  ``streaming`` is the in-flight
# state set by ``append_assistant_placeholder``; ``complete`` means the
# turn finished normally (matched by Phase 4's reconnect-replay path);
# ``error`` means the turn aborted (subprocess crash, malformed stream,
# explicit cancel, or client disconnect with no further updates landing).
ASSISTANT_STATUS_STREAMING = "streaming"
ASSISTANT_STATUS_COMPLETE = "complete"
ASSISTANT_STATUS_ERROR = "error"
# Phase 4 (Bug 3 fix): explicit user cancellation distinguishes from
# subprocess crash / malformed stream (those remain ASSISTANT_STATUS_ERROR).
# A cancelled turn carries whatever partial content the agent produced
# before the user pressed stop.
ASSISTANT_STATUS_CANCELLED = "cancelled"
_VALID_ASSISTANT_STATUSES = frozenset({
    ASSISTANT_STATUS_STREAMING,
    ASSISTANT_STATUS_COMPLETE,
    ASSISTANT_STATUS_ERROR,
    ASSISTANT_STATUS_CANCELLED,
})


def _is_valid_session_id(session_id: Any) -> bool:
    """True iff ``session_id`` is a UUID-shaped string safe to use in a path."""
    return isinstance(session_id, str) and bool(_VALID_SESSION_ID.match(session_id))


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond == 0:
        base = dt.isoformat()
        base = base.replace("+00:00", ".000000+00:00")
    else:
        base = dt.isoformat()
    return base.replace("+00:00", "Z")


def now_iso() -> str:
    """Public ISO-8601 timestamp helper.

    Phase 2 promotes this from the ``_now_iso`` private form because the
    SSE handler in ``app.py`` needs the same microsecond-precision shape
    when stamping message ``ts`` fields, and reaching across modules into
    a private name was flagged as a code smell in the Phase 1 audit
    (extra_bugs.md, minor). The legacy ``_now_iso`` alias below preserves
    the previous import path for any third-party caller that bound it.
    """
    return _format_iso(_now_dt())


# Backwards-compatible private alias. ``app.py`` historically did
# ``from storage import _now_iso``; the import keeps working unchanged.
_now_iso = now_iso


def email_slug(email: str) -> str:
    if not isinstance(email, str) or not email:
        raise ValueError("email must be a non-empty string")
    return _SLUG_UNSAFE_CHARS.sub("_", email.lower())


def _sessions_root() -> str:
    return os.environ.get("CHAT_SESSIONS_DIR", "/data/sessions")


def _user_dir(email: str) -> str:
    return os.path.join(_sessions_root(), email_slug(email))


def _ensure_dir(path: str) -> None:
    os.makedirs(path, mode=_DIR_MODE, exist_ok=True)


def _session_path(email: str, session_id: str) -> str:
    assert _is_valid_session_id(session_id), "session_id must be UUID-shaped"
    return os.path.join(_user_dir(email), f"{session_id}.json")


# ---------------------------------------------------------------------------
# Per-user settings.
#
# Stored as ``<user_dir>/_settings.json``. The leading underscore keeps the
# file out of session listings (session ids are UUIDs, never start with _).
# Schema is intentionally additive: missing keys take defaults from
# ``DEFAULT_SETTINGS``, unknown keys are dropped on write so a field that
# gets removed in a future schema doesn't leak through forever.
# ---------------------------------------------------------------------------

SETTINGS_FILENAME = "_settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_model": "glm",           # glm | kimi | qwen | deepseek | minimax | gemma4-local
    "send_on_enter": True,            # if False, Enter inserts newline; Cmd/Ctrl+Enter sends
    "persona": "",                    # personal system-prompt prefix, prepended to every turn
    "notify_on_complete": False,      # browser desktop notification when a turn ends
    "theme": "dark",                  # dark | light | system
    "show_token_costs": False,        # render per-message tokens/cost when available
    "auto_archive_days": 0,           # 0 = off; otherwise hide unarchived/unstarred sessions older than N days from listings
    "ui_language": "en",              # language for UI labels (sidebar, settings, composer)
    "output_language": "en",          # language for the model's responses
    "window_layout": "columns",       # multi-window tiling: columns (side by side) | grid (2x2 progression)
    # NOTE: the legacy single "language" field is migrated in _coerce_settings
    # to seed both ui_language and output_language. Don't add it to defaults.
}

# TokenHub aliases (tokenhub_runner._TOKENHUB_MODELS) + haihub model aliases
# (haihub_runner._HAIHUB_MODELS) + local home-GPU alias
# (local_runner._LOCAL_MODELS). Lineup since 2026-09-28: TokenHub glm/kimi lead the picker and the
# Anthropic/claude aliases (incl. "default", which meant "claude picks")
# were removed from chat.wizerith.ai. Saved blobs still carrying an old
# value (opus5 / default / ...) fail this membership test on the next read
# and fall back to DEFAULT_SETTINGS — a free migration to glm.
_VALID_MODELS = {"glm", "kimi", "qwen", "deepseek", "minimax", "gemma4-local"}
_VALID_THEMES = {"dark", "light", "system"}
# Site-wide display language is English + Simplified/Traditional Chinese only.
# Legacy "de"/"tl" values no longer validate, so any saved blob carrying them
# falls back to "en" on the next read (see _coerce_settings) — a free migration.
_VALID_LANGUAGES = {"en", "zh-CN", "zh-TW"}
_VALID_WINDOW_LAYOUTS = {"columns", "grid"}


def _settings_path(email: str) -> str:
    return os.path.join(_user_dir(email), SETTINGS_FILENAME)


def _coerce_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply defaults + per-field validation. Unknown keys dropped."""
    out = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return out
    if isinstance(raw.get("default_model"), str) and raw["default_model"] in _VALID_MODELS:
        out["default_model"] = raw["default_model"]
    if isinstance(raw.get("send_on_enter"), bool):
        out["send_on_enter"] = raw["send_on_enter"]
    if isinstance(raw.get("persona"), str):
        out["persona"] = raw["persona"][:4000]  # hard cap so it can't dominate context
    if isinstance(raw.get("notify_on_complete"), bool):
        out["notify_on_complete"] = raw["notify_on_complete"]
    if isinstance(raw.get("theme"), str) and raw["theme"] in _VALID_THEMES:
        out["theme"] = raw["theme"]
    if isinstance(raw.get("show_token_costs"), bool):
        out["show_token_costs"] = raw["show_token_costs"]
    if isinstance(raw.get("auto_archive_days"), int) and 0 <= raw["auto_archive_days"] <= 3650:
        out["auto_archive_days"] = raw["auto_archive_days"]
    # Legacy migration: pre-split, a single "language" field drove both UI
    # and response language. If a user's saved blob still has it (and not
    # yet the split fields), seed both new fields from it. Explicit values
    # for the new fields below override the legacy seed.
    if isinstance(raw.get("language"), str) and raw["language"] in _VALID_LANGUAGES:
        out["ui_language"] = raw["language"]
        out["output_language"] = raw["language"]
    if isinstance(raw.get("ui_language"), str) and raw["ui_language"] in _VALID_LANGUAGES:
        out["ui_language"] = raw["ui_language"]
    if isinstance(raw.get("output_language"), str) and raw["output_language"] in _VALID_LANGUAGES:
        out["output_language"] = raw["output_language"]
    if isinstance(raw.get("window_layout"), str) and raw["window_layout"] in _VALID_WINDOW_LAYOUTS:
        out["window_layout"] = raw["window_layout"]
    return out


def get_settings(email: str) -> dict[str, Any]:
    """Return the user's settings, falling back to defaults for any
    missing/invalid field. Always succeeds — never raises on a missing
    file or a corrupt blob."""
    path = _settings_path(email)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(DEFAULT_SETTINGS)
    return _coerce_settings(raw)


def update_settings(email: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the user's settings, persist, and return the
    full coerced result. Validation happens via ``_coerce_settings`` —
    unknown keys are silently dropped, invalid values fall back to
    defaults rather than raising."""
    current = get_settings(email)
    merged = dict(current)
    if isinstance(patch, dict):
        for k, v in patch.items():
            merged[k] = v
    coerced = _coerce_settings(merged)
    _atomic_write_json(_settings_path(email), coerced)
    return coerced


# ---------------------------------------------------------------------------
# Cross-session memory.
#
# Per-user freeform markdown that the model reads at the start of every
# turn and can update at the end via the <memory_update>...</memory_update>
# tag protocol (parsed in app.py, stripped from what the user sees).
# Stored as ``<user_dir>/_memory.md`` next to ``_settings.json`` — same
# leading-underscore trick keeps it out of session listings.
# ---------------------------------------------------------------------------

MEMORY_FILENAME = "_memory.md"
MAX_MEMORY_BYTES = 8000  # capped so it can't dominate the context window


def _memory_path(email: str) -> str:
    return os.path.join(_user_dir(email), MEMORY_FILENAME)


def get_memory(email: str) -> str:
    """Return the user's memory blob, "" if absent. Never raises."""
    path = _memory_path(email)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read()
    except (FileNotFoundError, OSError):
        return ""
    return data[:MAX_MEMORY_BYTES]


def set_memory(email: str, text: str) -> str:
    """Persist ``text`` as the user's memory and return the truncated
    canonical form. Caps at MAX_MEMORY_BYTES — anything past that is
    dropped silently. Pass "" to clear."""
    if not isinstance(text, str):
        text = ""
    truncated = text[:MAX_MEMORY_BYTES]
    path = _memory_path(email)
    parent = os.path.dirname(path)
    _ensure_dir(parent)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(truncated)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _FILE_MODE)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return truncated


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    _ensure_dir(parent)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _FILE_MODE)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _read_session_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _strictly_after(prev_iso: str) -> str:
    normalised = prev_iso.replace("Z", "+00:00") if prev_iso.endswith("Z") else prev_iso
    try:
        prev_dt = datetime.fromisoformat(normalised)
    except ValueError:
        return _format_iso(_now_dt() + timedelta(seconds=1))
    if prev_dt.tzinfo is None:
        prev_dt = prev_dt.replace(tzinfo=timezone.utc)
    bumped = prev_dt + timedelta(microseconds=1)
    now = datetime.now(timezone.utc)
    return _format_iso(now if now > prev_dt else bumped)


# ---------------------------------------------------------------------------
# Per-message ``seq`` ordering helpers.
#
# Each message in ``session["messages"]`` carries a monotonic ``seq``
# integer assigned at write time; ``seq`` is dense, starts at 0, and is
# strictly increasing within a session (no holes, no duplicates). This
# is the *primary* ordering — array order in the JSON file is identical
# to seq order today, but downstream consumers (Phase 4's reconnect-replay
# in particular) MUST sort by seq, not by array index, because future
# tools may re-order on read.
#
# We deliberately don't rely on ``ts`` for ordering: two messages written
# in the same wall-clock microsecond would tie, and the system clock can
# move backwards. ``seq`` is monotonic by construction.
# ---------------------------------------------------------------------------


def _backfill_seq_inplace(messages: list[dict[str, Any]]) -> None:
    """Assign ``seq`` to any legacy messages that were written without one.

    Pre-Phase-2 sessions had no ``seq`` field. To keep them readable, we
    backfill on first touch using the existing array order, which is the
    historical write order for those sessions because the pre-Phase-2
    code only ever appended at the end. Mutates ``messages`` in place;
    idempotent.
    """
    if not isinstance(messages, list):
        return
    next_seq = 0
    # First pass: find the largest existing seq so we don't collide with it.
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("seq"), int):
            if m["seq"] >= next_seq:
                next_seq = m["seq"] + 1
    # Second pass: assign to any without a seq, preserving array order.
    for m in messages:
        if not isinstance(m, dict):
            continue
        if not isinstance(m.get("seq"), int):
            m["seq"] = next_seq
            next_seq += 1


def _next_seq(messages: list[dict[str, Any]]) -> int:
    """Smallest seq value strictly greater than every present seq.

    Returns 0 for an empty list. Tolerates legacy messages without seq by
    treating their position as their implicit seq (after a prior
    ``_backfill_seq_inplace`` call this collapses to ``max(seq) + 1``).
    """
    if not isinstance(messages, list) or not messages:
        return 0
    largest = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and isinstance(m.get("seq"), int):
            if m["seq"] > largest:
                largest = m["seq"]
        else:
            # Legacy / malformed: treat its index as a tentative seq so
            # we don't accidentally re-use a number for a freshly-appended
            # message. Backfill is supposed to have run first, but we don't
            # require it — defence in depth.
            if i > largest:
                largest = i
    return largest + 1


def _bump_updated_at(data: dict[str, Any]) -> None:
    """Set ``data["updated_at"]`` strictly after both prev updated_at and created_at."""
    candidate = now_iso()
    prev = max(
        data.get("updated_at", "") or "",
        data.get("created_at", "") or "",
    )
    data["updated_at"] = candidate if candidate > prev else _strictly_after(prev)


# ---------------------------------------------------------------------------
# Per-session async lock registry.
#
# The Phase 1 audit flagged the read-modify-write race in ``append_messages``
# as a major extra. The cheapest correct fix is to serialise writers per
# (email, session_id) at the app layer. We expose locks here so:
#
#   * the app layer can hold a single lock across a "read, mutate, write"
#     composite (e.g. append placeholder THEN update by seq), and
#   * the storage layer's own ``append_*`` / ``update_*`` mutators acquire
#     the same lock when called directly (so a caller that doesn't already
#     hold it is still safe).
#
# Locks are created lazily and cached forever for the process. The cache
# is unbounded in principle, but in practice it grows only as fast as the
# user opens distinct sessions in one process; a server restart clears it.
# Phase 4 may want to swap this for a true semaphore-with-eviction; today
# it isn't worth the complexity.
# ---------------------------------------------------------------------------

_session_locks: dict[tuple[str, str], asyncio.Lock] = {}


def get_session_lock(email: str, session_id: str) -> asyncio.Lock:
    """Return the (lazily created) lock for one (email, session_id).

    The returned lock is process-local; multi-process deploys would need
    file-level ``fcntl.flock`` for cross-process serialisation. Today the
    chat service runs as a single uvicorn process so the in-process lock
    is sufficient — Phase 5 may revisit this when scaling.

    Concurrency note: this function is sync on purpose. ``setdefault``
    is atomic at the bytecode level on CPython, so two coroutines that
    both miss the cache on a cold key still agree on a single shared
    lock object — no extra coordinator needed.
    """
    key = (email, session_id)
    lock = _session_locks.get(key)
    if lock is None:
        lock = _session_locks.setdefault(key, asyncio.Lock())
    return lock


# ---------------------------------------------------------------------------
# Public API — session CRUD (unchanged from Phase 1 except where noted).
# ---------------------------------------------------------------------------


def create_session(
    email: str,
    title: str | None = None,
    *,
    account: str | None = None,
    role: str | None = None,
    container: str | None = None,
    workspace: str | None = None,
    starred: bool = False,
    archived: bool = False,
    folder: str | None = None,
    forked_from: dict[str, Any] | None = None,
    seed_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a new session.

    ``account`` (optional) names which Claude account this session is
    locked to for its lifetime. claude's `--resume <uuid>` requires the
    session UUID to exist on the same account that originally created it,
    so the session cannot transparently switch accounts mid-conversation.
    The chat layer picks an account once at create time (via
    ``account_pool.pick`` when enabled) and persists the choice here so
    every subsequent turn uses the same account.

    ``role`` (optional) is the email's role at creation time — "admin" or
    "user". Frozen here because role determines dispatch (admin sessions
    run claude in chat-host-shell with host namespaces; non-admin sessions
    run in their per-user container). Locking at create time means a
    later /api/me-role flip — e.g. removing an email from ADMIN_EMAILS —
    does NOT retroactively re-route the existing session; the session
    keeps the dispatcher it was created with so --resume continuity is
    preserved. Legacy sessions get None and the runner treats that as
    "user" (safe default).

    ``container`` (optional) is the per-user docker container name the
    chat layer provisioned at session-create time when ``role == "user"``.
    Persisted here so every subsequent turn (and a chat-service restart)
    routes the claude invocation into the same container without having
    to re-resolve it from email each call. None for admin sessions and
    legacy sessions (pre-multi-user-containers); the runner treats a
    missing container under role="user" as a soft fall-back to the
    legacy in-chat-container dispatch rather than crashing the turn.

    Legacy sessions created without ``account`` get None on disk; the
    runner treats None as "main" (the bridge user's primary credentials),
    matching pre-pool behavior.
    """
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string or None")
    if account is not None and not isinstance(account, str):
        raise ValueError("account must be a string or None")
    if role is not None and role not in ("admin", "user"):
        raise ValueError("role must be 'admin', 'user', or None")
    if container is not None and not isinstance(container, str):
        raise ValueError("container must be a string or None")
    if workspace is not None and workspace not in ("personal", "shared", "admin"):
        raise ValueError("workspace must be 'personal', 'shared', 'admin', or None")
    if folder is not None and not isinstance(folder, str):
        raise ValueError("folder must be a string or None")
    now = now_iso()
    seeded: list[dict[str, Any]] = []
    if seed_messages:
        for i, m in enumerate(seed_messages):
            if not isinstance(m, dict):
                continue
            copy = dict(m)
            copy["seq"] = i
            seeded.append(copy)
    session = {
        "id": str(uuid.uuid4()),
        "email": email,
        "title": title,
        "claude_session_id": str(uuid.uuid4()),
        "account": account,
        "role": role,
        "container": container,
        # workspace mode for this session, locked at creation. "personal"
        # routes turns into the per-email container; "shared" routes into
        # the tenant's wizerith-shared container. None = legacy session
        # (treated as "personal" downstream). claude_runner already
        # dispatches via session["container"], so no runner changes are
        # needed — workspace is the human-readable label.
        "workspace": workspace,
        "starred": bool(starred),
        "archived": bool(archived),
        "folder": folder,
        "forked_from": forked_from,
        "messages": seeded,
        "created_at": now,
        "updated_at": now,
    }
    _atomic_write_json(_session_path(email, session["id"]), session)
    return session


def _list_files(user_dir: str) -> list[str]:
    try:
        return [
            os.path.join(user_dir, name)
            for name in os.listdir(user_dir)
            if name.endswith(".json") and not name.startswith(".tmp-")
        ]
    except FileNotFoundError:
        return []


def list_sessions(email: str, workspace: str | None = None) -> list[dict[str, Any]]:
    """Return the user's sessions, optionally filtered by workspace.

    `workspace` accepts:
      - "personal" — only sessions explicitly tagged personal OR with no
        workspace field (legacy sessions pre-date the workspace toggle and
        are personal by definition).
      - "shared"   — only sessions tagged "shared".
      - None       — every session for the user (back-compat for callers
        that don't care about the toggle).
    """
    user_dir = _user_dir(email)
    summaries: list[dict[str, Any]] = []
    for path in _list_files(user_dir):
        data = _read_session_file(path)
        if not data:
            continue
        if data.get("email") != email:
            continue
        session_ws = data.get("workspace") or "personal"
        if workspace is not None and session_ws != workspace:
            continue
        summaries.append({
            "id": data.get("id"),
            "title": data.get("title"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "starred": bool(data.get("starred", False)),
            "archived": bool(data.get("archived", False)),
            "folder": data.get("folder"),
            "workspace": data.get("workspace"),
        })
    summaries.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return summaries


def get_session(email: str, session_id: str) -> dict[str, Any] | None:
    """Return the full session dict, or None if absent or owned by another email.

    Phase 2: backfills ``seq`` on any legacy messages that pre-date the
    seq scheme, in-memory only — does NOT rewrite the file just for that.
    A subsequent mutation will persist the assigned seqs alongside the
    new content.
    """
    if not _is_valid_session_id(session_id):
        return None
    data = _read_session_file(_session_path(email, session_id))
    if data is None:
        return None
    if data.get("email") != email:
        return None
    msgs = data.get("messages")
    if isinstance(msgs, list):
        _backfill_seq_inplace(msgs)
    return data


def rename_session(email: str, session_id: str, title: str) -> dict[str, Any] | None:
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    return update_session_metadata(email, session_id, title=title)


_PATCHABLE_METADATA_FIELDS = frozenset({"title", "starred", "archived", "folder"})


def update_session_metadata(
    email: str,
    session_id: str,
    **fields: Any,
) -> dict[str, Any] | None:
    """Patch user-controlled session metadata: title, starred, archived, folder.

    Each field is optional; only fields explicitly passed are written. None
    is a legitimate value for ``title`` and ``folder`` (clears the field).
    """
    if not _is_valid_session_id(session_id):
        return None
    bad = set(fields) - _PATCHABLE_METADATA_FIELDS
    if bad:
        raise ValueError(f"unsupported metadata fields: {sorted(bad)}")
    if "title" in fields and fields["title"] is not None and not isinstance(fields["title"], str):
        raise ValueError("title must be a string or None")
    if "folder" in fields and fields["folder"] is not None and not isinstance(fields["folder"], str):
        raise ValueError("folder must be a string or None")
    if "starred" in fields and not isinstance(fields["starred"], bool):
        raise ValueError("starred must be a bool")
    if "archived" in fields and not isinstance(fields["archived"], bool):
        raise ValueError("archived must be a bool")
    data = get_session(email, session_id)
    if data is None:
        return None
    changed = False
    for key, value in fields.items():
        if data.get(key) != value:
            data[key] = value
            changed = True
    if changed:
        _bump_updated_at(data)
        _atomic_write_json(_session_path(email, session_id), data)
    return data


def search_sessions(
    email: str,
    query: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Brute-force full-text search across session messages and titles.

    Returns summaries (same shape as ``list_sessions`` plus a ``snippet``
    field with the first matching context window). Case-insensitive
    substring match — small chat history (sessions per user, messages per
    session) makes a real index unnecessary today.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []
    user_dir = _user_dir(email)
    results: list[tuple[str, dict[str, Any]]] = []
    for path in _list_files(user_dir):
        data = _read_session_file(path)
        if not data or data.get("email") != email:
            continue
        snippet: str | None = None
        title = (data.get("title") or "").lower()
        if needle in title:
            snippet = data.get("title") or ""
        if snippet is None:
            for m in data.get("messages") or []:
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                if not isinstance(content, str):
                    continue
                idx = content.lower().find(needle)
                if idx == -1:
                    continue
                start = max(0, idx - 40)
                end = min(len(content), idx + len(needle) + 80)
                window = content[start:end].replace("\n", " ").strip()
                if start > 0:
                    window = "…" + window
                if end < len(content):
                    window = window + "…"
                snippet = window
                break
        if snippet is None:
            continue
        summary = {
            "id": data.get("id"),
            "title": data.get("title"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "starred": bool(data.get("starred", False)),
            "archived": bool(data.get("archived", False)),
            "folder": data.get("folder"),
            "snippet": snippet,
        }
        results.append((data.get("updated_at") or "", summary))
    results.sort(key=lambda t: t[0], reverse=True)
    return [s for _, s in results[:limit]]


def fork_session_seed(
    email: str,
    src_session_id: str,
    from_seq: int,
    *,
    account: str | None = None,
    role: str | None = None,
    container: str | None = None,
) -> dict[str, Any] | None:
    """Create a new session seeded with ``messages[seq < from_seq]`` from src.

    The new session is independent (own ``id`` and ``claude_session_id``)
    and starts with ``claude_initialized=False``. The caller is expected
    to immediately post a new user message — the worker detects "new
    claude session, but message history exists" and assembles a context
    preamble so claude continues coherently.

    Returns the fully-persisted new session dict, or None if the source
    session is missing / not owned.
    """
    if not isinstance(from_seq, int) or from_seq < 0:
        raise ValueError("from_seq must be a non-negative int")
    src = get_session(email, src_session_id)
    if src is None:
        return None
    src_messages = src.get("messages") or []
    keep: list[dict[str, Any]] = []
    for m in src_messages:
        if not isinstance(m, dict):
            continue
        seq = m.get("seq")
        if not isinstance(seq, int) or seq >= from_seq:
            continue
        keep.append({k: v for k, v in m.items() if k != "seq"})
    title = src.get("title")
    new = create_session(
        email,
        title=title,
        account=account or src.get("account"),
        role=role or src.get("role"),
        container=container or src.get("container"),
        folder=src.get("folder"),
        forked_from={"session_id": src_session_id, "from_seq": from_seq},
        seed_messages=keep,
    )
    return new


def export_session_markdown(email: str, session_id: str) -> str | None:
    """Render a session as a single Markdown document. None if not found.

    Output shape: H1 with the title (or "Untitled session"), a metadata
    line, then alternating "## You" / "## Assistant" sections with the
    raw message content. Tool-call markers and untrusted-content fences
    are passed through verbatim — the file is intended as a faithful
    transcript, not a polished essay.
    """
    data = get_session(email, session_id)
    if data is None:
        return None
    title = data.get("title") or "Untitled session"
    parts: list[str] = []
    parts.append(f"# {title}")
    parts.append("")
    created = data.get("created_at") or ""
    updated = data.get("updated_at") or ""
    parts.append(f"_Session `{data.get('id')}` — created {created}, updated {updated}_")
    parts.append("")
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            parts.append("## You")
        elif role == "assistant":
            parts.append("## Assistant")
        else:
            parts.append(f"## {role}")
        parts.append("")
        parts.append(content)
        parts.append("")
    return "\n".join(parts)


_BACKFILLABLE_META_FIELDS = frozenset(
    {"account", "role", "container", "workspace"}
)


def backfill_session_meta(
    email: str,
    session_id: str,
    **fields: Any,
) -> dict[str, Any] | None:
    """Atomically set top-level session-meta fields (account/role/container).

    Used by app.py's resume path to auto-heal legacy sessions that pre-date
    the per-user-container model — without this, a non-admin session whose
    ``container`` was never assigned would fall through to the runner's
    legacy-local dispatch, which leaks /home/felix into the model's tool
    calls. See app.py post_message + claude_runner.run_turn fail-closed.

    Whitelisted to a fixed field set so a buggy caller can't write arbitrary
    keys into a session JSON. Pass-through is no-op-on-equal: if the field
    already has the requested value we don't bump updated_at.
    """
    if not _is_valid_session_id(session_id):
        return None
    bad = set(fields) - _BACKFILLABLE_META_FIELDS
    if bad:
        raise ValueError(f"unsupported backfill fields: {sorted(bad)}")
    data = get_session(email, session_id)
    if data is None:
        return None
    changed = False
    for key, value in fields.items():
        if data.get(key) != value:
            data[key] = value
            changed = True
    if changed:
        _bump_updated_at(data)
        _atomic_write_json(_session_path(email, session_id), data)
    return data


def delete_session(email: str, session_id: str) -> bool:
    if not _is_valid_session_id(session_id):
        return False
    if get_session(email, session_id) is None:
        return False
    path = _session_path(email, session_id)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


# ---------------------------------------------------------------------------
# Public API — message append / update.
#
# The new functions ``append_user_message``, ``append_assistant_placeholder``,
# and ``update_assistant_message`` are the Phase 2 finer-grained writers that
# the SSE handler uses to persist a turn before/after the model finishes —
# closing the data-loss window described in audit/bug_repro.md (Bug 1).
#
# The legacy ``append_messages`` is preserved for the small number of
# callers that still want a batch append (it now also assigns seqs).
# ---------------------------------------------------------------------------


def _read_or_404(email: str, session_id: str) -> dict[str, Any] | None:
    """Internal helper: read + validate ownership, returning None on any miss.

    All public mutators funnel through this so the "missing or cross-email"
    path is uniform; callers translate None to a not-found response in
    whatever transport they speak. The function name retains the legacy
    ``_404`` suffix because it is a stable internal landmark — renaming
    would churn unrelated diffs.
    """
    if not _is_valid_session_id(session_id):
        return None
    return get_session(email, session_id)


def append_user_message(
    email: str,
    session_id: str,
    content: str,
    *,
    ts: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    """Persist a user message immediately and return ``(session, seq)``.

    Called by ``app.post_message`` BEFORE the SSE response is constructed,
    so the user's prompt is durable even if the client disconnects before
    the first delta lands. Returns ``(None, None)`` on missing /
    cross-email session.

    This function does NOT acquire the session lock — the caller is
    expected to hold it for the read-modify-write composite (the SSE
    handler holds the lock across user-append + placeholder-append +
    update-on-done). For one-shot use, wrap the call yourself:

        async with storage.get_session_lock(email, sid):
            session, seq = storage.append_user_message(email, sid, text)
    """
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    data = _read_or_404(email, session_id)
    if data is None:
        return None, None
    msgs = data.setdefault("messages", [])
    if not isinstance(msgs, list):
        msgs = []
        data["messages"] = msgs
    seq = _next_seq(msgs)
    msg: dict[str, Any] = {
        "role": "user",
        "content": content,
        "ts": ts or now_iso(),
        "seq": seq,
    }
    if attachments:
        msg["attachments"] = attachments
    msgs.append(msg)
    _bump_updated_at(data)
    _atomic_write_json(_session_path(email, session_id), data)
    return data, seq


def append_assistant_placeholder(
    email: str,
    session_id: str,
    *,
    ts: str | None = None,
    via: str | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    """Persist an empty assistant message in ``streaming`` state.

    The SSE handler calls this immediately after ``append_user_message``
    so a refresh mid-stream surfaces the placeholder (with status
    ``streaming``) rather than an apparently-missing reply. The Phase 4
    reconnect path will look for ``status == "streaming"`` to decide
    whether to re-attach to a running agent.

    ``via`` records a non-user trigger for the turn (currently only
    ``"wake"``). A scheduled wake appends this placeholder with NO user
    message ahead of it, so the SPA had no way to tell a timer-driven reply
    apart from a normal one and rendered it as the assistant speaking
    unprompted. Omitted entirely for ordinary turns so the persisted shape
    is unchanged for everything that isn't a wake.
    """
    data = _read_or_404(email, session_id)
    if data is None:
        return None, None
    msgs = data.setdefault("messages", [])
    if not isinstance(msgs, list):
        msgs = []
        data["messages"] = msgs
    seq = _next_seq(msgs)
    placeholder: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "ts": ts or now_iso(),
        "seq": seq,
        "status": ASSISTANT_STATUS_STREAMING,
    }
    if via:
        placeholder["via"] = via
    msgs.append(placeholder)
    _bump_updated_at(data)
    _atomic_write_json(_session_path(email, session_id), data)
    return data, seq


def update_assistant_message(
    email: str,
    session_id: str,
    seq: int,
    *,
    content: str,
    status: str,
    ts: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Locate the assistant message with ``seq`` and update content/status.

    Returns the updated session dict, or None if the session is missing,
    not owned by ``email``, or no assistant message with that seq exists.
    Validates ``status`` against the small allowed set so a typo in the
    SSE handler surfaces immediately rather than silently writing an
    arbitrary string into the file.

    The function is idempotent: calling it twice with the same arguments
    is a no-op aside from the ``updated_at`` bump.
    """
    if not isinstance(seq, int):
        raise ValueError("seq must be an int")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if status not in _VALID_ASSISTANT_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_ASSISTANT_STATUSES)!r}"
        )
    data = _read_or_404(email, session_id)
    if data is None:
        return None
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return None
    target_idx: int | None = None
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            continue
        if m.get("seq") == seq:
            target_idx = i
            break
    if target_idx is None:
        return None
    msgs[target_idx]["content"] = content
    msgs[target_idx]["status"] = status
    msgs[target_idx]["ts"] = ts or now_iso()
    # Per-turn metadata footer (model / tokens / tok_s / completed_at).
    # Persisted so the footer survives a session reload. Only written when
    # provided — error/cancel paths leave any prior value untouched.
    if meta is not None:
        msgs[target_idx]["meta"] = meta
    _bump_updated_at(data)
    _atomic_write_json(_session_path(email, session_id), data)
    return data


def append_messages(
    email: str,
    session_id: str,
    msgs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Atomically append messages to a session's transcript (legacy batch API).

    Phase 2: each appended message is assigned a fresh ``seq`` integer if
    it doesn't already have one — assigned monotonically from
    ``_next_seq(existing)``, in the order given. The lock guard in the
    app layer ensures concurrent batch-appends still get distinct seqs.

    Kept for tests and for any future caller that legitimately wants to
    write a pair of messages in one shot. The SSE handler now uses the
    finer-grained ``append_user_message`` / ``append_assistant_placeholder``
    / ``update_assistant_message`` triple instead.

    Returns the updated session dict, or None if the session is missing
    or not owned by ``email``.
    """
    if not _is_valid_session_id(session_id):
        return None
    if not isinstance(msgs, list):
        raise ValueError("msgs must be a list")
    data = get_session(email, session_id)
    if data is None:
        return None
    existing = data.get("messages")
    if not isinstance(existing, list):
        existing = []
    next_seq = _next_seq(existing)
    for m in msgs:
        if not isinstance(m, dict):
            continue
        # Don't clobber a caller-supplied seq (tests use this to simulate
        # already-numbered legacy messages); just keep walking past.
        if not isinstance(m.get("seq"), int):
            m["seq"] = next_seq
            next_seq += 1
        else:
            # Make sure our running counter stays ahead of any caller-
            # supplied seq so subsequent auto-assignments don't collide.
            if m["seq"] >= next_seq:
                next_seq = m["seq"] + 1
        existing.append(m)
    data["messages"] = existing
    _bump_updated_at(data)
    _atomic_write_json(_session_path(email, session_id), data)
    return data


def set_title(
    email: str,
    session_id: str,
    title: str,
) -> dict[str, Any] | None:
    if not _is_valid_session_id(session_id):
        return None
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title:
        return None
    data = get_session(email, session_id)
    if data is None:
        return None
    if data.get("title"):
        return data
    data["title"] = title
    _bump_updated_at(data)
    _atomic_write_json(_session_path(email, session_id), data)
    return data


def sweep_stale_streaming_messages() -> int:
    """Flip every assistant message stuck at status='streaming' → 'error'.

    Called once on app startup. The worker normally persists a terminal
    status (complete / error / cancelled) inside try/except — so the
    only way to leak a permanent 'streaming' is for the worker process
    to die WITHOUT running its Python exception handlers: SIGKILL, OOM,
    or container restart mid-turn. Those messages are gone forever from
    the worker's POV; we surface them as 'error' instead of leaving the
    UI's streaming spinner spinning indefinitely on the next page load.

    Returns the number of messages swept. Best-effort: a per-file read
    or write failure is logged and skipped rather than aborting the
    whole sweep.
    """
    root = _sessions_root()
    if not os.path.isdir(root):
        return 0
    swept = 0
    sweep_note = "Stream interrupted (server restarted before this turn completed)."
    for user_slug in os.listdir(root):
        user_dir = os.path.join(root, user_slug)
        if not os.path.isdir(user_dir):
            continue
        for entry in os.listdir(user_dir):
            if not entry.endswith(".json") or entry.startswith("_"):
                continue
            path = os.path.join(user_dir, entry)
            try:
                data = _read_session_file(path)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            msgs = data.get("messages")
            if not isinstance(msgs, list):
                continue
            mutated = False
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if m.get("role") != "assistant":
                    continue
                if m.get("status") != ASSISTANT_STATUS_STREAMING:
                    continue
                m["status"] = ASSISTANT_STATUS_ERROR
                # Append a note to the partial content so the user sees
                # WHY this bubble ended where it did. Preserve whatever
                # text streamed before the interruption.
                existing = m.get("content")
                if not isinstance(existing, str):
                    existing = ""
                m["content"] = (
                    existing.rstrip() + ("\n\n" if existing.strip() else "")
                    + f"⚠ {sweep_note}"
                )
                mutated = True
                swept += 1
            if mutated:
                _bump_updated_at(data)
                try:
                    _atomic_write_json(path, data)
                except Exception:
                    # If we can't write the corrected status, leave the
                    # session as-is; next boot will try again.
                    continue
    return swept


def mark_claude_initialized(
    email: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Atomically set claude_initialized=True on the session record.

    Called by the message-stream endpoint right before spawning claude
    for the first time, so a turn that errors out mid-stream still marks
    the underlying claude session as "claude has registered this UUID."
    The next attempt then correctly uses --resume rather than --session-id,
    which would otherwise fail with "Session ID … is already in use" —
    claude registers the UUID at subprocess spawn time, not at successful
    turn time, so message-count is the wrong heuristic for first-turn-ness.
    """
    if not _is_valid_session_id(session_id):
        return None
    data = get_session(email, session_id)
    if data is None:
        return None
    if data.get("claude_initialized"):
        return data
    data["claude_initialized"] = True
    _atomic_write_json(_session_path(email, session_id), data)
    return data


__all__ = [
    "ASSISTANT_STATUS_CANCELLED",
    "ASSISTANT_STATUS_COMPLETE",
    "ASSISTANT_STATUS_ERROR",
    "ASSISTANT_STATUS_STREAMING",
    "sweep_stale_streaming_messages",
    "append_assistant_placeholder",
    "append_messages",
    "append_user_message",
    "create_session",
    "delete_session",
    "email_slug",
    "export_session_markdown",
    "fork_session_seed",
    "get_session",
    "get_session_lock",
    "list_sessions",
    "mark_claude_initialized",
    "now_iso",
    "rename_session",
    "search_sessions",
    "set_title",
    "update_assistant_message",
    "update_session_metadata",
]
