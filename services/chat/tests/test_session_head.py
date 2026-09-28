"""``GET /api/sessions/{id}/head`` — the SPA's idle-poll change detector.

Why it exists: scheduled wakes run server-side with no client subscribed, so
the SPA has to poll to notice them. Polling ``GET /sessions/{id}`` would ship
the whole message array every tick, and ``GET /sessions`` parses every session
file the user owns. This endpoint reads one file and returns a handful of
fields, so the expensive fetch only happens once something actually changed.

The contract the client depends on:
  * ``updated_at`` moves whenever a message is appended (that IS the signal).
  * ownership is enforced with 404, like the sibling endpoints.
  * the payload stays small — no message bodies.
"""

from __future__ import annotations

import storage


USER = "alice@example.com"
OTHER = "bob@example.com"


def _new_session(client, auth_headers, email=USER) -> str:
    resp = client.post("/api/sessions", headers=auth_headers(email), json={"title": "t"})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_head_reports_empty_session(client, auth_headers) -> None:
    sid = _new_session(client, auth_headers)
    resp = client.get(f"/api/sessions/{sid}/head", headers=auth_headers(USER))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == sid
    assert body["message_count"] == 0
    assert body["last_role"] is None
    assert body["last_status"] is None
    assert body["updated_at"]


def test_head_carries_no_message_bodies(client, auth_headers) -> None:
    """The whole point is that it stays small — a regression that starts
    returning messages would silently undo the optimisation."""
    sid = _new_session(client, auth_headers)
    storage.append_user_message(USER, sid, "a very distinctive message body")
    resp = client.get(f"/api/sessions/{sid}/head", headers=auth_headers(USER))
    assert resp.status_code == 200
    assert "messages" not in resp.json()
    assert "distinctive" not in resp.text


def test_head_updated_at_moves_when_a_wake_appends(client, auth_headers) -> None:
    """The exact signal the poll keys on: a server-initiated turn must change
    updated_at, or the client never refetches and the wake stays invisible."""
    sid = _new_session(client, auth_headers)
    before = client.get(f"/api/sessions/{sid}/head", headers=auth_headers(USER)).json()

    storage.append_assistant_placeholder(USER, sid, via="wake")

    after = client.get(f"/api/sessions/{sid}/head", headers=auth_headers(USER)).json()
    assert after["message_count"] == before["message_count"] + 1
    assert after["updated_at"] != before["updated_at"]
    # "a turn is running" — this is what makes the client re-attach to the SSE.
    assert after["last_role"] == "assistant"
    assert after["last_status"] == storage.ASSISTANT_STATUS_STREAMING


def test_head_reflects_turn_completion(client, auth_headers) -> None:
    sid = _new_session(client, auth_headers)
    _, seq = storage.append_assistant_placeholder(USER, sid, via="wake")
    storage.update_assistant_message(
        USER, sid, seq, content="done", status=storage.ASSISTANT_STATUS_COMPLETE,
    )
    body = client.get(f"/api/sessions/{sid}/head", headers=auth_headers(USER)).json()
    assert body["last_status"] == storage.ASSISTANT_STATUS_COMPLETE


def test_head_404s_for_another_users_session(client, auth_headers) -> None:
    sid = _new_session(client, auth_headers, email=USER)
    resp = client.get(f"/api/sessions/{sid}/head", headers=auth_headers(OTHER))
    assert resp.status_code == 404


def test_head_404s_for_unknown_session(client, auth_headers) -> None:
    resp = client.get(
        "/api/sessions/00000000-0000-0000-0000-000000000000/head",
        headers=auth_headers(USER),
    )
    assert resp.status_code == 404
