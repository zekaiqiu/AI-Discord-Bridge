"""Bridge side of the token ledger (2026-09-29): the GLM/Kimi/MiMo stream
step records one row per call with the provider's usage, run_claude binds a
bridge turn, and token_report renders and alerts from the ledger."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import bot
import token_report


class _Resp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _Stream:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self, lines):
        self.resp = _Resp(lines)
        self.payloads = []

    def stream(self, *a, json=None, **kw):
        self.payloads.append(json)
        return _Stream(self.resp)


def _d(obj):
    return "data: " + json.dumps(obj)


def _rows(table="calls"):
    import os
    c = sqlite3.connect(os.environ["TOKEN_LEDGER_DB"])
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(f"SELECT * FROM {table}")]


def test_run_haihub_requests_usage_and_records_each_call(monkeypatch):
    lines = [
        _d({"choices": [{"delta": {"reasoning_content": "think"}}]}),
        _d({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}),
        _d({"choices": [], "usage": {"prompt_tokens": 5000, "completion_tokens": 700,
            "prompt_tokens_details": {"cached_tokens": 4096},
            "completion_tokens_details": {"reasoning_tokens": 500}}}),
        "data: [DONE]",
    ]
    client = _Client(lines)

    class _AC:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return client

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(bot.httpx, "AsyncClient", _AC)
    monkeypatch.setitem(bot._OPENAI_PROVIDERS, "tokenhub", {
        **bot._OPENAI_PROVIDERS["tokenhub"], "key": lambda: "k",
        "base_url": "https://tokenhub.example/v1"})
    monkeypatch.setattr(bot, "current_model", lambda: {
        "id": "glm-5.3", "provider": "tokenhub", "api_model": "glm-5.3",
        "label": "GLM", "haihub_id": "glm-5.3"})
    monkeypatch.setattr(bot, "current_effort", lambda m: "high")

    async def go():
        route = bot.bridge_account_router.RouteDecision(
            name="glm-5.3", home_path=bot.bridge_account_router.MAIN_HOME,
            switched=False, previous=None, provider="tokenhub")

        async def inner(*a, **kw):
            return await bot._run_haihub("hi", route, "glm-5.3", label="GLM",
                                         emergency=False, provider="tokenhub", effort="high")
        monkeypatch.setattr(bot, "_run_claude", inner)
        return await bot.run_claude("hi")
    out, _ = asyncio.run(go())
    assert out == "answer"
    assert client.payloads[0]["stream_options"] == {"include_usage": True}
    (r,) = _rows()
    assert (r["app"], r["provider"], r["model"], r["effort"]) == ("bridge", "tokenhub", "glm-5.3", "high")
    assert (r["input_tokens"], r["cache_read_tokens"], r["output_tokens"], r["reasoning_tokens"]) == (904, 4096, 700, 500)
    assert r["estimated"] == 0 and r["status"] == "ok" and r["turn_id"].startswith("bridge:")
    (t,) = _rows("turns")
    assert t["status"] == "ok" and t["total_tokens"] == 5700 and t["user"].startswith("discord:")

    report = token_report.render(["24h"])
    assert "bridge" in report and "glm-5.3" in report and "5.70k" in report
    assert "TURN bridge:" in token_report.render(["turn", t["turn_id"]])


def test_alerts_fire_once_per_turn(monkeypatch, tmp_path):
    import token_ledger
    monkeypatch.setattr(token_report, "TURN_TOKENS", 1000)
    ctx = token_ledger.begin_turn(app="chat", user="u@x", turn_id="big1", model="glm-5.3")
    token_ledger.record_call(provider="tokenhub", model="glm-5.3",
                             usage={"input_tokens": 1500, "output_tokens": 10,
                                    "total_tokens": 1510}, ctx=ctx)
    token_ledger.end_turn("ok", ctx)
    state = tmp_path / "alerts.json"
    first = token_report.check_alerts(state)
    assert len(first) == 1 and "big1" in first[0]
    assert token_report.check_alerts(state) == []
