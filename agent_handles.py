"""Auto-assigned short handles for agents (Part 2 of the !agent spec).

Spawned tasks get `task-N`; spawned projects get `proj-N`. The counter is
monotonically increasing per VM lifetime and persisted across bot
restarts. Both UUID and handle resolve the same agent in every command
that takes `<id>`.

Storage shape (~/.local/state/claude-bridge/handles.json):

    {
      "counters": {"task": 7, "proj": 12},
      "task_handles": {"t-3988e1": "task-7", "t-4b3f17": "task-6", ...},
      "proj_handles": {"proj-833d6d3e": "proj-12", ...}
    }

The state lives in the same dir as tasks.json so a single `rm -rf` of
state nukes everything related, and so the bot doesn't need a second
backup story.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import os
from pathlib import Path
from typing import Literal

log = logging.getLogger("claude-bridge.handles")

# Same state dir as tasks.py — keep all bridge state co-located.
STATE_DIR = Path(
    os.environ.get(
        "CLAUDE_BRIDGE_STATE_DIR",
        str(Path.home() / ".local/state/claude-bridge"),
    )
)
HANDLES_JSON = STATE_DIR / "handles.json"

AgentKind = Literal["task", "proj"]

# Recognise an existing handle (e.g. "task-7", "proj-12"). We use this to
# decide whether a string is a handle or a UUID — handles match this regex,
# UUIDs don't (their hex segment isn't a small integer).
_HANDLE_RE = re.compile(r"^(task|proj)-\d+$")


# A single-process lock guards read-modify-write on the JSON file. We
# don't need cross-process locking — the bridge is the only writer.
_LOCK = threading.Lock()


def _load() -> dict:
    if not HANDLES_JSON.exists():
        return {"counters": {"task": 0, "proj": 0}, "task_handles": {}, "proj_handles": {}}
    try:
        with open(HANDLES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("handles.json unreadable; starting from empty state")
        return {"counters": {"task": 0, "proj": 0}, "task_handles": {}, "proj_handles": {}}
    data.setdefault("counters", {})
    data["counters"].setdefault("task", 0)
    data["counters"].setdefault("proj", 0)
    data.setdefault("task_handles", {})
    data.setdefault("proj_handles", {})
    return data


def _save(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HANDLES_JSON.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(HANDLES_JSON)


def assign(uuid: str, kind: AgentKind) -> str:
    """Assign and return the next handle for a freshly-spawned agent.

    Idempotent: calling twice for the same uuid returns the same handle.
    """
    if kind not in ("task", "proj"):
        raise ValueError(f"unknown kind: {kind!r}")
    with _LOCK:
        data = _load()
        bucket = "task_handles" if kind == "task" else "proj_handles"
        existing = data[bucket].get(uuid)
        if existing:
            return existing
        data["counters"][kind] += 1
        handle = f"{kind}-{data['counters'][kind]}"
        data[bucket][uuid] = handle
        _save(data)
        return handle


def handle_for(uuid: str, kind: AgentKind) -> str | None:
    """Look up an existing handle for a uuid, or None if unassigned."""
    with _LOCK:
        data = _load()
        bucket = "task_handles" if kind == "task" else "proj_handles"
        return data[bucket].get(uuid)


def resolve(handle_or_uuid: str) -> tuple[str, AgentKind] | None:
    """Resolve either a handle ("task-7") or a UUID ("t-3988e1") to
    (uuid, kind). Returns None if the input doesn't match either form.

    UUIDs are returned as-is — the caller already has the canonical id,
    we just classify whether it's a task or a project so the right
    handler can be invoked.
    """
    s = handle_or_uuid.strip()
    if not s:
        return None
    if _HANDLE_RE.match(s):
        kind: AgentKind = "task" if s.startswith("task-") else "proj"
        with _LOCK:
            data = _load()
            bucket = "task_handles" if kind == "task" else "proj_handles"
            for uuid, handle in data[bucket].items():
                if handle == s:
                    return uuid, kind
        return None
    # UUID form: bridge tasks use "t-XXXXXX"; pipeline projects use
    # "proj-XXXXXXXX". Distinguish by prefix.
    if s.startswith("t-"):
        return s, "task"
    if s.startswith("proj-"):
        return s, "proj"
    return None


def is_handle(s: str) -> bool:
    return bool(_HANDLE_RE.match(s.strip()))
