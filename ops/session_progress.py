#!/usr/bin/env python3
"""session_progress -- periodic "how is it going" report on a working session.

Distinct from wake_watchdog, deliberately:

  * wake_watchdog  answers "is it BROKEN?"  -> hourly, silent when healthy
  * session_progress answers "what is it DOING?" -> frequent, always speaks

They share only the Discord transport. Keeping them separate means the
watchdog stays a quiet alarm (its alerts mean something because they are
rare) while this one can be chatty without devaluing them.

Self-terminating by design. This is the lesson from 2026-08-20: a monitor
that cannot tell "finished" from "stalled" nags a completed session forever
and its output stops being read. When the watched session has no pending
wake AND has been idle past IDLE_DONE_MINUTES, this sends one final message
saying reporting has stopped, flips itself to ``enabled: false`` in its own
config, and goes quiet. Re-enable by hand to resume.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wake_watchdog import (  # noqa: E402  -- shares the Discord transport only
    STORE, _find_session_file, _load_json, _parse_ts, notify,
)

# Where each pooled account's HOME lives, for locating claude transcripts.
# Resolved here rather than imported from the chat service's claude_runner:
# this script runs on the HOST and must not depend on the container's modules
# being importable — the whole point of an out-of-band observer.
ACCOUNTS_ROOT = os.environ.get(
    "SP_ACCOUNTS_ROOT",
    "/opt/wizerith/claude-accounts/_wizerith-ai-pool")
MAIN_HOME = os.environ.get("SP_MAIN_HOME", "/home/felix")


def _account_home(name) -> str | None:
    if not name or name == "main":
        return MAIN_HOME
    path = os.path.join(ACCOUNTS_ROOT, str(name))
    return path if os.path.isdir(path) else None

CONFIG_PATH = os.environ.get(
    "SP_CONFIG", "/home/felix/ops/session_progress.json")
STATE_PATH = os.environ.get(
    "SP_STATE", "/home/felix/ops/session_progress_state.json")

# No pending wake + idle at least this long = the session is done working,
# not mid-thought. Generous: a single turn here can legitimately run 20+ min.
IDLE_DONE_MINUTES = 30

# How much of the newest message to quote. Discord caps a message at 2000;
# this leaves room for several sessions plus the header lines.
EXCERPT_CHARS = 420


def _log(msg: str) -> None:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp}  {msg}", flush=True)


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        _log(f"WARN could not write {path}: {exc}")


def _excerpt(text: str) -> str:
    """Last meaningful slice of the message -- the tail, not the head.

    The head of a long agent turn is preamble; the tail is where it says what
    it just found. Quoting the tail is what makes a progress ping worth
    reading rather than a repeat of how the turn opened."""
    text = " ".join((text or "").split())
    if not text:
        return "_(no text yet — still streaming)_"
    if len(text) <= EXCERPT_CHARS:
        return text
    return "…" + text[-EXCERPT_CHARS:]


def live_progress(session: dict) -> tuple[int, str] | None:
    """Read an IN-FLIGHT turn's progress from the claude CLI transcript.

    Why not the session JSON: ``app._consume_into_run`` calls
    ``storage.update_assistant_message`` only on a TERMINAL event, so a
    session read mid-turn always shows ``chars=0``. The first version of this
    reporter therefore said "no text yet — still streaming" for 48 minutes
    while the agent was 126 tool calls deep and had produced 40 blocks of
    commentary. That is a progress reporter that cannot report progress.

    The CLI's own transcript IS appended live, so it is the honest source
    while a turn is open. Returns (tool_call_count, last_text_block), or None
    if the transcript can't be located or read."""
    sid = session.get("claude_session_id")
    if not sid:
        return None
    home = _account_home(session.get("account"))
    if home is None:
        return None
    import glob as _glob
    matches = _glob.glob(os.path.join(home, ".claude", "projects", "*", f"{sid}.jsonl"))
    if not matches:
        return None
    tools = 0
    last_text = ""
    try:
        with open(matches[0], errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                content = (row.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        tools += 1
                    elif c.get("type") == "text" and (c.get("text") or "").strip():
                        last_text = c["text"]
    except OSError:
        return None
    return tools, last_text


def report(session_id: str, cfg: dict, store: dict, state: dict) -> None:
    label = cfg.get("label") or session_id[:8]
    if cfg.get("enabled") is False:
        _log(f"{label}: disabled, skipping")
        return None

    path = _find_session_file(session_id)
    if path is None:
        _log(f"{label}: session file gone")
        cfg["enabled"] = False
        return (f"⚠️ **{label}** — session file has disappeared; stopping "
                f"progress reports.", "")
    session = _load_json(path, {})
    messages = session.get("messages") or []
    if not messages:
        _log(f"{label}: no messages yet")
        return None

    last = messages[-1]
    seq = last.get("seq")
    status = last.get("status")
    ts = _parse_ts(last.get("ts") or "")
    now = _dt.datetime.now(_dt.timezone.utc)
    idle_min = int((now - ts).total_seconds() // 60) if ts else 9999

    pending = [s for s in store.get("schedules", [])
               if s.get("session_id") == session_id]
    next_wake = None
    if pending:
        nf = min(s.get("next_fire", 0) for s in pending)
        next_wake = _dt.datetime.fromtimestamp(
            nf, _dt.timezone.utc).strftime("%H:%MZ")

    st = state.setdefault(session_id, {})
    prev_seq = st.get("last_seq")
    advanced = prev_seq != seq
    st["last_seq"] = seq

    # Finished? No wake queued and quiet for a while. Say so once and stop.
    if not pending and status != "streaming" and idle_min >= IDLE_DONE_MINUTES:
        _log(f"{label}: appears finished (idle {idle_min}m, no wakes) — stopping")
        cfg["enabled"] = False
        return (f"✅ **{label}** — finished (no wake pending, idle {idle_min}m "
                f"at msg {seq}). **Stopping reports.**",
                last.get("content") or "")

    if status == "streaming":
        live = live_progress(session)
        if live is not None:
            tools, text = live
            head = (f"⏳ **{label}** — working {idle_min}m, "
                    f"**{tools} tool calls** so far")
            if text.strip():
                _log(f"{label}: streaming tools={tools} idle={idle_min}m")
                return (head, text)
        else:
            head = f"⏳ **{label}** — working (turn open {idle_min}m, msg {seq})"
    elif advanced:
        head = f"📋 **{label}** — new message {seq} ({status})"
    else:
        head = (f"⏸️ **{label}** — no new message since {seq} "
                f"({status}, {idle_min}m ago)")
    if next_wake:
        head += f" · next wake {next_wake}"

    _log(f"{label}: seq={seq} status={status} idle={idle_min}m "
         f"advanced={advanced}")
    return (head, last.get("content") or "")


def main() -> int:
    cfg_blob = _load_json(CONFIG_PATH, None)
    if not cfg_blob or not cfg_blob.get("sessions"):
        _log(f"no config at {CONFIG_PATH}; nothing to report on")
        return 0
    store = _load_json(STORE, None)
    if store is None:
        _log(f"schedule store unreadable at {STORE}")
        store = {"schedules": []}
    state = _load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}

    blocks = []
    for session_id, cfg in list(cfg_blob["sessions"].items()):
        try:
            got = report(session_id, cfg or {}, store, state)
            if got:
                blocks.append(got)
        except Exception as exc:  # noqa: BLE001 -- one bad session must not
            _log(f"{session_id[:8]}: ERROR {exc!r}")

    # ONE message for all sessions, not one per session. Three separate pings
    # every 10 minutes is 18 DMs an hour and stops being read; a single digest
    # is the same information at a glance. Excerpt budget is split across the
    # sessions actually reporting so the last one never gets truncated away.
    if blocks:
        budget = max(120, (1700 - 90 * len(blocks)) // len(blocks))
        parts = []
        for head, body in blocks:
            body = " ".join((body or "").split())
            if not body:
                body = "_(no text yet)_"
            elif len(body) > budget:
                body = "…" + body[-budget:]
            parts.append(f"{head}\n> {body}")
        notify("\n\n".join(parts))

    # cfg may have been mutated (self-disable); persist it.
    _save(CONFIG_PATH, cfg_blob)
    _save(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
