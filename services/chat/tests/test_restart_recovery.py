"""A backend restart must not lose in-flight turns: placeholders left at
status='streaming' are re-run on the same seq at boot (the user's message
is already persisted), and only non-recoverable ones are written off.
"""
from __future__ import annotations

import asyncio
from typing import Any

import app
import storage
import tokenhub_runner

EMAIL = "restart@example.com"


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
    monkeypatch.setattr(app, "CHAT_DEFAULT_MODEL", "glm")
    monkeypatch.setattr(app, "_persistent_enabled", lambda: False)


def _fake_tokenhub(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_run_turn(**kwargs):
        calls.append(kwargs)
        yield {"type": "delta", "text": "recovered reply"}
        yield {"type": "done", "full_text": "recovered reply", "meta": {"model": kwargs.get("model")}}

    monkeypatch.setattr(app.tokenhub_runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(tokenhub_runner, "run_turn", fake_run_turn)
    return calls


def _interrupted_session(*, via=None, trailing_user=False):
    sess = storage.create_session(EMAIL, role="admin")
    sid = sess["id"]
    storage.append_user_message(EMAIL, sid, "earlier question")
    _, seq0 = storage.append_assistant_placeholder(EMAIL, sid)
    storage.update_assistant_message(EMAIL, sid, seq0, content="earlier answer",
                                     status=storage.ASSISTANT_STATUS_COMPLETE)
    storage.append_user_message(EMAIL, sid, "please build the report")
    _, seq = storage.append_assistant_placeholder(EMAIL, sid, via=via)
    if trailing_user:
        storage.append_user_message(EMAIL, sid, "another message after it")
    return sid, seq


async def _boot_and_drain():
    result = await app._recover_interrupted_turns()
    tasks = list(app._active_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)
    return result


def test_interrupted_turn_is_rerun_on_same_placeholder(tmp_sessions_dir, monkeypatch):
    _quiet(monkeypatch)
    calls = _fake_tokenhub(monkeypatch)
    sid, seq = _interrupted_session()

    recovered, swept = asyncio.run(_boot_and_drain())
    assert (recovered, swept) == (1, 0)
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "glm"
    assert call["prompt"].startswith("please build the report")
    assert "re-run automatically" in call["prompt"]
    assert "earlier question" in (call["prior_history"] or "")
    assert "earlier answer" in (call["prior_history"] or "")
    assert "please build the report" not in (call["prior_history"] or "")
    msgs = storage.get_session(EMAIL, sid)["messages"]
    last = msgs[-1]
    assert last["seq"] == seq
    assert last["status"] == storage.ASSISTANT_STATUS_COMPLETE
    assert last["content"] == "recovered reply"
    assert not app._active_runs and not app._active_tasks


def test_wake_and_non_tail_placeholders_are_written_off(tmp_sessions_dir, monkeypatch):
    _quiet(monkeypatch)
    calls = _fake_tokenhub(monkeypatch)
    sid_wake, seq_wake = _interrupted_session(via="wake")
    sid_mid, seq_mid = _interrupted_session(trailing_user=True)

    recovered, swept = asyncio.run(_boot_and_drain())
    assert (recovered, swept) == (0, 2)
    assert calls == []
    for sid, seq in ((sid_wake, seq_wake), (sid_mid, seq_mid)):
        m = [x for x in storage.get_session(EMAIL, sid)["messages"] if x.get("seq") == seq][0]
        assert m["status"] == storage.ASSISTANT_STATUS_ERROR
        assert "server restarted" in m["content"]


def test_boot_is_a_noop_when_nothing_is_streaming(tmp_sessions_dir, monkeypatch):
    _quiet(monkeypatch)
    calls = _fake_tokenhub(monkeypatch)
    sess = storage.create_session(EMAIL, role="admin")
    storage.append_user_message(EMAIL, sess["id"], "hi")
    _, seq = storage.append_assistant_placeholder(EMAIL, sess["id"])
    storage.update_assistant_message(EMAIL, sess["id"], seq, content="done",
                                     status=storage.ASSISTANT_STATUS_COMPLETE)
    assert asyncio.run(_boot_and_drain()) == (0, 0)
    assert calls == []


def test_shutdown_cancel_leaves_placeholder_streaming(tmp_sessions_dir, monkeypatch):
    """A worker cancelled because the PROCESS is exiting must not mark the
    turn cancelled (that would hide it from recovery on the next boot)."""
    _quiet(monkeypatch)

    async def hanging_run_turn(**kwargs):
        yield {"type": "delta", "text": "partial"}
        await asyncio.sleep(3600)

    monkeypatch.setattr(app.tokenhub_runner, "run_turn", hanging_run_turn)
    monkeypatch.setattr(tokenhub_runner, "run_turn", hanging_run_turn)
    sid, seq = _interrupted_session()

    async def go():
        await app._recover_interrupted_turns()
        task = list(app._active_tasks.values())[0]
        await asyncio.sleep(0.05)
        monkeypatch.setattr(app, "_shutting_down", True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    last = storage.get_session(EMAIL, sid)["messages"][-1]
    assert last["seq"] == seq
    assert last["status"] == storage.ASSISTANT_STATUS_STREAMING
