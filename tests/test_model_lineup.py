"""Bridge model table (!model) — lineup invariants.

MiMo V2.6 Pro (Xiaomi) is the third option since 2026-09-28 and runs on its
own OpenAI-compatible provider ("mimo"); without a key the host-tool loop
returns a clear "not configured" line instead of erroring mid-turn.
"""
from __future__ import annotations

import asyncio

import bot
import bridge_account_router


def test_mimo_is_third_option_and_registered():
    ids = [m["id"] for m in bot.AVAILABLE_MODELS]
    assert ids[:3] == ["kimi-k3", "glm-5.3", "mimo-v2.6-pro"]
    m = bot._model_by_id("mimo-v2.6-pro")
    assert m["provider"] == "mimo"
    assert m["api_model"] == "mimo-v2.6-pro"
    assert m["effort"] is None
    assert "mimo" in bot._OPENAI_PROVIDERS


def test_every_openai_model_has_a_provider_entry():
    for m in bot.AVAILABLE_MODELS:
        if m["provider"] != "anthropic":
            assert m["provider"] in bot._OPENAI_PROVIDERS, m["id"]
            assert m.get("api_model") or m.get("haihub_id"), m["id"]


def test_model_ids_unique():
    ids = [m["id"] for m in bot.AVAILABLE_MODELS]
    assert len(ids) == len(set(ids))


def test_mimo_key_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(bot, "_MIMO_KEY_FILE", tmp_path / "absent")
    assert bot._resolve_mimo_key() is None
    (tmp_path / "k").write_text("file-key\n")
    monkeypatch.setattr(bot, "_MIMO_KEY_FILE", tmp_path / "k")
    assert bot._resolve_mimo_key() == "file-key"
    monkeypatch.setenv("MIMO_API_KEY", "env-key")
    assert bot._resolve_mimo_key() == "env-key"


def test_mimo_turn_without_key_fails_closed(monkeypatch):
    monkeypatch.setitem(
        bot._OPENAI_PROVIDERS["mimo"], "key", lambda: None,
    )
    route = bridge_account_router.RouteDecision(
        name="mimo-v2.6-pro", home_path=bridge_account_router.MAIN_HOME,
        switched=False, previous=None, provider="mimo",
    )
    text, _ = asyncio.run(bot._run_haihub(
        "hi", route, "mimo-v2.6-pro", label="MiMo V2.6 Pro",
        emergency=False, provider="mimo",
    ))
    assert "not configured" in text
