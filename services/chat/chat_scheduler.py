"""Persistent, dependency-free scheduler for chat.wizerith sessions.

Why this exists: a chat session is the stock Claude Code CLI, so the model
will confidently say "I'll ping you in 30 minutes" — but the web deployment
had no backing for that. The long-lived session process is idle-evicted after
15 min (``session_process.py``), there was no scheduler to survive that, and
the only notification path was a browser popup that needs the tab open. So the
promise was pure text. This module is the missing piece: a durable timer store
the model writes to (via a ``_schedule_request_*.json`` marker, same protocol
as image-gen), plus the timing logic a tick loop in ``app.py`` drives to fire
each due wake back into the session's thread.

Design choices:
  * **Wall-clock epoch seconds** for fire times (``time.time()``), NOT
    ``monotonic`` — schedules fire at absolute future moments and must survive
    a backend restart, so they're persisted and compared in real time.
  * **No croniter dependency** (it isn't in the chat image). We support the
    shapes that cover the actual ask: a relative delay (``in: "30m"``), an
    absolute time (``at: "2026-06-18T17:00:00Z"``), and a simple recurring
    interval (``every: "1h"``). Real cron can be layered on later if wanted.
  * **A single global JSON file** ``<sessions_root>/_schedules.json`` so the
    tick loop finds every user's due wakes in one read instead of walking
    every per-user dir. The chat service is single-process asyncio, so a plain
    in-process lock serialises the read-modify-write; the file is written
    atomically (tmp + rename) so a crash mid-write can't corrupt it.

This module holds NO knowledge of how a wake is delivered — ``app.py`` owns
the fire callback (respawn the session, inject the prompt, stream the reply
into the thread). Here we only persist records and decide what's due.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import storage  # only for _sessions_root(); keeps the store path in one place

_log = logging.getLogger(__name__)


# How long a claimed one-shot stays leased before it becomes due again. Sized
# to be far longer than any legitimate wake turn (including one queued behind
# a long-running user turn), so the lease only ever lapses on a real crash or
# restart — if it lapsed while a fire was still running we would double-fire.
LEASE_SECONDS = 7200.0

# A wake whose computed delay/interval is below this is clamped up — guards
# against a model emitting ``in: "0s"`` and the tick loop hot-looping.
_MIN_DELAY_SECONDS = 5.0
# Don't let a single session accumulate unbounded timers (a runaway loop or a
# confused model). Registration past this is rejected.
MAX_PER_SESSION = 25
# Hard cap on how far out a one-shot can be armed (~1 year) — a sanity bound,
# not a product limit.
_MAX_DELAY_SECONDS = 366 * 24 * 3600.0

_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 1}

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Store path + atomic IO
# --------------------------------------------------------------------------
def _store_path() -> str:
    return os.path.join(storage._sessions_root(), "_schedules.json")


def _load() -> dict[str, Any]:
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"schedules": [], "counter": 0}
    if not isinstance(data, dict):
        return {"schedules": [], "counter": 0}
    scheds = data.get("schedules")
    if not isinstance(scheds, list):
        data["schedules"] = []
    if not isinstance(data.get("counter"), int):
        data["counter"] = 0
    return data


def _save(data: dict[str, Any]) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=0)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Time parsing
# --------------------------------------------------------------------------
def parse_duration(spec: Any) -> Optional[float]:
    """Parse ``"30m"`` / ``"2h"`` / ``"1d"`` / ``"90"`` (bare = seconds) into
    seconds. Returns None on anything unparseable."""
    if isinstance(spec, (int, float)):
        return float(spec) if spec > 0 else None
    if not isinstance(spec, str):
        return None
    m = _DUR_RE.match(spec)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    return value * _UNIT_SECONDS.get(unit, 1)


def parse_at(spec: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp into an epoch. Accepts a trailing ``Z``.
    A naive (tz-less) value is interpreted as UTC. Returns None if unparseable."""
    if not isinstance(spec, str) or not spec.strip():
        return None
    s = spec.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def register(
    email: str,
    session_id: str,
    prompt: str,
    *,
    delay_seconds: Optional[float] = None,
    at_epoch: Optional[float] = None,
    every_seconds: Optional[float] = None,
    note: Optional[str] = None,
    now: Optional[float] = None,
    attempt: int = 0,
) -> dict[str, Any]:
    """Create one schedule and persist it. Exactly one of ``delay_seconds`` /
    ``at_epoch`` determines the first fire; ``every_seconds`` (optional) makes
    it recurring (and seeds the first fire if no delay/at was given).

    ``attempt`` is the retry generation: 0 for a wake the model asked for, >0
    for one ``requeue``d after a failed fire. It rides along in the record so
    the fire path can bound its own retries (see app._fire_schedule).

    Raises ValueError on bad input (empty prompt, no timing, per-session cap).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("schedule prompt must be non-empty")
    now = time.time() if now is None else now

    if at_epoch is not None:
        next_fire = float(at_epoch)
    elif delay_seconds is not None:
        next_fire = now + float(delay_seconds)
    elif every_seconds is not None:
        next_fire = now + float(every_seconds)
    else:
        raise ValueError("schedule needs one of: delay_seconds, at_epoch, every_seconds")

    # Clamp: never in the past / sub-tick, never absurdly far out.
    if next_fire < now + _MIN_DELAY_SECONDS:
        next_fire = now + _MIN_DELAY_SECONDS
    if next_fire > now + _MAX_DELAY_SECONDS:
        next_fire = now + _MAX_DELAY_SECONDS

    interval: Optional[float] = None
    if every_seconds is not None:
        interval = max(_MIN_DELAY_SECONDS, float(every_seconds))

    with _lock:
        data = _load()
        active = [s for s in data["schedules"] if s.get("session_id") == session_id]
        if len(active) >= MAX_PER_SESSION:
            raise ValueError(
                f"session already has {len(active)} scheduled wakes (max {MAX_PER_SESSION})"
            )
        data["counter"] = int(data.get("counter", 0)) + 1
        rec = {
            "id": uuid.uuid4().hex[:12],
            "n": data["counter"],
            "email": email,
            "session_id": session_id,
            "prompt": prompt[:4000],
            "note": (note or "").strip()[:200] or None,
            "next_fire": next_fire,
            "interval_seconds": interval,
            "created_at": _iso(now),
            "last_fired": None,
            "attempt": int(attempt),
        }
        data["schedules"].append(rec)
        _save(data)
    return rec


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    """The shape the frontend consumes (epochs + a human ISO for next_fire).

    ``running`` exists because ``claim_due`` LEASES a one-shot instead of
    deleting it: while the wake's turn is in flight its ``next_fire`` sits at
    ``now + LEASE_SECONDS``. Without this flag the composer's clock pill read
    that lease as a real schedule and told the user their wake would fire "in
    2h" at the exact moment it was already running — and then the record
    vanished when it completed. A lapsed lease (fire crashed, ``next_fire``
    back in the past) is deliberately NOT reported as running: that wake is
    genuinely due again."""
    next_fire = rec.get("next_fire")
    leased = rec.get("leased_at")
    running = bool(leased) and isinstance(next_fire, (int, float)) and next_fire > time.time()
    return {
        "id": rec.get("id"),
        "prompt": rec.get("prompt"),
        "note": rec.get("note"),
        "next_fire": next_fire,
        "next_fire_iso": _iso(next_fire) if next_fire else None,
        "interval_seconds": rec.get("interval_seconds"),
        "created_at": rec.get("created_at"),
        "last_fired": rec.get("last_fired"),
        "running": running,
    }


def list_for(email: str, session_id: Optional[str] = None) -> list[dict[str, Any]]:
    with _lock:
        data = _load()
        out = [
            _public(s)
            for s in data["schedules"]
            if s.get("email") == email
            and (session_id is None or s.get("session_id") == session_id)
        ]
    out.sort(key=lambda s: s.get("next_fire") or 0)
    return out


def count_for(email: str, session_id: str) -> int:
    with _lock:
        data = _load()
        return sum(
            1
            for s in data["schedules"]
            if s.get("email") == email and s.get("session_id") == session_id
        )


def cancel(email: str, sched_id: str) -> bool:
    """Remove one schedule the caller owns. Returns True if it existed."""
    with _lock:
        data = _load()
        before = len(data["schedules"])
        data["schedules"] = [
            s
            for s in data["schedules"]
            if not (s.get("email") == email and s.get("id") == sched_id)
        ]
        removed = len(data["schedules"]) < before
        if removed:
            _save(data)
        return removed


def complete(sched_id: Optional[str]) -> bool:
    """Drop a leased one-shot once its fire has reached a terminal outcome.

    Paired with the lease ``claim_due`` takes out (see LEASE_SECONDS). No-op
    for recurring records — those re-arm themselves and must never be removed
    by the fire path. Returns True if a record was removed."""
    if not sched_id:
        return False
    with _lock:
        data = _load()
        keep = [s for s in data["schedules"]
                if not (s.get("id") == sched_id and not s.get("interval_seconds"))]
        if len(keep) == len(data["schedules"]):
            return False
        data["schedules"] = keep
        _save(data)
        return True


def claim_due(now: Optional[float] = None) -> list[dict[str, Any]]:
    """Atomically take every schedule whose ``next_fire <= now``.

    Recurring records are re-armed to the next future multiple of their
    interval (so a backend that was down for several intervals fires ONCE and
    catches up, not N times).

    One-shot records are LEASED, not removed: they stay in the store with
    ``next_fire`` pushed out by LEASE_SECONDS, and the fire path calls
    ``complete()`` when the wake has actually reached a terminal outcome.
    Previously they were dropped here, which meant that between the claim and
    the reply the wake existed ONLY in the firing task's memory — a restart,
    a crash, or a fire still blocked behind a long-running turn lost it with
    no trace. With the lease, an incomplete fire simply lapses and the wake
    fires again on a later tick.

    Returns the raw records to hand to the fire callback. The persisted
    ``last_fired`` is stamped here so a fire crash doesn't replay the same
    wake on the very next tick."""
    now = time.time() if now is None else now
    claimed: list[dict[str, Any]] = []
    with _lock:
        data = _load()
        keep: list[dict[str, Any]] = []
        dirty = False
        for s in data["schedules"]:
            nf = s.get("next_fire")
            if not isinstance(nf, (int, float)) or nf > now:
                keep.append(s)
                continue
            # Due. Snapshot for firing.
            claimed.append(dict(s))
            dirty = True
            interval = s.get("interval_seconds")
            if isinstance(interval, (int, float)) and interval >= _MIN_DELAY_SECONDS:
                # Re-arm to the first future tick (catch-up collapses misses).
                next_fire = nf + interval
                if next_fire <= now:
                    missed = ((now - next_fire) // interval) + 1
                    next_fire += missed * interval
                s = dict(s)
                s["next_fire"] = next_fire
                s["last_fired"] = _iso(now)
                keep.append(s)
            else:
                # One-shot: lease it rather than drop it. ``complete()``
                # removes it once the fire has actually produced something.
                s = dict(s)
                s["next_fire"] = now + LEASE_SECONDS
                s["last_fired"] = _iso(now)
                s["leased_at"] = now
                keep.append(s)
        if dirty:
            data["schedules"] = keep
            _save(data)
    return claimed


def requeue(email: str, session_id: str, prompt: str, *, delay_seconds: float,
            note: Optional[str] = None, attempt: int = 0) -> None:
    """Re-arm a one-shot wake that couldn't fire (session busy, or the turn
    itself failed). Best-effort — swallows the per-session cap so a transient
    busy session never loses a wake, but logs it, because a swallowed requeue
    IS a lost wake and that must not be invisible."""
    try:
        register(email, session_id, prompt, delay_seconds=delay_seconds,
                 note=note, attempt=attempt)
    except ValueError as exc:
        _log.warning(
            "requeue dropped for session %s (attempt %s): %s",
            session_id, attempt, exc,
        )
