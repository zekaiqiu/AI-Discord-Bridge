"""SSE message endpoint: framing, persistence, error paths, email-leak guard."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from helpers import consume_sse, create_session  # shared test helpers


USER_A = "alice@example.com"
USER_B = "bob@example.com"


# Phase 2: SSE parser is now shared across both test trees via
# ``tests/helpers.py`` (workspace-root tests/).  Local alias keeps
# existing call sites stable.
_consume_sse = consume_sse


# Phase 3: ``create_session`` now lives in tests/helpers.py.
# Local underscore alias keeps existing call sites stable.
_create_session = create_session


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_streams_delta_then_done(client, auth_headers, fake_claude):
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream"), resp.headers

    events = _consume_sse(resp)
    types = [e.get("event") for e in events]
    assert types.count("done") == 1, types
    assert types.count("error") == 0, types
    assert types.count("delta") >= 1, types
    # ``done`` is the terminal frame.
    assert types[-1] == "done"

    # Concatenated delta text should match the full_text in done.
    delta_text = "".join(
        e["data"]["text"] for e in events if e.get("event") == "delta"
    )
    done_text = next(e for e in events if e.get("event") == "done")["data"]["full_text"]
    assert done_text == delta_text == "hello world"


def test_transcript_persisted_after_happy_path(client, auth_headers, fake_claude):
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    before = client.get(f"/api/sessions/{sid}", headers=headers).json()
    assert before["messages"] == []

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping"},
    )
    assert resp.status_code == 200
    _consume_sse(resp)  # drain

    after = client.get(f"/api/sessions/{sid}", headers=headers).json()
    msgs = after["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "ping"
    assert msgs[1]["content"] == "hello world"
    assert all("ts" in m for m in msgs)
    assert after["updated_at"] > before["updated_at"]


# ---------------------------------------------------------------------------
# Error paths — no transcript mutation, exactly one error event, no done.
# ---------------------------------------------------------------------------

def test_subprocess_crash_emits_one_error_no_done(client, auth_headers, fake_claude):
    fake_claude.set_scenario("crash")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    before = client.get(f"/api/sessions/{sid}", headers=headers).json()

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping"},
    )
    assert resp.status_code == 200
    events = _consume_sse(resp)
    types = [e.get("event") for e in events]
    assert types.count("error") == 1, types
    assert types.count("done") == 0, types

    # Phase 2 (Bug 1 fix): the user message and the assistant placeholder
    # are persisted BEFORE the model is invoked — so a subprocess crash
    # leaves them durable, with the assistant placeholder marked
    # ``status="error"``. The pre-fix assertion (``messages == []``)
    # encoded the buggy behaviour and has been updated.
    after = client.get(f"/api/sessions/{sid}", headers=headers).json()
    assert before["messages"] == []
    assert len(after["messages"]) == 2
    assert after["messages"][0]["role"] == "user"
    assert after["messages"][0]["content"] == "ping"
    assert after["messages"][1]["role"] == "assistant"
    assert after["messages"][1]["status"] == "error"


def test_malformed_json_emits_one_error_no_done(client, auth_headers, fake_claude):
    fake_claude.set_scenario("malformed")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping"},
    )
    events = _consume_sse(resp)
    types = [e.get("event") for e in events]
    assert types.count("error") == 1, types
    assert types.count("done") == 0, types

    # Phase 2 (Bug 1 fix): user message + placeholder persisted before
    # the model stream is parsed; a malformed-JSON early exit leaves
    # the placeholder with ``status="error"``.
    after = client.get(f"/api/sessions/{sid}", headers=headers).json()
    assert len(after["messages"]) == 2
    assert after["messages"][0]["role"] == "user"
    assert after["messages"][1]["role"] == "assistant"
    assert after["messages"][1]["status"] == "error"


# ---------------------------------------------------------------------------
# Email leak guard.
# ---------------------------------------------------------------------------

def test_email_not_leaked_to_claude_seam(client, auth_headers, fake_claude):
    fake_claude.set_scenario("happy")
    leaked_email = "verysecret-leak@example.org"
    headers = auth_headers(leaked_email)
    sid = _create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "what's the weather?"},
    )
    assert resp.status_code == 200
    _consume_sse(resp)

    # Recorder captures EVERY spawn_claude(args, stdin) call. Concatenate and
    # search for the email anywhere — argv, stdin, anywhere.
    blob = fake_claude.all_text
    assert leaked_email not in blob, (
        f"email leaked into claude args/stdin:\n{blob}"
    )


# ---------------------------------------------------------------------------
# Cross-email isolation.
# ---------------------------------------------------------------------------

def test_cross_email_post_message_is_404(client, auth_headers, fake_claude):
    fake_claude.set_scenario("happy")
    sid = _create_session(client, auth_headers(USER_A))

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=auth_headers(USER_B),
        json={"text": "hijack"},
    )
    assert resp.status_code == 404
    # Body must not reveal session existence.
    body = json.dumps(resp.json()).lower()
    assert sid.lower() not in body
    assert "alice" not in body


def test_post_message_rejects_empty_text(client, auth_headers, fake_claude):
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "   "},
    )
    assert resp.status_code == 400


def test_post_message_allows_attachments_only(
    client, auth_headers, fake_claude, tmp_attachments_dir
):
    """An attachments-only turn (no text) is valid in chat mode — the runner
    injects an attachment preamble so the model still has actionable input."""
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    up = client.post(
        f"/api/sessions/{sid}/attachments",
        headers=headers,
        files=[("files", ("note.txt", b"read me\n", "text/plain"))],
    )
    assert up.status_code == 200, up.text
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": ""},
    )
    assert resp.status_code == 200, resp.text
    events = _consume_sse(resp)
    assert [e.get("event") for e in events].count("done") == 1


def test_post_message_empty_text_no_attachments_still_rejected(
    client, auth_headers, fake_claude, tmp_attachments_dir
):
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": ""},
    )
    assert resp.status_code == 400


def test_post_message_image_mode_requires_text_even_with_attachment(
    client, auth_headers, fake_claude, tmp_attachments_dir
):
    """Image generation needs a text prompt; an attachment can't substitute."""
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    up = client.post(
        f"/api/sessions/{sid}/attachments",
        headers=headers,
        files=[("files", ("pic.png", b"\x89PNG\r\n\x1a\n", "image/png"))],
    )
    assert up.status_code == 200, up.text
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "", "mode": "image"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tool events.
# ---------------------------------------------------------------------------

