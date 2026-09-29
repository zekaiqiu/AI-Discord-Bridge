"""Long agent sessions on the bridge (2026-09-29): no wall-clock turn limit by
default (a silence watchdog instead), an hour per shell command, and in-turn
context compaction for the GLM/Kimi tool loop."""
from __future__ import annotations

import bot


def test_defaults_do_not_cut_long_turns(monkeypatch):
    # The module default; the systemd override is set to 0 as well.
    import importlib, os
    monkeypatch.delenv("CLAUDE_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("CLAUDE_SILENCE_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("BRIDGE_TOOL_TIMEOUT_SEC", raising=False)
    src = open(bot.__file__, encoding="utf-8").read()
    assert 'os.environ.get("CLAUDE_RUN_TIMEOUT_SEC", "0")' in src
    assert 'os.environ.get("BRIDGE_TOOL_TIMEOUT_SEC", "3600")' in src
    assert bot.CLAUDE_SILENCE_TIMEOUT_SEC >= 3600


def _history(n_tools: int, size: int = 16000):
    msgs = [{"role": "system", "content": "S" * 40000}, {"role": "user", "content": "U" * 40000}]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": f"step {i}", "reasoning_content": "r" * 5000,
                     "tool_calls": [{"id": str(i), "type": "function",
                                     "function": {"name": "run_bash", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": "o" * size})
    return msgs


def test_compaction_bounds_context_and_keeps_recent_results():
    msgs = _history(300)
    elided = bot._qwen_compact_history(msgs, bot._QWEN_CONTEXT_CHAR_BUDGET)
    assert elided > 0
    assert sum(bot._qwen_msg_chars(m) for m in msgs) <= bot._QWEN_CONTEXT_CHAR_BUDGET
    tools = [m for m in msgs if m["role"] == "tool"]
    assert all(m["content"] == "o" * 16000 for m in tools[-bot._QWEN_CONTEXT_KEEP_RECENT:])
    assert tools[0]["content"].startswith("[earlier tool output elided")
    # Prompts and the model's own narration are never touched.
    assert msgs[0]["content"] == "S" * 40000 and msgs[1]["content"] == "U" * 40000
    assert all(m["content"].startswith("step ") for m in msgs if m["role"] == "assistant")


def test_compaction_is_a_no_op_within_budget():
    msgs = _history(3)
    assert bot._qwen_compact_history(msgs, 10**9) == 0


def test_overflow_detector():
    f = bot._qwen_is_overflow
    assert f("tokenhub HTTP 400: maximum context length is 131072 tokens")
    assert f("haihub HTTP 413: input length too long")
    assert not f("tokenhub HTTP 401: invalid key")
    assert not f("tokenhub HTTP 429: rate limited")
