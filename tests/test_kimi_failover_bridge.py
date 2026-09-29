"""Bridge: Kimi K3 is the default pick and falls back from the Kimi Code key
to the TokenHub key (kimi-k3) on a limit error (2026-09-29). Hermetic."""
from __future__ import annotations

import asyncio

import pytest

import bot
import bridge_account_router


@pytest.fixture
def keys(monkeypatch, tmp_path):
    kf, tf = tmp_path / "kimi", tmp_path / "th"
    kf.write_text("sk-kimi")
    tf.write_text("sk-th")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
    monkeypatch.setattr(bot, "_KIMI_KEY_FILE", kf)
    monkeypatch.setattr(bot, "_TOKENHUB_KEY_FILE", tf)
    monkeypatch.setattr(bot, "_kimi_primary_down_until", 0.0)
    return kf, tf


def _script(monkeypatch, results):
    """results: list of (content, error). Records (provider, base, key, model)."""
    queue, seen = list(results), []

    async def fake(client, key, payload, sink, text_parts, *, base_url, provider, reasoning_parts=None):
        seen.append((provider, base_url, key, payload["model"]))
        content, error = queue.pop(0)
        if content:
            text_parts.append(content)
        return [], content, error

    monkeypatch.setattr(bot, "_qwen_stream_step", fake)
    return seen


def _run():
    route = bridge_account_router.RouteDecision(
        name="kimi-k3", home_path=bridge_account_router.MAIN_HOME,
        switched=False, previous=None, provider="kimi")
    text, _ = asyncio.run(bot._run_haihub(
        "hi", route, "k3", label="Kimi K3", emergency=False, provider="kimi", effort="max"))
    return text


def test_kimi_is_the_default_pick(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "BRIDGE_MODEL_FILE", tmp_path / "absent")
    assert bot.current_model()["id"] == "kimi-k3"
    assert bot.AVAILABLE_MODELS[0]["id"] == "kimi-k3"


def test_limit_error_fails_over_to_tokenhub_and_cools_down(keys, monkeypatch):
    seen = _script(monkeypatch, [("", "kimi HTTP 429: usage limit reached"), ("answer", None)])
    assert _run() == "answer"
    assert seen[0] == ("kimi", bot._KIMI_BASE_URL, "sk-kimi", "k3")
    assert seen[1] == ("tokenhub", bot._TOKENHUB_BASE_URL, "sk-th", "kimi-k3")
    seen2 = _script(monkeypatch, [("again", None)])
    _run()
    assert seen2[0][0] == "tokenhub"


def test_non_limit_error_does_not_fail_over(keys, monkeypatch):
    seen = _script(monkeypatch, [("", "kimi HTTP 500: boom")])
    assert "error" in _run()
    assert len(seen) == 1 and bot._kimi_primary_down_until == 0.0


def test_fallback_failure_surfaces_once(keys, monkeypatch):
    seen = _script(monkeypatch, [("", "kimi HTTP 429: quota"), ("", "tokenhub HTTP 429: quota")])
    assert "tokenhub HTTP 429" in _run()
    assert len(seen) == 2


def test_no_kimi_code_key_uses_tokenhub(keys, monkeypatch):
    keys[0].unlink()
    seen = _script(monkeypatch, [("ok", None)])
    assert _run() == "ok"
    assert seen[0][0] == "tokenhub" and seen[0][3] == "kimi-k3"


def test_no_keys_fails_closed(keys, monkeypatch):
    keys[0].unlink(); keys[1].unlink()
    assert "unavailable" in _run()


def test_limit_detection():
    assert bot._qwen_is_limit("kimi HTTP 403: membership expired")
    assert not bot._qwen_is_limit("kimi HTTP 400: context length exceeded")
    assert not bot._qwen_is_limit("kimi HTTP 502: bad gateway")
