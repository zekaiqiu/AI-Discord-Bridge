"""Unit tests for the stale-resume self-heal and clean error surfacing
added to claude_runner. Run inside the chat image (python3.12 + deps)."""
import asyncio

import claude_runner as cr


# --- pure helpers ----------------------------------------------------------

def test_is_missing_session_error_matches_cli_wording():
    e = cr.ClaudeRunnerError(
        "claude exited with code 1: No conversation found with session ID: abc"
    )
    assert cr._is_missing_session_error(e) is True


def test_is_missing_session_error_ignores_other_failures():
    assert cr._is_missing_session_error(
        cr.ClaudeRunnerError("claude exited with code 1: Credit balance is too low")
    ) is False


def test_humanize_maps_known_signatures():
    msg = cr._humanize_claude_error(
        cr.ClaudeRunnerError("claude exited with code 1: Credit balance is too low")
    )
    assert "credit" in msg.lower()
    # The friendly text must NOT contain a giant command dump.
    assert "docker exec" not in msg
    assert "--resume" not in msg


def test_humanize_falls_back_to_short_raw():
    raw = "claude exited with code 7: some weird failure"
    assert cr._humanize_claude_error(cr.ClaudeRunnerError(raw)) == raw


# --- heal retry in run_turn ------------------------------------------------

def _collect(agen):
    async def _run():
        out = []
        async for ev in agen:
            out.append(ev)
        return out
    return asyncio.run(_run())


def _install_fake_spawn(monkeypatch_attr, behaviors):
    """behaviors: list of callables; behaviors[i] drives attempt i+1.
    Each is an async-gen body taking no args. Captures argv per call."""
    calls = []
    state = {"n": 0}

    def fake_spawn(args, **kw):
        calls.append(list(args))
        idx = state["n"]
        state["n"] += 1

        async def _agen():
            async for line in behaviors[idx]():
                yield line
        return _agen()

    setattr(cr, "spawn_claude", fake_spawn)
    return calls


def test_run_turn_heals_stale_resume(monkeypatch=None):
    async def attempt1():
        raise cr.ClaudeRunnerError(
            "claude exited with code 1: No conversation found with session ID: x"
        )
        yield  # pragma: no cover  (makes this an async generator)

    async def attempt2():
        # Fresh session succeeds; emit nothing -> clean done.
        return
        yield  # pragma: no cover

    orig = cr.spawn_claude
    try:
        calls = _install_fake_spawn(None, [attempt1, attempt2])
        events = _collect(cr.run_turn(
            claude_session_id="sess-uuid",
            prompt="what is 2+2",
            is_first_turn=False,
            role="admin",
            resume_heal_history="User: hi\n\nAssistant: hello",
            memory="",
        ))
    finally:
        cr.spawn_claude = orig

    # Exactly one terminal, and it's a clean done (heal succeeded).
    assert len(calls) == 2, f"expected 2 spawns, got {len(calls)}"
    assert "--resume" in calls[0] and "--session-id" not in calls[0]
    assert "--session-id" in calls[1] and "--resume" not in calls[1]
    # The heal attempt must replay the transcript into the prompt.
    heal_prompt = calls[1][-1]
    assert "Conversation history" in heal_prompt
    assert "Assistant: hello" in heal_prompt
    assert "what is 2+2" in heal_prompt
    terminals = [e for e in events if e["type"] in ("done", "error")]
    assert len(terminals) == 1 and terminals[0]["type"] == "done"


def test_run_turn_no_heal_surfaces_friendly_error():
    async def attempt1():
        raise cr.ClaudeRunnerError(
            "claude exited with code 1: Credit balance is too low"
        )
        yield  # pragma: no cover

    orig = cr.spawn_claude
    try:
        calls = _install_fake_spawn(None, [attempt1])
        events = _collect(cr.run_turn(
            claude_session_id="sess-uuid",
            prompt="hi",
            is_first_turn=False,
            role="admin",
            resume_heal_history="User: hi\n\nAssistant: hello",
            memory="",
        ))
    finally:
        cr.spawn_claude = orig

    # Non-resume error: no retry, friendly message, no command dump.
    assert len(calls) == 1
    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1
    assert "credit" in errs[0]["message"].lower()
    assert "docker exec" not in errs[0]["message"]


if __name__ == "__main__":
    test_is_missing_session_error_matches_cli_wording()
    test_is_missing_session_error_ignores_other_failures()
    test_humanize_maps_known_signatures()
    test_humanize_falls_back_to_short_raw()
    test_run_turn_heals_stale_resume()
    test_run_turn_no_heal_surfaces_friendly_error()
    print("ALL HEAL/ERROR TESTS PASSED")
