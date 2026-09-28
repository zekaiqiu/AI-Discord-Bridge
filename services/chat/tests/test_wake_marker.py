"""A scheduled wake's assistant message is tagged ``via="wake"``.

Why this matters for rendering: ``_fire_wake_turn`` appends an assistant
placeholder with no user message in front of it (deliberate — the wake
instruction is the model's own note to itself, not something the user said).
The SPA therefore had no way to distinguish a timer-driven reply from a
normal one, and rendered it as the assistant speaking unprompted. The ``via``
field is the signal the Thread uses to label the bubble.

The field must be OMITTED, not null, on ordinary turns — the persisted
session JSON is read by the export path and by older clients.
"""

from __future__ import annotations

from pathlib import Path

import storage


USER = "alice@example.com"


def _new_session(client, auth_headers) -> str:
    resp = client.post("/api/sessions", headers=auth_headers(USER), json={"title": "t"})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_ordinary_placeholder_has_no_via_key(client, auth_headers, tmp_sessions_dir: Path) -> None:
    sid = _new_session(client, auth_headers)
    _, seq = storage.append_assistant_placeholder(USER, sid)
    assert seq is not None
    msg = storage.get_session(USER, sid)["messages"][-1]
    assert "via" not in msg, "ordinary turns must not gain a new persisted key"


def test_wake_placeholder_is_tagged(client, auth_headers, tmp_sessions_dir: Path) -> None:
    sid = _new_session(client, auth_headers)
    _, seq = storage.append_assistant_placeholder(USER, sid, via="wake")
    assert seq is not None
    msg = storage.get_session(USER, sid)["messages"][-1]
    assert msg["via"] == "wake"
    assert msg["status"] == storage.ASSISTANT_STATUS_STREAMING
    assert msg["role"] == "assistant"


def test_via_survives_the_content_update(client, auth_headers, tmp_sessions_dir: Path) -> None:
    """update_assistant_message rewrites the same dict in place; the tag must
    still be there once the wake's reply lands, since that is when the client
    actually renders it."""
    sid = _new_session(client, auth_headers)
    _, seq = storage.append_assistant_placeholder(USER, sid, via="wake")
    storage.update_assistant_message(
        USER, sid, seq, content="hello from the timer",
        status=storage.ASSISTANT_STATUS_COMPLETE,
    )
    msg = storage.get_session(USER, sid)["messages"][-1]
    assert msg["via"] == "wake"
    assert msg["content"] == "hello from the timer"
    assert msg["status"] == storage.ASSISTANT_STATUS_COMPLETE


def test_wake_placeholder_reaches_the_api(client, auth_headers) -> None:
    """The field must survive the GET serialisation — that is the payload the
    SPA actually reads."""
    sid = _new_session(client, auth_headers)
    storage.append_assistant_placeholder(USER, sid, via="wake")
    resp = client.get(f"/api/sessions/{sid}", headers=auth_headers(USER))
    assert resp.status_code == 200
    assert resp.json()["messages"][-1]["via"] == "wake"
