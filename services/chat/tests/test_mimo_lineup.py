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


def test_mimo_is_parked_not_served(monkeypatch):
    # Parked 2026-09-28: the runner module stays, the alias is out of the
    # lineup, so a stale "mimo" from a saved thread/blob lands on the default.
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    assert app_module._normalize_model("mimo") == "glm"
    assert "mimo" not in app_module.NON_CLAUDE_MODELS
    assert "mimo" not in storage._VALID_MODELS
    assert mimo_runner.is_mimo_model("mimo")
    assert not mimo_runner.is_mimo_model("glm")
    assert mimo_runner._MIMO_MODELS["mimo"] == "mimo-v2.6-pro"
    name, model_id, vendor = prompt_blocks.MODEL_IDENTITY["mimo"]
    assert model_id == "mimo-v2.6-pro" and vendor == "Xiaomi"


def test_mimo_has_no_effort_levels_until_probed():
    # No key on the host yet → levels unverified → effort must never be sent.
    assert app_module._validated_effort("mimo", "high") is None
    assert "mimo" not in app_module.EFFORT_LEVELS


def test_settings_migrate_mimo_default_to_glm():
    coerced = storage._coerce_settings({"default_model": "mimo"})
    assert coerced["default_model"] == "glm"
    # unknown values still fall back to the default model
    assert storage._coerce_settings({"default_model": "nope"})["default_model"] == "glm"


def test_dispatch_never_reaches_mimo_runner_while_parked(
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
    assert captured["calls"] == [], "parked model must not be dispatched"


def test_runner_fails_closed_without_key(monkeypatch, tmp_path):
    import tokenhub_runner
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
    monkeypatch.setattr(mimo_runner, "_MIMO_KEY_FILE", tmp_path / "absent")
    # the runner falls back to the TokenHub key; keep the test off the network
    monkeypatch.setattr(tokenhub_runner, "_TOKENHUB_KEY_FILE", tmp_path / "absent-th")

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


# --- TokenHub fallback (2026-09-28: TokenHub lists mimo-v2.6-pro; the plan
# key only needs its model scope widened in the TokenHub console) ---------

def _no_mimo_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(mimo_runner, "_MIMO_KEY_FILE", tmp_path / "absent-mimo")


def _tokenhub_key(monkeypatch, tmp_path, value: str | None):
    import tokenhub_runner
    monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
    kf = tmp_path / "glm_key"
    if value is None:
        kf = tmp_path / "absent-tokenhub"
    else:
        kf.write_text(value + "\n")
    monkeypatch.setattr(tokenhub_runner, "_TOKENHUB_KEY_FILE", kf)


def test_endpoint_falls_back_to_tokenhub_key(monkeypatch, tmp_path):
    import tokenhub_runner
    _no_mimo_key(monkeypatch, tmp_path)
    _tokenhub_key(monkeypatch, tmp_path, "sk-tp-plan")
    assert mimo_runner.resolve_endpoint() == (
        tokenhub_runner.TOKENHUB_BASE_URL, "sk-tp-plan", "tokenhub",
    )


def test_endpoint_prefers_explicit_mimo_key(monkeypatch, tmp_path):
    _tokenhub_key(monkeypatch, tmp_path, "sk-tp-plan")
    monkeypatch.setenv("MIMO_API_KEY", "xiaomi-key")
    assert mimo_runner.resolve_endpoint() == (
        mimo_runner.MIMO_BASE_URL, "xiaomi-key", "mimo",
    )


def test_endpoint_none_without_any_key(monkeypatch, tmp_path):
    _no_mimo_key(monkeypatch, tmp_path)
    _tokenhub_key(monkeypatch, tmp_path, None)
    assert mimo_runner.resolve_endpoint() is None

    async def collect():
        return [ev async for ev in mimo_runner.run_turn(model="mimo", prompt="hi")]

    events = asyncio.run(collect())
    assert events[0]["type"] == "error"
    assert "not configured" in events[0]["message"]


def test_tokenhub_403_carries_scope_hint(monkeypatch, tmp_path):
    """A 403 from TokenHub on the fallback path tells the user exactly what to
    change (key scope in the TokenHub console), not a bare 'HTTP 403'."""
    import haihub_runner
    _no_mimo_key(monkeypatch, tmp_path)
    _tokenhub_key(monkeypatch, tmp_path, "sk-tp-plan")
    seen: dict[str, Any] = {}

    async def fake_run_turn(**kwargs: Any):
        seen.update(kwargs)
        yield {"type": "error", "message": "haihub HTTP 403: The current API Key is not authorized to access model mimo-v2.6-pro."}

    monkeypatch.setattr(haihub_runner, "run_turn", fake_run_turn)

    async def collect():
        return [ev async for ev in mimo_runner.run_turn(model="mimo", prompt="hi")]

    events = asyncio.run(collect())
    assert seen["base_url"].endswith("/plan/v3")
    assert seen["api_key"] == "sk-tp-plan"
    assert events[0]["type"] == "error"
    assert "HTTP 403" in events[0]["message"]
    assert "TokenHub console" in events[0]["message"]


def test_provider_error_suffix_extracts_message():
    import haihub_runner
    body = '{"error":{"type":"permission_error","code":"403002","message":"The current API Key is not authorized to access model mimo-v2.6-pro. See: x"}}'
    assert haihub_runner._provider_error_suffix(body).startswith(": The current API Key is not authorized")
    assert haihub_runner._provider_error_suffix("<html>502</html>") == ""
    assert haihub_runner._provider_error_suffix('{"error":"plain"}') == ": plain"
