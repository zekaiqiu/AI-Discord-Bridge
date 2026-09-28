"""What a scheduled wake leaves on screen: no blank bubbles, no phantom countdowns.

The reason used to exist only on the SSE ``error`` event. For a user-initiated
turn that is enough — the sender is attached and sees it. A SCHEDULED WAKE
fires with no subscriber at all, so the reason was thrown away and the thread
was left holding an assistant message with ``content: ""`` and
``status: "error"`` forever. The SPA rendered that as a completely blank
bubble: no text, no spinner, no explanation. Nine of them were sitting in live
sessions when this was written.

The persisted notice is byte-identical to the line the SPA appends locally
when it IS attached, so a client that saw the live event and a client that
refetches end up with the same content and the tail merge stays a no-op.
"""

from __future__ import annotations

from typing import Any

import app


USER = "alice@example.com"


def _create_session(client, headers) -> str:
    resp = client.post("/api/sessions", headers=headers, json={"title": "t"})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_notice_matches_the_shape_the_spa_appends() -> None:
    assert app._with_error_notice("", "boom") == "\n\n_⚠ generation failed: boom_"


def test_notice_keeps_the_partial_it_is_appended_to() -> None:
    out = app._with_error_notice("half a sentence", "boom")
    assert out.startswith("half a sentence")
    assert out.endswith("generation failed: boom_")


def test_blank_reason_still_says_something() -> None:
    """An empty/whitespace message must not degrade back to a blank bubble."""
    assert "unknown error" in app._with_error_notice("", "   ")


def test_crashed_turn_persists_the_reason(client, auth_headers, fake_claude) -> None:
    """End-to-end: the assistant message left behind by a dead subprocess
    carries the failure reason, so a client that was never attached can still
    render something."""
    fake_claude.set_scenario("crash")
    headers = auth_headers(USER)
    sid = _create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages", headers=headers, json={"text": "ping"},
    )
    assert resp.status_code == 200
    resp.read()

    msgs = client.get(f"/api/sessions/{sid}", headers=headers).json()["messages"]
    assistant = msgs[-1]
    assert assistant["role"] == "assistant"
    assert assistant["status"] == "error"
    assert assistant["content"], "a failed turn must not persist an empty bubble"
    assert "generation failed" in assistant["content"]


def test_cancelled_turn_is_left_alone(client, auth_headers) -> None:
    """Only errors get a notice. A cancel is a deliberate user action and the
    SPA labels it from ``status`` alone — adding a failure line there would
    read as a bug report for something the user did on purpose."""
    import storage

    headers = auth_headers(USER)
    sid = _create_session(client, headers)
    _, seq = storage.append_assistant_placeholder(USER, sid)
    storage.update_assistant_message(
        USER, sid, seq, content="", status=storage.ASSISTANT_STATUS_CANCELLED,
    )
    msg = storage.get_session(USER, sid)["messages"][-1]
    assert msg["content"] == ""
    assert msg["status"] == "cancelled"


# ---------------------------------------------------------------------------
# A firing one-shot wake must not render as "pending in 2h".
# ---------------------------------------------------------------------------

def test_leased_wake_reports_running(monkeypatch, tmp_sessions_dir) -> None:
    """``claim_due`` leases a one-shot instead of deleting it, pushing
    ``next_fire`` out by LEASE_SECONDS. The composer pill rendered that lease
    as a real countdown — telling the user the wake fires "in 2h" while it was
    already running, then making it vanish on completion."""
    import chat_scheduler

    chat_scheduler.register(USER, "sid-1", "do the thing", delay_seconds=60)
    claimed = chat_scheduler.claim_due(now=__import__("time").time() + 120)
    assert len(claimed) == 1

    listed = chat_scheduler.list_for(USER, "sid-1")
    assert len(listed) == 1
    assert listed[0]["running"] is True


def test_pending_wake_is_not_running(monkeypatch, tmp_sessions_dir) -> None:
    import chat_scheduler

    chat_scheduler.register(USER, "sid-2", "later", delay_seconds=600)
    listed = chat_scheduler.list_for(USER, "sid-2")
    assert len(listed) == 1
    assert listed[0]["running"] is False


def test_lapsed_lease_is_due_again_not_running(monkeypatch, tmp_sessions_dir) -> None:
    """A fire that was killed mid-turn leaves ``leased_at`` set with
    ``next_fire`` back in the past. That wake is genuinely due again, so it
    must not keep claiming to be running."""
    import time as _time

    import chat_scheduler

    chat_scheduler.register(USER, "sid-3", "x", delay_seconds=60)
    chat_scheduler.claim_due(now=_time.time() + 120)
    data = chat_scheduler._load()
    for rec in data["schedules"]:
        rec["next_fire"] = _time.time() - 5
    chat_scheduler._save(data)

    listed = chat_scheduler.list_for(USER, "sid-3")
    assert listed[0]["running"] is False
