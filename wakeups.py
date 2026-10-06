"""Self-wakeup store for the inline chat session.

The bridge runs each chat turn as a one-shot ``claude --resume`` subprocess and
does not parse the agent's tool calls, so the harness's ``ScheduleWakeup`` tool
cannot be observed directly. This module provides the equivalent capability via
durable sidecar files that a poller in bot.py fires on schedule by re-invoking
the *same* session with a stored prompt.

A wakeup is a JSON file in WAKEUP_DIR:
    {"id": "<uuid>", "fire_at": <epoch float>, "prompt": "<text>",
     "created_at": "<iso>"}

Files survive a bot restart (durable on disk); the poller picks up any whose
fire_at has passed. Creating one is intentionally dead simple so the agent can
do it from a shell turn:

    python3 wakeups.py add 1500 "/loop continue the migration"

Listing / clearing:
    python3 wakeups.py list
    python3 wakeups.py clear
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import List, Tuple

WAKEUP_DIR = os.environ.get(
    "BRIDGE_WAKEUP_DIR",
    # <repo>/work/wakeups next to this module — portable across hosts/users.
    str(Path(__file__).resolve().parent / "work" / "wakeups"),
)

# Guard rails: a wakeup prompt longer than this is almost certainly a mistake,
# and a delay outside this window is rejected so a fat-fingered value can't
# park a wakeup for years or hammer the loop.
MAX_PROMPT_CHARS = 8000
MIN_DELAY_SEC = 30
MAX_DELAY_SEC = 7 * 24 * 3600


def _ensure_dir() -> None:
    os.makedirs(WAKEUP_DIR, exist_ok=True)


def add(prompt: str, delay_seconds: float, now: float | None = None) -> dict:
    """Create a wakeup that fires `delay_seconds` from now. Returns the record."""
    if not prompt or not prompt.strip():
        raise ValueError("wakeup prompt is empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt too long ({len(prompt)} > {MAX_PROMPT_CHARS})")
    delay_seconds = float(delay_seconds)
    if delay_seconds < MIN_DELAY_SEC:
        delay_seconds = MIN_DELAY_SEC
    if delay_seconds > MAX_DELAY_SEC:
        raise ValueError(f"delay too large ({delay_seconds}s > {MAX_DELAY_SEC}s)")
    now = time.time() if now is None else now
    rec = {
        "id": uuid.uuid4().hex,
        "fire_at": now + delay_seconds,
        "prompt": prompt,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    _ensure_dir()
    path = os.path.join(WAKEUP_DIR, rec["id"] + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(tmp, path)  # atomic — poller never sees a half-written file
    return rec


def _load(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        if "fire_at" in rec and "prompt" in rec:
            return rec
    except (OSError, ValueError):
        return None
    return None


def list_all() -> List[Tuple[str, dict]]:
    """All valid wakeups as (path, record), soonest fire_at first."""
    _ensure_dir()
    out: List[Tuple[str, dict]] = []
    for name in os.listdir(WAKEUP_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(WAKEUP_DIR, name)
        rec = _load(path)
        if rec is not None:
            out.append((path, rec))
    out.sort(key=lambda pr: pr[1].get("fire_at", 0))
    return out


def due(now: float | None = None) -> List[Tuple[str, dict]]:
    """Wakeups whose fire_at <= now, soonest first."""
    now = time.time() if now is None else now
    return [(p, r) for (p, r) in list_all() if r.get("fire_at", 0) <= now]


def remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------ CLI
def _main(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: wakeups.py add <delay_seconds> <prompt> | list | clear")
        return 0
    cmd = argv[0]
    if cmd == "add":
        if len(argv) < 3:
            print("usage: wakeups.py add <delay_seconds> <prompt>")
            return 2
        rec = add(argv[2] if len(argv) == 3 else " ".join(argv[2:]),
                  float(argv[1]))
        print(f"wakeup {rec['id']} fires at "
              f"{time.strftime('%H:%M:%SZ', time.gmtime(rec['fire_at']))} "
              f"(+{int(float(argv[1]))}s)")
        return 0
    if cmd == "list":
        items = list_all()
        if not items:
            print("(no wakeups)")
        now = time.time()
        for _, r in items:
            print(f"{r['id'][:8]}  in {int(r['fire_at']-now)}s  {r['prompt'][:80]!r}")
        return 0
    if cmd == "clear":
        n = 0
        for path, _ in list_all():
            remove(path); n += 1
        print(f"cleared {n}")
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
