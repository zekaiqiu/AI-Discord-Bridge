"""Kimi K3 runs on the Kimi Code plan key (kimi_runner), not TokenHub,
since 2026-09-29. Hermetic: fake runner / key files, no network."""

from __future__ import annotations

from typing import Any

import asyncio

import app as app_module
import haihub_runner
import kimi_runner
import tokenhub_runner
from helpers import consume_sse, create_session

USER_A = "alice@example.com"


def _install_fake(monkeypatch: Any, module: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {"calls": []}

    async def fake_run_turn(**kwargs: Any):
        captured["calls"].append(kwargs)
        yield {"type": "delta", "text": "hi"}
        yield {"type": "done", "full_text": "hi", "meta": {"model": kwargs.get("model")}}

    monkeypatch.setattr(module, "run_turn", fake_run_turn)
    return captured


def test_kimi_is_not_a_tokenhub_model():
    assert kimi_runner.is_kimi_model("kimi")
    assert not tokenhub_runner.is_tokenhub_model("kimi")
    assert tokenhub_runner.is_tokenhub_model("glm")
    assert app_module._is_api_model("kimi")
    assert app_module._ledger_provider("kimi") == "kimi"


def test_ledger_provider_tag_from_url():
    assert haihub_runner.provider_of(kimi_runner.KIMI_BASE_URL) == "kimi"


def test_kimi_turn_routes_to_kimi_runner(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    kimi = _install_fake(monkeypatch, kimi_runner)
    th = _install_fake(monkeypatch, tokenhub_runner)
    headers = auth_headers(USER_A)
    sid = create_session(client, headers)
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping", "model": "kimi", "effort": "max"},
    )
    assert resp.status_code == 200
    consume_sse(resp)
    assert th["calls"] == []
    assert kimi["calls"][0]["model"] == "kimi"
    assert kimi["calls"][0]["effort"] == "max"


def test_resolve_key_env_then_file(monkeypatch, tmp_path):
    kf = tmp_path / "kimi_key"
    monkeypatch.setattr(kimi_runner, "_KIMI_KEY_FILE", kf)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    assert kimi_runner.resolve_key() is None
    kf.write_text("sk-kimi-file\n")
    assert kimi_runner.resolve_key() == "sk-kimi-file"
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-env")
    assert kimi_runner.resolve_key() == "sk-kimi-env"


def test_run_turn_passes_endpoint_and_model_map(monkeypatch, tmp_path):
    kf = tmp_path / "kimi_key"
    kf.write_text("sk-kimi-x")
    monkeypatch.setattr(kimi_runner, "_KIMI_KEY_FILE", kf)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any):
        seen.update(kwargs)
        yield {"type": "done", "full_text": "", "meta": {}}

    monkeypatch.setattr(haihub_runner, "run_turn", fake)
    async def collect():
        return [e async for e in kimi_runner.run_turn(model="kimi", effort="high")]

    evs = asyncio.run(collect())
    assert evs[-1]["type"] == "done"
    assert seen["base_url"] == kimi_runner.KIMI_BASE_URL
    assert seen["api_key"] == "sk-kimi-x"
    assert seen["models_map"] == {"kimi": "k3"}
    assert seen["effort"] == "high"


def test_run_turn_without_key_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(kimi_runner, "_KIMI_KEY_FILE", tmp_path / "absent")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setattr(tokenhub_runner, "_TOKENHUB_KEY_FILE", tmp_path / "absent-th")
    monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
    async def collect():
        return [e async for e in kimi_runner.run_turn(model="kimi")]

    evs = asyncio.run(collect())
    assert evs == [{"type": "error", "message": kimi_runner.NOT_CONFIGURED_MESSAGE}]
