"""Kimi K3 is the default model and falls back from the Kimi Code plan key to
the TokenHub key (kimi-k3) when the primary hits a limit (2026-09-29).
Hermetic: scripted _stream_step, tmp key files, no network."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import haihub_runner
import kimi_runner
import storage
import tokenhub_runner


@pytest.fixture
def keys(monkeypatch, tmp_path):
    kf, tf = tmp_path / "kimi", tmp_path / "th"
    kf.write_text("sk-kimi")
    tf.write_text("sk-th")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
    monkeypatch.setattr(kimi_runner, "_KIMI_KEY_FILE", kf)
    monkeypatch.setattr(tokenhub_runner, "_TOKENHUB_KEY_FILE", tf)
    monkeypatch.setattr(kimi_runner, "_primary_down_until", 0.0)
    return kf, tf


def _script(monkeypatch, steps):
    """Each step: {"error": msg} or {"chunks": [...]}. Records (base, key, model)."""
    queue, seen = list(steps), []

    async def fake_stream_step(client, payload, *, base_url="", api_key=""):
        seen.append((base_url, api_key, payload["model"]))
        step = queue.pop(0)
        if "error" in step:
            yield {"type": "_error", "message": step["error"]}
            return
        for c in step["chunks"]:
            yield {"type": "delta", "text": c}
        yield {"type": "_meta", "tool_calls": [], "content": "".join(step["chunks"]),
               "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "finish_reason": "stop"}

    monkeypatch.setattr(haihub_runner, "_stream_step", fake_stream_step)
    return seen


def _collect():
    async def go():
        return [e async for e in kimi_runner.run_turn(
            prompt="hi", model="kimi", container="c1", chat_session_id="s", effort="max")]
    return asyncio.run(go())


def test_kimi_is_the_default_model():
    assert storage.DEFAULT_SETTINGS["default_model"] == "kimi"


@pytest.mark.parametrize("msg", [
    "haihub HTTP 429: rate limit exceeded",
    "haihub HTTP 403: membership quota used up",
    "haihub HTTP 401: invalid api key",
    "provider error: usage limit reached for this plan",
])
def test_limit_errors_are_detected(msg):
    assert haihub_runner._is_limit_error(msg)


@pytest.mark.parametrize("msg", [
    "haihub HTTP 400: context length exceeded, maximum context is 262144",
    "haihub HTTP 500: internal error",
    "request failed: ReadTimeout",
])
def test_other_errors_do_not_fail_over(msg):
    assert not haihub_runner._is_limit_error(msg)


def test_limit_error_fails_over_to_tokenhub_and_cools_down(keys, monkeypatch):
    seen = _script(monkeypatch, [
        {"error": "haihub HTTP 429: usage limit reached"},
        {"chunks": ["answer"]},
    ])
    events = _collect()
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"] == "answer"
    assert not [e for e in events if e["type"] == "error"]
    assert seen[0] == (kimi_runner.KIMI_BASE_URL, "sk-kimi", "k3")
    assert seen[1] == (tokenhub_runner.TOKENHUB_BASE_URL.rstrip("/"), "sk-th", "kimi-k3")
    # The next turn skips Kimi Code while it cools down.
    seen2 = _script(monkeypatch, [{"chunks": ["again"]}])
    _collect()
    assert seen2[0][1] == "sk-th"


def test_primary_is_retried_after_cooldown(keys, monkeypatch):
    monkeypatch.setattr(kimi_runner, "_primary_down_until", 0.0)
    primary, fallback = kimi_runner.resolve_endpoints()
    assert primary["api_key"] == "sk-kimi" and fallback["api_key"] == "sk-th"


def test_non_limit_error_does_not_fail_over(keys, monkeypatch):
    seen = _script(monkeypatch, [{"error": "haihub HTTP 500: boom"}])
    events = _collect()
    assert [e for e in events if e["type"] == "error"]
    assert len(seen) == 1
    assert kimi_runner._primary_down_until == 0.0


def test_fallback_errors_surface_without_looping(keys, monkeypatch):
    seen = _script(monkeypatch, [
        {"error": "haihub HTTP 429: quota"},
        {"error": "haihub HTTP 429: quota"},
    ])
    events = _collect()
    assert [e for e in events if e["type"] == "error"]
    assert len(seen) == 2


def test_no_kimi_code_key_goes_straight_to_tokenhub(keys, monkeypatch):
    keys[0].unlink()
    seen = _script(monkeypatch, [{"chunks": ["ok"]}])
    _collect()
    assert seen[0][1:] == ("sk-th", "kimi-k3")


def test_no_keys_at_all_errors(keys):
    keys[0].unlink(); keys[1].unlink()
    assert _collect() == [{"type": "error", "message": kimi_runner.NOT_CONFIGURED_MESSAGE}]
