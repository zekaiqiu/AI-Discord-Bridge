"""Session JSON read/write helpers.

A session JSON file is the single source of truth for "which dispatch path
should this session use" — written once at create_session time, read on
every turn (run_turn re-reads with no cache so a process restart still
picks up the persisted container name).

Schema:
    {
        "session_id": str,
        "email": str,                  # normalized: .strip().lower()
        "role": "admin" | "user",
        "container": str,              # OPTIONAL — present iff role == "user"
        "created_at": str              # ISO8601 UTC, e.g. "2025-01-02T03:04:05.123456Z"
    }

Phase 3 may add per-user override fields (resource limits, idle policy);
the writer/reader treat the dict as opaque so additive fields are safe.
"""

from __future__ import annotations

import json
import os
import tempfile

SESSIONS_DIR_ENV = "PORTFOLIO_SESSIONS_DIR"
DEFAULT_SESSIONS_DIR = "/workspace/.sessions"


def sessions_dir() -> str:
    """Return the sessions dir, creating it if missing."""
    path = os.environ.get(SESSIONS_DIR_ENV, DEFAULT_SESSIONS_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def session_path(session_id: str) -> str:
    return os.path.join(sessions_dir(), f"{session_id}.json")


def load_session(session_id: str) -> dict:
    """Read the session JSON. Raises FileNotFoundError if missing."""
    with open(session_path(session_id), "r", encoding="utf-8") as f:
        return json.load(f)


def save_session(session_id: str, data: dict) -> None:
    """Atomic write: tmp file + os.replace, so a crashed write never leaves
    a half-written JSON file on disk."""
    target = session_path(session_id)
    target_dir = os.path.dirname(target)
    # mkstemp placed in the same dir so os.replace below is atomic
    # (cross-device renames would not be). The local name `tmp_file_path`
    # avoids visual collision with pytest's `tmp_path` fixture for readers.
    fd, tmp_file_path = tempfile.mkstemp(
        prefix=f".{session_id}.", suffix=".tmp", dir=target_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_file_path, target)
    except Exception:
        # Best-effort cleanup; re-raise so callers see the real error.
        try:
            os.unlink(tmp_file_path)
        except FileNotFoundError:
            pass
        raise


def session_exists(session_id: str) -> bool:
    return os.path.exists(session_path(session_id))
