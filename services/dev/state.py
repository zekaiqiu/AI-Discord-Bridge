"""Server-side IDE state persistence.

Stored as JSON inside the user's own per-user container under
/workspace/.wizerith/ide-state.json so:
  - it survives container recreate (lives in the user's volume)
  - it lives next to the user's files (no DB needed)
  - it follows the user across browsers/devices

Shape (kept deliberately small / forward-compatible):
{
  "version": 1,
  "open_tabs": [
    {"path": "foo/bar.py", "active": true, "cursor_line": 12, "cursor_col": 4}
  ],
  "selected_interpreter": "/usr/local/bin/python3",   // M2 will use it
  "theme": "dark"
}
"""

from __future__ import annotations

import json
from typing import Any

from docker_exec import DockerExecError, run_exec
from file_ops import shquote


STATE_DIR = "/workspace/.wizerith"
STATE_PATH = "/workspace/.wizerith/ide-state.json"


DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "open_tabs": [],
    "selected_interpreter": None,
    "theme": "dark",
}


def load(container: str) -> dict[str, Any]:
    """Read the user's IDE state; return DEFAULT_STATE if missing."""
    try:
        out = run_exec(
            container,
            ["sh", "-c", f"cat {shquote(STATE_PATH)} 2>/dev/null || true"],
        )
    except DockerExecError:
        return dict(DEFAULT_STATE)
    raw = out.decode("utf-8", errors="replace").strip()
    if not raw:
        return dict(DEFAULT_STATE)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # State file got corrupted somehow — don't lock the user out, just
        # behave as if it didn't exist. The next save will overwrite.
        return dict(DEFAULT_STATE)
    if not isinstance(parsed, dict):
        return dict(DEFAULT_STATE)
    # Light merge so future-added fields don't surface as KeyError to the
    # frontend if the user's state file pre-dates them.
    merged = dict(DEFAULT_STATE)
    merged.update(parsed)
    return merged


def save(container: str, state: dict[str, Any]) -> None:
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    script = (
        f"set -e\n"
        f"mkdir -p {shquote(STATE_DIR)}\n"
        f"cat > {shquote(STATE_PATH + '.tmp')}\n"
        f"mv {shquote(STATE_PATH + '.tmp')} {shquote(STATE_PATH)}\n"
    )
    run_exec(container, ["sh", "-c", script], stdin_bytes=payload, timeout=10.0)
