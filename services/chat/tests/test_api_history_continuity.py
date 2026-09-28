"""Stateless API models (TokenHub glm/kimi, haihub qwen/deepseek/minimax) must
receive the session's own prior transcript on EVERY turn.

haihub_runner's contract (module docstring): "haihub is stateless per request,
so app.py passes the FULL prior transcript as prior_history every turn."
Without it, turn 2+ of a glm session has no memory of turn 1 — the model then
compensates from the cross-session <memory> block and whatever it finds in the
shared /workspace, which reads to the user as "sessions bleed into each other".
"""
from __future__ import annotations

from typing import Any

import app as app_module
import tokenhub_runner
from helpers import consume_sse, create_session

USER = "continuity@example.com"


def _install_fake_tokenhub(monkeypatch: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {"calls": []}

    async def fake_run_turn(**kwargs: Any):
        captured["calls"].append(kwargs)
        n = len(captured["calls"])
        yield {"type": "delta", "text": f"reply-{n}"}
        yield {"type": "done", "full_text": f"reply-{n}", "meta": {"model": kwargs.get("model")}}

    monkeypatch.setattr(app_module.tokenhub_runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(tokenhub_runner, "run_turn", fake_run_turn)
    return captured


def _turn(client, sid, headers, text):
    resp = client.post(f"/api/sessions/{sid}/messages", headers=headers,
                       json={"text": text, "model": "glm"})
    assert resp.status_code == 200, resp.text
    events = consume_sse(resp)
    assert [e["event"] for e in events].count("done") == 1, events


def test_second_turn_carries_first_turn_history(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_tokenhub(monkeypatch)
    headers = auth_headers(USER)
    sid = create_session(client, headers)

    _turn(client, sid, headers, "my cat is called Pixel")
    _turn(client, sid, headers, "what is my cat called?")

    assert len(captured["calls"]) == 2
    first, second = captured["calls"]
    assert not first["prior_history"], "turn 1 has nothing to carry"
    hist = second["prior_history"] or ""
    assert "my cat is called Pixel" in hist, (
        "turn 2 did not receive turn 1's user message as history: %r" % hist
    )
    assert "reply-1" in hist, "turn 2 did not receive turn 1's assistant reply"
    # Only THIS session's transcript, and the new message is not duplicated.
    assert "what is my cat called?" not in hist


def test_history_is_scoped_to_the_session(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_tokenhub(monkeypatch)
    headers = auth_headers(USER)
    sid_a = create_session(client, headers)
    sid_b = create_session(client, headers)

    _turn(client, sid_a, headers, "secret-from-A")
    _turn(client, sid_b, headers, "hello from B")
    _turn(client, sid_b, headers, "second message in B")

    hist_b = captured["calls"][2]["prior_history"] or ""
    assert "hello from B" in hist_b
    assert "secret-from-A" not in hist_b, "session A's transcript leaked into session B"
