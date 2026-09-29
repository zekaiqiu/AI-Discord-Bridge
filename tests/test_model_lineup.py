"""Bridge model table (!model) — lineup invariants.

MiMo V2.6 Pro (Xiaomi) was added on 2026-09-28 and parked the same day (no key
with MiMo scope). Re-added 2026-09-29 with MiMo V2.6 Flash, both on the Xiaomi
Token Plan endpoint.
"""
from __future__ import annotations

import asyncio

import bot
import bridge_account_router


def test_mimo_pro_and_flash_follow_tokenhub_models():
    ids = [m["id"] for m in bot.AVAILABLE_MODELS]
    assert ids[:4] == ["kimi-k3", "glm-5.3", "mimo-v2.6-pro", "mimo-v2.6-flash"]
    for mid in ("mimo-v2.6-pro", "mimo-v2.6-flash"):
        m = bot._model_by_id(mid)
        assert m["provider"] == "mimo" and m["api_model"] == mid
        assert m["effort"] == ["low", "medium", "high"]  # "max" -> HTTP 400


def test_persisted_mimo_choice_is_kept(monkeypatch, tmp_path):
    f = tmp_path / "model"
    f.write_text("mimo-v2.6-flash", encoding="utf-8")
    monkeypatch.setattr(bot, "BRIDGE_MODEL_FILE", f)
    assert bot.current_model()["id"] == "mimo-v2.6-flash"


def test_mimo_default_base_url_is_token_plan():
    import os
    if not os.environ.get("MIMO_BASE_URL"):
        assert bot._MIMO_BASE_URL == "https://token-plan-sgp.xiaomimimo.com/v1"


def ids_of(models):
    return [m["id"] for m in models]


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


def test_mimo_endpoint_falls_back_to_tokenhub(monkeypatch, tmp_path):
    """No MiMo key but a TokenHub key -> the turn goes to TokenHub's endpoint
    with that key (TokenHub lists mimo-v2.6-pro; only the key scope gates it)."""
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
    monkeypatch.setattr(bot, "_MIMO_KEY_FILE", tmp_path / "absent")
    (tmp_path / "glm").write_text("sk-tp-plan\n")
    monkeypatch.setattr(bot, "_TOKENHUB_KEY_FILE", tmp_path / "glm")
    assert bot._resolve_mimo_endpoint() == (bot._TOKENHUB_BASE_URL, "sk-tp-plan")
    prov = bot._OPENAI_PROVIDERS["mimo"]
    assert prov["key"]() == "sk-tp-plan"
    assert prov["base_url"]() == bot._TOKENHUB_BASE_URL
    # explicit MiMo key wins
    monkeypatch.setenv("MIMO_API_KEY", "xiaomi-key")
    assert bot._resolve_mimo_endpoint() == (bot._MIMO_BASE_URL, "xiaomi-key")
    # neither -> (None, None) and the loop fails closed
    monkeypatch.delenv("MIMO_API_KEY")
    monkeypatch.setattr(bot, "_TOKENHUB_KEY_FILE", tmp_path / "absent2")
    assert bot._resolve_mimo_endpoint() == (None, None)


def test_kimi_runs_on_kimi_code_plan():
    """Kimi K3 moved from TokenHub to the Kimi Code plan key on 2026-09-29;
    the id stays "kimi-k3" so persisted picks survive."""
    import os
    m = bot._model_by_id("kimi-k3")
    assert m["provider"] == "kimi"
    assert m["api_model"] == os.environ.get("KIMI_MODEL", "k3")
    assert m["effort"] == ["low", "high", "max"]
    if not os.environ.get("KIMI_BASE_URL"):
        assert bot._OPENAI_PROVIDERS["kimi"]["base_url"] == "https://api.kimi.ai/coding/v1"
    assert bot._OPENAI_PROVIDERS["kimi"]["key"] is bot._resolve_kimi_key


def test_kimi_key_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setattr(bot, "_KIMI_KEY_FILE", tmp_path / "absent")
    assert bot._resolve_kimi_key() is None
    (tmp_path / "k").write_text("sk-kimi-file\n")
    monkeypatch.setattr(bot, "_KIMI_KEY_FILE", tmp_path / "k")
    assert bot._resolve_kimi_key() == "sk-kimi-file"
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-env")
    assert bot._resolve_kimi_key() == "sk-kimi-env"


def test_stale_kimi_medium_effort_is_dropped(monkeypatch, tmp_path):
    f = tmp_path / "effort.json"
    f.write_text('{"kimi-k3": "medium"}', encoding="utf-8")
    monkeypatch.setattr(bot, "BRIDGE_EFFORT_FILE", f)
    assert bot.current_effort(bot._model_by_id("kimi-k3")) is None
