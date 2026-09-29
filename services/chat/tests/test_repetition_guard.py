"""Repetition guard (2026-09-29): a degenerate "EMIT. GO. FINAL. GO." loop in
a streamed step is cut, the step ends with finish_reason="repetition", the
loop is trimmed from the step's content, and run_turn retries once as a
wrap-up (low effort, nudge, no tools). A second degeneration gives up with a
visible notice. Real prose, code and link footers must never trip it.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import haihub_runner
from haihub_runner import _RepetitionGuard, _duplicate_unit_fraction


# --- fixtures ---------------------------------------------------------------------

_LOOP_PHRASES = [
    "GO.", "EMIT.", "---", "[END]", "FINAL.", "EXECUTE.", "DONE.", "And — GO!!!",
    "[EMIT]", "(Emission.)", "[END OF THINKING]", "Writing it now, for real.",
]


def loop_text(n_chars: int, seed: int = 1) -> str:
    rnd = random.Random(seed)
    out: list[str] = []
    while sum(len(s) + 1 for s in out) < n_chars:
        out.append(rnd.choice(_LOOP_PHRASES))
    return " ".join(out)


_WORDS = (
    "the market opened lower after the central bank signalled that rates would "
    "stay elevated through the first half while inflation prints remained sticky "
    "and analysts revised their forecasts for earnings growth across sectors "
    "including energy financials and technology with volume above average"
).split()


def prose_text(n_chars: int, seed: int = 2) -> str:
    rnd = random.Random(seed)
    out: list[str] = []
    while sum(len(s) + 1 for s in out) < n_chars:
        k = rnd.randint(6, 18)
        out.append(" ".join(rnd.choice(_WORDS) for _ in range(k)).capitalize() + ".")
    return " ".join(out)


def code_text(n_chars: int) -> str:
    body = "\n".join(
        f"    if x == {i}:\n        return {i}\n    else:\n        pass" for i in range(400)
    )
    return ("```python\n" + body + "\n```")[:n_chars]


def footer_text() -> str:
    # The link footer that came closest in the store: many short repeated
    # tokens, but every unit differs.
    return "\n".join(
        f"- [returns_{n}.parquet](/api/sessions/abc/generated/returns_{n}.parquet) ({n}.1 KB)"
        for n in range(80)
    )


# --- detector -------------------------------------------------------------------

def test_duplicate_fraction_separates_loops_from_text():
    frac, n = _duplicate_unit_fraction(loop_text(3000))
    # Twelve distinct phrases drawn at random: well above the 0.5 threshold.
    assert n >= haihub_runner._REP_MIN_UNITS and frac >= 0.6
    assert _duplicate_unit_fraction(prose_text(3000))[0] < 0.1
    # Code yields no verdict: fenced blocks are dropped, and even with the
    # fence gone (a window that starts mid-block while streaming) code lines
    # carry no sentence punctuation, so they are not units at all.
    assert _duplicate_unit_fraction(code_text(3000)) == (0.0, 0)
    assert _duplicate_unit_fraction(code_text(3000).replace("```python\n", ""))[0] == 0.0
    assert _duplicate_unit_fraction(footer_text())[0] < 0.1


def test_guard_trips_on_a_loop_and_keeps_the_text_before_it():
    g = _RepetitionGuard()
    good = prose_text(6000)
    for i in range(0, len(good), 37):
        assert not g.feed(good[i:i + 37])
    bad = loop_text(20000)
    tripped_at = None
    for i in range(0, len(bad), 41):
        if g.feed(bad[i:i + 41]):
            tripped_at = i
            break
    assert tripped_at is not None
    # Cut well before the loop ran away: within two windows of its start.
    assert tripped_at < 2 * haihub_runner._REP_WINDOW + 500
    assert g.cut_at is not None
    assert g.cut_at <= len(good) + haihub_runner._REP_WINDOW
    assert g.cut_at >= len(good) - haihub_runner._REP_WINDOW


def test_guard_never_trips_on_prose_code_or_footers():
    for text in (prose_text(60000), code_text(60000), footer_text() * 8):
        g = _RepetitionGuard()
        for i in range(0, len(text), 53):
            assert not g.feed(text[i:i + 53]), text[:60]


def test_guard_needs_two_consecutive_strikes():
    g = _RepetitionGuard()
    # One striking check is not a verdict; the next check (after
    # _REP_CHECK_EVERY more chars) must agree before the step is cut.
    assert not g.feed(loop_text(haihub_runner._REP_WINDOW + 200))
    assert not g.tripped and g._strikes == 1
    assert g.feed(loop_text(haihub_runner._REP_CHECK_EVERY, seed=9))
    # cut_at is the start of the first striking window, a check-interval in.
    assert g.tripped and 0 <= g.cut_at <= haihub_runner._REP_CHECK_EVERY + 200


# --- _stream_step -------------------------------------------------------------------

class _FakeResp:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.status_code = 200
        self.closed = False

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
        self._resp.closed = True
        return False


class _FakeClient:
    def __init__(self, chunks):
        self._chunks = chunks
        self.resp = _FakeResp(chunks)

    def stream(self, *a, **kw):
        return _FakeStream(self.resp)


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _delta(**fields) -> bytes:
    return _sse({"choices": [{"delta": fields}]})


def _collect_step(client):
    async def go():
        out = []
        async for ev in haihub_runner._stream_step(client, {"model": "m"}, base_url="http://x", api_key="k"):
            out.append(ev)
        return out
    return asyncio.run(go())


def _chunked(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def test_stream_step_cuts_a_content_loop_and_trims_it():
    good = prose_text(6000)
    chunks = [_delta(content=c) for c in _chunked(good + loop_text(40000), 60)]
    chunks.append(_delta(content="never reached"))
    chunks.append(b"data: [DONE]\n\n")
    client = _FakeClient(chunks)
    events = _collect_step(client)
    meta = [e for e in events if e["type"] == "_meta"][0]
    assert meta["finish_reason"] == "repetition"
    assert meta["degenerate"] == "content"
    assert meta["tool_calls"] == []
    assert "never reached" not in meta["content"]
    assert meta["content"].startswith(good[:200])
    # Trimmed: the persisted step keeps the good text (minus at most one
    # window, the granularity of the cut) and little of the loop.
    assert len(good) - haihub_runner._REP_WINDOW <= len(meta["content"]) <= len(good) + haihub_runner._REP_WINDOW
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    # The live stream was cut well short of the 40k loop.
    assert len(streamed) < len(good) + 3 * haihub_runner._REP_WINDOW
    assert client.resp.closed


def test_stream_step_cuts_a_reasoning_loop_and_keeps_content():
    chunks = [_delta(content="Working on it. ")]
    chunks += [_delta(reasoning_content=c) for c in _chunked(loop_text(40000), 70)]
    chunks.append(b"data: [DONE]\n\n")
    events = _collect_step(_FakeClient(chunks))
    meta = [e for e in events if e["type"] == "_meta"][0]
    assert meta["finish_reason"] == "repetition"
    assert meta["degenerate"] == "reasoning"
    assert meta["content"] == "Working on it. "


def test_stream_step_passes_a_long_normal_reply():
    text = prose_text(30000)
    chunks = [_delta(content=c) for c in _chunked(text, 80)]
    chunks.append(_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    chunks.append(b"data: [DONE]\n\n")
    meta = [e for e in _collect_step(_FakeClient(chunks)) if e["type"] == "_meta"][0]
    assert meta["finish_reason"] == "stop"
    assert meta["content"] == text


# --- run_turn --------------------------------------------------------------------

def _script(monkeypatch, steps: list[dict[str, Any]]):
    queue = list(steps)
    payloads: list[dict[str, Any]] = []

    async def fake_stream_step(client, payload, *, base_url="", api_key=""):
        payloads.append(json.loads(json.dumps(payload)))
        step = queue.pop(0)
        for c in step.get("chunks", []):
            yield {"type": "delta", "text": c}
        meta = {
            "type": "_meta",
            "tool_calls": step.get("tool_calls", []),
            "content": "".join(step.get("chunks", [])),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "finish_reason": step.get("finish", "tool_calls" if step.get("tool_calls") else "stop"),
        }
        if step.get("degenerate"):
            meta["degenerate"] = step["degenerate"]
        yield meta

    async def fake_exec(container, command, *, workdir="/workspace", home="/workspace"):
        return "tool-ok"

    monkeypatch.setattr(haihub_runner, "_stream_step", fake_stream_step)
    monkeypatch.setattr(haihub_runner, "_exec_bash", fake_exec)
    return payloads


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


def test_run_turn_retries_a_degenerate_step_as_a_wrap_up(monkeypatch):
    payloads = _script(monkeypatch, [
        {"chunks": ["Looking at the files."], "tool_calls": TOOL},
        {"chunks": ["Found the index. "], "finish": "repetition", "degenerate": "content"},
        {"chunks": ["Summary: 2500 members found; enrichment unfinished."]},
    ])
    events = _collect(effort="max")
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"] == (
        "Looking at the files.\n\nFound the index.\n\n"
        + haihub_runner._REPETITION_NOTICE
        + "\n\nSummary: 2500 members found; enrichment unfinished."
    )
    # The first two steps carried the user's effort and tools; the wrap-up
    # ran at low effort, without tools, with the nudge appended.
    assert payloads[0]["reasoning_effort"] == "max" and "tools" in payloads[0]
    assert payloads[1]["reasoning_effort"] == "max" and "tools" in payloads[1]
    wrap = payloads[2]
    assert wrap["reasoning_effort"] == haihub_runner._RETRY_EFFORT
    assert "tools" not in wrap and "tool_choice" not in wrap
    assert wrap["messages"][-1] == {"role": "system", "content": haihub_runner._REPETITION_RETRY_NUDGE}
    assert haihub_runner._REPETITION_GIVEUP_NOTICE not in done["full_text"]


def test_run_turn_gives_up_after_a_second_degeneration(monkeypatch):
    _script(monkeypatch, [
        {"chunks": ["Start. "], "finish": "repetition", "degenerate": "content"},
        {"chunks": [], "finish": "repetition", "degenerate": "reasoning"},
        {"chunks": ["must not run"]},
    ])
    events = _collect()
    done = [e for e in events if e["type"] == "done"][0]
    assert done["full_text"].startswith("Start.\n\n" + haihub_runner._REPETITION_NOTICE)
    assert done["full_text"].endswith(haihub_runner._REPETITION_GIVEUP_NOTICE)
    assert "must not run" not in done["full_text"]
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert haihub_runner._REPETITION_GIVEUP_NOTICE in streamed


def test_run_turn_unaffected_when_nothing_degenerates(monkeypatch):
    payloads = _script(monkeypatch, [
        {"chunks": ["A. "], "tool_calls": TOOL},
        {"chunks": ["B."]},
    ])
    done = [e for e in _collect(effort="high") if e["type"] == "done"][0]
    assert done["full_text"] == "A. \n\nB."
    assert all(p["reasoning_effort"] == "high" and "tools" in p for p in payloads)
