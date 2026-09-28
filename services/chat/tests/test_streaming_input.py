"""_build_run_args streaming-input mode + normalize_session_turn contract."""

import asyncio

from claude_runner import _build_run_args, normalize_session_turn


def test_streaming_args_omit_inline_prompt():
    args = _build_run_args(
        "sess-123", "HELLO_PROMPT", None, is_first_turn=True, streaming_input=True
    )
    # Realtime streaming input selected; -p is a bare flag.
    assert "--input-format" in args
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert "-p" in args
    # The prompt is delivered via stdin (send_user), NEVER inline as argv.
    assert "HELLO_PROMPT" not in args
    # First turn still creates via --session-id.
    assert "--session-id" in args and "--resume" not in args


def test_oneshot_args_keep_inline_prompt():
    args = _build_run_args(
        "sess-123", "HELLO_PROMPT", None, is_first_turn=False, streaming_input=False
    )
    assert "HELLO_PROMPT" in args
    assert "--input-format" not in args
    assert "--resume" in args  # non-first turn resumes


async def _aiter(events):
    for e in events:
        yield e


async def _collect(turn_events, **kw):
    return [e async for e in normalize_session_turn(_aiter(turn_events), **kw)]


def test_normalizer_result_yields_single_done():
    out = asyncio.run(_collect([{"type": "result", "subtype": "success"}], model="opus"))
    assert len(out) == 1 and out[0]["type"] == "done"
    assert out[0]["meta"]["model"] == "opus"


def test_normalizer_delta_then_done_accumulates_text():
    out = asyncio.run(
        _collect([{"delta": "hello "}, {"delta": "world"}, {"type": "result"}])
    )
    types = [e["type"] for e in out]
    assert types == ["delta", "delta", "done"]
    assert out[-1]["full_text"] == "hello world"


def test_normalizer_no_result_is_error():
    # Process died mid-turn (Turn closed on EOF without a result) -> error,
    # mirroring run_turn's exactly-one-terminal contract.
    out = asyncio.run(_collect([{"delta": "partial"}]))
    assert out[-1]["type"] == "error"
    assert "before the turn completed" in out[-1]["message"]


def test_effective_prompt_passthrough():
    from claude_runner import build_effective_prompt

    assert build_effective_prompt("just text") == "just text"


def test_effective_prompt_blocks_and_order():
    from claude_runner import build_effective_prompt

    out = build_effective_prompt(
        "USER_MSG",
        prior_history="H",
        persona="be terse",
        output_language="zh-CN",
        memory="likes opus",
    )
    # All four prefixes present, exact run_turn text.
    assert "[Personal system prompt from this user" in out
    assert "[Response language: respond in Simplified Chinese" in out
    assert "[Cross-session memory" in out and "<memory>\nlikes opus\n</memory>" in out
    assert "[Conversation history" in out and "USER_MSG" in out
    # Outer-to-inner order matches run_turn: memory, language, persona, history.
    assert (
        out.index("[Cross-session memory")
        < out.index("[Response language")
        < out.index("[Personal system prompt")
        < out.index("[Conversation history")
    )


def test_effective_prompt_empty_memory_placeholder():
    from claude_runner import build_effective_prompt

    out = build_effective_prompt("x", memory="")
    assert "(empty — nothing recorded yet)" in out
    # "en" / unknown language adds no block.
    assert "[Response language" not in build_effective_prompt("x", output_language="en")


if __name__ == "__main__":
    test_streaming_args_omit_inline_prompt()
    test_oneshot_args_keep_inline_prompt()
    test_normalizer_result_yields_single_done()
    test_normalizer_delta_then_done_accumulates_text()
    test_normalizer_no_result_is_error()
    test_effective_prompt_passthrough()
    test_effective_prompt_blocks_and_order()
    test_effective_prompt_empty_memory_placeholder()
    print("PASS: streaming args + normalizer + effective-prompt contract")
