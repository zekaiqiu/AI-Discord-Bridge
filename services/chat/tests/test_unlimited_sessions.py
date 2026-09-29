"""Long agent sessions (2026-09-29): no tool-call cap by default, an hour per
command, and in-turn context compaction so an unlimited tool loop can neither
overflow the model's context nor end on a provider length error."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import haihub_runner


def test_defaults_are_unlimited_steps_and_long_commands():
    assert haihub_runner._MAX_STEPS == 0
    assert haihub_runner._TOOL_TIMEOUT >= 3600


def _script(monkeypatch, steps, *, errors=None):
    """Scripted model steps. ``errors`` maps a call index to an error message
    returned for that call instead of a step."""
    queue = list(steps)
    calls: list[dict[str, Any]] = []
    errors = dict(errors or {})

    async def fake_stream_step(client, payload, *, base_url="", api_key=""):
        idx = len(calls)
        calls.append(json.loads(json.dumps(payload)))
        if idx in errors:
            yield {"type": "_error", "message": errors[idx]}
            return
        step = queue.pop(0)
        for c in step.get("chunks", []):
            yield {"type": "delta", "text": c}
        yield {
            "type": "_meta",
            "tool_calls": step.get("tool_calls", []),
            "content": "".join(step.get("chunks", [])),
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "finish_reason": "tool_calls" if step.get("tool_calls") else "stop",
        }

    async def fake_exec(container, command, *, workdir="/workspace", home="/workspace"):
        return "x" * 16000  # a full-size tool result every call

    monkeypatch.setattr(haihub_runner, "_stream_step", fake_stream_step)
    monkeypatch.setattr(haihub_runner, "_exec_bash", fake_exec)
    return calls


def _collect():
    async def go():
        return [ev async for ev in haihub_runner.run_turn(
            prompt="hi", model="qwen", container="c1", api_key="k", chat_session_id="s",
        )]
    return asyncio.run(go())


def _tool(i):
    return [{"id": f"call_{i}", "name": "run_bash", "args": json.dumps({"command": f"echo {i}"})}]


def test_three_hundred_tool_calls_run_to_completion(monkeypatch):
    n = 300
    steps = [{"chunks": [f"step {i}. "], "tool_calls": _tool(i)} for i in range(n)]
    steps.append({"chunks": ["All done."]})
    calls = _script(monkeypatch, steps)
    events = _collect()
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"].endswith("All done.")
    assert "tool-call limit" not in done["full_text"]
    assert sum(1 for e in events if e["type"] == "tool_start") == n
    # The context stayed bounded the whole way: every request fits the budget
    # (plus the fixed system/user prompt), even with 300 x 16k of tool output.
    sizes = [sum(haihub_runner._message_chars(m) for m in c["messages"]) for c in calls]
    assert max(sizes) <= haihub_runner._CONTEXT_CHAR_BUDGET + 20000, max(sizes)
    # The newest results are always intact.
    last = calls[-1]["messages"]
    tools = [m for m in last if m.get("role") == "tool"]
    assert all(m["content"] == "x" * 16000 for m in tools[-haihub_runner._CONTEXT_KEEP_RECENT_TOOLS:])
    assert tools[0]["content"].startswith("[earlier tool output elided")


def test_context_overflow_error_compacts_and_retries_the_step(monkeypatch):
    steps = [{"chunks": ["a. "], "tool_calls": _tool(i)} for i in range(12)] + [{"chunks": ["fin."]}]
    calls = _script(
        monkeypatch, steps,
        errors={10: "haihub HTTP 400 — This model's maximum context length is 131072 tokens"},
    )
    events = _collect()
    assert not [e for e in events if e["type"] == "error"]
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"].endswith("fin.")
    # The retried request is smaller than the rejected one.
    before = sum(haihub_runner._message_chars(m) for m in calls[10]["messages"])
    after = sum(haihub_runner._message_chars(m) for m in calls[11]["messages"])
    assert after < before


def test_non_context_errors_still_end_the_turn(monkeypatch):
    _script(monkeypatch, [{"chunks": ["x"]}], errors={0: "haihub HTTP 401 — invalid key"})
    events = _collect()
    assert [e for e in events if e["type"] == "error"][0]["message"].startswith("haihub HTTP 401")


def test_compaction_never_touches_prompts_or_recent_results():
    msgs = [{"role": "system", "content": "S" * 50000}, {"role": "user", "content": "U" * 50000}]
    for i in range(20):
        msgs.append({"role": "assistant", "content": f"n{i}", "tool_calls": [
            {"id": str(i), "type": "function", "function": {"name": "run_bash", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": "r" * 16000})
    elided = haihub_runner._compact_tool_history(msgs, 200000, keep_recent=4)
    assert elided > 0
    assert msgs[0]["content"] == "S" * 50000 and msgs[1]["content"] == "U" * 50000
    tools = [m for m in msgs if m["role"] == "tool"]
    assert all(m["content"] == "r" * 16000 for m in tools[-4:])
    assert all(m["content"].startswith("n") for m in msgs if m["role"] == "assistant")
    assert haihub_runner._compact_tool_history(msgs, 10**9) == 0


def test_overflow_detector():
    f = haihub_runner._is_context_overflow
    assert f("haihub HTTP 400 — maximum context length exceeded")
    assert f("provider error: input length too long")
    assert not f("haihub HTTP 401 — invalid api key")
    assert not f("haihub HTTP 429 — rate limited")
