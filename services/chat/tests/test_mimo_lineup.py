"""MiMo V2.6 Pro / Flash (Xiaomi Token Plan) — served since 2026-09-29.

Covers: both aliases are served non-claude models (normalize, storage,
identity, effort), dispatch reaches mimo_runner (fake, no network), the
runner defaults to the Token Plan endpoint, and it fails closed with a clear
message when no key is configured (instead of a blank reply).
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


MIMO_ALIASES = {"mimo": "mimo-v2.6-pro", "mimo-flash": "mimo-v2.6-flash"}


def test_mimo_aliases_are_served(monkeypatch):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    for alias, model_id in MIMO_ALIASES.items():
        assert app_module._normalize_model(alias) == alias
        assert alias in app_module.NON_CLAUDE_MODELS
        assert alias in storage._VALID_MODELS
        assert mimo_runner.is_mimo_model(alias)
        assert mimo_runner._MIMO_MODELS[alias] == model_id
        _, ident_id, vendor = prompt_blocks.MODEL_IDENTITY[alias]
        assert ident_id == model_id and vendor == "Xiaomi"
    assert not mimo_runner.is_mimo_model("glm")


def test_mimo_default_base_url_is_token_plan():
    import importlib, os
    if os.environ.get("MIMO_BASE_URL"):
        return  # an explicit override wins; nothing to assert about the default
    importlib.reload(mimo_runner)
    assert mimo_runner.MIMO_BASE_URL == "https://token-plan-sgp.xiaomimimo.com/v1"


def test_mimo_effort_levels_match_gateway():
    # Probed 2026-09-29: low/medium/high accepted, "max" -> HTTP 400.
    for alias in MIMO_ALIASES:
        assert app_module.EFFORT_LEVELS[alias] == ("low", "medium", "high")
        assert app_module._validated_effort(alias, "high") == "high"
        assert app_module._validated_effort(alias, "max") is None


def test_settings_accept_mimo_defaults():
    for alias in MIMO_ALIASES:
        assert storage._coerce_settings({"default_model": alias})["default_model"] == alias
    # unknown values still fall back to the default model
    assert storage._coerce_settings({"default_model": "nope"})["default_model"] == "kimi"


def test_dispatch_reaches_mimo_runner(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_mimo(monkeypatch)
    headers = auth_headers(USER_A)
    for alias in MIMO_ALIASES:
        sid = create_session(client, headers)
        resp = client.post(
            f"/api/sessions/{sid}/messages",
            headers=headers,
            json={"text": "ping", "model": alias, "effort": "high"},
        )
        assert resp.status_code == 200
        events = consume_sse(resp)
        assert [e.get("event") for e in events].count("done") == 1
    assert [c.get("model") for c in captured["calls"]] == list(MIMO_ALIASES)
    assert all(c.get("effort") == "high" for c in captured["calls"])


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
