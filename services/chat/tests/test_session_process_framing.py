"""Turn-framing + bg-task accounting for the persistent session process.

Drives SessionProcess._pump with a fake stdout scripted from the real CLI
capture (user turn -> result, then an autonomous task_notification -> new
init/assistant -> result). No real claude needed: we inject a StreamReader.
"""

import asyncio
import json

from session_process import SessionProcess, Turn


# The exact shape observed live (CLI 2.1.172), trimmed to the framing-relevant
# events: a user turn that starts a bg task and ends, then an AUTO continuation
# turn the CLI opens itself when the task finishes.
SCRIPT = [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
    {"type": "system", "subtype": "task_started"},
    {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "started"}]}},
    {"type": "result", "subtype": "success"},
    # --- background task finishes; everything below is autonomous ---
    {"type": "system", "subtype": "task_notification"},
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done: ZZMARKER"}]}},
    {"type": "result", "subtype": "success"},
]


class _FakeProc:
    returncode = None
    pid = -1


async def _drain(turn: Turn) -> list:
    return [evt async for evt in turn]


async def _run() -> None:
    sp = SessionProcess(args=["true"])
    # Inject a fake process + a feedable stdout, then pre-open the user turn the
    # way send_user would (without needing a real stdin/process).
    reader = asyncio.StreamReader()
    fake = _FakeProc()
    fake.stdout = reader  # type: ignore[attr-defined]
    sp._proc = fake  # type: ignore[assignment]
    user_turn = Turn("user")
    sp._current = user_turn
    sp._pump_task = asyncio.create_task(sp._pump())

    # Feed the whole script, then EOF.
    for evt in SCRIPT:
        reader.feed_data((json.dumps(evt) + "\n").encode("utf-8"))
    reader.feed_eof()

    # User turn: frames events 0..result(inclusive), ends at the first result.
    user_events = await _drain(user_turn)
    assert user_events[-1]["type"] == "result", user_events[-1]
    assert sum(1 for e in user_events if e["type"] == "result") == 1
    assert any(
        e.get("type") == "system" and e.get("subtype") == "task_started"
        for e in user_events
    )

    # Auto-continuation turn surfaces via async iteration of the process.
    auto_turn = await sp.__anext__()
    assert auto_turn.kind == "auto"
    auto_events = await _drain(auto_turn)
    assert auto_events[0]["type"] == "system" and auto_events[0]["subtype"] == "task_notification"
    assert auto_events[-1]["type"] == "result"
    assert any(
        e.get("type") == "assistant"
        and "ZZMARKER" in json.dumps(e.get("message", {}))
        for e in auto_events
    ), "auto turn must carry the agent's report-back"

    # Background accounting: started(+1) then notification(-1) -> 0, so the
    # process is now evictable once idle.
    assert sp.pending_bg == 0, sp.pending_bg

    # After EOF the auto-turn iterator terminates cleanly.
    try:
        await asyncio.wait_for(sp.__anext__(), timeout=1.0)
        raise AssertionError("expected StopAsyncIteration after process EOF")
    except StopAsyncIteration:
        pass

    sp._pump_task.cancel()
    print("PASS: user turn framed, auto-continuation surfaced, bg accounting -> 0")


def test_framing_and_bg_accounting() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    test_framing_and_bg_accounting()
