"""Thinking display (2026-09-29): the model's hidden reasoning is streamed as
``reasoning`` events and persisted as ``meta.reasoning``; how much is SHOWN
is a client-only setting (off / brief / full). Display-only: nothing here
may change what is requested from the model.

Covers the three sources — OpenAI-style ``reasoning_content`` deltas,
MiniMax-style inline ``<think>`` spans, and claude ``thinking`` frames — the
haihub tool-loop forwarding, and the app worker's stream + persist path.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import app as app_module
import claude_runner
import haihub_runner
import storage
import tokenhub_runner
from helpers import consume_sse, create_session

USER = "thinker@example.com"


# --- haihub _stream_step: three reasoning sources -------------------------------

class _FakeResp:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.status_code = 200

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b""


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, chunks):
        self._chunks = chunks

    def stream(self, *a, **kw):
        return _FakeStream(_FakeResp(self._chunks))


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _delta(**fields) -> bytes:
    return _sse({"choices": [{"delta": fields}]})


def _collect_step(chunks):
    async def go():
        out = []
        async for ev in haihub_runner._stream_step(
            _FakeClient(chunks), {"model": "m"}, base_url="http://x", api_key="k",
        ):
            out.append(ev)
        return out
    return asyncio.run(go())


def test_reasoning_content_deltas_become_reasoning_events():
    events = _collect_step([
        _delta(reasoning_content="Let me "),
        _delta(reasoning_content="think."),
        _delta(content="Answer."),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n\n",
    ])
    reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning")
    visible = "".join(e["text"] for e in events if e["type"] == "delta")
    assert reasoning == "Let me think."
    assert visible == "Answer."
    meta = [e for e in events if e["type"] == "_meta"][0]
    # Reasoning never joins the reply content.
    assert meta["content"] == "Answer."


def test_reasoning_field_variant_is_accepted():
    events = _collect_step([
        _delta(reasoning="hmm"),
        _delta(content="ok"),
        b"data: [DONE]\n\n",
    ])
    assert [e["text"] for e in events if e["type"] == "reasoning"] == ["hmm"]


def test_inline_think_spans_stream_as_reasoning_and_stay_out_of_content():
    events = _collect_step([
        _delta(content="<think>plan "),
        _delta(content="the reply</think>Visible "),
        _delta(content="text."),
        b"data: [DONE]\n\n",
    ])
    reasoning = "".join(e["text"] for e in events if e["type"] == "reasoning")
    visible = "".join(e["text"] for e in events if e["type"] == "delta")
    assert reasoning == "plan the reply"
    assert visible == "Visible text."
    assert [e for e in events if e["type"] == "_meta"][0]["content"] == "Visible text."


def test_unclosed_think_is_still_returned_as_literal_text():
    # Existing behaviour: an unclosed <think> is more likely the model quoting
    # the tag than reasoning, so flush() hands it back as visible text.
    events = _collect_step([_delta(content="<think>not really"), b"data: [DONE]\n\n"])
    visible = "".join(e["text"] for e in events if e["type"] == "delta")
    assert visible == "<think>not really"


# --- haihub run_turn: forwarded through the tool loop ----------------------------

def test_run_turn_forwards_reasoning_from_every_step(monkeypatch):
    queue = [
        {"reasoning": ["I should ", "run ls."], "chunks": ["Looking."],
         "tool_calls": [{"id": "c1", "name": "run_bash", "args": '{"command":"ls"}'}]},
        {"reasoning": ["Now answer."], "chunks": ["Done."]},
    ]

    async def fake_stream_step(client, payload, *, base_url="", api_key=""):
        step = queue.pop(0)
        for r in step.get("reasoning", []):
            yield {"type": "reasoning", "text": r}
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
        return "tool-ok"

    monkeypatch.setattr(haihub_runner, "_stream_step", fake_stream_step)
    monkeypatch.setattr(haihub_runner, "_exec_bash", fake_exec)

    async def go():
        out = []
        async for ev in haihub_runner.run_turn(
            prompt="hi", model="qwen", container="c1", api_key="k", chat_session_id="s",
        ):
            out.append(ev)
        return out

    events = asyncio.run(go())
    assert "".join(e["text"] for e in events if e["type"] == "reasoning") == "I should run ls.Now answer."
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"] == "Looking.\n\nDone."
    assert "I should" not in done["full_text"]


# --- claude stream-json: thinking frames ------------------------------------------

def test_claude_thinking_delta_and_block_are_extracted():
    x = claude_runner._extract_reasoning_text
    assert x({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm "}}) == "hm "
    assert x({"type": "content_block_delta", "delta": {"type": "signature_delta", "signature": "abc"}}) is None
    assert x({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "visible"}}) is None
    assert x({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "deep "}, {"type": "text", "text": "reply"},
    ]}}) == "deep "
    assert x({"type": "assistant", "message": {"content": [{"type": "text", "text": "reply"}]}}) is None
    # The text extractor must keep ignoring thinking frames.
    assert claude_runner._extract_delta_text(
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm"}}
    ) is None


def test_claude_session_turn_yields_reasoning_events():
    async def frames():
        for f in [
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "think "}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "more"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "reply"}},
            {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]:
            yield f

    async def go():
        return [ev async for ev in claude_runner.normalize_session_turn(frames(), model="fable")]

    events = asyncio.run(go())
    assert "".join(e["text"] for e in events if e["type"] == "reasoning") == "think more"
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"] == "reply"


# --- app worker: streamed as SSE, persisted on meta -------------------------------

def _install_fake(monkeypatch, gen_factory):
    monkeypatch.setattr(app_module.tokenhub_runner, "run_turn", gen_factory)
    monkeypatch.setattr(tokenhub_runner, "run_turn", gen_factory)


def test_reasoning_is_streamed_and_persisted_on_meta(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")

    async def fake_run_turn(**kw):
        yield {"type": "reasoning", "text": "first I "}
        yield {"type": "reasoning", "text": "consider"}
        yield {"type": "delta", "text": "hi"}
        yield {"type": "done", "full_text": "hi", "meta": {"model": "glm-5.3"}}

    _install_fake(monkeypatch, fake_run_turn)
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    r = client.post(f"/api/sessions/{sid}/messages", headers=headers, json={"text": "ping", "model": "glm"})
    events = consume_sse(r)
    reasoning = [e["data"]["text"] for e in events if e["event"] == "reasoning"]
    assert reasoning == ["first I ", "consider"]
    done = [e for e in events if e["event"] == "done"][0]
    assert done["data"]["full_text"] == "hi"
    assert done["data"]["meta"]["reasoning"] == "first I consider"
    assert done["data"]["meta"]["model"] == "glm-5.3"
    last = storage.get_session(USER, sid)["messages"][-1]
    assert last["content"] == "hi"
    assert last["meta"]["reasoning"] == "first I consider"


def test_no_reasoning_leaves_meta_untouched(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")

    async def fake_run_turn(**kw):
        yield {"type": "delta", "text": "hi"}
        yield {"type": "done", "full_text": "hi", "meta": {"model": "glm-5.3"}}

    _install_fake(monkeypatch, fake_run_turn)
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    r = client.post(f"/api/sessions/{sid}/messages", headers=headers, json={"text": "ping", "model": "glm"})
    events = consume_sse(r)
    assert not [e for e in events if e["event"] == "reasoning"]
    last = storage.get_session(USER, sid)["messages"][-1]
    assert "reasoning" not in last["meta"]


def test_persisted_reasoning_is_capped():
    big = ["x" * 50_000] * 4  # 200k chars
    text = app_module._capped_reasoning(big)
    assert text is not None
    assert len(text) == app_module.REASONING_PERSIST_CAP + len(app_module._REASONING_CAP_NOTE)
    assert text.endswith(app_module._REASONING_CAP_NOTE)
    assert app_module._capped_reasoning([]) is None
    assert app_module._capped_reasoning(["  \n"]) is None
