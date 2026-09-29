"""Regressions from the 2026-09-29 review of the day's chat changes:
<think> reasoning leaking on a degenerate cut, overflow misdetection,
tool-call-argument compaction, the wrap-up surviving an overflow retry,
and the run event log coalescing chunks.
"""
from __future__ import annotations

import asyncio
import json

import app
import haihub_runner
from test_repetition_guard import (
    TOOL, _FakeClient, _chunked, _collect, _collect_step, _delta, _script, loop_text,
)


def test_loop_inside_unclosed_think_does_not_leak():
    chunks = [_delta(content="Answer prefix. <think>")]
    chunks += [_delta(content=c) for c in _chunked(loop_text(40000), 70)]
    chunks.append(b"data: [DONE]\n\n")
    events = _collect_step(_FakeClient(chunks))
    meta = [e for e in events if e["type"] == "_meta"][0]
    assert meta["finish_reason"] == "repetition"
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    for text in (meta["content"], streamed):
        assert "<think>" not in text and "GO." not in text
        assert text.startswith("Answer prefix.")


def test_overflow_detector_is_specific():
    f = haihub_runner._is_context_overflow
    assert f("haihub HTTP 400 — maximum context length exceeded")
    assert f("provider error: input length too long")
    assert f("haihub HTTP 413 — prompt is too long")
    assert not f("provider error: rate limit exceeds quota")
    assert not f("haihub HTTP 400 — max_tokens exceeds model maximum")


def test_compaction_elides_old_tool_call_arguments_keeping_pairing():
    big = json.dumps({"command": "cat > f <<'EOF'\n" + "x" * 50000 + "\nEOF"})
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i in range(12):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "run_bash", "arguments": big}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    haihub_runner._compact_tool_history(msgs, 450000, keep_recent=8)
    assert sum(haihub_runner._message_chars(m) for m in msgs) <= 450000
    ids = [tc["id"] for m in msgs if m.get("tool_calls") for tc in m["tool_calls"]]
    assert ids == [f"c{i}" for i in range(12)]
    # Newest calls untouched, oldest stubbed.
    assert msgs[-2]["tool_calls"][0]["function"]["arguments"] == big
    assert "elided" in json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"])


def test_wrap_up_survives_an_overflow_retry(monkeypatch):
    queue = [
        {"chunks": ["Start. "], "finish": "repetition", "degenerate": "content"},
        {"error": "haihub HTTP 400 — maximum context length exceeded"},
        {"chunks": ["Wrapped up."]},
    ]
    payloads = []

    async def fake_stream_step(client, payload, *, base_url="", api_key=""):
        payloads.append(json.loads(json.dumps(payload)))
        step = queue.pop(0)
        if "error" in step:
            yield {"type": "_error", "message": step["error"]}
            return
        for c in step["chunks"]:
            yield {"type": "delta", "text": c}
        meta = {"type": "_meta", "tool_calls": [], "content": "".join(step["chunks"]),
                "usage": {}, "finish_reason": step.get("finish", "stop")}
        if step.get("degenerate"):
            meta["degenerate"] = step["degenerate"]
        yield meta

    _script(monkeypatch, [])
    monkeypatch.setattr(haihub_runner, "_stream_step", fake_stream_step)
    events = _collect(effort="max")
    assert [e for e in events if e["type"] == "done"]
    for p in payloads[1:]:
        assert p["reasoning_effort"] == haihub_runner._RETRY_EFFORT
        assert "tools" not in p
        assert p["messages"][-1]["content"] == haihub_runner._REPETITION_RETRY_NUDGE


def test_run_log_coalesces_chunks_and_caps_reasoning(monkeypatch):
    monkeypatch.setattr(app, "REASONING_PERSIST_CAP", 100)

    async def go():
        run = app._TurnRun(email="e", session_id="s", assistant_seq=1)
        _, q = await run.attach()
        for _ in range(50):
            await run.emit({"type": "reasoning", "text": "abcde"})
        for _ in range(3):
            await run.emit({"type": "delta", "text": "hi "})
        await run.emit({"type": "tool_start", "name": "x"})
        await run.emit({"type": "delta", "text": "end"})
        return run, q

    run, q = asyncio.run(go())
    assert [e["type"] for e in run.events] == ["reasoning", "delta", "tool_start", "delta"]
    assert 100 <= len(run.events[0]["text"]) <= 110
    assert run.events[1]["text"] == "hi hi hi "
    assert q.qsize() == 55  # live subscribers still get every event
