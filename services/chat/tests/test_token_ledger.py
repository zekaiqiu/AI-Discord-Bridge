"""Token ledger (2026-09-29): one row per model API call and one per turn, for
both the OpenAI-compatible runners and the claude CLI stream, attributed to
the turn bound in the task context.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import haihub_runner
import token_ledger


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    monkeypatch.setenv("TOKEN_LEDGER_DB", str(db))
    monkeypatch.setenv("TOKEN_LEDGER_PRICES", str(tmp_path / "prices.json"))
    token_ledger._current.set(None)

    def rows(table="calls"):
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    rows.path = tmp_path
    return rows


# --- normalisation / pricing -------------------------------------------------------

def test_openai_usage_splits_cached_and_reasoning():
    n = token_ledger.normalize_openai_usage({
        "prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300,
        "prompt_tokens_details": {"cached_tokens": 800},
        "completion_tokens_details": {"reasoning_tokens": 200},
    })
    assert n == {"input_tokens": 200, "cache_read_tokens": 800, "cache_write_tokens": 0,
                 "output_tokens": 300, "reasoning_tokens": 200, "total_tokens": 1300}


def test_openai_usage_top_level_cached_tokens():
    n = token_ledger.normalize_openai_usage(
        {"prompt_tokens": 50, "completion_tokens": 5, "cached_tokens": 40})
    assert n["input_tokens"] == 10 and n["cache_read_tokens"] == 40
    assert n["total_tokens"] == 55


def test_anthropic_usage_total_includes_cache_buckets():
    n = token_ledger.normalize_anthropic_usage({
        "input_tokens": 3, "output_tokens": 400,
        "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 50000})
    assert n["total_tokens"] == 52403 and n["cache_read_tokens"] == 50000


def test_price_lookup_handles_suffixes_and_override(ledger):
    assert token_ledger.price_for("claude-opus-5-5[1m]") == token_ledger.PRICES["claude-opus-5-5"]
    assert token_ledger.price_for("claude-haiku-4-5-20251001") == token_ledger.PRICES["claude-haiku-4-5"]
    assert token_ledger.price_for("glm-5.3") is None
    (ledger.path / "prices.json").write_text(json.dumps({"GLM-5.3": [1, 2]}))
    assert token_ledger.price_for("glm-5.3") == (1.0, 2.0, 0.0, 0.0)
    assert token_ledger.cost_usd("glm-5.3", 1_000_000, 500_000, 0, 0) == pytest.approx(2.0)


# --- OpenAI-compatible stream step ------------------------------------------------

class _Resp:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self.status_code = status

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b'{"error":"nope"}'


class _Stream:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self, chunks, status=200):
        self.resp = _Resp(chunks, status)

    def stream(self, *a, **kw):
        return _Stream(self.resp)


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _step(client, payload, base_url="https://tokenhub.example/v1"):
    async def go():
        return [ev async for ev in haihub_runner._stream_step(
            client, payload, base_url=base_url, api_key="k")]
    return asyncio.run(go())


def test_stream_step_records_one_row_with_real_usage(ledger):
    chunks = [
        _sse({"choices": [{"delta": {"reasoning_content": "hmm"}}]}),
        _sse({"choices": [{"delta": {"content": "hello"}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
              "function": {"name": "run_bash", "arguments": "{}"}}]},
              "finish_reason": "tool_calls"}]}),
        _sse({"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30,
              "prompt_tokens_details": {"cached_tokens": 100}}}),
        b"data: [DONE]\n\n",
    ]
    payload = {"model": "glm-5.3", "messages": [{"role": "user", "content": "x" * 40}],
               "tools": [{}], "reasoning_effort": "high", "max_tokens": 64000}

    async def go():
        ctx = token_ledger.begin_turn(app="chat", user="a@b", session="s1", turn_id="t1")
        evs = [ev async for ev in haihub_runner._stream_step(
            _Client(chunks), payload, base_url="https://tokenhub.example/v1", api_key="k")]
        token_ledger.end_turn("ok", ctx)
        return evs
    asyncio.run(go())

    (r,) = ledger()
    assert (r["app"], r["user"], r["session"], r["turn_id"], r["call_idx"]) == ("chat", "a@b", "s1", "t1", 1)
    assert (r["provider"], r["model"], r["effort"], r["purpose"]) == ("tokenhub", "glm-5.3", "high", "tool_step")
    assert (r["input_tokens"], r["cache_read_tokens"], r["output_tokens"], r["total_tokens"]) == (20, 100, 30, 150)
    assert r["estimated"] == 0 and r["status"] == "ok" and r["finish_reason"] == "tool_calls"
    assert r["tool_calls"] == 1 and r["out_chars"] == 5 and r["reasoning_chars"] == 3
    assert r["request_chars"] == 40 and r["request_messages"] == 1 and r["max_tokens"] == 64000
    assert r["ttft_ms"] is not None and r["latency_ms"] is not None
    (t,) = ledger("turns")
    assert (t["status"], t["n_calls"], t["n_tool_calls"], t["total_tokens"]) == ("ok", 1, 1, 150)
    assert t["ts_end"] is not None


def test_stream_step_without_usage_is_estimated(ledger):
    chunks = [_sse({"choices": [{"delta": {"content": "y" * 400}}]}), b"data: [DONE]\n\n"]
    _step(_Client(chunks), {"model": "m", "messages": [{"role": "user", "content": "x" * 800}]})
    (r,) = ledger()
    assert r["estimated"] == 1 and r["input_tokens"] == 200 and r["output_tokens"] == 100
    assert r["turn_id"] is None  # unattributed but still recorded


def test_stream_step_http_error_records_error_with_zero_tokens(ledger):
    _step(_Client([], status=429), {"model": "m", "messages": [{"role": "user", "content": "x" * 800}]})
    (r,) = ledger()
    assert r["status"] == "error" and "429" in r["error"] and r["total_tokens"] == 0


def test_cancelled_step_after_end_turn_keeps_turn_status(ledger):
    """A step closed after end_turn (async-gen GC) is still attributed to its
    turn and must not flip the turn back to 'running'."""
    chunks = [_sse({"choices": [{"delta": {"content": "a" * 80}}]})] * 5 + [b"data: [DONE]\n\n"]

    async def go():
        ctx = token_ledger.begin_turn(app="chat", turn_id="t2")
        agen = haihub_runner._stream_step(_Client(chunks), {"model": "m", "messages": []},
                                          base_url="http://haihub.x", api_key="k")
        await agen.__anext__()
        token_ledger.end_turn("cancelled", ctx)
        await agen.aclose()
    asyncio.run(go())
    (r,) = ledger()
    assert r["status"] == "cancelled" and r["turn_id"] == "t2" and r["estimated"] == 1
    (t,) = ledger("turns")
    assert t["status"] == "cancelled" and t["n_calls"] == 1


# --- claude CLI stream ---------------------------------------------------------------

def _claude_frames():
    u0 = {"input_tokens": 5, "cache_creation_input_tokens": 1000,
          "cache_read_input_tokens": 20000, "output_tokens": 1}
    return [
        {"type": "stream_event", "event": {"type": "message_start", "message": {
            "id": "msg_1", "model": "claude-opus-5-5", "usage": u0}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "hi there"}}},
        # The CLI repeats the message per content block with partial usage.
        {"type": "assistant", "message": {"id": "msg_1", "model": "claude-opus-5-5",
                                          "usage": u0, "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": u0,
                                          "content": [{"type": "tool_use", "id": "tu1"}]}},
        {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                           "usage": {"output_tokens": 250}}},
        # Subagent message: only assistant frames.
        {"type": "assistant", "parent_tool_use_id": "tu1", "message": {
            "id": "msg_sub", "model": "claude-haiku-4-5-20251001",
            "usage": {"input_tokens": 900, "output_tokens": 80}}},
        {"type": "result", "total_cost_usd": 0.12,
         "usage": {"input_tokens": 905, "output_tokens": 330}},
    ]


def test_claude_tracker_dedups_messages_and_tags_subagents(ledger):
    async def go():
        ctx = token_ledger.begin_turn(app="bridge", user="z3kai", turn_id="t3")
        tr = token_ledger.ClaudeStreamTracker()
        for f in _claude_frames():
            tr.feed(f)
        tr.flush()
        token_ledger.end_turn("ok", ctx)
    asyncio.run(go())
    main, sub = ledger()
    assert main["model"] == "claude-opus-5-5" and main["purpose"] == "claude_msg"
    assert (main["input_tokens"], main["cache_write_tokens"], main["cache_read_tokens"],
            main["output_tokens"]) == (5, 1000, 20000, 250)
    assert main["tool_calls"] == 1 and main["finish_reason"] == "tool_use"
    assert main["cost_usd"] == pytest.approx((5 * 4 + 250 * 20 + 20000 * 0.2 + 1000 * 5) / 1e6)
    assert sub["purpose"] == "subagent" and sub["output_tokens"] == 80
    (t,) = ledger("turns")
    assert t["n_calls"] == 2 and t["cli_cost_usd"] == pytest.approx(0.12)
    assert t["output_tokens"] == 330 and t["total_tokens"] == 5 + 1000 + 20000 + 250 + 980


def test_claude_tracker_falls_back_to_result_usage(ledger):
    tr = token_ledger.ClaudeStreamTracker()
    tr.feed({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 2}})
    tr.flush("ok", model_hint="claude-sonnet-5")
    tr.flush("ok")  # idempotent
    (r,) = ledger()
    assert r["purpose"] == "turn_aggregate" and r["total_tokens"] == 12 and r["model"] == "claude-sonnet-5"


def test_ledger_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_LEDGER_DB", "off")
    assert token_ledger.connect() is None
    token_ledger.record_call(provider="p", model="m", usage={"total_tokens": 1})  # no raise


def test_one_hour_cache_writes_bill_at_twice_input():
    """Real Fable 5.1 frame from 2026-09-29; the CLI reported $0.2080185."""
    n = token_ledger.normalize_anthropic_usage({
        "input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 10234,
        "cache_creation_input_tokens": 10262,
        "cache_creation": {"ephemeral_1h_input_tokens": 10262, "ephemeral_5m_input_tokens": 0}})
    assert n["cache_write_1h_tokens"] == 10262
    c = token_ledger.cost_usd("claude-fable-5-1", n["input_tokens"], n["output_tokens"],
                              n["cache_read_tokens"], n["cache_write_tokens"],
                              n["cache_write_1h_tokens"])
    assert c == pytest.approx(0.2080185, abs=1e-9)
