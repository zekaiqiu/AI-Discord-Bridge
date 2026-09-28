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
