"""MiMo V2.6 Pro (Xiaomi) — third picker option since 2026-09-28.

Covers: the alias is a served non-claude model (normalize, storage, identity),
dispatch reaches mimo_runner (fake, no network), and the runner fails closed
with a clear message while no key is configured (instead of a blank reply).
"""
from __future__ import annotations

import asyncio
from typing import Any

import app as app_module
import mimo_runner
import prompt_blocks
import storage
from helpers import consume_sse, create_session

USER_A = "alice@example.com"


def _install_fake_mimo(monkeypatch: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {"calls": []}

    async def fake_run_turn(**kwargs: Any):
        captured["calls"].append(kwargs)
        yield {"type": "delta", "text": "ni hao"}
        yield {"type": "done", "full_text": "ni hao", "meta": {"model": kwargs.get("model")}}

    monkeypatch.setattr(app_module.mimo_runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(mimo_runner, "run_turn", fake_run_turn)
    return captured


def test_mimo_is_a_served_alias(monkeypatch):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    assert app_module._normalize_model("mimo") == "mimo"
    assert app_module._is_api_model("mimo")
    assert mimo_runner.is_mimo_model("mimo")
    assert not mimo_runner.is_mimo_model("glm")
    assert mimo_runner._MIMO_MODELS["mimo"] == "mimo-v2.6-pro"
    assert "mimo" in storage._VALID_MODELS
    name, model_id, vendor = prompt_blocks.MODEL_IDENTITY["mimo"]
    assert model_id == "mimo-v2.6-pro" and vendor == "Xiaomi"


def test_mimo_has_no_effort_levels_until_probed():
    # No key on the host yet → levels unverified → effort must never be sent.
    assert app_module._validated_effort("mimo", "high") is None
    assert "mimo" not in app_module.EFFORT_LEVELS


def test_settings_accept_mimo_default():
    coerced = storage._coerce_settings({"default_model": "mimo"})
    assert coerced["default_model"] == "mimo"
    # unknown values still fall back to the default model
    assert storage._coerce_settings({"default_model": "nope"})["default_model"] == "glm"


def test_dispatch_reaches_mimo_runner(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_mimo(monkeypatch)
    headers = auth_headers(USER_A)
    sid = create_session(client, headers)
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping", "model": "mimo", "effort": "high"},
    )
    assert resp.status_code == 200
    events = consume_sse(resp)
    assert [e.get("event") for e in events].count("done") == 1
    assert len(captured["calls"]) == 1, "mimo_runner.run_turn was not called"
    call = captured["calls"][0]
    assert call["model"] == "mimo"
    # unverified effort is dropped, never forwarded
    assert call["effort"] is None


def test_runner_fails_closed_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(mimo_runner, "_MIMO_KEY_FILE", tmp_path / "absent")

    async def collect():
        return [ev async for ev in mimo_runner.run_turn(model="mimo", prompt="hi")]

    events = asyncio.run(collect())
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "not configured" in events[0]["message"]


def test_resolve_key_prefers_env_then_file(monkeypatch, tmp_path):
    kf = tmp_path / "k"
    kf.write_text("file-key\n")
    monkeypatch.setattr(mimo_runner, "_MIMO_KEY_FILE", kf)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    assert mimo_runner.resolve_key() == "file-key"
    monkeypatch.setenv("MIMO_API_KEY", "env-key")
    assert mimo_runner.resolve_key() == "env-key"
