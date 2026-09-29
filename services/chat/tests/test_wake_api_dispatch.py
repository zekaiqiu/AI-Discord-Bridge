"""Scheduled wakes must run on the user's default model, not unconditionally
on the claude CLI (which, with the pool accounts dead, errored every wake
since 2026-09-25), and a wake whose thread was deleted must cancel itself
instead of re-arming forever.
"""
from __future__ import annotations

import asyncio
from typing import Any

import app
import storage
import tokenhub_runner

EMAIL = "wake@example.com"


def _run(coro):
    return asyncio.run(coro)


def _quiet(monkeypatch):
    async def _noop_img(session_id):
        return None

    async def _noop_sched(session_id, email, errors=None):
        return 0

    monkeypatch.setattr(app, "_process_image_requests", _noop_img)
    monkeypatch.setattr(app, "_process_schedule_requests", _noop_sched)
    monkeypatch.setattr(app, "_scan_new_artifacts", lambda **kw: [])
    monkeypatch.setattr(app, "_format_artifacts_markdown", lambda a: "")
    monkeypatch.setattr(app, "_ensure_artifacts_dir", lambda **kw: None)
    monkeypatch.setattr(app.claude_runner, "host_shell_container", lambda: "host-shell")


def _fake_tokenhub(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_run_turn(**kwargs):
        calls.append(kwargs)
        yield {"type": "delta", "text": "woke"}
        yield {"type": "done", "full_text": "woke", "meta": {"model": kwargs.get("model")}}

    monkeypatch.setattr(app.tokenhub_runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(tokenhub_runner, "run_turn", fake_run_turn)
    return calls


def test_wake_runs_on_default_api_model_with_thread_history(
    tmp_sessions_dir, monkeypatch
):
    monkeypatch.setattr(app, "CHAT_DEFAULT_MODEL", "glm")
    monkeypatch.setattr(app, "_persistent_enabled", lambda: False)
    _quiet(monkeypatch)
    calls = _fake_tokenhub(monkeypatch)
    claude_calls: list = []
    monkeypatch.setattr(
        app.claude_runner, "run_turn",
        lambda **kw: claude_calls.append(kw) or (_ for _ in ()).throw(AssertionError("claude path used")),
    )

    sess = storage.create_session(EMAIL, role="admin")
    sid = sess["id"]
    storage.append_user_message(EMAIL, sid, "please remind me about the report")
    storage.mark_claude_initialized(EMAIL, sid)
    rec = {"id": "w1", "email": EMAIL, "session_id": sid,
           "prompt": "check the report now", "interval_seconds": None, "attempt": 0}

    _run(app._fire_schedule_locked(rec))

    assert not claude_calls
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "glm"
    assert "check the report now" in call["prompt"]
    assert "[Scheduled wake" in call["prompt"]
    assert "please remind me about the report" in (call["prior_history"] or "")
    # admin session → tools in the host shell, like a typed turn
    assert call["container"] == "host-shell"
    assert call["tool_home"] == "/home/felix"
    msgs = storage.get_session(EMAIL, sid)["messages"]
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["status"] == "complete"
    assert msgs[-1]["via"] == "wake"
    assert msgs[-1]["content"].startswith("woke")


def test_wake_for_deleted_session_cancels_itself(tmp_sessions_dir, monkeypatch):
    cancelled: list[tuple[str, str]] = []
    monkeypatch.setattr(app.chat_scheduler, "cancel",
                        lambda email, sid: cancelled.append((email, sid)) or True)
    rec = {"id": "gone1", "email": EMAIL,
           "session_id": "11111111-2222-3333-4444-555555555555",
           "prompt": "p", "interval_seconds": 3600.0}
    _run(app._fire_schedule_locked(rec))
    assert cancelled == [(EMAIL, "gone1")]
