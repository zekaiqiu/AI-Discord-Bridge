"""Uploads must reach the OpenAI-compatible model path (they were silently
invisible there), and a <memory_update> block anywhere in a multi-step reply
must be applied and stripped, not shown raw.
"""
from __future__ import annotations

from typing import Any

import app as app_module
import claude_runner
import tokenhub_runner
from helpers import consume_sse, create_session

USER = "attach-user@example.com"
ADMIN = "attach-admin@example.com"


def _fake_tokenhub(monkeypatch):
    captured: dict[str, Any] = {"calls": []}

    async def fake_run_turn(**kwargs):
        captured["calls"].append(kwargs)
        yield {"type": "delta", "text": "ok"}
        yield {"type": "done", "full_text": "ok", "meta": {"model": kwargs.get("model")}}

    monkeypatch.setattr(app_module.tokenhub_runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(tokenhub_runner, "run_turn", fake_run_turn)
    return captured


def _upload(client, sid, headers, name, data, mime):
    r = client.post(f"/api/sessions/{sid}/attachments", headers=headers,
                    files=[("files", (name, data, mime))])
    assert r.status_code in (200, 201), r.text


def test_user_upload_is_staged_and_announced_to_api_model(
    client, auth_headers, fake_claude, monkeypatch, tmp_attachments_dir, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _fake_tokenhub(monkeypatch)
    staged: list = []
    monkeypatch.setattr(
        claude_runner, "_stage_attachments_into_user_container",
        lambda container, sid, d, **kw: staged.append((container, sid)) or f"/workspace/.attachments/{sid}",
    )
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    _upload(client, sid, headers, "notes.txt", b"hello", "text/plain")

    r = client.post(f"/api/sessions/{sid}/messages", headers=headers,
                    json={"text": "summarize my file", "model": "glm"})
    assert r.status_code == 200
    consume_sse(r)
    prompt = captured["calls"][0]["prompt"]
    assert staged and staged[0][1] == sid, "attachments were not staged into the container"
    assert f"/workspace/.attachments/{sid}/notes.txt" in prompt
    assert "run_bash" in prompt
    assert prompt.endswith("summarize my file")


def test_admin_upload_maps_to_host_path(
    client, auth_headers, fake_claude, monkeypatch, tmp_attachments_dir, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN)
    monkeypatch.setenv("CHAT_ATTACHMENTS_HOST_DIR", "/home/felix/projects/chat-wizerith/attachments")
    captured = _fake_tokenhub(monkeypatch)
    headers = auth_headers(ADMIN)
    sid = create_session(client, headers)
    _upload(client, sid, headers, "data.csv", b"a,b\n1,2\n", "text/csv")

    r = client.post(f"/api/sessions/{sid}/messages", headers=headers,
                    json={"text": "plot it", "model": "glm"})
    assert r.status_code == 200
    consume_sse(r)
    prompt = captured["calls"][0]["prompt"]
    assert f"/home/felix/projects/chat-wizerith/attachments/{sid}/data.csv" in prompt


def test_no_upload_leaves_prompt_untouched(
    client, auth_headers, fake_claude, monkeypatch, tmp_attachments_dir, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _fake_tokenhub(monkeypatch)
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    r = client.post(f"/api/sessions/{sid}/messages", headers=headers,
                    json={"text": "just chat", "model": "glm"})
    consume_sse(r)
    assert captured["calls"][0]["prompt"] == "just chat"


def test_memory_block_at_end_still_extracted():
    text, mem = app_module._extract_memory_update("answer\n\n<memory_update>\nlikes tea\n</memory_update>")
    assert text == "answer" and mem == "likes tea"


def test_memory_block_mid_reply_is_applied_and_stripped():
    joined = "Checked.\n<memory_update>\nowns a ThinkPad\n</memory_update>\n\nFinal answer here."
    text, mem = app_module._extract_memory_update(joined)
    assert mem == "owns a ThinkPad"
    assert "<memory_update>" not in text and "ThinkPad" not in text
    assert text.startswith("Checked.") and text.endswith("Final answer here.")


def test_last_memory_block_wins_and_all_are_stripped():
    joined = "a\n<memory_update>\nfirst\n</memory_update>\nb\n<memory_update>\nsecond\n</memory>\nc"
    text, mem = app_module._extract_memory_update(joined)
    assert mem == "second"
    assert "memory_update" not in text
    assert text.replace("\n", "") == "abc"


def test_no_memory_block_is_untouched():
    assert app_module._extract_memory_update("plain") == ("plain", None)
