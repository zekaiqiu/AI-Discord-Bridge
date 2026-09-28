"""The OpenAI-compatible runner must keep the text of EVERY tool-loop step in
the final reply (2026-09-28: it kept only the last step's content, so
narration streamed between tool calls vanished on completion and a final
step with no text produced "No reply — this turn produced no text").
"""
from __future__ import annotations

import asyncio
from typing import Any

import haihub_runner


def _script(monkeypatch, steps: list[dict[str, Any]]):
    """Replace _stream_step with a scripted sequence of model steps."""
    queue = list(steps)

    async def fake_stream_step(client, payload, *, base_url="", api_key=""):
        step = queue.pop(0)
        for chunk in step.get("chunks", []):
            yield {"type": "delta", "text": chunk}
        yield {
            "type": "_meta",
            "tool_calls": step.get("tool_calls", []),
            "content": "".join(step.get("chunks", [])),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    async def fake_exec(container, command, *, workdir="/workspace", home="/workspace"):
        return "tool-ok"

    monkeypatch.setattr(haihub_runner, "_stream_step", fake_stream_step)
    monkeypatch.setattr(haihub_runner, "_exec_bash", fake_exec)
    return queue


def _collect(**kw):
    async def go():
        out = []
        async for ev in haihub_runner.run_turn(
            prompt="hi", model="qwen", container="c1", api_key="k",
            chat_session_id="s", **kw,
        ):
            out.append(ev)
        return out
    return asyncio.run(go())


TOOL = [{"id": "call_1", "name": "run_bash", "args": '{"command":"ls"}'}]


def test_multistep_reply_keeps_every_steps_text(monkeypatch):
    _script(monkeypatch, [
        {"chunks": ["Looking at ", "the files."], "tool_calls": TOOL},
        {"chunks": ["Found it. "], "tool_calls": TOOL},
        {"chunks": ["Final ", "answer."]},
    ])
    events = _collect()
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"] == "Looking at the files.\n\nFound it. \n\nFinal answer."
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert streamed == done["full_text"], "live stream and persisted reply must match"
    assert [e["type"] for e in events].count("tool_start") == 2


def test_final_step_without_text_keeps_earlier_narration(monkeypatch):
    _script(monkeypatch, [
        {"chunks": ["Checked the broker: healthy."], "tool_calls": TOOL},
        {"chunks": []},  # model ends the turn silently after the tool result
    ])
    done = [e for e in _collect() if e["type"] == "done"][0]
    assert done["full_text"] == "Checked the broker: healthy."


def test_single_step_reply_unchanged(monkeypatch):
    _script(monkeypatch, [{"chunks": ["plain ", "answer"]}])
    events = _collect()
    assert [e for e in events if e["type"] == "done"][0]["full_text"] == "plain answer"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "plain answer"


def test_tool_loop_is_capped(monkeypatch):
    monkeypatch.setattr(haihub_runner, "_MAX_STEPS", 3)
    _script(monkeypatch, [{"chunks": [f"step {i}"], "tool_calls": TOOL} for i in range(10)])
    events = _collect()
    done = [e for e in events if e["type"] == "done"][0]
    assert [e["type"] for e in events].count("tool_start") == 3
    assert done["full_text"].endswith("[stopped after 3 tool steps without a final answer]")
    assert done["full_text"].startswith("step 0\n\nstep 1\n\nstep 2")


# ---------------------------------------------------------------------------
# finish_reason="length" with no visible text: the hidden reasoning ate the
# whole output budget (GLM at effort=max, 2026-09-28). Retry once at low
# effort with a nudge; if still empty, say so instead of a blank bubble.
# ---------------------------------------------------------------------------

def _script_with_payloads(monkeypatch, steps):
    queue = list(steps); payloads = []

    async def fake_stream_step(client, payload, *, base_url="", api_key=""):
        import copy
        payloads.append(copy.deepcopy(payload))
        step = queue.pop(0)
        for chunk in step.get("chunks", []):
            yield {"type": "delta", "text": chunk}
        yield {"type": "_meta", "tool_calls": step.get("tool_calls", []),
               "content": "".join(step.get("chunks", [])),
               "usage": {"prompt_tokens": 10, "completion_tokens": 16384},
               "finish_reason": step.get("finish", "stop")}

    async def fake_exec(container, command, *, workdir="/workspace", home="/workspace"):
        return "tool-ok"

    monkeypatch.setattr(haihub_runner, "_stream_step", fake_stream_step)
    monkeypatch.setattr(haihub_runner, "_exec_bash", fake_exec)
    return payloads


def test_length_with_no_text_retries_at_low_effort(monkeypatch):
    payloads = _script_with_payloads(monkeypatch, [
        {"chunks": ["Checking."], "tool_calls": TOOL},
        {"chunks": [], "finish": "length"},          # reasoning ate the budget
        {"chunks": ["Here is the answer."]},          # the retry
    ])
    events = _collect(effort="max")
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"] == "Checking.\n\nHere is the answer."
    assert payloads[1]["reasoning_effort"] == "max"
    retry = payloads[2]
    assert retry["reasoning_effort"] == haihub_runner._RETRY_EFFORT
    assert retry["messages"][-1]["role"] == "system"
    assert "ran out of output budget" in retry["messages"][-1]["content"]
    assert "tools" not in retry
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert streamed == done["full_text"]


def test_length_twice_yields_visible_notice(monkeypatch):
    _script_with_payloads(monkeypatch, [
        {"chunks": [], "finish": "length"},
        {"chunks": [], "finish": "length"},
    ])
    events = _collect(effort="max")
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"] == haihub_runner._NO_REPLY_NOTICE
    assert "".join(e["text"] for e in events if e["type"] == "delta") == done["full_text"]


def test_truncated_reply_is_marked(monkeypatch):
    _script_with_payloads(monkeypatch, [{"chunks": ["Long answer cut off mid"], "finish": "length"}])
    done = [e for e in _collect() if e["type"] == "done"][0]
    assert done["full_text"].startswith("Long answer cut off mid")
    assert done["full_text"].endswith(haihub_runner._TRUNCATED_NOTICE)


def test_stop_finish_is_untouched(monkeypatch):
    _script_with_payloads(monkeypatch, [{"chunks": ["fine"], "finish": "stop"}])
    done = [e for e in _collect() if e["type"] == "done"][0]
    assert done["full_text"] == "fine"