def test_tool_events_forwarded(client, auth_headers, fake_claude):
    fake_claude.set_scenario("tool")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "use a tool"},
    )
    events = _consume_sse(resp)
    types = [e.get("event") for e in events]
    assert "tool_start" in types
    assert "tool_end" in types
    assert types.count("done") == 1
    # tool_start carries a name field.
    ts = next(e for e in events if e.get("event") == "tool_start")
    assert ts["data"]["name"] == "search"


# ---------------------------------------------------------------------------
# Admin role → claude_runner dispatches via docker exec into chat-host-shell.
# Non-admin role → claude_runner dispatches into the per-user container
# provisioned at session-create time.
# ---------------------------------------------------------------------------

def test_admin_session_dispatches_to_host_shell(
    client, auth_headers, fake_claude, monkeypatch
):
    """An admin email's chat session runs claude inside the host-shell
    sidecar (dispatch='host'); a non-admin email's session runs in the
    per-user container (dispatch='user')."""
    fake_claude.set_scenario("happy")
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    # Admin email → host dispatch.
    s = client.post("/api/sessions", headers=auth_headers("admin@example.com")).json()
    r = client.post(
        f"/api/sessions/{s['id']}/messages",
        headers=auth_headers("admin@example.com"),
        json={"text": "ping"},
    )
    assert r.status_code == 200
    # Drain the SSE so the worker actually runs the spawn.
    list(r.iter_lines())
    # main_turn_dispatch() filters out the parallel title call (which
    # always uses dispatch="local"); without that, the title's later
    # spawn races and overwrites a single ``last_dispatch`` value.
    assert fake_claude.main_turn_dispatch() == "host", (
        "admin role must route to chat-host-shell; "
        f"saw dispatches={fake_claude.dispatches}"
    )

    # Non-admin email → per-user container dispatch.
    s2 = client.post("/api/sessions", headers=auth_headers("user@example.com")).json()
    r2 = client.post(
        f"/api/sessions/{s2['id']}/messages",
        headers=auth_headers("user@example.com"),
        json={"text": "ping"},
    )
    assert r2.status_code == 200
    list(r2.iter_lines())
    assert fake_claude.main_turn_dispatch() == "user", (
        "non-admin role must route to per-user container; "
        f"saw dispatches={fake_claude.dispatches}"
    )
